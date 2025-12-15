# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
HIR → MIR lowering (expressions/statements, if/loop).

Pipeline placement:
  AST (lang2/stage0/ast.py) → HIR (lang2/stage1/hir_nodes.py) → MIR (this file) → SSA → LLVM/obj

This module lowers sugar-free HIR into explicit MIR instructions/blocks.
Currently supported:
  - literals, vars, unary/binary ops, field/index reads
  - let/assign/expr/return statements
	- `if` with then/else/join blocks
	- `loop` with break/continue
	- plain calls, method calls, DV construction
	- ternary expressions (diamond CFG + hidden temp)
  - `throw` lowered to Error/ResultErr + return, with try-stack routing to the
    nearest catch block (event codes from optional exception metadata)
  - `try` with multiple catch arms: dispatch compares `ErrorEvent` codes
    against per-arm constants (from the optional exception env; fallback 0),
    jumps to matching catch/catch-all, and unwinds to an outer try when no arm
    matches (returning FnResult.Err only when there is no outer try)
 Remaining TODO: rethrow/result-driven try sugar and any complex call
 names/receivers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Set, Mapping, Optional

from lang2.driftc import stage1 as H
from lang2.driftc.checker import FnSignature
from lang2.driftc.core.span import Span
from lang2.driftc.core.types_core import TypeKind, TypeTable, TypeId
from . import mir_nodes as M


class MirBuilder:
	"""
	Helper to construct a MIR function incrementally.

	Manages:
	- function scaffold (params, locals, blocks)
	- current block pointer
	- temp naming for intermediate values

	Entry point for this stage:
	  - build a MirBuilder with the function name
	  - use HIRToMIR to populate it
	  - read out builder.func when done
	"""

	def __init__(self, name: str):
		entry_block = M.BasicBlock(name="entry")
		self.func = M.MirFunc(
			name=name,
			params=[],
			locals=[],
			blocks={"entry": entry_block},
			entry="entry",
		)
		self.block = entry_block
		self._temp_counter = 0
		self._locals_set: Set[M.LocalId] = set()

	def new_temp(self) -> M.ValueId:
		"""Allocate a fresh temporary ValueId for intermediate results."""
		self._temp_counter += 1
		return f"t{self._temp_counter}"

	def emit(self, instr: M.MInstr) -> M.ValueId | None:
		"""
		Append a MIR instruction to the current block and return its dest, if any.
		"""
		self.block.instructions.append(instr)
		if hasattr(instr, "dest"):
			return getattr(instr, "dest")
		return None

	def set_terminator(self, term: M.MTerminator) -> None:
		"""Set the terminator for the current block."""
		self.block.terminator = term

	def ensure_local(self, name: M.LocalId) -> None:
		"""Record a local name on the function if it hasn't been seen yet."""
		if name not in self._locals_set:
			self._locals_set.add(name)
			self.func.locals.append(name)

	def new_block(self, name_hint: str) -> M.BasicBlock:
		"""
		Create a new basic block with a unique name derived from name_hint.
		Caller is responsible for setting it as current via set_block.
		"""
		base = name_hint
		suffix = 0
		name = base
		while name in self.func.blocks:
			suffix += 1
			name = f"{base}{suffix}"
		block = M.BasicBlock(name=name)
		self.func.blocks[name] = block
		return block

	def set_block(self, block: M.BasicBlock) -> None:
		"""Switch the current insertion block."""
		self.block = block


