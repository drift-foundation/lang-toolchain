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
	):
		"""
		Create a lowering context.

		`exc_env` (optional) maps DV/exception type names to event codes so
		throw lowering can emit real codes instead of placeholders.
		"""
		self.b = builder
		# Stack of (continue_target, break_target) block names for nested loops.
		self._loop_stack: list[tuple[str, str]] = []
		# Stack of try contexts for nested try/catch (innermost on top).
		self._try_stack: list["_TryCtx"] = []
		# Optional exception environment: maps DV/exception type name -> event code.
		self._exc_env = exc_env
		# Track best-effort local types (TypeId) to tag typed MIR nodes.
		self._local_types: dict[str, TypeId] = dict(param_types) if param_types else {}
		# Optional shared TypeTable for typed MIR nodes (arrays, etc.).
		self._type_table = type_table or TypeTable()
		# Cache some common types for reuse when shared.
		self._int_type = self._type_table.ensure_int()
		self._bool_type = self._type_table.ensure_bool()
		self._string_type = self._type_table.ensure_string()
		self._string_empty_const = self.b.new_temp()
		# Inject a private empty string literal for String.EMPTY; this is a
		# zero-length, null-data string produced at MIR lowering time.
		self.b.emit(M.ConstString(dest=self._string_empty_const, value=""))
		self._uint_type = self._type_table.ensure_uint()
		self._unknown_type = self._type_table.ensure_unknown()

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

	def _visit_expr_HLiteralBool(self, expr: H.HLiteralBool) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstBool(dest=dest, value=expr.value))
		return dest

	def _visit_expr_HLiteralString(self, expr: H.HLiteralString) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstString(dest=dest, value=expr.value))
		return dest

	def _visit_expr_HVar(self, expr: H.HVar) -> M.ValueId:
		self.b.ensure_local(expr.name)
		# Treat String.EMPTY as a builtin zero-length string literal.
		if expr.name == "String.EMPTY":
			return self._string_empty_const
		dest = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=dest, local=expr.name))
		return dest

	def _visit_expr_HUnary(self, expr: H.HUnary) -> M.ValueId:
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
		if expr.op in (H.BinaryOp.ADD, H.BinaryOp.EQ) and left_ty == self._string_type and right_ty == self._string_type:
			if expr.op is H.BinaryOp.ADD:
				self.b.emit(M.StringConcat(dest=dest, left=left, right=right))
				return dest
			if expr.op is H.BinaryOp.EQ:
				self.b.emit(M.StringEq(dest=dest, left=left, right=right))
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
		dest = self.b.new_temp()
		self.b.emit(M.LoadField(dest=dest, subject=subject, field=expr.name))
		return dest

	def _visit_expr_HIndex(self, expr: H.HIndex) -> M.ValueId:
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
		arg_vals = [self.lower_expr(a) for a in expr.args]
		dest = self.b.new_temp()
		self.b.emit(M.Call(dest=dest, fn=expr.fn.name, args=arg_vals))
		return dest

	def _visit_expr_HMethodCall(self, expr: H.HMethodCall) -> M.ValueId:
		receiver = self.lower_expr(expr.receiver)
		arg_vals = [self.lower_expr(a) for a in expr.args]
		dest = self.b.new_temp()
		self.b.emit(
			M.MethodCall(dest=dest, receiver=receiver, method_name=expr.method_name, args=arg_vals)
		)
		return dest

	def _visit_expr_HDVInit(self, expr: H.HDVInit) -> M.ValueId:
		arg_vals = [self.lower_expr(a) for a in expr.args]
		dest = self.b.new_temp()
		self.b.emit(M.ConstructDV(dest=dest, dv_type_name=expr.dv_type_name, args=arg_vals))
		return dest

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

	def _visit_stmt_HExprStmt(self, stmt: H.HExprStmt) -> None:
		# Evaluate and discard
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
			subject = self.lower_expr(stmt.target.subject)
			self.b.emit(M.StoreField(subject=subject, field=stmt.target.name, value=val))
		elif isinstance(stmt.target, H.HIndex):
			subject = self.lower_expr(stmt.target.subject)
			index = self.lower_expr(stmt.target.index)
			elem_ty = self._infer_array_elem_type(stmt.target.subject)
			self.b.emit(M.ArrayIndexStore(elem_ty=elem_ty, array=subject, index=index, value=val))
		else:
			raise NotImplementedError(f"Unsupported assignment target: {type(stmt.target).__name__}")

	def _visit_stmt_HReturn(self, stmt: H.HReturn) -> None:
		if self.b.block.terminator is not None:
			return
		val = self.lower_expr(stmt.value) if stmt.value is not None else None
		self.b.set_terminator(M.Return(value=val))

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

		# Payload of the error (already a DiagnosticValue expression in HIR).
		payload_val = self.lower_expr(stmt.value)

		# Event code from exception metadata if available; otherwise 0.
		code_const = self._lookup_error_code(stmt.value)
		code_val = self.b.new_temp()
		self.b.emit(M.ConstInt(dest=code_val, value=code_const))

		# Build the Error value.
		err_val = self.b.new_temp()
		self.b.emit(M.ConstructError(dest=err_val, code=code_val, payload=payload_val))

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
		  - If there is no outer try, wrap into FnResult.Err and return.
		"""
		if self._try_stack:
			ctx = self._try_stack[-1]
			self.b.ensure_local(ctx.error_local)
			self.b.emit(M.StoreLocal(local=ctx.error_local, value=err_val))
			self.b.set_terminator(M.Goto(target=ctx.dispatch_block_name))
		else:
			res_val = self.b.new_temp()
			self.b.emit(M.ConstructResultErr(dest=res_val, error=err_val))
			self.b.set_terminator(M.Return(value=res_val))

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

		# Create catch blocks for each arm.
		catch_blocks: list[tuple[H.HCatchArm, M.BasicBlock]] = []
		catch_all_block: M.BasicBlock | None = None
		catch_all_seen = False
		for idx, arm in enumerate(stmt.catches):
			cb = self.b.new_block(f"try_catch_{idx}")
			catch_blocks.append((arm, cb))
			if arm.event_name is None:
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

		# Pop context before lowering dispatch/catches so throws inside catch go to outer try.
		self._try_stack.pop()

		# Dispatch: load error, project event code, branch to arms.
		self.b.set_block(dispatch_block)
		err_tmp = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=err_tmp, local=error_local))
		code_tmp = self.b.new_temp()
		self.b.emit(M.ErrorEvent(dest=code_tmp, error=err_tmp))

		# Chain event-specific arms with IfTerminator, else falling through.
		event_arms = [(arm, cb) for arm, cb in catch_blocks if arm.event_name is not None]
		if event_arms:
			# We build a chain of Ifs; the final else falls through to the final resolution.
			current_block = dispatch_block
			for arm, cb in event_arms:
				self.b.set_block(current_block)
				arm_code = self._lookup_catch_event_code(arm.event_name)
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
		for arm, cb in catch_blocks:
			self.b.set_block(cb)
			err_again = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=err_again, local=error_local))
			if arm.binder:
				self.b.ensure_local(arm.binder)
				self.b.emit(M.StoreLocal(local=arm.binder, value=err_again))
			code_again = self.b.new_temp()
			self.b.emit(M.ErrorEvent(dest=code_again, error=err_again))
			self.lower_block(arm.block)
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

	def _infer_expr_type(self, expr: H.HExpr) -> TypeId | None:
		"""
		Minimal expression type inference to tag array instructions with elem types.
		"""
		if isinstance(expr, H.HLiteralInt):
			return self._int_type
		if isinstance(expr, H.HLiteralBool):
			return self._bool_type
		if isinstance(expr, H.HLiteralString):
			return self._string_type
		if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
			name = expr.fn.name
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
		if isinstance(expr, H.HArrayLiteral):
			elem_ty = self._infer_array_literal_elem_type(expr)
			return self._type_table.new_array(elem_ty)
		if isinstance(expr, H.HVar):
			return self._local_types.get(expr.name)
		if isinstance(expr, H.HIndex):
			array_ty = self._infer_expr_type(expr.subject)
			if array_ty is not None:
				ty_def = self._type_table.get(array_ty)
				if ty_def.kind is TypeKind.ARRAY and ty_def.param_types:
					return ty_def.param_types[0]
		# Unknown for other expressions (vars, calls, etc.)
		return None

	def _lookup_error_code(self, payload_expr: H.HExpr) -> int:
		"""
		Best-effort event code lookup from exception metadata.

		If the payload is an HDVInit with a known DV/exception name and an
		exception env was provided, return that code; otherwise return 0.
		"""
		if isinstance(payload_expr, H.HDVInit) and self._exc_env is not None:
			return self._exc_env.get(payload_expr.dv_type_name, 0)
		return 0

	def _lookup_catch_event_code(self, event_name: str) -> int:
		"""
		Lookup event code for a catch arm by exception/event name.

		Uses the same exception env mapping (name -> code) as throw lowering;
		fallback to 0 if unknown.
		"""
		if self._exc_env is not None:
			return self._exc_env.get(event_name, 0)
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