class HIRToMIR:
	"""
	Lower sugar-free HIR into MIR using per-node visitors.

	Supported constructs:
	  - literals, vars, unary/binary ops, field/index reads
	  - let/assign/expr/return
	  - `if` with then/else/join
	  - `loop` with break/continue
	  - plain calls, method calls, DV construction
	  - ternary expressions (diamond CFG + hidden temp)
	  - `throw` → ConstructError + ResultErr + Return, with try-stack routing
	  - `try` with multiple catch arms (dispatch via ErrorEvent codes, catch-all,
	    unwind to outer try on no match; return FnResult.Err only when no outer
	    try exists)

	Entry points (stage API):
	  - lower_expr: lower a single HIR expression to a MIR ValueId
	  - lower_stmt: lower a single HIR statement, appending MIR to the builder
	  - lower_block: lower an HIR block (list of statements)
	Helper visitors are prefixed with an underscore; public surface is the
	lower_* methods above.
	"""

	def __init__(
		self,
		builder: MirBuilder,
		type_table: Optional[TypeTable] = None,
		exc_env: Mapping[str, int] | None = None,
		param_types: Mapping[str, TypeId] | None = None,
		signatures: Mapping[str, FnSignature] | None = None,
		can_throw_by_name: Mapping[str, bool] | None = None,
		return_type: TypeId | None = None,
	):
		"""
		Create a lowering context.

			`exc_env` (optional) maps exception FQNs to event codes so
			throw lowering can emit real codes instead of placeholders.
			"""
		self.b = builder
		# Stack of (continue_target, break_target) block names for nested loops.
		self._loop_stack: list[tuple[str, str]] = []
		# Stack of try contexts for nested try/catch (innermost on top).
		self._try_stack: list["_TryCtx"] = []
		# Error value bound by the innermost catch block (if any) for rethrow.
		self._current_catch_error: M.ValueId | None = None
		# Optional exception environment: maps exception FQN -> event code.
		self._exc_env = exc_env
		# Track best-effort local types (TypeId) to tag typed MIR nodes.
		self._local_types: dict[str, TypeId] = dict(param_types) if param_types else {}
		# Optional shared TypeTable for typed MIR nodes (arrays, etc.).
		self._type_table = type_table or TypeTable()
		self._exception_schemas: dict[str, tuple[str, list[str]]] = getattr(self._type_table, "exception_schemas", {}) or {}
		# Cache some common types for reuse when shared.
		self._int_type = self._type_table.ensure_int()
		self._float_type = self._type_table.ensure_float()
		self._bool_type = self._type_table.ensure_bool()
		self._string_type = self._type_table.ensure_string()
		self._string_empty_const = self.b.new_temp()
		# Inject a private empty string literal for String.EMPTY; this is a
		# zero-length, null-data string produced at MIR lowering time.
		self.b.emit(M.ConstString(dest=self._string_empty_const, value=""))
		self._uint_type = self._type_table.ensure_uint()
		self._unknown_type = self._type_table.ensure_unknown()
		self._void_type = self._type_table.ensure_void()
		self._dv_type = self._type_table.ensure_diagnostic_value()
		self._opt_int = self._type_table.new_optional(self._int_type)
		self._opt_bool = self._type_table.new_optional(self._bool_type)
		self._opt_string = self._type_table.new_optional(self._string_type)
		self._signatures = signatures or {}
		# Best-effort can-throw classification for functions. This is intentionally
		# separate from signatures: the surface language does not expose FnResult,
		# and "can-throw" is an effect inferred from the body (or declared by a
		# future `nothrow`/throws annotation).
		self._can_throw_by_name: dict[str, bool] = dict(can_throw_by_name) if can_throw_by_name else {}
		self._current_fn_can_throw: bool | None = self._can_throw_by_name.get(self.b.func.name)
		self._ret_type = return_type
		# Cache the current function signature for defensive fallbacks in older
		# unit tests that bypass the checker.
		self._fn_sig = self._signatures.get(self.b.func.name)

	def _fn_can_throw(self) -> bool | None:
		"""
		Best-effort can-throw flag for the current function.

		Preferred source is `can_throw_by_name` computed by the checker. We keep
		a signature-based fallback only for legacy/unit tests that bypass the
		checker in this stage.
		"""
		if self._current_fn_can_throw is not None:
			return self._current_fn_can_throw
		if self._fn_sig is None:
			return None
		if self._fn_sig.declared_can_throw is not None:
			return bool(self._fn_sig.declared_can_throw)
		# Legacy fallback: old surface model treated FnResult returns as can-throw.
		rt = self._fn_sig.return_type_id
		if rt is not None and self._type_table.get(rt).kind is TypeKind.FNRESULT:
			return True
		return None

	# --- Expression lowering ---

	def lower_expr(self, expr: H.HExpr) -> M.ValueId:
		"""
		Entry point: lower a single HIR expression to a MIR ValueId.

		Dispatches to a private _visit_expr_* helper. Public stage API: callers
		should only invoke lower_expr/stmt/block; helpers stay private.
		"""
		method = getattr(self, f"_visit_expr_{type(expr).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No MIR lowering for expr {type(expr).__name__}")
		return method(expr)

	def _visit_expr_HLiteralInt(self, expr: H.HLiteralInt) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstInt(dest=dest, value=expr.value))
		return dest

	def _visit_expr_HLiteralFloat(self, expr: H.HLiteralFloat) -> M.ValueId:
		"""
		Lower a Float literal.

		Float is a surface type in lang2 v1 and maps to IEEE-754 `double` in LLVM.
		The parser enforces strict float literal syntax; by the time we reach HIR,
		the literal value is already a Python `float`.
		"""
		dest = self.b.new_temp()
		self.b.emit(M.ConstFloat(dest=dest, value=expr.value))
		return dest

	def _visit_expr_HLiteralBool(self, expr: H.HLiteralBool) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstBool(dest=dest, value=expr.value))
		return dest

	def _visit_expr_HLiteralString(self, expr: H.HLiteralString) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstString(dest=dest, value=expr.value))
		return dest

	def _visit_expr_HFString(self, expr: H.HFString) -> M.ValueId:
		"""
		Lower an f-string into explicit String concatenations.

		We perform this lowering in stage2 (rather than stage1) so we can:
		- use best-effort type inference for hole expressions, and
		- translate supported hole value types into Strings via dedicated MIR ops.

		MVP limitations:
		- Only empty `:spec` is supported (non-empty specs are rejected).
		- Supported hole value types are Bool/Int/Uint/Float/String.
		"""
		if len(expr.parts) != len(expr.holes) + 1:
			raise AssertionError("HFString invariant violated: parts.len != holes.len + 1")

		def _const_part(text: str) -> M.ValueId:
			if text == "":
				return self._string_empty_const
			tmp = self.b.new_temp()
			self.b.emit(M.ConstString(dest=tmp, value=text))
			return tmp

		acc = _const_part(expr.parts[0])
		for idx, hole in enumerate(expr.holes):
			if hole.spec:
				raise AssertionError("non-empty f-string :spec reached stage2 (checker bug)")

			val = self.lower_expr(hole.expr)
			ty = self._infer_expr_type(hole.expr)
			if ty is None:
				raise AssertionError("f-string hole type is unknown in stage2 (checker bug)")

			if ty == self._string_type:
				val_str = val
			elif ty == self._int_type:
				val_str = self.b.new_temp()
				self.b.emit(M.StringFromInt(dest=val_str, value=val))
			elif ty == self._bool_type:
				val_str = self.b.new_temp()
				self.b.emit(M.StringFromBool(dest=val_str, value=val))
			elif ty == self._uint_type:
				val_str = self.b.new_temp()
				self.b.emit(M.StringFromUint(dest=val_str, value=val))
			elif ty == self._float_type:
				val_str = self.b.new_temp()
				self.b.emit(M.StringFromFloat(dest=val_str, value=val))
			else:
				raise AssertionError("unsupported f-string hole type reached stage2 (checker bug)")

			tmp = self.b.new_temp()
			self.b.emit(M.StringConcat(dest=tmp, left=acc, right=val_str))
			acc = tmp

			part_text = expr.parts[idx + 1]
			if part_text:
				part_val = _const_part(part_text)
				tmp2 = self.b.new_temp()
				self.b.emit(M.StringConcat(dest=tmp2, left=acc, right=part_val))
				acc = tmp2
		return acc

	def _visit_expr_HVar(self, expr: H.HVar) -> M.ValueId:
		self.b.ensure_local(expr.name)
		# Treat String.EMPTY as a builtin zero-length string literal.
		if expr.name == "String.EMPTY":
			return self._string_empty_const
		dest = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=dest, local=expr.name))
		return dest

	def _visit_expr_HBorrow(self, expr: H.HBorrow) -> M.ValueId:
		"""
		Lower a borrow expression (`&x` / `&mut x`).

		MVP: borrowing is only supported from addressable places. The checker and
		stage1 normalization are responsible for ensuring we only see:
		  - locals/params and nested projections (`x.field`, `arr[i]`, ...)
		  - reborrows through deref places (`&*p` / `&mut *p`)
		  - shared borrows of rvalues rewritten via temporary materialization
		    (`&(expr)` becomes `val tmp = expr; &tmp`).

		This lowering is intentionally place-driven: it computes the address of the
		referenced storage and returns it as the borrow result.
		"""
		ptr, _inner = self._lower_addr_of_place(expr.subject, is_mut=expr.is_mut)
		return ptr

	def _visit_expr_HUnary(self, expr: H.HUnary) -> M.ValueId:
		# Dereference is modeled as an explicit MIR load.
		if expr.op is H.UnaryOp.DEREF:
			ptr_val = self.lower_expr(expr.expr)
			ptr_ty = self._infer_expr_type(expr.expr)
			if ptr_ty is None:
				raise AssertionError("deref type unknown in MIR lowering (checker bug)")
			td = self._type_table.get(ptr_ty)
			if td.kind is not TypeKind.REF or not td.param_types:
				raise AssertionError("deref of non-ref reached MIR lowering (checker bug)")
			inner_ty = td.param_types[0]
			dest = self.b.new_temp()
			self.b.emit(M.LoadRef(dest=dest, ptr=ptr_val, inner_ty=inner_ty))
			return dest
		operand = self.lower_expr(expr.expr)
		dest = self.b.new_temp()
		self.b.emit(M.UnaryOpInstr(dest=dest, op=expr.op, operand=operand))
		return dest

	def _visit_expr_HBinary(self, expr: H.HBinary) -> M.ValueId:
		left = self.lower_expr(expr.left)
		right = self.lower_expr(expr.right)
		dest = self.b.new_temp()
		# String-aware lowering: redirect +/== on strings to dedicated MIR ops.
		left_ty = self._infer_expr_type(expr.left)
		right_ty = self._infer_expr_type(expr.right)
		if left_ty == self._string_type and right_ty == self._string_type:
			if expr.op is H.BinaryOp.ADD:
				self.b.emit(M.StringConcat(dest=dest, left=left, right=right))
				return dest
			if expr.op is H.BinaryOp.EQ:
				self.b.emit(M.StringEq(dest=dest, left=left, right=right))
				return dest
			# Ordering comparisons are defined as a deterministic, locale-independent
			# lexicographic comparison on the underlying UTF-8 byte sequences.
			if expr.op in (H.BinaryOp.NE, H.BinaryOp.LT, H.BinaryOp.LE, H.BinaryOp.GT, H.BinaryOp.GE):
				cmp_tmp = self.b.new_temp()
				self.b.emit(M.StringCmp(dest=cmp_tmp, left=left, right=right))
				zero = self.b.new_temp()
				self.b.emit(M.ConstInt(dest=zero, value=0))
				self.b.emit(M.BinaryOpInstr(dest=dest, op=expr.op, left=cmp_tmp, right=zero))
				return dest
		self.b.emit(M.BinaryOpInstr(dest=dest, op=expr.op, left=left, right=right))
		return dest

	def _visit_expr_HField(self, expr: H.HField) -> M.ValueId:
		subject = self.lower_expr(expr.subject)
		# Array/String len/capacity sugar: field access produces ArrayLen/ArrayCap/StringLen.
		if expr.name == "len":
			dest = self.b.new_temp()
			subj_ty = self._infer_expr_type(expr.subject)
			self._lower_len(subj_ty, subject, dest)
			return dest
		if expr.name in ("cap", "capacity"):
			dest = self.b.new_temp()
			self.b.emit(M.ArrayCap(dest=dest, array=subject))
			return dest
		if expr.name == "attrs":
			raise NotImplementedError("attrs view must be indexed: Error.attrs[\"key\"]")
		# Struct field access.
		subj_ty = self._infer_expr_type(expr.subject)
		if subj_ty is None:
			raise AssertionError("struct field type unknown in MIR lowering (checker bug)")
		sub_def = self._type_table.get(subj_ty)
		if sub_def.kind is not TypeKind.STRUCT:
			raise NotImplementedError(f"field access is only supported on structs in v1 (have {sub_def.kind})")
		info = self._type_table.struct_field(subj_ty, expr.name)
		if info is None:
			raise AssertionError("unknown struct field reached MIR lowering (checker bug)")
		field_idx, field_ty = info
		dest = self.b.new_temp()
		self.b.emit(
			M.StructGetField(
				dest=dest,
				subject=subject,
				struct_ty=subj_ty,
				field_index=field_idx,
				field_ty=field_ty,
			)
		)
		return dest

	def _visit_expr_HIndex(self, expr: H.HIndex) -> M.ValueId:
		if isinstance(expr.subject, H.HField) and expr.subject.name == "attrs":
			err_val = self.lower_expr(expr.subject.subject)
			key_val = self.lower_expr(expr.index)
			dest = self.b.new_temp()
			self.b.emit(M.ErrorAttrsGetDV(dest=dest, error=err_val, key=key_val))
			self._local_types[dest] = self._dv_type
			return dest
		subject = self.lower_expr(expr.subject)
		index = self.lower_expr(expr.index)
		dest = self.b.new_temp()
		elem_ty = self._infer_array_elem_type(expr.subject)
		self.b.emit(M.ArrayIndexLoad(dest=dest, elem_ty=elem_ty, array=subject, index=index))
		return dest

	def _visit_expr_HArrayLiteral(self, expr: H.HArrayLiteral) -> M.ValueId:
		elem_ty = self._infer_array_literal_elem_type(expr)
		values = [self.lower_expr(e) for e in expr.elements]
		dest = self.b.new_temp()
		self.b.emit(M.ArrayLit(dest=dest, elem_ty=elem_ty, elements=values))
		return dest

	def _lower_len(self, subj_ty: Optional[TypeId], subj_val: M.ValueId, dest: M.ValueId) -> None:
		"""Lower length for Array<T> and String to Uint."""
		if subj_ty is None:
			# Conservative fallback: assume array when type is unknown.
			self.b.emit(M.ArrayLen(dest=dest, array=subj_val))
			return
		td = self._type_table.get(subj_ty)
		if td.kind is TypeKind.REF and td.param_types:
			# MVP convenience: allow len(&String) / len(&Array<T>) by implicit
			# dereference at the builtin boundary. This keeps borrow support
			# usable without introducing autoref/autoderef globally.
			inner_ty = td.param_types[0]
			tmp = self.b.new_temp()
			self.b.emit(M.LoadRef(dest=tmp, ptr=subj_val, inner_ty=inner_ty))
			self._lower_len(inner_ty, tmp, dest)
			return
		if td.kind is TypeKind.ARRAY:
			self.b.emit(M.ArrayLen(dest=dest, array=subj_val))
		elif subj_ty == self._string_type:
			self.b.emit(M.StringLen(dest=dest, value=subj_val))
		else:
			raise NotImplementedError("len(x): unsupported argument type")

	# Stubs for unhandled expressions
	def _visit_expr_HCall(self, expr: H.HCall) -> M.ValueId:
		"""
		Plain function call. For now only direct function names are supported;
		indirect/function-valued calls will be added later if needed.
		"""
		if isinstance(expr.fn, H.HVar):
			name = expr.fn.name
			# Struct constructor: `Point(1, 2)` constructs a struct value.
			#
			# This only triggers when there is no function signature for the same
			# name (to avoid ambiguity in older tests).
			if name in getattr(self._type_table, "struct_schemas", {}) and name not in self._signatures:
				struct_ty = self._type_table.ensure_named(name)
				struct_def = self._type_table.get(struct_ty)
				if struct_def.kind is not TypeKind.STRUCT:
					raise AssertionError("struct schema name resolved to non-STRUCT TypeId (checker bug)")
				arg_vals = [self.lower_expr(a) for a in expr.args]
				if len(arg_vals) != len(struct_def.param_types):
					raise AssertionError("struct ctor arg count mismatch reached MIR lowering (checker bug)")
				dest = self.b.new_temp()
				self.b.emit(M.ConstructStruct(dest=dest, struct_ty=struct_ty, args=arg_vals))
				return dest
			# Builtin byte_length/len(x) for String/Array.
			if name in ("len", "byte_length") and len(expr.args) == 1:
				arg_expr = expr.args[0]
				arg_val = self.lower_expr(arg_expr)
				arg_ty = self._infer_expr_type(arg_expr)
				if arg_ty is None:
					raise NotImplementedError(f"{name}(x): unable to infer argument type")
				dest = self.b.new_temp()
				self._lower_len(arg_ty, arg_val, dest)
				self._local_types[dest] = self._uint_type
				return dest
			# string_eq(a,b)
			if name == "string_eq" and len(expr.args) == 2:
				l_expr, r_expr = expr.args
				l_val = self.lower_expr(l_expr)
				r_val = self.lower_expr(r_expr)
				if self._infer_expr_type(l_expr) != self._string_type or self._infer_expr_type(r_expr) != self._string_type:
					raise NotImplementedError("string_eq requires String operands")
				dest = self.b.new_temp()
				self.b.emit(M.StringEq(dest=dest, left=l_val, right=r_val))
				self._local_types[dest] = self._bool_type
				return dest
			# string_concat(a,b)
			if name == "string_concat" and len(expr.args) == 2:
				l_expr, r_expr = expr.args
				l_val = self.lower_expr(l_expr)
				r_val = self.lower_expr(r_expr)
				if self._infer_expr_type(l_expr) != self._string_type or self._infer_expr_type(r_expr) != self._string_type:
					raise NotImplementedError("string_concat requires String operands")
				dest = self.b.new_temp()
				self.b.emit(M.StringConcat(dest=dest, left=l_val, right=r_val))
				self._local_types[dest] = self._string_type
				return dest
		if not isinstance(expr.fn, H.HVar):
			raise NotImplementedError("Only direct function-name calls are supported in MIR lowering")
		result = self._lower_call(expr)
		if result is None:
			raise AssertionError("Void-returning call used in expression context (checker bug)")
		# Calls to can-throw functions are always "checked": they either produce the
		# ok payload value or propagate an Error into the nearest try (or out of the
		# current function).
		if self._callee_is_can_throw(expr.fn.name):
			ok_tid = self._return_typeid_for_callee(expr.fn.name)
			if ok_tid is None:
				raise AssertionError("can-throw callee must have a declared return type")
			def emit_call() -> M.ValueId:
				return result
			return self._lower_can_throw_call_value(emit_call=emit_call, ok_ty=ok_tid)
		return result

	def _visit_expr_HMethodCall(self, expr: H.HMethodCall) -> M.ValueId:
		if expr.method_name in ("as_int", "as_bool", "as_string"):
			if expr.args:
				raise NotImplementedError(f"{expr.method_name} takes no arguments")
			dv_val = self.lower_expr(expr.receiver)
			dest = self.b.new_temp()
			if expr.method_name == "as_int":
				self.b.emit(M.DVAsInt(dest=dest, dv=dv_val))
				self._local_types[dest] = self._opt_int
				return dest
			if expr.method_name == "as_bool":
				self.b.emit(M.DVAsBool(dest=dest, dv=dv_val))
				self._local_types[dest] = self._opt_bool
				return dest
			if expr.method_name == "as_string":
				self.b.emit(M.DVAsString(dest=dest, dv=dv_val))
				self._local_types[dest] = self._opt_string
				return dest
		result = self._lower_method_call(expr)
		if result is None:
			raise AssertionError("Void-returning method call used in expression context (checker bug)")
		if self._callee_is_can_throw(expr.method_name):
			ok_tid = self._return_typeid_for_callee(expr.method_name)
			if ok_tid is None:
				raise AssertionError("can-throw callee must have a declared return type")
			def emit_call() -> M.ValueId:
				return result
			return self._lower_can_throw_call_value(emit_call=emit_call, ok_ty=ok_tid)
		return result

	def _visit_expr_HDVInit(self, expr: H.HDVInit) -> M.ValueId:
		arg_vals = [self.lower_expr(a) for a in expr.args]
		dest = self.b.new_temp()
		self.b.emit(M.ConstructDV(dest=dest, dv_type_name=expr.dv_type_name, args=arg_vals))
		return dest

	def _visit_expr_HExceptionInit(self, expr: H.HExceptionInit) -> M.ValueId:
		"""
		Exception init is only valid as a throw payload in v1; fail loudly if it
		reaches expression position.
		"""
		raise NotImplementedError("ExceptionInit is only valid as a throw payload")

	def _visit_expr_HResultOk(self, expr: H.HResultOk) -> M.ValueId:
		"""
		Lower FnResult.Ok(value) into ConstructResultOk(dest, value).

		This gives tests/pipeline a clean way to return FnResult without
		hand-writing MIR.
		"""
		val = self.lower_expr(expr.value)
		dest = self.b.new_temp()
		self.b.emit(M.ConstructResultOk(dest=dest, value=val))
		return dest

	def _visit_expr_HTernary(self, expr: H.HTernary) -> M.ValueId:
		"""
		Lower ternary expression by building a diamond CFG that stores into a
		hidden local and reloads it at the join. SSA will place φs as needed.
		"""
		# Allocate a hidden local for the ternary result.
		temp_local = f"__tern_tmp{self.b.new_temp()}"
		self.b.ensure_local(temp_local)

		# Evaluate condition in the current block.
		cond_val = self.lower_expr(expr.cond)

		# Create then/else/join blocks.
		then_block = self.b.new_block("tern_then")
		else_block = self.b.new_block("tern_else")
		join_block = self.b.new_block("tern_join")

		# Branch on condition from the current block.
		self.b.set_terminator(
			M.IfTerminator(cond=cond_val, then_target=then_block.name, else_target=else_block.name)
		)

		# Then branch: compute then_expr, store to temp, jump to join.
		self.b.set_block(then_block)
		then_val = self.lower_expr(expr.then_expr)
		self.b.emit(M.StoreLocal(local=temp_local, value=then_val))
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=join_block.name))

		# Else branch: compute else_expr, store to temp, jump to join.
		self.b.set_block(else_block)
		else_val = self.lower_expr(expr.else_expr)
		self.b.emit(M.StoreLocal(local=temp_local, value=else_val))
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=join_block.name))

		# Join: load the temp as the value of the ternary and continue.
		self.b.set_block(join_block)
		dest = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=dest, local=temp_local))
		return dest

	def _visit_expr_HTryExpr(self, expr: H.HTryExpr) -> M.ValueId:
		"""
		Lower expression-form try/catch by desugaring to a try CFG that merges
		values through a hidden local and a join block.
		"""
		# Hidden local for the expression result.
		temp_local = f"__try_expr_tmp{self.b.new_temp()}"
		self.b.ensure_local(temp_local)

		# Blocks: attempt body, dispatch for errors, catch arms, join for value.
		attempt_block = self.b.new_block("tryexpr_attempt")
		dispatch_block = self.b.new_block("tryexpr_dispatch")
		join_block = self.b.new_block("tryexpr_join")

		# Hidden local to carry the caught Error.
		error_local = f"__try_err{self.b.new_temp()}"
		self.b.ensure_local(error_local)
		self._local_types[error_local] = self._type_table.ensure_error()

		# Prepare catch blocks.
		catch_blocks: list[tuple[H.HTryExprArm, M.BasicBlock]] = []
		catch_all_block: M.BasicBlock | None = None
		catch_all_seen = False
		for idx, arm in enumerate(expr.arms):
			cb = self.b.new_block(f"tryexpr_catch_{idx}")
			catch_blocks.append((arm, cb))
			if arm.event_fqn is None:
				if catch_all_block is not None:
					raise RuntimeError("multiple catch-all arms are not supported")
				catch_all_block = cb
				catch_all_seen = True
			else:
				if catch_all_seen:
					raise RuntimeError("catch-all must be the last catch arm")

		# Enter attempt block and register try context so throws route to dispatch.
		self.b.set_terminator(M.Goto(target=attempt_block.name))
		self._try_stack.append(
			_TryCtx(
				error_local=error_local,
				dispatch_block_name=dispatch_block.name,
				cont_block_name=join_block.name,
			)
		)

		# Lower attempt body.
		self.b.set_block(attempt_block)
		attempt_val = self.lower_expr(expr.attempt)
		# attempt in v1 is guaranteed to produce a value (non-void) by the checker.
		self.b.emit(M.StoreLocal(local=temp_local, value=attempt_val))
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=join_block.name))

		# Pop try context before dispatch so throws in catches unwind to the outer try.
		# (Rethrow uses `_current_catch_error`, not the try stack.)
		self._try_stack.pop()

		# Dispatch: load error and compare event codes.
		self.b.set_block(dispatch_block)
		err_tmp = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=err_tmp, local=error_local))
		code_tmp = self.b.new_temp()
		self.b.emit(M.ErrorEvent(dest=code_tmp, error=err_tmp))

		event_arms = [(arm, cb) for arm, cb in catch_blocks if arm.event_fqn is not None]
		if event_arms:
			current_block = dispatch_block
			for arm, cb in event_arms:
				self.b.set_block(current_block)
				arm_code = self._lookup_catch_event_code(arm.event_fqn)
				arm_code_const = self.b.new_temp()
				self.b.emit(M.ConstInt(dest=arm_code_const, value=arm_code))
				cmp_tmp = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=cmp_tmp, op=M.BinaryOp.EQ, left=code_tmp, right=arm_code_const))

				else_block = self.b.new_block("tryexpr_dispatch_next")
				self.b.set_terminator(M.IfTerminator(cond=cmp_tmp, then_target=cb.name, else_target=else_block.name))
				current_block = else_block

			self.b.set_block(current_block)
			if catch_all_block is not None:
				self.b.set_terminator(M.Goto(target=catch_all_block.name))
			else:
				self._propagate_error(err_tmp)
		else:
			self.b.set_block(dispatch_block)
			if catch_all_block is not None:
				self.b.set_terminator(M.Goto(target=catch_all_block.name))
			else:
				self._propagate_error(err_tmp)

		# Lower catch arms: bind error if requested, evaluate body+result, jump to join.
		for arm, cb in catch_blocks:
			self.b.set_block(cb)
			err_again = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=err_again, local=error_local))
			if arm.binder:
				self.b.ensure_local(arm.binder)
				self._local_types[arm.binder] = self._type_table.ensure_error()
				self.b.emit(M.StoreLocal(local=arm.binder, value=err_again))
			prev_catch_err = self._current_catch_error
			self._current_catch_error = error_local
			self.lower_block(arm.block)
			if arm.result is None:
				raise RuntimeError("try/catch expression arm must produce a value")
			arm_val = self.lower_expr(arm.result)
			self._current_catch_error = prev_catch_err
			self.b.emit(M.StoreLocal(local=temp_local, value=arm_val))
			if self.b.block.terminator is None:
				self.b.set_terminator(M.Goto(target=join_block.name))

		# Resume at join with the merged value.
		self.b.set_block(join_block)
		dest = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=dest, local=temp_local))
		return dest

	# --- Statement lowering ---

	def lower_stmt(self, stmt: H.HStmt) -> None:
		"""
		Entry point: lower a single HIR statement into MIR (appends to builder).

		Dispatches to a private _visit_stmt_* helper. Public stage API: callers
		should only invoke lower_expr/stmt/block; helpers stay private.
		"""
		method = getattr(self, f"_visit_stmt_{type(stmt).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No MIR lowering for stmt {type(stmt).__name__}")
		method(stmt)

	def lower_block(self, block: H.HBlock) -> None:
		"""Entry point: lower an HIR block (list of statements) into MIR."""
		for stmt in block.statements:
			self.lower_stmt(stmt)

	def lower_function_body(self, block: H.HBlock) -> None:
		"""
		Lower a full function body block and ensure the function ends in a terminator.

		MIR requires every basic block to end with a terminator. For the entry
		function body, we also want a production-safe invariant:
		  - `returns Void` functions may omit an explicit `return;` and will get an
		    implicit return.
		  - non-Void functions must end in an explicit return (checker responsibility).
		"""
		self.lower_block(block)
		if self.b.block.terminator is not None:
			return
		can_throw = self._fn_can_throw() is True
		fn_is_void = self._ret_type is not None and self._type_table.is_void(self._ret_type)
		if not fn_is_void:
			raise AssertionError("missing return reached MIR lowering (checker bug)")
		if not can_throw:
			self.b.set_terminator(M.Return(value=None))
			return
		# Can-throw `returns Void` lowers to FnResult<Void, Error>.
		res_val = self.b.new_temp()
		self.b.emit(M.ConstructResultOk(dest=res_val, value=None))
		self.b.set_terminator(M.Return(value=res_val))

	def _visit_stmt_HExprStmt(self, stmt: H.HExprStmt) -> None:
		# Evaluate and discard.
		#
		# - Non-throwing Void calls can be lowered as `Call(dest=None, ...)`.
		# - Can-throw calls must still be checked so Err paths route into the try
		#   dispatch (or propagate out of the function) even when the Ok value is
		#   ignored.
		if isinstance(stmt.expr, H.HCall) and isinstance(stmt.expr.fn, H.HVar):
			if self._callee_is_can_throw(stmt.expr.fn.name):
				fnres_val = self._lower_call(expr=stmt.expr)
				assert fnres_val is not None

				def emit_call() -> M.ValueId:
					return fnres_val

				self._lower_can_throw_call_stmt(emit_call=emit_call)
				return
			if self._call_returns_void(stmt.expr):
				self._lower_call(expr=stmt.expr)
				return
		if isinstance(stmt.expr, H.HMethodCall):
			if self._callee_is_can_throw(stmt.expr.method_name):
				fnres_val = self._lower_method_call(expr=stmt.expr)
				assert fnres_val is not None

				def emit_call() -> M.ValueId:
					return fnres_val

				self._lower_can_throw_call_stmt(emit_call=emit_call)
				return
			if self._call_returns_void(stmt.expr):
				self._lower_method_call(expr=stmt.expr)
				return
		self.lower_expr(stmt.expr)

	def _visit_stmt_HLet(self, stmt: H.HLet) -> None:
		self.b.ensure_local(stmt.name)
		val = self.lower_expr(stmt.value)
		val_ty = self._infer_expr_type(stmt.value)
		if val_ty is not None:
			self._local_types[stmt.name] = val_ty
		self.b.emit(M.StoreLocal(local=stmt.name, value=val))

	def _visit_stmt_HAssign(self, stmt: H.HAssign) -> None:
		val = self.lower_expr(stmt.value)
		if isinstance(stmt.target, H.HVar):
			self.b.ensure_local(stmt.target.name)
			val_ty = self._infer_expr_type(stmt.value)
			if val_ty is not None:
				self._local_types[stmt.target.name] = val_ty
			self.b.emit(M.StoreLocal(local=stmt.target.name, value=val))
		elif isinstance(stmt.target, H.HField):
			# Lower field assignment via an address computation + StoreRef so the
			# write updates the original storage (not a temporary copy).
			ptr, inner_ty = self._lower_addr_of_place(stmt.target, is_mut=True)
			self.b.emit(M.StoreRef(ptr=ptr, value=val, inner_ty=inner_ty))
		elif isinstance(stmt.target, H.HIndex):
			subject = self.lower_expr(stmt.target.subject)
			index = self.lower_expr(stmt.target.index)
			elem_ty = self._infer_array_elem_type(stmt.target.subject)
			self.b.emit(M.ArrayIndexStore(elem_ty=elem_ty, array=subject, index=index, value=val))
		elif isinstance(stmt.target, H.HUnary) and stmt.target.op is H.UnaryOp.DEREF:
			# `*p = v` assignment through a mutable reference.
			ptr_val = self.lower_expr(stmt.target.expr)
			ptr_ty = self._infer_expr_type(stmt.target.expr)
			if ptr_ty is None:
				raise AssertionError("deref assignment type unknown in MIR lowering (checker bug)")
			td = self._type_table.get(ptr_ty)
			if td.kind is not TypeKind.REF or not td.param_types:
				raise AssertionError("deref assignment of non-ref reached MIR lowering (checker bug)")
			inner_ty = td.param_types[0]
			self.b.emit(M.StoreRef(ptr=ptr_val, value=val, inner_ty=inner_ty))
		else:
			raise NotImplementedError(f"Unsupported assignment target: {type(stmt.target).__name__}")

	def _visit_stmt_HReturn(self, stmt: H.HReturn) -> None:
		if self.b.block.terminator is not None:
			return
		can_throw = self._fn_can_throw() is True
		fn_is_void = self._ret_type is not None and self._type_table.is_void(self._ret_type)

		if not can_throw:
			if fn_is_void:
				if stmt.value is not None:
					raise AssertionError("Void function must not have a return value (checker bug)")
				self.b.set_terminator(M.Return(value=None))
				return
			if stmt.value is None:
				raise AssertionError("non-void bare return reached MIR lowering (checker bug)")
			val = self.lower_expr(stmt.value)
			self.b.set_terminator(M.Return(value=val))
			return

		# Can-throw function: surface `returns T` lowers to an internal
		# `FnResult<T, Error>` return. Wrap normal returns into Ok.
		if fn_is_void:
			if stmt.value is not None:
				raise AssertionError("Void function must not have a return value (checker bug)")
			res_val = self.b.new_temp()
			self.b.emit(M.ConstructResultOk(dest=res_val, value=None))
			self.b.set_terminator(M.Return(value=res_val))
			return
		if stmt.value is None:
			raise AssertionError("non-void bare return reached MIR lowering (checker bug)")
		val = self.lower_expr(stmt.value)
		res_val = self.b.new_temp()
		self.b.emit(M.ConstructResultOk(dest=res_val, value=val))
		self.b.set_terminator(M.Return(value=res_val))

	def _visit_stmt_HBreak(self, stmt: H.HBreak) -> None:
		# Break jumps to the innermost loop's break target.
		if not self._loop_stack:
			raise NotImplementedError("break outside of loop not supported yet")
		_, break_target = self._loop_stack[-1]
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=break_target))

	def _visit_stmt_HContinue(self, stmt: H.HContinue) -> None:
		# Continue jumps to the innermost loop's continue target (loop header).
		if not self._loop_stack:
			raise NotImplementedError("continue outside of loop not supported yet")
		continue_target, _ = self._loop_stack[-1]
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=continue_target))

	def _visit_stmt_HIf(self, stmt: H.HIf) -> None:
		# If the current block already ended, do nothing.
		if self.b.block.terminator is not None:
			return

		# 1) Evaluate condition in the current block.
		cond_val = self.lower_expr(stmt.cond)

		# 2) Create then/else/join blocks.
		then_block = self.b.new_block("if_then")
		else_block = self.b.new_block("if_else") if stmt.else_block is not None else None
		join_block = self.b.new_block("if_join")

		# 3) Emit conditional terminator on current block.
		then_target = then_block.name
		else_target = else_block.name if else_block is not None else join_block.name
		self.b.set_terminator(
			M.IfTerminator(cond=cond_val, then_target=then_target, else_target=else_target)
		)

		# 4) Lower then block.
		self.b.set_block(then_block)
		self.lower_block(stmt.then_block)
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=join_block.name))

		# 5) Lower else block if present.
		if else_block is not None:
			self.b.set_block(else_block)
			self.lower_block(stmt.else_block)
			if self.b.block.terminator is None:
				self.b.set_terminator(M.Goto(target=join_block.name))

		# 6) Continue in join block.
		self.b.set_block(join_block)

	def _visit_stmt_HLoop(self, stmt: H.HLoop) -> None:
		# If the current block already ended, do nothing.
		if self.b.block.terminator is not None:
			return

		# Create loop blocks.
		header = self.b.new_block("loop_header")
		body = self.b.new_block("loop_body")
		exit_block = self.b.new_block("loop_exit")

		# Jump from current block to loop header.
		self.b.set_terminator(M.Goto(target=header.name))

		# Record loop context: continue -> header, break -> exit.
		self._loop_stack.append((header.name, exit_block.name))

		# Header: fall through to body.
		self.b.set_block(header)
		self.b.set_terminator(M.Goto(target=body.name))

		# Body: lower statements.
		self.b.set_block(body)
		self.lower_block(stmt.body)
		if self.b.block.terminator is None:
			# If body falls through, loop back.
			self.b.set_terminator(M.Goto(target=header.name))

		# Pop loop context and continue in exit block.
		self._loop_stack.pop()
		self.b.set_block(exit_block)

	def _visit_stmt_HThrow(self, stmt: H.HThrow) -> None:
		"""
		Lower `throw expr` into:
		  - construct an Error (event code + diagnostic payload),
		  - wrap it in FnResult.Err,
		  - return from the current function.

		This matches the ABI model where functions return `FnResult<R, Error>`.

		Event codes are taken from exception metadata when available (via
		`exc_env`), otherwise 0 as a placeholder.
		"""
		if self.b.block.terminator is not None:
			return
		can_throw = self._fn_can_throw()

		err_val = self.b.new_temp()
		if isinstance(stmt.value, H.HExceptionInit):
			from lang2.driftc.core.exception_ctor_args import KwArg as _KwArg, resolve_exception_ctor_args

			code_const = self._lookup_error_code(event_fqn=stmt.value.event_fqn)
			code_val = self.b.new_temp()
			self.b.emit(M.ConstInt(dest=code_val, value=code_const))

			event_fqn_val = self.b.new_temp()
			self.b.emit(M.ConstString(dest=event_fqn_val, value=stmt.value.event_fqn))

			schema = self._exception_schemas.get(stmt.value.event_fqn)
			if schema is None:
				raise AssertionError(f"missing exception schema for {stmt.value.event_fqn!r} (checker bug)")
			_decl_fqn, schema_fields = schema

			resolved, diags = resolve_exception_ctor_args(
				event_fqn=stmt.value.event_fqn,
				declared_fields=schema_fields,
				pos_args=[(a, getattr(a, "loc", Span())) for a in stmt.value.pos_args],
				kw_args=[
					_KwArg(name=kw.name, value=kw.value, name_span=getattr(kw, "loc", Span()))
					for kw in stmt.value.kw_args
				],
				span=getattr(stmt.value, "loc", Span()),
			)
			if diags:
				# The checker is responsible for reporting these to the user.
				raise AssertionError("exception ctor args reached MIR lowering with diagnostics (checker bug)")

			if not resolved:
				# No declared fields: build error with no attrs.
				self.b.emit(
					M.ConstructError(
						dest=err_val,
						code=code_val,
						event_fqn=event_fqn_val,
						payload=None,
						attr_key=None,
					)
				)
			else:
				field_dvs: list[tuple[str, M.ValueId]] = []
				for name, field_expr in resolved:
					if isinstance(field_expr, H.HDVInit):
						dv_val = self.lower_expr(field_expr)
					elif isinstance(field_expr, (H.HLiteralInt, H.HLiteralBool, H.HLiteralString)):
						inner_val = self.lower_expr(field_expr)
						dv_val = self.b.new_temp()
						# Only primitive literals are auto-wrapped into DiagnosticValue. This
						# keeps exception field attrs aligned with the DV ABI and avoids
						# silently accepting unsupported payload shapes.
						kind_name = "Int" if isinstance(field_expr, H.HLiteralInt) else "Bool"
						if isinstance(field_expr, H.HLiteralString):
							kind_name = "String"
						self.b.emit(M.ConstructDV(dest=dv_val, dv_type_name=kind_name, args=[inner_val]))
					else:
						raise AssertionError(
							f"exception field {name!r} must be a DiagnosticValue or primitive literal (checker bug)"
						)
					field_dvs.append((name, dv_val))

				first_name, first_dv = field_dvs[0]
				first_key = self.b.new_temp()
				self.b.emit(M.ConstString(dest=first_key, value=first_name))
				self.b.emit(
					M.ConstructError(
						dest=err_val,
						code=code_val,
						event_fqn=event_fqn_val,
						payload=first_dv,
						attr_key=first_key,
					)
				)
				for name, dv in field_dvs[1:]:
					key = self.b.new_temp()
					self.b.emit(M.ConstString(dest=key, value=name))
					self.b.emit(M.ErrorAddAttrDV(error=err_val, key=key, value=dv))
		else:
			# Throwing an existing Error value (e.g., from try-result sugar unwrap_err).
			err_val = self.lower_expr(stmt.value)

		# If we are inside a try, route to the catch block instead of returning.
		if self._try_stack and self.b.block.terminator is None:
			ctx = self._try_stack[-1]
			self.b.ensure_local(ctx.error_local)
			self.b.emit(M.StoreLocal(local=ctx.error_local, value=err_val))
			self.b.set_terminator(M.Goto(target=ctx.dispatch_block_name))
			return

		# Otherwise, propagate to an outer try if present, or return Err.
		self._propagate_error(err_val)

	def _propagate_error(self, err_val: M.ValueId) -> None:
		"""
		Propagate an Error value according to current try context:

		  - If there is an outer try on the stack, store into its error_local and
		    jump to its dispatch block (unwind to nearest outer try).
		  - If there is no outer try, the error escapes the current function:
		    wrap into FnResult.Err and return (can-throw ABI).
		"""
		if self._try_stack:
			ctx = self._try_stack[-1]
			self.b.ensure_local(ctx.error_local)
			self.b.emit(M.StoreLocal(local=ctx.error_local, value=err_val))
			self.b.set_terminator(M.Goto(target=ctx.dispatch_block_name))
		else:
			if self._fn_can_throw() is not True:
				# Defensive invariant: earlier stages guarantee that non-can-throw
				# functions cannot let an Error escape. However, MIR lowering cannot
				# always prove that a dispatch "else" path is unreachable (e.g., a
				# try/catch without a catch-all in a non-throwing function).
				#
				# Do not crash the compiler here. Instead, encode the invariant into
				# MIR so LLVM can emit an `unreachable` and tests can still build the
				# full pipeline. If this path is ever taken at runtime, it's a bug in
				# the front-end/checker.
				self.b.set_terminator(M.Unreachable())
				return
			res_val = self.b.new_temp()
			self.b.emit(M.ConstructResultErr(dest=res_val, error=err_val))
			self.b.set_terminator(M.Return(value=res_val))

	def _visit_stmt_HRethrow(self, stmt: H.HRethrow) -> None:
		"""
		Rethrow the currently caught Error; only valid inside a catch arm.

		This reuses the same propagation path as a throw of an existing Error,
		using the current try context's hidden error_local.
		"""
		if self.b.block.terminator is not None:
			return
		if self._current_catch_error is None:
			raise AssertionError("rethrow outside catch (checker bug)")
		err_val = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=err_val, local=self._current_catch_error))
		self._propagate_error(err_val)

	def _visit_stmt_HTry(self, stmt: H.HTry) -> None:
		"""
		Lower a try/catch with multiple arms into explicit blocks with a dispatch:

		  entry -> try_body
		  try_body -> try_cont (falls through)
		  throw in try_body -> try_dispatch
		  try_dispatch: ErrorEvent + event-code chain -> matching catch arm or catch-all
		  unmatched + no catch-all -> unwind to outer try if present, else return Err
		  each catch arm -> try_cont (if it falls through)

		Notes/assumptions:
		  - We defensively reject malformed arms here: at most one catch-all and
		    it must be the last arm.
		  - Unmatched errors first unwind to an outer try (if any) using the
		    same try-stack machinery as throw; only when there is no outer try
		    do we propagate Err out of this function.
		"""
		if self.b.block.terminator is not None:
			return
		if not stmt.catches:
			raise RuntimeError("HTry lowering requires at least one catch arm")

		body_block = self.b.new_block("try_body")
		dispatch_block = self.b.new_block("try_dispatch")
		cont_block = self.b.new_block("try_cont")

		# Hidden local to carry the Error into the dispatch/catch blocks.
		error_local = f"__try_err{self.b.new_temp()}"
		self.b.ensure_local(error_local)
		# Track the hidden error slot type so downstream inference/codegen has a concrete type.
		self._local_types[error_local] = self._type_table.ensure_error()

		# Create catch blocks for each arm.
		catch_blocks: list[tuple[H.HCatchArm, M.BasicBlock]] = []
		catch_all_block: M.BasicBlock | None = None
		catch_all_seen = False
		for idx, arm in enumerate(stmt.catches):
			cb = self.b.new_block(f"try_catch_{idx}")
			catch_blocks.append((arm, cb))
			if arm.event_fqn is None:
				if catch_all_block is not None:
					raise RuntimeError("multiple catch-all arms are not supported")
				catch_all_block = cb
				# Remember that we've seen a catch-all; any later event-specific
				# arms would be dead. We reject that here instead of silently
				# generating unreachable blocks.
				catch_all_seen = True
			else:
				if catch_all_seen:
					raise RuntimeError("catch-all must be the last catch arm")

		# Entry: jump into body and register try context so throws can target dispatch.
		self.b.set_terminator(M.Goto(target=body_block.name))
		self._try_stack.append(
			_TryCtx(
				error_local=error_local,
				dispatch_block_name=dispatch_block.name,
				cont_block_name=cont_block.name,
			)
		)

		# Lower try body.
		self.b.set_block(body_block)
		self.lower_block(stmt.body)
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=cont_block.name))

		# Pop context before lowering dispatch so throws in catch bodies route to the outer try.
		# Rethrow reads the caught error from `_current_catch_error` (set while lowering each catch body).
		self._try_stack.pop()

		# Dispatch: load error, project event code, branch to arms.
		self.b.set_block(dispatch_block)
		err_tmp = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=err_tmp, local=error_local))
		code_tmp = self.b.new_temp()
		self.b.emit(M.ErrorEvent(dest=code_tmp, error=err_tmp))

		# Chain event-specific arms with IfTerminator, else falling through.
		event_arms = [(arm, cb) for arm, cb in catch_blocks if arm.event_fqn is not None]
		if event_arms:
			# We build a chain of Ifs; the final else falls through to the final resolution.
			current_block = dispatch_block
			for arm, cb in event_arms:
				self.b.set_block(current_block)
				arm_code = self._lookup_catch_event_code(arm.event_fqn)
				arm_code_const = self.b.new_temp()
				self.b.emit(M.ConstInt(dest=arm_code_const, value=arm_code))
				cmp_tmp = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=cmp_tmp, op=M.BinaryOp.EQ, left=code_tmp, right=arm_code_const))

				else_block = self.b.new_block("try_dispatch_next")
				self.b.set_terminator(M.IfTerminator(cond=cmp_tmp, then_target=cb.name, else_target=else_block.name))
				current_block = else_block

			# Resolve final else: either catch-all or propagate via try stack/Err.
			self.b.set_block(current_block)
			if catch_all_block is not None:
				self.b.set_terminator(M.Goto(target=catch_all_block.name))
			else:
				self._propagate_error(err_tmp)
		else:
			# No event-specific arms: either jump to catch-all or propagate.
			self.b.set_block(dispatch_block)
			if catch_all_block is not None:
				self.b.set_terminator(M.Goto(target=catch_all_block.name))
			else:
				self._propagate_error(err_tmp)

		# Lower each catch arm: bind error if requested, emit ErrorEvent for handler logic, then body.
		for arm_idx, (arm, cb) in enumerate(catch_blocks):
			self.b.set_block(cb)
			err_again = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=err_again, local=error_local))
			if arm.binder:
				self.b.ensure_local(arm.binder)
				self._local_types[arm.binder] = self._type_table.ensure_error()
				self.b.emit(M.StoreLocal(local=arm.binder, value=err_again))
			code_again = self.b.new_temp()
			self.b.emit(M.ErrorEvent(dest=code_again, error=err_again))
			# Make the caught error available to `rethrow` inside this catch arm.
			prev_catch_err = self._current_catch_error
			self._current_catch_error = error_local
			self.lower_block(arm.block)
			self._current_catch_error = prev_catch_err
			if self.b.block.terminator is None:
				self.b.set_terminator(M.Goto(target=cont_block.name))

		# Continue in cont.
		self.b.set_block(cont_block)

	# --- Helpers ---

	def _infer_array_elem_type(self, subject: H.HExpr) -> TypeId:
		"""
		Best-effort element type inference for array subjects when lowering
		index loads/stores. Falls back to an Unknown elem type.
		"""
		# Fast path: if the subject is a known local with an Array type, reuse it.
		if isinstance(subject, H.HVar) and subject.name in self._local_types:
			subj_ty = self._local_types[subject.name]
			ty_def = self._type_table.get(subj_ty)
			if ty_def.kind is TypeKind.ARRAY and ty_def.param_types:
				return ty_def.param_types[0]

		subj_ty = self._infer_expr_type(subject)
		if subj_ty is None:
			return self._unknown_type
		ty_def = self._type_table.get(subj_ty)
		if ty_def.kind is TypeKind.ARRAY and ty_def.param_types:
			return ty_def.param_types[0]
		# Strings are not arrays; bail out to Unknown so later passes can diagnose.
		if ty_def.kind is TypeKind.SCALAR and ty_def.name == "String":
			return self._unknown_type
		return self._unknown_type

	def _infer_array_literal_elem_type(self, expr: H.HArrayLiteral) -> TypeId:
		"""
		Best-effort element type inference for array literals.
		"""
		elem_types = [self._infer_expr_type(e) for e in expr.elements]
		elem_types = [t for t in elem_types if t is not None]
		if not elem_types:
			return self._unknown_type
		first = elem_types[0]
		if all(t == first for t in elem_types):
			return first
		return self._unknown_type

	def _return_type_for_name(self, name: str) -> TypeId | None:
		"""Look up a return TypeId for a given function/method name when available."""
		sig = self._signatures.get(name)
		if sig and sig.return_type_id is not None:
			return sig.return_type_id
		# Try display-name matches (method_name).
		for cand in self._signatures.values():
			if cand.method_name == name and cand.return_type_id is not None:
				return cand.return_type_id
		return None

	def _call_returns_void(self, expr: H.HExpr) -> bool:
		if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
			if self._callee_is_can_throw(expr.fn.name):
				# Can-throw calls return an internal FnResult value, even when the
				# surface ok type is Void.
				return False
			ret = self._return_type_for_name(expr.fn.name)
			return ret is not None and self._type_table.is_void(ret)
		if isinstance(expr, H.HMethodCall):
			if self._callee_is_can_throw(expr.method_name):
				return False
			ret = self._return_type_for_name(expr.method_name)
			return ret is not None and self._type_table.is_void(ret)
		return False

	def _lower_call(self, expr: H.HCall) -> M.ValueId | None:
		if not isinstance(expr.fn, H.HVar):
			raise NotImplementedError("Only direct function-name calls are supported in MIR lowering")
		arg_vals = [self.lower_expr(a) for a in expr.args]
		# Can-throw calls always return an internal FnResult value, even when the
		# surface ok type is Void.
		if self._callee_is_can_throw(expr.fn.name):
			dest = self.b.new_temp()
			self.b.emit(M.Call(dest=dest, fn=expr.fn.name, args=arg_vals))
			return dest
		ret_tid = self._return_type_for_name(expr.fn.name)
		if ret_tid is not None and self._type_table.is_void(ret_tid):
			self.b.emit(M.Call(dest=None, fn=expr.fn.name, args=arg_vals))
			return None
		dest = self.b.new_temp()
		self.b.emit(M.Call(dest=dest, fn=expr.fn.name, args=arg_vals))
		return dest

	def _lower_method_call(self, expr: H.HMethodCall) -> M.ValueId | None:
		receiver = self.lower_expr(expr.receiver)
		arg_vals = [self.lower_expr(a) for a in expr.args]
		if self._callee_is_can_throw(expr.method_name):
			dest = self.b.new_temp()
			self.b.emit(M.MethodCall(dest=dest, receiver=receiver, method_name=expr.method_name, args=arg_vals))
			return dest
		ret_tid = self._return_type_for_name(expr.method_name)
		if ret_tid is not None and self._type_table.is_void(ret_tid):
			self.b.emit(M.MethodCall(dest=None, receiver=receiver, method_name=expr.method_name, args=arg_vals))
			return None
		dest = self.b.new_temp()
		self.b.emit(M.MethodCall(dest=dest, receiver=receiver, method_name=expr.method_name, args=arg_vals))
		return dest

	def _return_typeid_for_callee(self, name: str) -> TypeId | None:
		"""
		Return the declared return TypeId for a callee by name when available.

		This is the *surface* return type (`T` in `returns T`), not the internal
		ABI return type. When the callee is can-throw, the compiler still treats
		this as the ok payload type.
		"""
		sig = self._signatures.get(name)
		if sig and sig.return_type_id is not None:
			return sig.return_type_id
		for cand in self._signatures.values():
			if cand.method_name == name and cand.return_type_id is not None:
				return cand.return_type_id
		return None

	def _callee_is_can_throw(self, name: str) -> bool:
		"""
		Best-effort can-throw classification for a callee.

		In lang2 v1, "can-throw" is an effect on a function, not a surface return
		type. The checker computes a can-throw map; we treat that as the source of
		truth when present and fall back to signature hints in legacy tests.
		"""
		if name in self._can_throw_by_name:
			return bool(self._can_throw_by_name[name])
		sig = self._signatures.get(name)
		if sig is None:
			for cand in self._signatures.values():
				if cand.method_name == name:
					sig = cand
					break
		if sig is not None and sig.declared_can_throw is not None:
			return bool(sig.declared_can_throw)
		# Legacy fallback: old surface model treated FnResult returns as can-throw.
		rt = self._return_typeid_for_callee(name)
		return rt is not None and self._type_table.get(rt).kind is TypeKind.FNRESULT

	def _lower_can_throw_call_value(
		self,
		*,
		emit_call: callable,
		ok_ty: TypeId,
	) -> M.ValueId:
		"""
		Lower a can-throw call in a try context as an expression producing the ok payload.

		We call the callee to obtain a FnResult value, branch on `is_err`, route the
		error to the current try dispatch when err, and otherwise extract+return
		the ok value through a hidden local + join block.
		"""
		# Hidden local for the ok payload.
		ok_local = f"__call_ok{self.b.new_temp()}"
		self.b.ensure_local(ok_local)
		self._local_types[ok_local] = ok_ty

		fnres_val = emit_call()
		is_err = self.b.new_temp()
		self.b.emit(M.ResultIsErr(dest=is_err, result=fnres_val))

		ok_block = self.b.new_block("call_ok")
		err_block = self.b.new_block("call_err")
		join_block = self.b.new_block("call_join")

		self.b.set_terminator(
			M.IfTerminator(cond=is_err, then_target=err_block.name, else_target=ok_block.name)
		)

		# Err path: route the error to an active try (if any), otherwise propagate
		# out of the current function.
		self.b.set_block(err_block)
		err_val = self.b.new_temp()
		self.b.emit(M.ResultErr(dest=err_val, result=fnres_val))
		if self._try_stack:
			ctx = self._try_stack[-1]
			self.b.emit(M.StoreLocal(local=ctx.error_local, value=err_val))
			self.b.set_terminator(M.Goto(target=ctx.dispatch_block_name))
		else:
			self._propagate_error(err_val)

		# Ok path: extract ok value and continue at join.
		self.b.set_block(ok_block)
		ok_val = self.b.new_temp()
		self.b.emit(M.ResultOk(dest=ok_val, result=fnres_val))
		self.b.emit(M.StoreLocal(local=ok_local, value=ok_val))
		self.b.set_terminator(M.Goto(target=join_block.name))

		# Join: load ok from hidden local as the value of this expression.
		self.b.set_block(join_block)
		dest = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=dest, local=ok_local))
		return dest

	def _lower_can_throw_call_stmt(
		self,
		*,
		emit_call: callable,
	) -> None:
		"""
		Lower a can-throw call in a try context as a statement (ignores ok value).

		We still must check for Err and route it to the current try dispatch.
		"""
		fnres_val = emit_call()
		is_err = self.b.new_temp()
		self.b.emit(M.ResultIsErr(dest=is_err, result=fnres_val))

		ok_block = self.b.new_block("call_ok")
		err_block = self.b.new_block("call_err")
		join_block = self.b.new_block("call_join")

		self.b.set_terminator(
			M.IfTerminator(cond=is_err, then_target=err_block.name, else_target=ok_block.name)
		)

		# Err path: route the error to an active try (if any), otherwise propagate
		# out of the current function.
		self.b.set_block(err_block)
		err_val = self.b.new_temp()
		self.b.emit(M.ResultErr(dest=err_val, result=fnres_val))
		if self._try_stack:
			ctx = self._try_stack[-1]
			self.b.emit(M.StoreLocal(local=ctx.error_local, value=err_val))
			self.b.set_terminator(M.Goto(target=ctx.dispatch_block_name))
		else:
			self._propagate_error(err_val)

		# Ok path: ignore ok payload and continue.
		self.b.set_block(ok_block)
		self.b.set_terminator(M.Goto(target=join_block.name))

		# Join: continue lowering subsequent statements in the surrounding block.
		self.b.set_block(join_block)

	def _infer_expr_type(self, expr: H.HExpr) -> TypeId | None:
		"""
		Minimal expression type inference to tag typed MIR nodes.

		This is intentionally conservative: it only returns a TypeId when the type
		can be inferred locally (literals, some builtins, locals with known types).
		"""
		if isinstance(expr, H.HLiteralInt):
			return self._int_type
		if isinstance(expr, H.HLiteralFloat):
			return self._float_type
		if isinstance(expr, H.HLiteralBool):
			return self._bool_type
		if isinstance(expr, H.HLiteralString):
			return self._string_type
		if isinstance(expr, H.HFString):
			return self._string_type
		if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
			name = expr.fn.name
			# Struct constructor call: result is the struct TypeId.
			if name in getattr(self._type_table, "struct_schemas", {}):
				return self._type_table.ensure_named(name)
			sig_ret = self._return_type_for_name(name)
			if sig_ret is not None:
				return sig_ret
			if name == "string_concat":
				return self._string_type
			if name == "string_eq":
				return self._bool_type
			if name == "len" and expr.args:
				arg_ty = self._infer_expr_type(expr.args[0])
				if arg_ty is not None:
					td = self._type_table.get(arg_ty)
					if td.kind is TypeKind.ARRAY or (td.kind is TypeKind.SCALAR and td.name == "String"):
						return self._uint_type
		if isinstance(expr, H.HField) and expr.name in ("len", "cap", "capacity"):
			subj_ty = self._infer_expr_type(expr.subject)
			if subj_ty is None:
				return None
			ty_def = self._type_table.get(subj_ty)
			if ty_def.kind is TypeKind.ARRAY or (ty_def.kind is TypeKind.SCALAR and ty_def.name == "String"):
				return self._uint_type
			if expr.name == "attrs" and ty_def.kind is TypeKind.ERROR:
				return self._dv_type
		if isinstance(expr, H.HField):
			subj_ty = self._infer_expr_type(expr.subject)
			if subj_ty is None:
				return None
			sub_def = self._type_table.get(subj_ty)
			if sub_def.kind is TypeKind.STRUCT:
				info = self._type_table.struct_field(subj_ty, expr.name)
				if info is None:
					return None
				_, fty = info
				return fty
		if isinstance(expr, H.HArrayLiteral):
			elem_ty = self._infer_array_literal_elem_type(expr)
			return self._type_table.new_array(elem_ty)
		if isinstance(expr, H.HVar):
			return self._local_types.get(expr.name)
		if isinstance(expr, H.HBorrow):
			inner = self._infer_expr_type(expr.subject)
			inner = inner if inner is not None else self._unknown_type
			return self._type_table.ensure_ref_mut(inner) if expr.is_mut else self._type_table.ensure_ref(inner)
		if isinstance(expr, H.HUnary) and expr.op is H.UnaryOp.DEREF:
			operand_ty = self._infer_expr_type(expr.expr)
			if operand_ty is None:
				return None
			td = self._type_table.get(operand_ty)
			if td.kind is TypeKind.REF and td.param_types:
				return td.param_types[0]
			return None
		if isinstance(expr, H.HIndex):
			array_ty = self._infer_expr_type(expr.subject)
			if array_ty is not None:
				ty_def = self._type_table.get(array_ty)
				if ty_def.kind is TypeKind.ARRAY and ty_def.param_types:
					return ty_def.param_types[0]
		if isinstance(expr, H.HMethodCall):
			ret = self._return_type_for_name(expr.method_name)
			if ret is not None:
				return ret
			recv_ty = self._infer_expr_type(expr.receiver)
			if recv_ty is not None:
				recv_def = self._type_table.get(recv_ty)
				if recv_def.kind is TypeKind.DIAGNOSTICVALUE:
					if expr.method_name == "as_int":
						return self._opt_int
					if expr.method_name == "as_bool":
						return self._opt_bool
					if expr.method_name == "as_string":
						return self._opt_string
		if hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
			return self._infer_expr_type(expr.attempt)
		return None

	def _lower_addr_of_place(self, expr: H.HExpr, *, is_mut: bool) -> tuple[M.ValueId, TypeId]:
		"""
		Lower an addressable HIR "place" to a pointer and its pointee TypeId.

		This is the common primitive for:
		  - borrows (`&place` / `&mut place`)
		  - field assignment lowering (`place.field = v`)

		`is_mut` records the mutability of the originating borrow/assignment; LLVM
		lowering uses the same pointer representation for `&T` and `&mut T`.

		Invariants:
		  - The checker (plus stage1 temporary materialization) ensures `expr` is a
		    real place. If we see an rvalue here, it's a pipeline bug.
		"""
		# Locals/params: address-of storage.
		if isinstance(expr, H.HVar):
			self.b.ensure_local(expr.name)
			ty = self._infer_expr_type(expr)
			if ty is None:
				raise AssertionError("address-of local type unknown in MIR lowering (checker bug)")
			dest = self.b.new_temp()
			self.b.emit(M.AddrOfLocal(dest=dest, local=expr.name, is_mut=is_mut))
			return dest, ty

		# Deref place: `*p` as an lvalue yields the underlying pointer value.
		if isinstance(expr, H.HUnary) and expr.op is H.UnaryOp.DEREF:
			ptr_ty = self._infer_expr_type(expr.expr)
			if ptr_ty is None:
				raise AssertionError("deref place type unknown in MIR lowering (checker bug)")
			td = self._type_table.get(ptr_ty)
			if td.kind is not TypeKind.REF or not td.param_types:
				raise AssertionError("deref place of non-ref reached MIR lowering (checker bug)")
			if is_mut and not td.ref_mut:
				raise AssertionError("mutable deref place without &mut reached MIR lowering (checker bug)")
			inner_ty = td.param_types[0]
			ptr_val = self.lower_expr(expr.expr)
			return ptr_val, inner_ty

		# Array index place: `arr[i]` yields the element address.
		if isinstance(expr, H.HIndex):
			array_val = self.lower_expr(expr.subject)
			index_val = self.lower_expr(expr.index)
			elem_ty = self._infer_array_elem_type(expr.subject)
			dest = self.b.new_temp()
			self.b.emit(
				M.AddrOfArrayElem(
					dest=dest,
					array=array_val,
					index=index_val,
					inner_ty=elem_ty,
					is_mut=is_mut,
				)
			)
			return dest, elem_ty

		# Struct field place: `base.field`.
		if isinstance(expr, H.HField):
			base_ptr, base_ty = self._lower_addr_of_place(expr.subject, is_mut=is_mut)
			base_def = self._type_table.get(base_ty)
			if base_def.kind is not TypeKind.STRUCT:
				raise AssertionError("field place base is not a struct (checker bug)")
			info = self._type_table.struct_field(base_ty, expr.name)
			if info is None:
				raise AssertionError("unknown struct field reached MIR lowering (checker bug)")
			field_idx, field_ty = info
			dest = self.b.new_temp()
			self.b.emit(
				M.AddrOfField(
					dest=dest,
					base_ptr=base_ptr,
					struct_ty=base_ty,
					field_index=field_idx,
					field_ty=field_ty,
					is_mut=is_mut,
				)
			)
			return dest, field_ty

		raise AssertionError("address-of unsupported place reached MIR lowering (checker bug)")

	def _lookup_error_code(self, payload_expr: H.HExpr | None = None, *, event_fqn: str | None = None) -> int:
		"""
		Best-effort event code lookup from exception metadata.

		If the payload is an exception init and an exception env was provided,
		return that code; otherwise return 0.
		"""
		if self._exc_env is None:
			return 0
		if event_fqn:
			return self._exc_env.get(event_fqn, 0)
		if isinstance(payload_expr, H.HExceptionInit):
			fqn = getattr(payload_expr, "event_fqn", None)
			if fqn:
				return self._exc_env.get(fqn, 0)
		return 0

	def _lookup_catch_event_code(self, event_fqn: str) -> int:
		"""
		Lookup event code for a catch arm by canonical exception/event FQN.

		Uses the same exception env mapping (name -> code) as throw lowering;
		fallback to 0 if unknown.
		"""
		if self._exc_env is not None:
			return self._exc_env.get(event_fqn, 0)
		return 0


__all__ = ["MirBuilder", "HIRToMIR"]


@dataclass
class _TryCtx:
	"""
	Internal try/catch context to route throws to the correct catch block.

	error_local: hidden local where the thrown Error is stored.
	dispatch_block_name: block that projects the event code and dispatches to arms.
	cont_block_name: continuation block after the try/catch completes.
	"""

	error_local: str
	dispatch_block_name: str
	cont_block_name: str
