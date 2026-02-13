# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
HIR → MIR lowering (expressions/statements, if/loop).

Pipeline placement:
  AST (lang/stage0/ast.py) → HIR (lang/stage1/hir_nodes.py) → MIR (this file) → SSA → LLVM/obj

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
import copy
import hashlib
import os
from typing import List, Set, Mapping, Optional

from lang.driftc import stage1 as H
from lang.driftc import debug as drift_debug
from lang.driftc.stage1 import closures as C
from lang.driftc.stage1.place_expr import place_expr_from_lvalue_expr
from lang.driftc.stage1.call_info import (
	CallInfo,
	CallSig,
	CallTarget,
	CallTargetKind,
	IntrinsicKind,
	call_abi_ret_type,
)
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId, FunctionRefId, FunctionRefKind, function_symbol
from lang.driftc.core.container_ids import ARRAY_CONTAINER_ID
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import (
	TypeKind,
	TypeTable,
	TypeId,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.stage1.capture_discovery import discover_captures
from lang.driftc.stage1.closures import sort_captures
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

	def __init__(self, name: str, *, fn_id: FunctionId):
		if name != function_symbol(fn_id):
			raise AssertionError(f"MirBuilder name '{name}' must match fn_id symbol '{function_symbol(fn_id)}'")
		entry_block = M.BasicBlock(name="entry")
		self.func = M.MirFunc(
			name=name,
			params=[],
			locals=[],
			blocks={"entry": entry_block},
			entry="entry",
			fn_id=fn_id,
		)
		self.extra_funcs: list[M.MirFunc] = []
		self.block = entry_block
		self._temp_counter = 0
		self._locals_set: Set[M.LocalId] = set()
		self.current_span: Span | None = None

	def new_temp(self) -> M.ValueId:
		"""Allocate a fresh temporary ValueId for intermediate results."""
		self._temp_counter += 1
		return f"t{self._temp_counter}"

	def emit(self, instr: M.MInstr) -> M.ValueId | None:
		"""
		Append a MIR instruction to the current block and return its dest, if any.
		"""
		self.block.instructions.append(instr)
		if self.current_span is not None and self.current_span != Span():
			existing = getattr(instr, "span", None)
			if existing is None or existing == Span():
				setattr(instr, "span", self.current_span)
		if hasattr(instr, "dest"):
			return getattr(instr, "dest")
		return None

	def set_terminator(self, term: M.MTerminator) -> None:
		"""Set the terminator for the current block."""
		self.block.terminator = term
		if self.current_span is not None and self.current_span != Span():
			existing = getattr(term, "span", None)
			if existing is None or existing == Span():
				setattr(term, "span", self.current_span)

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


def make_builder(fn_id: FunctionId) -> MirBuilder:
	return MirBuilder(name=function_symbol(fn_id), fn_id=fn_id)


@dataclass(frozen=True)
class SynthSigSpec:
	fn_id: FunctionId
	sig: FnSignature
	kind: str


@dataclass(frozen=True)
class HiddenLambdaSpec:
	fn_id: FunctionId
	origin_fn_id: FunctionId | None
	lambda_expr: H.HLambda
	param_names: list[str]
	param_type_ids: list[TypeId]
	return_type_id: TypeId
	can_throw: bool
	has_captures: bool
	env_ty: TypeId | None
	env_field_types: list[TypeId]
	capture_map: dict[C.HCaptureKey, int]
	capture_kinds: list[C.HCaptureKind]
	lambda_capture_ref_is_value: bool
	is_callback_lambda: bool


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
		expr_types: Mapping[int, TypeId] | None = None,
		iface_coercions: Mapping[int, TypeId] | None = None,
		signatures_by_id: Mapping[FunctionId, FnSignature] | None = None,
		current_fn_id: FunctionId | None = None,
		type_param_subst: Mapping[str, TypeId] | None = None,
		call_info_by_callsite_id: Mapping[int, CallInfo] | None = None,
		call_resolutions: Mapping[int, object] | None = None,
		can_throw_by_id: Mapping[FunctionId, bool] | None = None,
		return_type: TypeId | None = None,
		binding_names: Mapping[int, str] | None = None,
		binding_types: Mapping[int, TypeId] | None = None,
		typed_mode: str | bool = False,
	):
		"""
		Create a lowering context.

			`exc_env` (optional) maps exception FQNs to event codes so
			throw lowering can emit real codes instead of placeholders.
			"""
		self.b = builder
		# Stack of (continue_target, break_target, scope_index) for nested loops.
		self._loop_stack: list[tuple[str, str, int]] = []
		# Stack of scopes; each scope stores locals that need drop at scope exit.
		self._scope_stack: list[list[str]] = []
		self._moved_locals: set[str] = set()
		self._current_stmt_span: Span | None = None
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
		self._byte_type = self._type_table.ensure_byte()
		self._string_type = self._type_table.ensure_string()
		self._string_empty_const = self.b.new_temp()
		# Inject a private empty string literal for String.EMPTY; this is a
		# zero-length, null-data string produced at MIR lowering time.
		self.b.emit(M.ConstString(dest=self._string_empty_const, value=""))
		self._uint_type = self._type_table.ensure_uint()
		self._uint64_type = self._type_table.ensure_uint64()
		self._unknown_type = self._type_table.ensure_unknown()
		self._void_type = self._type_table.ensure_void()
		self._dv_type = self._type_table.ensure_diagnostic_value()
		self._signatures_by_id = signatures_by_id or {}
		self._current_fn_id = current_fn_id
		self._type_param_subst: dict[str, TypeId] = dict(type_param_subst or {})
		if self._current_fn_id is not None and self._signatures_by_id:
			sig = self._signatures_by_id.get(self._current_fn_id)
			if sig is not None and sig.impl_target_type_id and sig.impl_target_type_args:
				schema = self._type_table.get_struct_schema(sig.impl_target_type_id)
				if schema is not None and schema.type_params:
					for name, arg in zip(schema.type_params, sig.impl_target_type_args):
						self._type_param_subst[name] = arg
		self._expr_types: dict[int, TypeId] = dict(expr_types) if expr_types else {}
		self._iface_coercions: dict[int, TypeId] = dict(iface_coercions) if iface_coercions else {}
		self._call_info_by_callsite_id: dict[int, CallInfo] = (
			dict(call_info_by_callsite_id) if call_info_by_callsite_id else {}
		)
		self._call_resolutions: dict[int, object] = dict(call_resolutions) if call_resolutions else {}
		self._type_id_token_cache: dict[TypeId, int] = {}
		if isinstance(typed_mode, bool):
			self._typed_mode = "strict" if typed_mode else "none"
		else:
			if typed_mode not in ("none", "strict", "recover"):
				raise ValueError(f"unexpected typed_mode {typed_mode!r}")
			self._typed_mode = typed_mode
		self._synth_sig_specs: list[SynthSigSpec] = []
		self._hidden_lambda_specs: list[HiddenLambdaSpec] = []
		# Best-effort can-throw classification for functions. This is intentionally
		# separate from signatures: the surface language does not expose FnResult,
		# and "can-throw" is an effect inferred from the body (or declared by a
		# future `nothrow`/throws annotation).
		self._can_throw_by_id: dict[FunctionId, bool] = dict(can_throw_by_id) if can_throw_by_id else {}
		self._current_fn_can_throw: bool | None = self._can_throw_by_id.get(current_fn_id) if current_fn_id else None
		self._ret_type = return_type
		self._suppress_drop_glue = False
		if current_fn_id is not None:
			if "Destructible::destroy" in function_symbol(current_fn_id):
				self._suppress_drop_glue = True
		self._param_drop_locals: list[str] = []
		if param_types:
			for name, ty in param_types.items():
				if self._needs_runtime_drop(ty):
					self._param_drop_locals.append(name)
		# Stage2 lowering is "assert-only" with respect to match pattern
		# normalization: the typed checker is expected to populate
		# `HMatchArm.binder_field_indices` once the scrutinee type is known.
		# Cache the current function signature for defensive fallbacks in older
		# unit tests that bypass the checker.
		self._fn_sig = self._signatures_by_id.get(current_fn_id) if current_fn_id else None
		# Stage2 expects caller-provided FunctionId/signature wiring; no name-based fallback.
		# Expected type hints for expression lowering.
		#
		# Stage2 can optionally consume the checker's per-expression type map.
		#
		# typed_mode == "strict": typecheck succeeded and expr_types contain no
		# Unknown entries (Unknown is an internal error).
		# typed_mode == "recover": expr_types may be partial; Unknown entries are
		# ignored and lowering falls back to local inference.
		# typed_mode == "none": expr_types are ignored.
		self._expected_type_stack: list[TypeId | None] = [None]
		# BindingId -> local name mapping (for shadowing-aware lowering).
		self._binding_locals: dict[int, str] = {}
		# BindingId -> source name mapping (for capture reconstruction).
		self._binding_names: dict[int, str] = dict(binding_names) if binding_names else {}
		self._binding_types: dict[int, TypeId] = dict(binding_types) if binding_types else {}
		self._next_synth_binding_id: int = -1
		# Lambda lowering context (hidden fn + env).
		self._lambda_env_local: str | None = None
		self._lambda_env_ty: TypeId | None = None
		self._lambda_env_field_types: list[TypeId] | None = None
		self._lambda_capture_slots: dict[C.HCaptureKey, int] | None = None
		self._lambda_capture_name_to_slot: dict[str, int] | None = None
		self._lambda_capture_kinds: list[C.HCaptureKind] | None = None
		self._lambda_capture_ref_is_value: bool = True
		self._lambda_is_callback: bool = False
		self._lambda_counter = 0
		# Names reserved for this function (params + locals).
		self._reserved_names: set[str] = set(self.b.func.params)
		self._local_binding_ids: set[int] = set()
		# Scope-aware set of `val ^x` captures active at the current throw site.
		self._capture_scope_stack: list[list[int]] = []
		self._active_captured_locals: dict[int, _CapturedLocal] = {}
		for bid, name in self._binding_names.items():
			ty = self._binding_types.get(bid)
			if ty is None:
				continue
			local_name = self._canonical_local(bid, name)
			if local_name not in self._local_types:
				self._local_types[local_name] = ty

	def _is_destructible_type(self, ty: TypeId) -> bool:
		if ty == self._type_table.ensure_error():
			return True
		try:
			return bool(self._type_table.is_destructible(ty))
		except Exception:
			return False

	def _needs_runtime_drop(self, ty: TypeId) -> bool:
		if ty == self._unknown_type:
			return False
		td = self._type_table.get(ty)
		if td.kind is TypeKind.REF:
			return False
		if td.kind is TypeKind.DIAGNOSTICVALUE:
			return True
		if td.kind is TypeKind.INTERFACE:
			return True
		try:
			if not self._type_table.is_copy(ty):
				return True
		except Exception:
			pass
		return self._is_destructible_type(ty)

	def _push_scope(self, *, include_params: bool) -> None:
		scope: list[str] = []
		if include_params and self._param_drop_locals:
			scope.extend(self._param_drop_locals)
		self._scope_stack.append(scope)
		self._capture_scope_stack.append([])

	def _pop_scope(self) -> None:
		if not self._scope_stack:
			return
		captured = self._capture_scope_stack.pop() if self._capture_scope_stack else []
		for bid in captured:
			self._active_captured_locals.pop(int(bid), None)
		self._scope_stack.pop()

	def _register_drop_local(self, local_name: str, ty: TypeId) -> None:
		if self._suppress_drop_glue:
			return
		if not self._scope_stack:
			return
		if not self._needs_runtime_drop(ty):
			return
		scope = self._scope_stack[-1]
		if local_name in scope:
			return
		scope.append(local_name)

	def _register_captured_local(self, *, binding_id: int, local_name: str, source_name: str, capture_name: str) -> None:
		if not self._capture_scope_stack:
			return
		self._active_captured_locals[int(binding_id)] = _CapturedLocal(
			binding_id=int(binding_id),
			local_name=local_name,
			source_name=source_name,
			capture_name=capture_name,
		)
		self._capture_scope_stack[-1].append(int(binding_id))

	def _capture_to_dv(self, *, value: M.ValueId, value_ty: TypeId) -> M.ValueId:
		if value_ty == self._dv_type:
			return value
		if value_ty == self._int_type:
			dv = self.b.new_temp()
			self.b.emit(M.ConstructDV(dest=dv, dv_type_name="Int", args=[value]))
			self._local_types[dv] = self._dv_type
			return dv
		if value_ty == self._uint_type:
			as_int = self.b.new_temp()
			self.b.emit(M.IntFromUint(dest=as_int, value=value))
			self._local_types[as_int] = self._int_type
			dv = self.b.new_temp()
			self.b.emit(M.ConstructDV(dest=dv, dv_type_name="Int", args=[as_int]))
			self._local_types[dv] = self._dv_type
			return dv
		if value_ty == self._bool_type:
			dv = self.b.new_temp()
			self.b.emit(M.ConstructDV(dest=dv, dv_type_name="Bool", args=[value]))
			self._local_types[dv] = self._dv_type
			return dv
		if value_ty == self._string_type:
			dv = self.b.new_temp()
			self.b.emit(M.ConstructDV(dest=dv, dv_type_name="String", args=[value]))
			self._local_types[dv] = self._dv_type
			return dv
		if value_ty == self._float_type:
			dv = self.b.new_temp()
			self.b.emit(M.ConstructDV(dest=dv, dv_type_name="Float", args=[value]))
			self._local_types[dv] = self._dv_type
			return dv
		raise AssertionError("captured local type must lower to DiagnosticValue (checker bug)")

	def _emit_captured_locals(self, err_val: M.ValueId) -> None:
		if not self._active_captured_locals:
			return
		frame_name = function_symbol(self._current_fn_id) if self._current_fn_id is not None else self.b.func.name
		frame_val = self.b.new_temp()
		self.b.emit(M.ConstString(dest=frame_val, value=frame_name))
		for bid in sorted(self._active_captured_locals.keys()):
			cap = self._active_captured_locals[bid]
			val = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=val, local=cap.local_name))
			val_ty = self._local_types.get(cap.local_name) or self._binding_types.get(cap.binding_id)
			if val_ty is None:
				raise AssertionError("captured local type missing in MIR lowering (checker bug)")
			dv = self._capture_to_dv(value=val, value_ty=val_ty)
			key_val = self.b.new_temp()
			self.b.emit(M.ConstString(dest=key_val, value=cap.capture_name))
			self.b.emit(M.ErrorAddLocalDV(error=err_val, frame=frame_val, key=key_val, value=dv))

	def _emit_scope_drops(self, *, scope_index: int) -> None:
		if self._suppress_drop_glue:
			return
		if not self._scope_stack:
			return
		if scope_index < 0:
			scope_index = 0
		for scope in reversed(self._scope_stack[scope_index:]):
			for local in reversed(scope):
				if local in self._moved_locals:
					continue
				ty = self._local_types.get(local)
				if ty is None or not self._needs_runtime_drop(ty):
					continue
				tmp = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=tmp, local=local, ty=ty))
				self.b.emit(M.DropValue(value=tmp, ty=ty))
				self._local_types[tmp] = ty

	def synth_sig_specs(self) -> list[SynthSigSpec]:
		return list(self._synth_sig_specs)

	def hidden_lambda_specs(self) -> list[HiddenLambdaSpec]:
		return list(self._hidden_lambda_specs)

	def _canonical_local(self, binding_id: int | None, fallback: str) -> str:
		"""
		Map a binding id to a unique local name, avoiding collisions on shadowed names.

		We prefer the original name when unused; otherwise suffix with the binding id.
		"""
		if binding_id is None and fallback == "_":
			name = f"__discard{self.b.new_temp()}"
			self._reserved_names.add(name)
			return name
		if fallback.startswith("__match_binder_"):
			self._reserved_names.add(fallback)
			return fallback
		if binding_id is None:
			return fallback
		existing = self._binding_locals.get(binding_id)
		if existing:
			return existing
		if binding_id not in self._local_binding_ids and fallback in self.b.func.params:
			name = fallback
		elif fallback in self._reserved_names or fallback in self._binding_locals.values():
			name = f"{fallback}__b{binding_id}"
		else:
			name = fallback
		self._binding_locals[binding_id] = name
		self._reserved_names.add(name)
		return name

	def _capture_key_for_expr(self, expr: H.HExpr) -> C.HCaptureKey | None:
		"""
		Return a capture key for a local or local.field.field chain, else None.
		"""
		if isinstance(expr, H.HPlaceExpr):
			root = getattr(expr.base, "binding_id", None)
			if root is None:
				return None
			fields: list[str] = []
			for proj in expr.projections:
				if isinstance(proj, H.HPlaceField):
					fields.append(proj.name)
				else:
					return None
			key = C.HCaptureKey(root_local=int(root), proj=tuple(C.HCaptureProj(field=f) for f in fields))
			if self._lambda_capture_slots is not None and key in self._lambda_capture_slots:
				return key
			if int(root) in self._local_binding_ids:
				return None
			return key
		if isinstance(expr, H.HVar):
			if expr.binding_id is None:
				return None
			key = C.HCaptureKey(root_local=int(expr.binding_id), proj=())
			if self._lambda_capture_slots is not None and key in self._lambda_capture_slots:
				return key
			if int(expr.binding_id) in self._local_binding_ids:
				return None
			return key
		if isinstance(expr, H.HField):
			fields: list[str] = []
			cur = expr
			while isinstance(cur, H.HField):
				fields.append(cur.name)
				cur = cur.subject
			if not isinstance(cur, H.HVar):
				return None
			if cur.binding_id is None:
				return None
			key = C.HCaptureKey(
				root_local=int(cur.binding_id),
				proj=tuple(C.HCaptureProj(field=f) for f in reversed(fields)),
			)
			if self._lambda_capture_slots is not None and key in self._lambda_capture_slots:
				return key
			if cur.binding_id is not None and int(cur.binding_id) in self._local_binding_ids:
				return None
			return key
		return None

	def _expr_from_capture_key(self, key: C.HCaptureKey) -> H.HExpr:
		root_name = self._binding_names.get(key.root_local, f"__b{key.root_local}")
		expr: H.HExpr = H.HVar(name=root_name, binding_id=key.root_local)
		for proj in key.proj:
			expr = H.HField(subject=expr, name=proj.field)
		return expr

	def _place_from_capture_key(self, key: C.HCaptureKey) -> H.HPlaceExpr:
		root_name = self._binding_names.get(key.root_local, f"__b{key.root_local}")
		return H.HPlaceExpr(
			base=H.HVar(name=root_name, binding_id=key.root_local),
			projections=[H.HPlaceField(name=proj.field) for proj in key.proj],
			loc=Span(),
		)

	def _load_capture_from_env(self, slot: int) -> M.ValueId:
		if self._lambda_env_local is None or self._lambda_env_ty is None or self._lambda_env_field_types is None:
			raise AssertionError("capture env not initialized (lowering bug)")
		field_ty = self._lambda_env_field_types[slot]
		field_val = self._load_capture_slot_value(slot)
		kind = None
		if self._lambda_capture_kinds is not None and slot < len(self._lambda_capture_kinds):
			kind = self._lambda_capture_kinds[slot]
		if kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT) and self._lambda_capture_ref_is_value:
			inner_ty = field_ty
			td = self._type_table.get(field_ty)
			if td.kind is TypeKind.REF and td.param_types:
				inner_ty = td.param_types[0]
			dest = self.b.new_temp()
			self.b.emit(M.LoadRef(dest=dest, ptr=field_val, inner_ty=inner_ty))
			return dest
		return field_val

	def _load_capture_slot_value(self, slot: int) -> M.ValueId:
		if self._lambda_env_local is None or self._lambda_env_ty is None or self._lambda_env_field_types is None:
			raise AssertionError("capture env not initialized (lowering bug)")
		env_ptr = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=env_ptr, local=self._lambda_env_local))
		env_val = self.b.new_temp()
		self.b.emit(M.LoadRef(dest=env_val, ptr=env_ptr, inner_ty=self._lambda_env_ty))
		field_ty = self._lambda_env_field_types[slot]
		dest = self.b.new_temp()
		self.b.emit(
			M.StructGetField(
				dest=dest,
				subject=env_val,
				struct_ty=self._lambda_env_ty,
				field_index=slot,
				field_ty=field_ty,
			)
		)
		return dest

	def _fn_can_throw(self) -> bool | None:
		"""
		Best-effort can-throw flag for the current function.

		Preferred source is `can_throw_by_id` computed by the checker. We keep
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

	def _current_module_name(self) -> str:
		"""
		Best-effort current module id (string) for nominal type resolution.

		This is used for module-scoped nominal types such as structs and variants.
		"""
		if self._fn_sig is not None and getattr(self._fn_sig, "module", None):
			return str(self._fn_sig.module)
		if "::" in self.b.func.name:
			parts = self.b.func.name.split("::")
			if len(parts) >= 2:
				return parts[0]
		return "main"

	def _type_id_token(self, ty: TypeId) -> int:
		cached = self._type_id_token_cache.get(ty)
		if cached is not None:
			return cached
		key = self._type_table.type_key_string(ty)
		digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
		token = int.from_bytes(digest, byteorder="little", signed=False)
		self._type_id_token_cache[ty] = token
		return token

	# --- Expression lowering ---

	def _lower_match(self, expr: "H.HMatchExpr", *, want_value: bool) -> M.ValueId | None:
		"""
		Lower `match` by building an explicit CFG dispatch on the scrutinee tag.

		MVP notes:
		- `match` is an expression in the language, but it may appear in statement
		  position as an ExprStmt. In statement position, arm result expressions
		  (if present) are evaluated and discarded, and arms may omit a result.
		- Pattern forms:
		  - `Ctor` (bare) is allowed only for zero-field constructors.
		  - `Ctor()` matches ctor tag and ignores payload.
		  - `Ctor(a,b,...)` binds fields positionally (exact arity).
		  - `Ctor(x=a,...)` binds a subset by name (normalized by the typed checker).
		"""
		# Evaluate scrutinee once in the current block; it dominates the dispatch/arms.
		scrut_val = self.lower_expr(expr.scrutinee)
		scrut_ref_val: M.ValueId | None = None
		scrut_ty = self._infer_expr_type(expr.scrutinee)
		scrut_is_ref = False
		scrut_ref_mut = False
		if scrut_ty is not None:
			scrut_def = self._type_table.get(scrut_ty)
			if scrut_def.kind is TypeKind.REF and scrut_def.param_types:
				scrut_is_ref = True
				scrut_ref_mut = bool(scrut_def.ref_mut)
				scrut_ref_val = scrut_val
				scrut_ty = scrut_def.param_types[0]
		if scrut_ty is None or self._type_table.get(scrut_ty).kind is not TypeKind.VARIANT:
			raise AssertionError("match scrutinee must have a concrete variant type (checker bug)")
		inst = self._type_table.get_variant_instance(scrut_ty)
		if inst is None:
			raise AssertionError("match scrutinee variant instance missing (type table bug)")
		scrut_source_local: str | None = None
		if isinstance(expr.scrutinee, H.HVar):
			scrut_source_local = self._canonical_local(getattr(expr.scrutinee, "binding_id", None), expr.scrutinee.name)

		# Optional hidden local for the match result when used as a value.
		result_local: str | None = None
		if want_value:
			result_local = f"__match_expr_tmp{self.b.new_temp()}"
			self.b.ensure_local(result_local)
			want_ty = self._current_expected_type() or self._infer_expr_type(expr)
			if want_ty is not None:
				self._local_types[result_local] = want_ty
			else:
				arm_ty: TypeId | None = None
				for arm in expr.arms:
					if arm.result is None:
						continue
					arm_ty = self._infer_expr_type(arm.result)
					if arm_ty is not None:
						break
				self._local_types[result_local] = arm_ty if arm_ty is not None else self._unknown_type

		dispatch_block = self.b.new_block("match_dispatch")
		join_block = self.b.new_block("match_join")
		arm_blocks: list[tuple[H.HMatchArm, M.BasicBlock]] = [
			(arm, self.b.new_block(f"match_arm_{idx}")) for idx, arm in enumerate(expr.arms)
		]

		# Enter dispatch.
		self.b.set_terminator(M.Goto(target=dispatch_block.name))

		# Dispatch: tag = VariantTag(scrutinee); chain IfTerminator tests in source order.
		self.b.set_block(dispatch_block)
		tag_tmp = self.b.new_temp()
		if scrut_is_ref:
			if scrut_ref_val is None:
				raise AssertionError("match ref scrutinee missing reference value (lowering bug)")
			self.b.emit(M.VariantTagRef(dest=tag_tmp, variant_ref=scrut_ref_val, variant_ty=scrut_ty))
		else:
			self.b.emit(M.VariantTag(dest=tag_tmp, variant=scrut_val, variant_ty=scrut_ty))
		self._local_types[tag_tmp] = self._uint_type

		# Find default arm (if any) and build dispatch chain for ctor arms.
		default_block: M.BasicBlock | None = None
		event_arms: list[tuple[H.HMatchArm, M.BasicBlock]] = []
		for arm, bb in arm_blocks:
			if arm.ctor is None:
				default_block = bb
			else:
				event_arms.append((arm, bb))

		current_block = dispatch_block
		for arm, bb in event_arms:
			assert arm.ctor is not None
			arm_def = inst.arms_by_name.get(arm.ctor)
			if arm_def is None:
				raise AssertionError("unknown constructor in match reached MIR lowering (checker bug)")
			self.b.set_block(current_block)
			tag_const = self.b.new_temp()
			self.b.emit(M.ConstUint(dest=tag_const, value=int(arm_def.tag)))
			self._local_types[tag_const] = self._uint_type
			cmp_tmp = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=cmp_tmp, op=M.BinaryOp.EQ, left=tag_tmp, right=tag_const))
			else_block = self.b.new_block("match_dispatch_next")
			self.b.set_terminator(M.IfTerminator(cond=cmp_tmp, then_target=bb.name, else_target=else_block.name))
			current_block = else_block

		# Final else path: jump to default arm (if present), otherwise unreachable.
		self.b.set_block(current_block)
		if default_block is not None:
			self.b.set_terminator(M.Goto(target=default_block.name))
		else:
			self.b.set_terminator(M.Unreachable())

		# Lower each arm block: bind pattern fields, lower statements, store optional
		# result, jump to join.
		join_reachable = False
		for arm, bb in arm_blocks:
			self.b.set_block(bb)
			self._push_scope(include_params=False)
			arm_scrut_local: str | None = None
			arm_scrut_ptr: M.ValueId | None = None
			arm_drop_locals: list[str] = []
			try:
				def _ensure_arm_scrut_ptr() -> None:
					nonlocal arm_scrut_local, arm_scrut_ptr
					if arm_scrut_ptr is not None:
						return
					arm_scrut_local = f"__match_scrut_tmp{self.b.new_temp()}"
					self.b.ensure_local(arm_scrut_local)
					self._local_types[arm_scrut_local] = scrut_ty
					if scrut_source_local is not None and not self._type_table.is_copy(scrut_ty):
						arm_scrut_moved_in = self.b.new_temp()
						self.b.emit(M.MoveOut(dest=arm_scrut_moved_in, local=scrut_source_local, ty=scrut_ty))
						self._local_types[arm_scrut_moved_in] = scrut_ty
						self.b.emit(M.StoreLocal(local=arm_scrut_local, value=arm_scrut_moved_in))
					else:
						self.b.emit(M.StoreLocal(local=arm_scrut_local, value=scrut_val))
					arm_scrut_ptr = self.b.new_temp()
					self.b.emit(M.AddrOfLocal(dest=arm_scrut_ptr, local=arm_scrut_local, is_mut=True))
					self._local_types[arm_scrut_ptr] = self._type_table.ensure_ref_mut(scrut_ty)

				if not want_value and not scrut_is_ref:
					_ensure_arm_scrut_ptr()

				if arm.ctor is not None:
					arm_def = inst.arms_by_name[arm.ctor]
					form = getattr(arm, "pattern_arg_form", "positional")
					if form == "bare":
						if arm_def.field_types:
							raise AssertionError(
								"bare ctor pattern for non-zero-field ctor reached MIR lowering (checker bug)"
							)
					elif form == "paren":
						if arm.binders:
							raise AssertionError("Ctor() pattern must not bind fields (checker bug)")
					else:
						# Typed checker is the single source of truth for match pattern
						# normalization. By the time we reach MIR lowering, any constructor
						# pattern that binds payload fields must already carry a normalized
						# binder→field-index mapping.
						field_indices = list(getattr(arm, "binder_field_indices", []) or [])
						if len(field_indices) != len(arm.binders):
							raise AssertionError("match binder field-index mapping missing (checker bug)")
						if (not scrut_is_ref) and any(
							not self._type_table.is_copy(arm_def.field_types[int(fidx)])
							for fidx in field_indices
						):
							_ensure_arm_scrut_ptr()

						for bname, fidx in zip(arm.binders, field_indices):
							if fidx < 0 or fidx >= len(arm_def.field_types):
								raise AssertionError("match binder field index out of range (checker bug)")
							bty = arm_def.field_types[fidx]
							binder_ty = bty
							if scrut_is_ref:
								binder_ty = self._type_table.ensure_ref_mut(bty) if scrut_ref_mut else self._type_table.ensure_ref(bty)
							field_val = self.b.new_temp()
							if scrut_is_ref:
								if scrut_ref_val is None:
									raise AssertionError("match ref scrutinee missing reference value (lowering bug)")
								self.b.emit(
									M.VariantGetFieldAddr(
										dest=field_val,
										variant_ref=scrut_ref_val,
										variant_ty=scrut_ty,
										ctor=arm.ctor,
										field_index=int(fidx),
										field_ty=bty,
									)
								)
								self._local_types[field_val] = binder_ty
								self.b.ensure_local(bname)
								self._local_types[bname] = binder_ty
								self.b.emit(M.StoreLocal(local=bname, value=field_val))
							else:
								if arm_scrut_ptr is not None and not self._type_table.is_copy(bty):
									self.b.emit(
										M.VariantGetFieldAddr(
											dest=field_val,
											variant_ref=arm_scrut_ptr,
											variant_ty=scrut_ty,
											ctor=arm.ctor,
											field_index=int(fidx),
											field_ty=bty,
										)
									)
									field_moved = self.b.new_temp()
									self.b.emit(M.LoadRef(dest=field_moved, ptr=field_val, inner_ty=bty))
									self._local_types[field_moved] = bty
									field_zero = self.b.new_temp()
									self.b.emit(M.ZeroValue(dest=field_zero, ty=bty))
									self._local_types[field_zero] = bty
									self.b.emit(M.StoreRef(ptr=field_val, value=field_zero, inner_ty=bty))
								else:
									self.b.emit(
										M.VariantGetField(
											dest=field_val,
											variant=scrut_val,
											variant_ty=scrut_ty,
											ctor=arm.ctor,
											field_index=int(fidx),
											field_ty=bty,
										)
									)
									self._local_types[field_val] = bty
									field_moved = field_val
								self.b.ensure_local(bname)
								self._local_types[bname] = binder_ty
								binder_def = self._type_table.get(binder_ty)
								if binder_def.kind is not TypeKind.REF and self._needs_runtime_drop(binder_ty):
									arm_drop_locals.append(bname)
									self._register_drop_local(bname, binder_ty)
								self.b.emit(M.StoreLocal(local=bname, value=field_moved))

				# Consume and drop by-value scrutinee before arm body so cleanup runs
				# even when the arm terminates early (e.g., return/throw).
				if arm_scrut_local is not None:
					arm_scrut_moved_pre = self.b.new_temp()
					self.b.emit(M.MoveOut(dest=arm_scrut_moved_pre, local=arm_scrut_local, ty=scrut_ty))
					self._local_types[arm_scrut_moved_pre] = scrut_ty
					self.b.emit(M.DropValue(value=arm_scrut_moved_pre, ty=scrut_ty))
					arm_scrut_local = None

				# Lower the arm body statements regardless of pattern kind.
				self.lower_block(arm.block)

				# In statement position, still evaluate any arm result expression and
				# discard its value (side effects must run).
				if not want_value and arm.result is not None:
					if self.b.block.terminator is None:
						self.lower_stmt(H.HExprStmt(expr=arm.result))

				# If this match is used as a value, store the arm's resulting expression.
				did_store_result = False
				if want_value and result_local is not None:
					if arm.result is None:
						if self.b.block.terminator is None:
							raise AssertionError(
								"value-producing match arm must yield a value or terminate (checker bug)"
							)
					else:
						# If an arm declares a result expression, its statement block must not
						# diverge; we must be able to evaluate and store the result before
						# branching to the match join.
						if self.b.block.terminator is not None:
							raise AssertionError(
								"value-producing match arm has a result expression but its block terminates (checker bug)"
							)
						if self._local_types.get(result_local) is self._unknown_type:
							arm_ty = self._infer_expr_type(arm.result)
							if arm_ty is not None:
								self._local_types[result_local] = arm_ty
						val = self.lower_expr(arm.result, expected_type=self._local_types.get(result_local))
						if self._local_types.get(result_local) is self._unknown_type:
							val_ty = self._local_types.get(val)
							if val_ty is not None:
								self._local_types[result_local] = val_ty
						self.b.emit(M.StoreLocal(local=result_local, value=val))
						did_store_result = True

				if arm_scrut_local is not None and self.b.block.terminator is None:
					arm_scrut_moved = self.b.new_temp()
					self.b.emit(M.MoveOut(dest=arm_scrut_moved, local=arm_scrut_local, ty=scrut_ty))
					self._local_types[arm_scrut_moved] = scrut_ty
					self.b.emit(M.DropValue(value=arm_scrut_moved, ty=scrut_ty))

				if self.b.block.terminator is None:
					for local in reversed(arm_drop_locals):
						if local in self._moved_locals:
							continue
						local_ty = self._local_types.get(local)
						if local_ty is None or not self._needs_runtime_drop(local_ty):
							continue
						tmp = self.b.new_temp()
						self.b.emit(M.MoveOut(dest=tmp, local=local, ty=local_ty))
						self._local_types[tmp] = local_ty
						self.b.emit(M.DropValue(value=tmp, ty=local_ty))
					if want_value and result_local is not None and not did_store_result:
						raise AssertionError(
							"value-producing match arm falls through without storing result (lowering bug)"
						)
					self.b.set_terminator(M.Goto(target=join_block.name))
					join_reachable = True
			finally:
				self._pop_scope()

		# Defensive invariant: every arm block must end in a terminator. This catches
		# structural lowering bugs where an arm block is accidentally skipped.
		for _arm, _bb in arm_blocks:
			if _bb.terminator is None:
				raise AssertionError("match arm missing terminator after lowering (lowering bug)")

		# Join point.
		self.b.set_block(join_block)
		if not want_value:
			if not join_reachable and self.b.block.terminator is None:
				self.b.set_terminator(M.Unreachable())
			return None
		assert result_local is not None
		dest = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=dest, local=result_local))
		return dest

	def _lower_expr_raw(self, expr: H.HExpr, *, expected_type: TypeId | None = None) -> M.ValueId:
		"""
		Entry point: lower a single HIR expression to a MIR ValueId.

		Dispatches to a private _visit_expr_* helper. Public stage API: callers
		should only invoke lower_expr/stmt/block; helpers stay private.
		"""
		self._expected_type_stack.append(expected_type)
		try:
			method = getattr(self, f"_visit_expr_{type(expr).__name__}", None)
			if method is None:
				raise NotImplementedError(f"No MIR lowering for expr {type(expr).__name__}")
			return method(expr)
		finally:
			self._expected_type_stack.pop()

	def lower_expr(self, expr: H.HExpr, *, expected_type: TypeId | None = None) -> M.ValueId:
		prev_span = self.b.current_span
		expr_span = Span.from_loc(getattr(expr, "loc", None))
		if expr_span != Span() and (prev_span is None or prev_span == Span()):
			self.b.current_span = expr_span
		try:
			if getattr(expr, "node_id", None) in self._iface_coercions:
				target_iface = self._iface_coercions[expr.node_id]
				value = self._lower_expr_raw(expr, expected_type=expected_type)
				value_ty = self._infer_expr_type(expr)
				if value_ty is None:
					raise AssertionError("interface coercion missing source type (checker bug)")
				if self._type_table.get(value_ty).kind is TypeKind.INTERFACE:
					src_inst = self._type_table.get_interface_instance(value_ty)
					src_base = src_inst.base_id if src_inst is not None else value_ty
					tgt_inst = self._type_table.get_interface_instance(target_iface)
					tgt_base = tgt_inst.base_id if tgt_inst is not None else target_iface
					offsets = self._type_table.interface_segment_offsets(src_base)
					if tgt_base not in offsets:
						raise AssertionError("interface upcast target not in linearization (checker bug)")
					dest = self.b.new_temp()
					self.b.emit(M.IfaceUpcast(dest=dest, iface=value, slot_offset=offsets[tgt_base]))
					self._local_types[dest] = target_iface
					return dest
				dest = self.b.new_temp()
				self.b.emit(
					M.ConstructIfaceValue(
						dest=dest,
						iface_ty=target_iface,
						value=value,
						value_ty=value_ty,
					)
				)
				self._local_types[dest] = target_iface
				return dest
			return self._lower_expr_raw(expr, expected_type=expected_type)
		finally:
			self.b.current_span = prev_span

	def _current_expected_type(self) -> TypeId | None:
		"""Return the current expected type hint for expression lowering."""
		return self._expected_type_stack[-1] if self._expected_type_stack else None

	def _visit_expr_HLiteralInt(self, expr: H.HLiteralInt) -> M.ValueId:
		dest = self.b.new_temp()
		expected = self._current_expected_type()
		if expected == self._byte_type:
			self.b.emit(M.ConstByte(dest=dest, value=expr.value))
			self._local_types[dest] = self._byte_type
			return dest
		if expected == self._uint64_type:
			self.b.emit(M.ConstUint64(dest=dest, value=expr.value))
			self._local_types[dest] = self._uint64_type
			return dest
		if expected == self._uint_type:
			self.b.emit(M.ConstUint(dest=dest, value=expr.value))
			self._local_types[dest] = self._uint_type
			return dest
		if expected == self._int_type:
			self.b.emit(M.ConstInt(dest=dest, value=expr.value))
			self._local_types[dest] = self._int_type
			return dest
		ty = self._infer_expr_type(expr)
		if ty == self._byte_type:
			self.b.emit(M.ConstByte(dest=dest, value=expr.value))
			self._local_types[dest] = self._byte_type
		elif ty == self._uint64_type:
			self.b.emit(M.ConstUint64(dest=dest, value=expr.value))
			self._local_types[dest] = self._uint64_type
		elif ty == self._uint_type:
			self.b.emit(M.ConstUint(dest=dest, value=expr.value))
			self._local_types[dest] = self._uint_type
		else:
			self.b.emit(M.ConstInt(dest=dest, value=expr.value))
			self._local_types[dest] = self._int_type
		return dest

	def _visit_expr_HLiteralFloat(self, expr: H.HLiteralFloat) -> M.ValueId:
		"""
		Lower a Float literal.

		Float is a surface type in lang v1 and maps to IEEE-754 `double` in LLVM.
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

	def _visit_expr_HFnPtrConst(self, expr: H.HFnPtrConst) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.FnPtrConst(dest=dest, fn_ref=expr.fn_ref, call_sig=expr.call_sig))
		return dest

	def _visit_expr_HCast(self, expr: H.HCast) -> M.ValueId:
		allowed_scalar_names = ("Int", "Uint", "Uint64", "Byte", "Bool")

		def _format_type_expr(te: object | None) -> str:
			if te is None:
				return "<unknown>"
			name = getattr(te, "name", None)
			args = list(getattr(te, "args", []) or [])
			module_id = getattr(te, "module_id", None)
			if hasattr(te, "can_throw") and callable(getattr(te, "can_throw")):
				can_throw = bool(te.can_throw())
			else:
				raw = getattr(te, "fn_throws", True)
				if raw is None:
					raw = True
				can_throw = bool(raw)
			if isinstance(name, str) and name == "fn" and args:
				params = args[:-1]
				ret = args[-1]
				params_s = ", ".join(_format_type_expr(a) for a in params)
				ret_s = _format_type_expr(ret)
				if can_throw:
					return f"Fn({params_s}) -> {ret_s}"
				return f"Fn({params_s}) nothrow -> {ret_s}"
			base = name if isinstance(name, str) else "<type>"
			if isinstance(module_id, str) and module_id:
				base = f"{module_id}.{base}"
			if args:
				base = f"{base}<{', '.join(_format_type_expr(a) for a in args)}>"
			return base

		def _is_scalar_cast_type(ty: TypeId) -> bool:
			td = self._type_table.get(ty)
			return td.kind is TypeKind.SCALAR and td.name in allowed_scalar_names

		def _resolve_scalar_target() -> TypeId | None:
			te = getattr(expr, "target_type_expr", None)
			if te is None:
				return None
			name = getattr(te, "name", None)
			args = list(getattr(te, "args", []) or [])
			if not isinstance(name, str) or name not in allowed_scalar_names or args:
				return None
			module_id = getattr(te, "module_id", None) or self._current_module_name()
			try:
				tid = resolve_opaque_type(te, self._type_table, module_id=module_id)
			except Exception:
				return None
			td = self._type_table.get(tid)
			if td.kind is TypeKind.SCALAR and td.name in allowed_scalar_names:
				return tid
			return None

		if drift_debug.enabled("stage2"):
			import sys
			target_desc = _format_type_expr(getattr(expr, "target_type_expr", None))
			print(f"[drift:debug] HCast node={expr.node_id} target={target_desc}", file=sys.stderr)
		target_ty = self._expr_types.get(expr.node_id) if self._typed_mode != "none" else None
		if self._typed_mode == "strict" and target_ty is None:
			target = _format_type_expr(getattr(expr, "target_type_expr", None))
			raise AssertionError(
				"internal compiler error: HCast must be eliminated during typecheck "
				f"(node_id={expr.node_id}, target={target}); "
				"rewrite to HFnPtrConst or emit a diagnostic"
			)
		resolved_target = _resolve_scalar_target()
		if resolved_target is not None:
			if target_ty is None:
				target_ty = resolved_target
			else:
				target_def = self._type_table.get(target_ty)
				resolved_def = self._type_table.get(resolved_target)
				if resolved_def.kind is TypeKind.SCALAR:
					if target_def.kind is not TypeKind.SCALAR:
						target_ty = resolved_target
					elif target_def.name != resolved_def.name:
						target_ty = resolved_target
		if target_ty is None:
			if self._typed_mode == "strict":
				target = _format_type_expr(getattr(expr, "target_type_expr", None))
				raise AssertionError(
					"internal compiler error: HCast must be eliminated during typecheck "
					f"(node_id={expr.node_id}, target={target}); "
					"rewrite to HFnPtrConst or emit a diagnostic"
				)
			target_ty = resolved_target
			if target_ty is None:
				target = _format_type_expr(getattr(expr, "target_type_expr", None))
				return self._recover_unknown_value(
					f"stage2: unable to resolve scalar cast target (node_id={expr.node_id}, target={target})"
				)
		src_ty = self._expr_types.get(expr.value.node_id) if self._typed_mode != "none" else None
		if src_ty is None:
			src_ty = self._infer_expr_type(expr.value)
		if src_ty is None:
			return self._recover_unknown_value(
				f"stage2: HCast missing source type (node_id={expr.node_id}); typecheck required"
			)
		if isinstance(expr.value, H.HLiteralInt):
			td = self._type_table.get(target_ty)
			if td.kind is TypeKind.SCALAR and td.name in ("Int", "Uint", "Byte"):
				if td.name == "Byte":
					if drift_debug.enabled("stage2"):
						import sys
						print(f"[drift:debug] cast<Byte> literal {expr.value.value}", file=sys.stderr)
					dest = self.b.new_temp()
					self.b.emit(M.ConstByte(dest=dest, value=expr.value.value))
					self._local_types[dest] = self._byte_type
					return dest
				val = self.lower_expr(expr.value, expected_type=target_ty)
				return val
		else:
			td = self._type_table.get(target_ty)
			if td.kind is TypeKind.SCALAR and td.name == "Byte" and drift_debug.enabled("stage2"):
				import sys
				print(f"[drift:debug] cast<Byte> nonliteral value={type(expr.value).__name__}", file=sys.stderr)
		val = self.lower_expr(expr.value)
		val_ty = self._local_types.get(val)
		if drift_debug.enabled("stage2"):
			import sys
			print(f"[drift:debug] HCast node={expr.node_id} val_ty={val_ty} target_ty={target_ty}", file=sys.stderr)
		if val_ty is not None and val_ty != target_ty:
			if _is_scalar_cast_type(val_ty) and _is_scalar_cast_type(target_ty):
				if drift_debug.enabled("stage2"):
					import sys
					print(f"[drift:debug] HCast emit CastScalar node={expr.node_id}", file=sys.stderr)
				dest = self.b.new_temp()
				self.b.emit(M.CastScalar(dest=dest, value=val, src_ty=val_ty, dst_ty=target_ty))
				self._local_types[dest] = target_ty
				return dest
		if src_ty == target_ty:
			return val
		if not _is_scalar_cast_type(src_ty) or not _is_scalar_cast_type(target_ty):
			return self._recover_unknown_value(
				f"stage2: unsupported scalar cast (node_id={expr.node_id}, src={src_ty}, dst={target_ty})"
			)
		dest = self.b.new_temp()
		self.b.emit(M.CastScalar(dest=dest, value=val, src_ty=src_ty, dst_ty=target_ty))
		self._local_types[dest] = target_ty
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
		# Compile-time constants are lowered to immediate MIR constants (no runtime
		# storage). Const symbols are represented as fully-qualified names:
		#   "<module_id>::<NAME>"
		#
		# For older unit tests that bypass the typed checker, we also accept an
		# unqualified name and resolve it within the current module.
		sym = expr.name
		candidates: list[str] = []
		if expr.module_id is not None:
			candidates.append(f"{expr.module_id}::{sym}")
		else:
			candidates.append(sym)
			if "::" not in sym:
				fn_name = getattr(self.b.func, "name", "")
				mod = fn_name.split("::")[0] if "::" in fn_name else "main"
				candidates.append(f"{mod}::{sym}")
		for cand in candidates:
			cv = self._type_table.lookup_const(cand)
			if cv is None:
				continue
			ty_id, val = cv
			dest = self.b.new_temp()
			if ty_id == self._int_type:
				self.b.emit(M.ConstInt(dest=dest, value=int(val)))
				return dest
			if ty_id == self._uint_type:
				self.b.emit(M.ConstUint(dest=dest, value=int(val)))
				return dest
			if ty_id == self._uint64_type:
				self.b.emit(M.ConstUint64(dest=dest, value=int(val)))
				return dest
			if ty_id == self._bool_type:
				self.b.emit(M.ConstBool(dest=dest, value=bool(val)))
				return dest
			if ty_id == self._string_type:
				self.b.emit(M.ConstString(dest=dest, value=str(val)))
				return dest
			if ty_id == self._float_type:
				self.b.emit(M.ConstFloat(dest=dest, value=float(val)))
				return dest
			raise AssertionError("unsupported const type reached MIR lowering (checker/package bug)")
		if self._typed_mode == "strict" and expr.binding_id is None and expr.module_id is None:
			raise AssertionError("typed_mode strict: missing binding_id for local read (checker bug)")
		if self._lambda_capture_slots is not None:
			key = self._capture_key_for_expr(expr)
			if key is not None and key in self._lambda_capture_slots:
				slot = self._lambda_capture_slots[key]
				kind = None
				if self._lambda_capture_kinds is not None and slot < len(self._lambda_capture_kinds):
					kind = self._lambda_capture_kinds[slot]
				if kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT):
					field_ty = self._lambda_env_field_types[slot] if self._lambda_env_field_types is not None else None
					ptr_val = self._load_capture_slot_value(slot)
					inner_ty = field_ty or self._infer_expr_type(expr) or unknown
					if field_ty is not None:
						td = self._type_table.get(field_ty)
						if td.kind is TypeKind.REF and td.param_types:
							inner_ty = td.param_types[0]
					dest = self.b.new_temp()
					self.b.emit(M.LoadRef(dest=dest, ptr=ptr_val, inner_ty=inner_ty))
					return dest
				return self._load_capture_from_env(slot)
		if expr.binding_id is None and self._lambda_capture_name_to_slot is not None:
			slot = self._lambda_capture_name_to_slot.get(expr.name)
			if slot is not None:
				return self._load_capture_from_env(slot)
		local_name = self._canonical_local(getattr(expr, "binding_id", None), expr.name)
		self.b.ensure_local(local_name)
		# Treat String.EMPTY as a builtin zero-length string literal.
		if expr.name == "String.EMPTY":
			return self._string_empty_const
		dest = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=dest, local=local_name))
		local_ty = self._local_types.get(local_name)
		if local_ty is not None:
			self._local_types[dest] = local_ty
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
		if not (hasattr(H, "HPlaceExpr") and isinstance(expr.subject, getattr(H, "HPlaceExpr"))):
			if expr.is_mut:
				raise AssertionError("non-canonical &mut borrow operand reached MIR lowering (normalize/typechecker bug)")
			inner_ty = self._infer_expr_type(expr.subject)
			if inner_ty is None:
				raise AssertionError("borrow operand type unknown in MIR lowering (checker bug)")
			tmp_local = f"__borrow_tmp{self.b.new_temp()}"
			self.b.ensure_local(tmp_local)
			self._local_types[tmp_local] = inner_ty
			val = self.lower_expr(expr.subject)
			self.b.emit(M.StoreLocal(local=tmp_local, value=val))
			ptr = self.b.new_temp()
			self.b.emit(M.AddrOfLocal(dest=ptr, local=tmp_local, is_mut=False))
			return ptr
		ptr, _inner = self._lower_addr_of_place(expr.subject, is_mut=expr.is_mut)
		return ptr

	def _visit_expr_HMove(self, expr: H.HMove) -> M.ValueId:
		"""
		Lower explicit `move <place>` as:
		  - read the current value, and
		  - reset the source storage to a well-defined zero value.

		Why reset the source?
		- It makes "moved-from" storage safe for future destructor/RAII work.
		- It avoids allocations when moving `String` (zero-initialized `%DriftString`
		  is a valid empty string representation in the runtime ABI).

		MVP restriction:
		- Only plain bindings (locals/params) are movable via `move` in this phase.
		  Moving out of projections (fields/indexes) would require partial-move
		  semantics and more precise liveness tracking.
		"""
		if not (hasattr(H, "HPlaceExpr") and isinstance(expr.subject, getattr(H, "HPlaceExpr"))):
			raise AssertionError("non-canonical move operand reached MIR lowering (normalize/typechecker bug)")
		if expr.subject.projections:
			raise AssertionError("move of projected place reached MIR lowering (checker bug)")
		if self._lambda_is_callback and self._lambda_capture_slots is not None:
			key = self._capture_key_for_expr(expr.subject)
			if key is not None:
				moved = self._move_from_callback_capture_slot(key)
				if moved is not None:
					return moved
		subj_name = self._canonical_local(getattr(expr.subject.base, "binding_id", None), expr.subject.base.name)
		self.b.ensure_local(subj_name)
		inner_ty = self._infer_expr_type(expr.subject.base)
		if inner_ty is None:
			raise AssertionError("move operand type unknown in MIR lowering (checker bug)")
		moved_val = self.b.new_temp()
		self.b.emit(M.MoveOut(dest=moved_val, local=subj_name, ty=inner_ty))
		self._local_types[moved_val] = inner_ty
		self._moved_locals.add(subj_name)
		return moved_val

	def _move_from_callback_capture_slot(self, key: C.HCaptureKey) -> M.ValueId | None:
		if not self._lambda_is_callback or self._lambda_capture_slots is None or self._lambda_capture_kinds is None:
			return None
		slot = self._lambda_capture_slots.get(key)
		if slot is None or slot >= len(self._lambda_capture_kinds):
			return None
		if self._lambda_capture_kinds[slot] is not C.HCaptureKind.MOVE:
			return None
		place = self._place_from_capture_key(key)
		ptr, inner_ty = self._lower_addr_of_place(place, is_mut=True)
		loaded = self.b.new_temp()
		self.b.emit(M.LoadRef(dest=loaded, ptr=ptr, inner_ty=inner_ty))
		self._local_types[loaded] = inner_ty
		tmp_local = f"__cap_move_{self.b.new_temp()}"
		self.b.ensure_local(tmp_local)
		self._local_types[tmp_local] = inner_ty
		self.b.emit(M.StoreLocal(local=tmp_local, value=loaded))
		moved_val = self.b.new_temp()
		self.b.emit(M.MoveOut(dest=moved_val, local=tmp_local, ty=inner_ty))
		self._local_types[moved_val] = inner_ty
		self._moved_locals.add(tmp_local)
		zero_val = self.b.new_temp()
		self.b.emit(M.ZeroValue(dest=zero_val, ty=inner_ty))
		self._local_types[zero_val] = inner_ty
		self.b.emit(M.StoreRef(ptr=ptr, value=zero_val, inner_ty=inner_ty))
		return moved_val

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
		left_expr = expr.left
		right_expr = expr.right
		if expr.op in (H.BinaryOp.AND, H.BinaryOp.OR):
			left = self.lower_expr(left_expr)
			temp_local = f"__logic_tmp{self.b.new_temp()}"
			self.b.ensure_local(temp_local)
			self._local_types[temp_local] = self._bool_type
			rhs_block = self.b.new_block("logic_rhs")
			short_block = self.b.new_block("logic_short")
			join_block = self.b.new_block("logic_join")
			if expr.op is H.BinaryOp.AND:
				self.b.set_terminator(M.IfTerminator(cond=left, then_target=rhs_block.name, else_target=short_block.name))
			else:
				self.b.set_terminator(M.IfTerminator(cond=left, then_target=short_block.name, else_target=rhs_block.name))
			self.b.set_block(short_block)
			short_val = self.b.new_temp()
			self.b.emit(M.ConstBool(dest=short_val, value=(expr.op is H.BinaryOp.OR)))
			self.b.emit(M.StoreLocal(local=temp_local, value=short_val))
			if self.b.block.terminator is None:
				self.b.set_terminator(M.Goto(target=join_block.name))
			self.b.set_block(rhs_block)
			right = self.lower_expr(right_expr)
			self.b.emit(M.StoreLocal(local=temp_local, value=right))
			if self.b.block.terminator is None:
				self.b.set_terminator(M.Goto(target=join_block.name))
			self.b.set_block(join_block)
			dest = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=dest, local=temp_local))
			return dest
		if isinstance(left_expr, H.HLiteralInt) and not isinstance(right_expr, H.HLiteralInt):
			right_ty = self._infer_expr_type(right_expr)
			left = self.lower_expr(left_expr, expected_type=right_ty) if right_ty is not None else self.lower_expr(left_expr)
			right = self.lower_expr(right_expr)
		elif isinstance(right_expr, H.HLiteralInt) and not isinstance(left_expr, H.HLiteralInt):
			left_ty = self._infer_expr_type(left_expr)
			left = self.lower_expr(left_expr)
			right = self.lower_expr(right_expr, expected_type=left_ty) if left_ty is not None else self.lower_expr(right_expr)
		else:
			left = self.lower_expr(left_expr)
			right = self.lower_expr(right_expr)
		dest = self.b.new_temp()
		# String-aware lowering: redirect +/== on strings to dedicated MIR ops.
		left_ty = self._infer_expr_type(left_expr)
		right_ty = self._infer_expr_type(right_expr)
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
		if self._lambda_capture_slots is not None:
			key = self._capture_key_for_expr(expr)
			if key is not None and key in self._lambda_capture_slots:
				return self._load_capture_from_env(self._lambda_capture_slots[key])
		subject = self.lower_expr(expr.subject)
		subj_ty = self._infer_expr_type(expr.subject)
		if subj_ty is None:
			raise AssertionError("field subject type unknown in MIR lowering (checker bug)")
		sub_def = self._type_table.get(subj_ty)
		if sub_def.kind is TypeKind.REF and sub_def.param_types:
			inner_ty = sub_def.param_types[0]
			loaded = self.b.new_temp()
			self.b.emit(M.LoadRef(dest=loaded, ptr=subject, inner_ty=inner_ty))
			subject = loaded
			subj_ty = inner_ty
			sub_def = self._type_table.get(subj_ty)
		if expr.name in ("len", "cap", "capacity", "gen") and sub_def.kind is TypeKind.STRUCT:
			info = self._type_table.struct_field(subj_ty, expr.name)
			if info is not None:
				field_idx, field_ty = info
				dest = self.b.new_temp()
				self.b.emit(M.StructGetField(dest=dest, subject=subject, struct_ty=subj_ty, field_index=field_idx, field_ty=field_ty))
				return dest
		# Array/String len/capacity sugar: field access produces ArrayLen/ArrayCap/StringLen.
		if expr.name == "len":
			dest = self.b.new_temp()
			self._lower_len(subj_ty, subject, dest)
			return dest
		if expr.name in ("cap", "capacity"):
			dest = self.b.new_temp()
			self.b.emit(M.ArrayCap(dest=dest, array=subject))
			return dest
		if expr.name == "gen":
			dest = self.b.new_temp()
			self.b.emit(M.ArrayGen(dest=dest, array=subject))
			return dest
		if expr.name == "attrs":
			raise NotImplementedError("attrs view must be indexed: Error.attrs[\"key\"]")
		if expr.name == "captures":
			raise NotImplementedError(
				"captures view must be indexed: Error.captures[\"frame\"][\"key\"]"
			)
		# Struct field access.
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
		if hasattr(H, "HPlaceExpr") and isinstance(expr.subject, getattr(H, "HPlaceExpr")):
			for idx, proj in enumerate(expr.subject.projections):
				if not isinstance(proj, H.HPlaceField):
					continue
				if proj.name == "attrs":
					if idx + 1 >= len(expr.subject.projections) or not isinstance(expr.subject.projections[idx + 1], H.HPlaceIndex):
						continue
					if idx + 2 != len(expr.subject.projections):
						continue
					err_val = self.lower_expr(expr.subject.base)
					err_ty = self._infer_expr_type(expr.subject.base)
					if err_ty is not None:
						err_def = self._type_table.get(err_ty)
						if err_def.kind is TypeKind.REF and err_def.param_types:
							inner_ty = err_def.param_types[0]
							tmp = self.b.new_temp()
							self.b.emit(M.LoadRef(dest=tmp, ptr=err_val, inner_ty=inner_ty))
							err_val = tmp
					key_val = self.lower_expr(expr.index)
					dest = self.b.new_temp()
					self.b.emit(M.ErrorAttrsGetDV(dest=dest, error=err_val, key=key_val))
					self._local_types[dest] = self._dv_type
					return dest
				if proj.name == "captures":
					if idx + 1 >= len(expr.subject.projections) or not isinstance(expr.subject.projections[idx + 1], H.HPlaceIndex):
						continue
					if idx + 2 != len(expr.subject.projections):
						continue
					err_val = self.lower_expr(expr.subject.base)
					err_ty = self._infer_expr_type(expr.subject.base)
					if err_ty is not None:
						err_def = self._type_table.get(err_ty)
						if err_def.kind is TypeKind.REF and err_def.param_types:
							inner_ty = err_def.param_types[0]
							tmp = self.b.new_temp()
							self.b.emit(M.LoadRef(dest=tmp, ptr=err_val, inner_ty=inner_ty))
							err_val = tmp
					frame_val = self.lower_expr(expr.subject.projections[idx + 1].index)
					key_val = self.lower_expr(expr.index)
					dest = self.b.new_temp()
					self.b.emit(M.ErrorCapturesGetDV(dest=dest, error=err_val, frame=frame_val, key=key_val))
					self._local_types[dest] = self._dv_type
					return dest
		if (
			isinstance(expr.subject, H.HIndex)
			and (
				(
					isinstance(expr.subject.subject, H.HField)
					and expr.subject.subject.name == "captures"
				)
				or (
					hasattr(H, "HPlaceExpr")
					and isinstance(expr.subject.subject, getattr(H, "HPlaceExpr"))
					and len(expr.subject.subject.projections) == 1
					and isinstance(expr.subject.subject.projections[0], H.HPlaceField)
					and expr.subject.subject.projections[0].name == "captures"
				)
			)
		):
			err_base = expr.subject.subject.subject if isinstance(expr.subject.subject, H.HField) else expr.subject.subject.base
			err_val = self.lower_expr(err_base)
			err_ty = self._infer_expr_type(err_base)
			if err_ty is not None:
				err_def = self._type_table.get(err_ty)
				if err_def.kind is TypeKind.REF and err_def.param_types:
					inner_ty = err_def.param_types[0]
					tmp = self.b.new_temp()
					self.b.emit(M.LoadRef(dest=tmp, ptr=err_val, inner_ty=inner_ty))
					err_val = tmp
			frame_val = self.lower_expr(expr.subject.index)
			key_val = self.lower_expr(expr.index)
			dest = self.b.new_temp()
			self.b.emit(M.ErrorCapturesGetDV(dest=dest, error=err_val, frame=frame_val, key=key_val))
			self._local_types[dest] = self._dv_type
			return dest
		if isinstance(expr.subject, H.HField) and expr.subject.name == "attrs":
			err_val = self.lower_expr(expr.subject.subject)
			err_ty = self._infer_expr_type(expr.subject.subject)
			if err_ty is not None:
				err_def = self._type_table.get(err_ty)
				if err_def.kind is TypeKind.REF and err_def.param_types:
					inner_ty = err_def.param_types[0]
					tmp = self.b.new_temp()
					self.b.emit(M.LoadRef(dest=tmp, ptr=err_val, inner_ty=inner_ty))
					err_val = tmp
			key_val = self.lower_expr(expr.index)
			dest = self.b.new_temp()
			self.b.emit(M.ErrorAttrsGetDV(dest=dest, error=err_val, key=key_val))
			self._local_types[dest] = self._dv_type
			return dest
		subject = self.lower_expr(expr.subject)
		index = self.lower_expr(expr.index)
		elem_ty = self._infer_array_elem_type(expr.subject)

		len_val = self.b.new_temp()
		self.b.emit(M.ArrayLen(dest=len_val, array=subject))
		self._local_types[len_val] = self._int_type
		zero_val = self.b.new_temp()
		self.b.emit(M.ConstInt(dest=zero_val, value=0))
		self._local_types[zero_val] = self._int_type
		lt_zero = self.b.new_temp()
		self.b.emit(M.BinaryOpInstr(dest=lt_zero, op=M.BinaryOp.LT, left=index, right=zero_val))
		ge_len = self.b.new_temp()
		self.b.emit(M.BinaryOpInstr(dest=ge_len, op=M.BinaryOp.GE, left=index, right=len_val))
		oob = self.b.new_temp()
		self.b.emit(M.BinaryOpInstr(dest=oob, op=M.BinaryOp.OR, left=lt_zero, right=ge_len))

		ok_block = self.b.new_block("idx_ok")
		err_block = self.b.new_block("idx_err")
		join_block = self.b.new_block("idx_join")
		self.b.set_terminator(M.IfTerminator(cond=oob, then_target=err_block.name, else_target=ok_block.name))

		tmp_local = f"__idx_tmp{self.b.new_temp()}"
		self.b.ensure_local(tmp_local)
		self._local_types[tmp_local] = elem_ty

		self.b.set_block(err_block)
		self._emit_index_error_throw(index_val=index)

		self.b.set_block(ok_block)
		dest = self.b.new_temp()
		self.b.emit(M.ArrayIndexLoadUnchecked(dest=dest, elem_ty=elem_ty, array=subject, index=index))
		self.b.emit(M.StoreLocal(local=tmp_local, value=dest))
		self.b.set_terminator(M.Goto(target=join_block.name))

		self.b.set_block(join_block)
		loaded = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=loaded, local=tmp_local))

		copy_status = self._type_table.copy_status(elem_ty)
		if copy_status is True:
			copy_dest = self.b.new_temp()
			self.b.emit(M.CopyValue(dest=copy_dest, value=loaded, ty=elem_ty))
			self._local_types[copy_dest] = elem_ty
			return copy_dest
		if copy_status is None:
			td = self._type_table.get(elem_ty)
			if td.kind is TypeKind.TYPEVAR:
				return loaded
			raise AssertionError("MIR lowering invariant violated: unresolved Copy status for array index read")
		td = self._type_table.get(elem_ty)
		if td.kind is not TypeKind.TYPEVAR:
			raise NotImplementedError("array index read requires Copy element type; borrow not supported in MVP")
		return loaded

	def _emit_index_error_throw(self, *, index_val: M.ValueId) -> None:
		event_fqn = "std.err:IndexError"
		schema = self._exception_schemas.get(event_fqn)
		if schema is None:
			schema_fields = ["container_id", "index"]
		else:
			_decl_fqn, schema_fields = schema
		field_set = set(schema_fields)
		for required in ("container_id", "index"):
			if required not in field_set:
				raise AssertionError(f"IndexError schema missing field {required!r} (checker bug)")

		code_const = self._lookup_error_code(event_fqn=event_fqn)
		code_val = self.b.new_temp()
		self.b.emit(M.ConstUint64(dest=code_val, value=code_const))
		self._local_types[code_val] = self._uint64_type
		event_fqn_val = self.b.new_temp()
		self.b.emit(M.ConstString(dest=event_fqn_val, value=event_fqn))

		container_const = self.b.new_temp()
		self.b.emit(M.ConstString(dest=container_const, value=ARRAY_CONTAINER_ID))
		dv_container = self.b.new_temp()
		self.b.emit(M.ConstructDV(dest=dv_container, dv_type_name="String", args=[container_const]))
		self._local_types[dv_container] = self._dv_type
		dv_index = self.b.new_temp()
		self.b.emit(M.ConstructDV(dest=dv_index, dv_type_name="Int", args=[index_val]))
		self._local_types[dv_index] = self._dv_type

		name_to_dv = {"container_id": dv_container, "index": dv_index}
		first_name = schema_fields[0]
		first_dv = name_to_dv[first_name]
		first_key = self.b.new_temp()
		self.b.emit(M.ConstString(dest=first_key, value=first_name))
		err_val = self.b.new_temp()
		self.b.emit(
			M.ConstructError(
				dest=err_val,
				code=code_val,
				event_fqn=event_fqn_val,
				payload=first_dv,
				attr_key=first_key,
			)
		)
		self._local_types[err_val] = self._type_table.ensure_error()
		for name in schema_fields[1:]:
			dv_val = name_to_dv[name]
			key_val = self.b.new_temp()
			self.b.emit(M.ConstString(dest=key_val, value=name))
			self.b.emit(M.ErrorAddAttrDV(error=err_val, key=key_val, value=dv_val))

		self._propagate_error(err_val)

	def _visit_expr_HArrayLiteral(self, expr: H.HArrayLiteral) -> M.ValueId:
		prev_span = self.b.current_span
		forced_span = self._current_stmt_span if self._current_stmt_span is not None and self._current_stmt_span != Span() else self.b.current_span
		if forced_span is not None and forced_span != Span():
			self.b.current_span = forced_span
		elem_ty = None
		expected = self._current_expected_type()
		if expected is not None:
			exp_def = self._type_table.get(expected)
			if exp_def.kind is TypeKind.ARRAY and exp_def.param_types:
				elem_ty = exp_def.param_types[0]
		if elem_ty is not None:
			kind = self._type_table.get(elem_ty).kind
			if kind in (TypeKind.UNKNOWN, TypeKind.TYPEVAR, TypeKind.FORWARD_NOMINAL):
				elem_ty = None
		if elem_ty is None and self._type_param_subst:
			known = self._expr_types.get(expr.node_id) if self._expr_types else None
			if known is not None:
				known_def = self._type_table.get(known)
				if known_def.kind is TypeKind.ARRAY and known_def.param_types:
					elem_ty = known_def.param_types[0]
			if elem_ty is None:
				# Best-effort substitution when the expected type carries forward nominals.
				expected = self._current_expected_type()
				if expected is not None:
					exp_def = self._type_table.get(expected)
					if exp_def.kind is TypeKind.ARRAY and exp_def.param_types:
						elem_ty = exp_def.param_types[0]
						td = self._type_table.get(elem_ty)
						if td.kind is TypeKind.FORWARD_NOMINAL and td.name in self._type_param_subst:
							elem_ty = self._type_param_subst[td.name]
		if elem_ty is None and self._expr_types and getattr(expr, "node_id", None) is not None:
			known = self._expr_types.get(expr.node_id)
			if known is not None:
				known_def = self._type_table.get(known)
				if known_def.kind is TypeKind.ARRAY and known_def.param_types:
					elem_ty = known_def.param_types[0]
		if elem_ty is None:
			elem_ty = self._infer_array_literal_elem_type(expr)
		dest = self.b.new_temp()
		length = len(expr.elements)
		len_val = self.b.new_temp()
		cap_val = self.b.new_temp()
		self.b.emit(M.ConstInt(dest=len_val, value=length))
		self.b.emit(M.ConstInt(dest=cap_val, value=length))
		self._local_types[len_val] = self._int_type
		self._local_types[cap_val] = self._int_type
		zero_len = self.b.new_temp()
		self.b.emit(M.ConstInt(dest=zero_len, value=0))
		self._local_types[zero_len] = self._int_type
		alloc = M.ArrayAlloc(dest=dest, elem_ty=elem_ty, length=zero_len, cap=cap_val)
		span = self.b.current_span if self.b.current_span is not None and self.b.current_span != Span() else self._current_stmt_span
		if span is not None and span != Span():
			alloc.span = span
		self.b.emit(alloc)
		for idx, elem_expr in enumerate(expr.elements):
			val = self.lower_expr(elem_expr)
			val_ty = self._infer_expr_type(elem_expr)
			if val_ty is not None:
				copy_status = self._type_table.copy_status(val_ty)
				is_lvalue_elem = isinstance(elem_expr, H.HVar)
				if hasattr(H, "HPlaceExpr") and isinstance(elem_expr, getattr(H, "HPlaceExpr")):
					is_lvalue_elem = True
				if copy_status is True and is_lvalue_elem and not isinstance(elem_expr, H.HMove):
					copy_dest = self.b.new_temp()
					self.b.emit(M.CopyValue(dest=copy_dest, value=val, ty=val_ty))
					self._local_types[copy_dest] = val_ty
					val = copy_dest
			idx_val = self.b.new_temp()
			self.b.emit(M.ConstInt(dest=idx_val, value=idx))
			self._local_types[idx_val] = self._int_type
			init = M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=dest, index=idx_val, value=val)
			span = self.b.current_span if self.b.current_span is not None and self.b.current_span != Span() else self._current_stmt_span
			if span is not None and span != Span():
				init.span = span
			self.b.emit(init)
		final_arr = self.b.new_temp()
		set_len = M.ArraySetLen(dest=final_arr, array=dest, length=len_val)
		span = self.b.current_span if self.b.current_span is not None and self.b.current_span != Span() else self._current_stmt_span
		if span is not None and span != Span():
			set_len.span = span
		self.b.emit(set_len)
		self._local_types[final_arr] = self._type_table.new_array(elem_ty)
		self.b.current_span = prev_span
		return final_arr

	def _visit_expr_HMapLiteral(self, expr: H.HMapLiteral) -> M.ValueId:
		map_ty = self._current_expected_type()
		if map_ty is None and self._expr_types and getattr(expr, "node_id", None) is not None:
			map_ty = self._expr_types.get(expr.node_id)
		if map_ty is None:
			raise AssertionError("map literal missing concrete target type in MIR lowering (checker bug)")
		td = self._type_table.get(map_ty)
		if td.kind is not TypeKind.STRUCT:
			raise AssertionError("map literal target type must be a concrete struct type (checker bug)")
		if not expr.entries:
			dest = self.b.new_temp()
			self.b.emit(M.ZeroValue(dest=dest, ty=map_ty))
			self._local_types[dest] = map_ty
			return dest

		struct_inst = self._type_table.get_struct_instance(map_ty)
		if struct_inst is None or len(struct_inst.type_args) < 2:
			raise AssertionError("map literal target must be an instantiated map type (checker bug)")
		base_def = self._type_table.get(struct_inst.base_id)
		if base_def.kind is not TypeKind.STRUCT or base_def.module_id != "std.containers" or base_def.name not in {"HashMapCore", "TreeMap"}:
			raise NotImplementedError("map literal MIR lowering currently supports std.containers HashMap/TreeMap only")
		key_ty = struct_inst.type_args[0]
		value_ty = struct_inst.type_args[1]

		insert_info = self._resolve_map_insert_call_info(map_ty=map_ty, key_ty=key_ty, value_ty=value_ty)

		map_local = f"__maplit{self.b.new_temp()}"
		self.b.ensure_local(map_local)
		self._local_types[map_local] = map_ty
		map_binding_id = self._next_synth_binding_id
		self._next_synth_binding_id -= 1
		self._binding_names[map_binding_id] = map_local
		self._binding_types[map_binding_id] = map_ty
		zero_map = self.b.new_temp()
		self.b.emit(M.ZeroValue(dest=zero_map, ty=map_ty))
		self._local_types[zero_map] = map_ty
		self.b.emit(M.StoreLocal(local=map_local, value=zero_map))

		for entry in expr.entries:
			insert_expr = H.HMethodCall(
				receiver=H.HVar(name=map_local, binding_id=map_binding_id, loc=getattr(expr, "loc", Span())),
				method_name="insert",
				args=[
					entry.key,
					entry.value,
				],
				loc=getattr(expr, "loc", Span()),
			)
			insert_res, _insert_sig = self._lower_method_call_with_info(insert_expr, insert_info)
			if insert_info.sig.can_throw:
				assert insert_res is not None
				def emit_insert() -> M.ValueId:
					return insert_res
				self._lower_can_throw_call_stmt(emit_call=emit_insert, ok_ty=insert_info.sig.user_ret_type)

		dest = self.b.new_temp()
		self.b.emit(M.MoveOut(dest=dest, local=map_local, ty=map_ty))
		self._moved_locals.add(map_local)
		self._local_types[dest] = map_ty
		return dest

	def _lower_len(self, subj_ty: Optional[TypeId], subj_val: M.ValueId, dest: M.ValueId) -> None:
		"""Lower length for Array<T> and String to Int."""
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

	def _lower_intrinsic_call_expr(
		self,
		expr: H.HCall,
		intrinsic: IntrinsicKind,
		*,
		info: CallInfo | None = None,
	) -> M.ValueId:
		if intrinsic is IntrinsicKind.SWAP:
			raise AssertionError("swap(...) used in expression context (checker bug)")
		if intrinsic is IntrinsicKind.DROP_VALUE:
			raise AssertionError("drop_value(...) used in expression context (checker bug)")
		if intrinsic is IntrinsicKind.RAW_DEALLOC:
			raise AssertionError("dealloc(...) used in expression context (checker bug)")
		if intrinsic is IntrinsicKind.RAW_WRITE:
			raise AssertionError("write(...) used in expression context (checker bug)")
		if intrinsic is IntrinsicKind.PTR_WRITE:
			raise AssertionError("ptr_write(...) used in expression context (checker bug)")
		if intrinsic is IntrinsicKind.REPLACE:
			if getattr(expr, "kwargs", None):
				raise AssertionError("replace(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 2:
				raise AssertionError("replace(...) arity mismatch reached MIR lowering (checker bug)")
			place_expr = expr.args[0]
			if isinstance(place_expr, H.HBorrow) and place_expr.is_mut:
				place_expr = place_expr_from_lvalue_expr(place_expr.subject)
			new_expr = expr.args[1]
			if not (hasattr(H, "HPlaceExpr") and isinstance(place_expr, getattr(H, "HPlaceExpr"))):
				raise AssertionError("replace(place, v): non-canonical place reached MIR lowering (normalize/typechecker bug)")
			ptr, inner_ty = self._lower_addr_of_place(place_expr, is_mut=True)
			old_val = self.b.new_temp()
			self.b.emit(M.LoadRef(dest=old_val, ptr=ptr, inner_ty=inner_ty))
			new_val = self.lower_expr(new_expr)
			self.b.emit(M.StoreRef(ptr=ptr, value=new_val, inner_ty=inner_ty))
			self._local_types[old_val] = inner_ty
			return old_val
		if intrinsic is IntrinsicKind.MAYBE_UNINIT:
			raise NotImplementedError("maybe_uninit intrinsic lowering is not implemented in MVP")
		if intrinsic is IntrinsicKind.MAYBE_WRITE:
			if getattr(expr, "kwargs", None):
				raise AssertionError("maybe_write(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 2:
				raise AssertionError("maybe_write(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None:
				raise AssertionError("maybe_write(...) missing CallInfo (checker bug)")
			ret_ty = info.sig.user_ret_type
			inner_ty = self._unwrap_ref_type(ret_ty)
			if inner_ty is None:
				raise AssertionError("maybe_write(...) missing &mut T return type (checker bug)")
			slot = self.lower_expr(expr.args[0])
			val = self.lower_expr(expr.args[1])
			raw_ptr = self.b.new_temp()
			ptr_ty = self._type_table.new_ptr(inner_ty)
			self.b.emit(M.PtrFromRef(dest=raw_ptr, src=slot, ptr_ty=ptr_ty))
			self.b.emit(M.PtrWrite(ptr=raw_ptr, value=val, elem_ty=inner_ty))
			self._local_types[raw_ptr] = ptr_ty
			self._local_types[slot] = ret_ty
			return slot
		if intrinsic in (IntrinsicKind.MAYBE_ASSUME_INIT_REF, IntrinsicKind.MAYBE_ASSUME_INIT_MUT):
			if getattr(expr, "kwargs", None):
				raise AssertionError(f"{intrinsic.value}(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 1:
				raise AssertionError(f"{intrinsic.value}(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None:
				raise AssertionError(f"{intrinsic.value}(...) missing CallInfo (checker bug)")
			ret_ty = info.sig.user_ret_type
			slot = self.lower_expr(expr.args[0])
			self._local_types[slot] = ret_ty
			return slot
		if intrinsic is IntrinsicKind.MAYBE_ASSUME_INIT_READ:
			if getattr(expr, "kwargs", None):
				raise AssertionError("maybe_assume_init_read(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 1:
				raise AssertionError("maybe_assume_init_read(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None:
				raise AssertionError("maybe_assume_init_read(...) missing CallInfo (checker bug)")
			ret_ty = info.sig.user_ret_type
			slot = self.lower_expr(expr.args[0])
			raw_ptr = self.b.new_temp()
			ptr_ty = self._type_table.new_ptr(ret_ty)
			self.b.emit(M.PtrFromRef(dest=raw_ptr, src=slot, ptr_ty=ptr_ty))
			dest = self.b.new_temp()
			self.b.emit(M.PtrRead(dest=dest, ptr=raw_ptr, elem_ty=ret_ty))
			zero = self.b.new_temp()
			self.b.emit(M.ZeroValue(dest=zero, ty=ret_ty))
			self._local_types[zero] = ret_ty
			self.b.emit(M.PtrWrite(ptr=raw_ptr, value=zero, elem_ty=ret_ty))
			self._local_types[raw_ptr] = ptr_ty
			self._local_types[dest] = ret_ty
			return dest
		if intrinsic in (IntrinsicKind.WRAPPING_ADD_U64, IntrinsicKind.WRAPPING_MUL_U64):
			name = intrinsic.value
			if getattr(expr, "kwargs", None):
				raise AssertionError(f"{name}(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 2:
				raise AssertionError(f"{name}(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None or len(info.sig.param_types) != 2:
				raise AssertionError(f"{name}(...) missing CallInfo types (checker bug)")
			if info.sig.param_types[0] != self._uint64_type or info.sig.param_types[1] != self._uint64_type:
				raise AssertionError(f"{name} requires Uint64 operands (checker bug)")
			left = self.lower_expr(expr.args[0])
			right = self.lower_expr(expr.args[1])
			dest = self.b.new_temp()
			if intrinsic is IntrinsicKind.WRAPPING_ADD_U64:
				self.b.emit(M.WrappingAddU64(dest=dest, left=left, right=right))
			else:
				self.b.emit(M.WrappingMulU64(dest=dest, left=left, right=right))
			self._local_types[dest] = self._uint64_type
			return dest
		if intrinsic is IntrinsicKind.RAW_ALLOC:
			if getattr(expr, "kwargs", None):
				raise AssertionError("alloc_uninit(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 1:
				raise AssertionError("alloc_uninit(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None:
				raise AssertionError("alloc_uninit(...) missing CallInfo (checker bug)")
			raw_ty = info.sig.user_ret_type
			elem_ty = self._raw_buffer_elem_type(raw_ty)
			if elem_ty is self._unknown_type:
				raise AssertionError("alloc_uninit(...) missing RawBuffer element type (checker bug)")
			cap_val = self.lower_expr(expr.args[0])
			dest = self.b.new_temp()
			self.b.emit(M.RawBufferAlloc(dest=dest, raw_ty=raw_ty, elem_ty=elem_ty, cap=cap_val))
			self._local_types[dest] = raw_ty
			return dest
		if intrinsic in (IntrinsicKind.RAWBUFFER_PTR, IntrinsicKind.RAWBUFFER_CAP):
			if getattr(expr, "kwargs", None):
				raise AssertionError(f"{intrinsic.value}(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 1:
				raise AssertionError(f"{intrinsic.value}(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None or not info.sig.param_types:
				raise AssertionError(f"{intrinsic.value}(...) missing CallInfo (checker bug)")
			raw_param = self._unwrap_ref_type(info.sig.param_types[0])
			if raw_param is None:
				raise AssertionError(f"{intrinsic.value}(...) missing RawBuffer reference type (checker bug)")
			buf_ref = self.lower_expr(expr.args[0])
			buf_val = self.b.new_temp()
			self.b.emit(M.LoadRef(dest=buf_val, ptr=buf_ref, inner_ty=raw_param))
			dest = self.b.new_temp()
			field_index = 0 if intrinsic is IntrinsicKind.RAWBUFFER_PTR else 1
			field_ty = info.sig.user_ret_type
			self.b.emit(M.StructGetField(dest=dest, subject=buf_val, struct_ty=raw_param, field_index=field_index, field_ty=field_ty))
			self._local_types[dest] = field_ty
			return dest
		if intrinsic is IntrinsicKind.RAWBUFFER_FROM_PARTS:
			if getattr(expr, "kwargs", None):
				raise AssertionError("rawbuffer_from_parts(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 2:
				raise AssertionError("rawbuffer_from_parts(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None:
				raise AssertionError("rawbuffer_from_parts(...) missing CallInfo (checker bug)")
			ptr_val = self.lower_expr(expr.args[0])
			cap_val = self.lower_expr(expr.args[1])
			dest = self.b.new_temp()
			self.b.emit(M.ConstructStruct(dest=dest, struct_ty=info.sig.user_ret_type, args=[ptr_val, cap_val]))
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		if intrinsic in (IntrinsicKind.RAW_PTR_AT_REF, IntrinsicKind.RAW_PTR_AT_MUT):
			if getattr(expr, "kwargs", None):
				raise AssertionError("ptr_at(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 2:
				raise AssertionError("ptr_at(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None or not info.sig.param_types:
				raise AssertionError("ptr_at(...) missing CallInfo (checker bug)")
			raw_param = self._unwrap_ref_type(info.sig.param_types[0])
			elem_ty = self._raw_buffer_elem_type(raw_param)
			if elem_ty is self._unknown_type:
				raise AssertionError("ptr_at(...) missing RawBuffer element type (checker bug)")
			buf_val = self.lower_expr(expr.args[0])
			idx_val = self.lower_expr(expr.args[1])
			dest = self.b.new_temp()
			self.b.emit(M.RawBufferPtrAt(dest=dest, buffer=buf_val, raw_ty=raw_param, elem_ty=elem_ty, index=idx_val))
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		if intrinsic is IntrinsicKind.RAW_READ:
			if getattr(expr, "kwargs", None):
				raise AssertionError("read(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 2:
				raise AssertionError("read(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None or not info.sig.param_types:
				raise AssertionError("read(...) missing CallInfo (checker bug)")
			raw_param = self._unwrap_ref_type(info.sig.param_types[0])
			elem_ty = self._raw_buffer_elem_type(raw_param)
			if elem_ty is self._unknown_type:
				raise AssertionError("read(...) missing RawBuffer element type (checker bug)")
			buf_val = self.lower_expr(expr.args[0])
			idx_val = self.lower_expr(expr.args[1])
			dest = self.b.new_temp()
			self.b.emit(M.RawBufferRead(dest=dest, buffer=buf_val, raw_ty=raw_param, elem_ty=elem_ty, index=idx_val))
			self._local_types[dest] = elem_ty
			return dest
		if intrinsic in (IntrinsicKind.PTR_FROM_REF, IntrinsicKind.PTR_FROM_REF_MUT):
			if getattr(expr, "kwargs", None):
				raise AssertionError("ptr_from_ref(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 1:
				raise AssertionError("ptr_from_ref(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None:
				raise AssertionError("ptr_from_ref(...) missing CallInfo (checker bug)")
			src_val = self.lower_expr(expr.args[0])
			dest = self.b.new_temp()
			self.b.emit(M.PtrFromRef(dest=dest, src=src_val, ptr_ty=info.sig.user_ret_type))
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		if intrinsic is IntrinsicKind.PTR_OFFSET:
			if getattr(expr, "kwargs", None):
				raise AssertionError("ptr_offset(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 2:
				raise AssertionError("ptr_offset(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None or not info.sig.param_types:
				raise AssertionError("ptr_offset(...) missing CallInfo (checker bug)")
			ptr_val = self.lower_expr(expr.args[0])
			offset_val = self.lower_expr(expr.args[1])
			elem_ty = self._raw_ptr_elem_type(info.sig.param_types[0])
			if elem_ty is self._unknown_type:
				raise AssertionError("ptr_offset(...) missing Ptr<T> element type (checker bug)")
			dest = self.b.new_temp()
			self.b.emit(M.PtrOffset(dest=dest, ptr=ptr_val, ptr_ty=info.sig.param_types[0], elem_ty=elem_ty, offset=offset_val))
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		if intrinsic is IntrinsicKind.PTR_READ:
			if getattr(expr, "kwargs", None):
				raise AssertionError("ptr_read(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 1:
				raise AssertionError("ptr_read(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None or not info.sig.param_types:
				raise AssertionError("ptr_read(...) missing CallInfo (checker bug)")
			ptr_val = self.lower_expr(expr.args[0])
			elem_ty = self._raw_ptr_elem_type(info.sig.param_types[0])
			if elem_ty is self._unknown_type:
				raise AssertionError("ptr_read(...) missing Ptr<T> element type (checker bug)")
			dest = self.b.new_temp()
			self.b.emit(M.PtrRead(dest=dest, ptr=ptr_val, elem_ty=elem_ty))
			self._local_types[dest] = elem_ty
			return dest
		if intrinsic is IntrinsicKind.PTR_IS_NULL:
			if getattr(expr, "kwargs", None):
				raise AssertionError("ptr_is_null(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 1:
				raise AssertionError("ptr_is_null(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None or not info.sig.param_types:
				raise AssertionError("ptr_is_null(...) missing CallInfo (checker bug)")
			ptr_val = self.lower_expr(expr.args[0])
			dest = self.b.new_temp()
			self.b.emit(M.PtrIsNull(dest=dest, ptr=ptr_val, ptr_ty=info.sig.param_types[0]))
			self._local_types[dest] = self._bool_type
			return dest
		if intrinsic is IntrinsicKind.BYTE_LENGTH:
			name = intrinsic.value
			if getattr(expr, "kwargs", None):
				raise AssertionError(f"{name}(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 1:
				raise AssertionError(f"{name}(...) arity mismatch reached MIR lowering (checker bug)")
			arg_expr = expr.args[0]
			arg_val = self.lower_expr(arg_expr)
			arg_ty = None
			if info is not None and info.sig.param_types:
				arg_ty = info.sig.param_types[0]
			if arg_ty is None or self._type_table.get(arg_ty).kind is TypeKind.UNKNOWN:
				raise AssertionError(f"{name}(x): missing argument type in CallInfo (checker bug)")
			dest = self.b.new_temp()
			self._lower_len(arg_ty, arg_val, dest)
			self._local_types[dest] = self._int_type
			return dest
		if intrinsic is IntrinsicKind.STRING_BYTE_AT:
			name = intrinsic.value
			if getattr(expr, "kwargs", None):
				raise AssertionError(f"{name}(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 2:
				raise AssertionError(f"{name}(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None or len(info.sig.param_types) < 2:
				raise AssertionError(f"{name}(...) missing CallInfo types (checker bug)")
			if info.sig.param_types[0] != self._type_table.ensure_ref(self._string_type) or info.sig.param_types[1] != self._int_type:
				raise AssertionError(f"{name} requires &String and Int operands (checker bug)")
			str_val = self.lower_expr(expr.args[0])
			str_arg_ty = info.sig.param_types[0]
			td = self._type_table.get(str_arg_ty)
			if td.kind is TypeKind.REF and td.param_types:
				inner_ty = td.param_types[0]
				load = self.b.new_temp()
				self.b.emit(M.LoadRef(dest=load, ptr=str_val, inner_ty=inner_ty))
				self._local_types[load] = inner_ty
				str_val = load
			idx_val = self.lower_expr(expr.args[1])
			dest = self.b.new_temp()
			self.b.emit(M.StringByteAt(dest=dest, value=str_val, index=idx_val))
			self._local_types[dest] = self._byte_type
			return dest
		if intrinsic is IntrinsicKind.STRING_EQ:
			if getattr(expr, "kwargs", None):
				raise AssertionError("string_eq(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 2:
				raise AssertionError("string_eq(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None or len(info.sig.param_types) < 2:
				raise AssertionError("string_eq(...) missing CallInfo types (checker bug)")
			if info.sig.param_types[0] != self._string_type or info.sig.param_types[1] != self._string_type:
				raise AssertionError("string_eq requires String operands (checker bug)")
			l_expr, r_expr = expr.args
			l_val = self.lower_expr(l_expr)
			r_val = self.lower_expr(r_expr)
			dest = self.b.new_temp()
			self.b.emit(M.StringEq(dest=dest, left=l_val, right=r_val))
			self._local_types[dest] = self._bool_type
			return dest
		if intrinsic is IntrinsicKind.STRING_CONCAT:
			if getattr(expr, "kwargs", None):
				raise AssertionError("string_concat(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 2:
				raise AssertionError("string_concat(...) arity mismatch reached MIR lowering (checker bug)")
			if info is None or len(info.sig.param_types) < 2:
				raise AssertionError("string_concat(...) missing CallInfo types (checker bug)")
			if info.sig.param_types[0] != self._string_type or info.sig.param_types[1] != self._string_type:
				raise AssertionError("string_concat requires String operands (checker bug)")
			l_expr, r_expr = expr.args
			l_val = self.lower_expr(l_expr)
			r_val = self.lower_expr(r_expr)
			dest = self.b.new_temp()
			self.b.emit(M.StringConcat(dest=dest, left=l_val, right=r_val))
			self._local_types[dest] = self._string_type
			return dest
		if intrinsic in (IntrinsicKind.CALLBACK0, IntrinsicKind.CALLBACK1, IntrinsicKind.CALLBACK2, IntrinsicKind.CALLBACK_THROW0, IntrinsicKind.CALLBACK_THROW1, IntrinsicKind.CALLBACK_THROW2):
			if getattr(expr, "kwargs", None):
				raise AssertionError(f"{intrinsic.value}(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 1:
				raise AssertionError(f"{intrinsic.value}(...) arity mismatch reached MIR lowering (checker bug)")
			arg = expr.args[0]
			if info is None:
				raise AssertionError(f"{intrinsic.value}(...) missing CallInfo (checker bug)")
			can_throw = intrinsic in (IntrinsicKind.CALLBACK_THROW0, IntrinsicKind.CALLBACK_THROW1, IntrinsicKind.CALLBACK_THROW2)
			dest = self.b.new_temp()
			if isinstance(arg, H.HFnPtrConst):
				self.b.emit(M.ConstructIface(dest=dest, iface_ty=info.sig.user_ret_type, fn_ref=arg.fn_ref, call_sig=arg.call_sig))
			elif isinstance(arg, H.HLambda):
				if not info.sig.param_types:
					raise AssertionError(f"{intrinsic.value} missing arg type for lambda (checker bug)")
				fn_ty = info.sig.param_types[0]
				fn_def = self._type_table.get(fn_ty)
				if fn_def.kind is not TypeKind.FUNCTION or not fn_def.param_types:
					raise AssertionError(f"{intrinsic.value} expected function type for lambda (checker bug)")
				sig_params = list(fn_def.param_types[:-1])
				ret_ty = fn_def.param_types[-1]
				call_sig = CallSig(param_types=tuple(sig_params), user_ret_type=ret_ty, can_throw=can_throw)
				fn_ref, env_ptr, env_ty = self._lower_lambda_callback(
					arg,
					param_types=sig_params,
					ret_type=ret_ty,
					can_throw=can_throw,
				)
				data_ty = self._type_table.ensure_ref_mut(env_ty) if env_ty is not None else None
				self.b.emit(
					M.ConstructIface(
						dest=dest,
						iface_ty=info.sig.user_ret_type,
						fn_ref=fn_ref,
						call_sig=call_sig,
						data=env_ptr,
						data_ty=data_ty,
						env_ty=env_ty,
					)
				)
			else:
				raise NotImplementedError(f"{intrinsic.value} requires a function pointer constant or lambda in MVP")
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		if intrinsic is IntrinsicKind.TYPE_ID:
			if getattr(expr, "kwargs", None):
				raise AssertionError("type_id(...) does not accept keyword arguments (checker bug)")
			if len(expr.args) != 0:
				raise AssertionError("type_id(...) arity mismatch reached MIR lowering (checker bug)")
			type_arg_exprs = list(getattr(expr, "type_args", []) or [])
			if len(type_arg_exprs) != 1:
				raise AssertionError("type_id<T>() requires exactly one type argument (checker bug)")
			cur_mod = self._current_module_name()
			type_arg = resolve_opaque_type(
				type_arg_exprs[0],
				self._type_table,
				module_id=getattr(type_arg_exprs[0], "module_id", None) or cur_mod,
				type_params=self._type_param_subst or None,
			)
			token = self._type_id_token(type_arg)
			dest = self.b.new_temp()
			self.b.emit(M.ConstUint64(dest=dest, value=token))
			self._local_types[dest] = self._uint64_type
			return dest
		raise AssertionError(f"unknown intrinsic '{intrinsic.value}' reached MIR lowering (checker bug)")

	# Stubs for unhandled expressions
	def _visit_expr_HCall(self, expr: H.HCall) -> M.ValueId:
		"""
		Plain function call. For now only direct function names are supported;
		indirect/function-valued calls will be added later if needed.
		"""
		if drift_debug.enabled("stage2"):
			import sys
			print(f"[drift:debug][stage2] visit HCall fn={getattr(expr.fn, 'name', None)} loc={Span.from_loc(getattr(expr, 'loc', None))}", file=sys.stderr)
		if isinstance(expr.fn, H.HLambda):
			return self._lower_lambda_immediate_call(expr.fn, expr.args)
		if hasattr(H, "HQualifiedMember") and isinstance(expr.fn, getattr(H, "HQualifiedMember")):
			info = self._call_info_for_expr_optional(expr)
			if info is None and self._typed_mode != "none":
				raise AssertionError("missing CallInfo for qualified member call (checker bug)")
			if info is None:
				info = self._call_info_from_ufcs(expr)
			if info is None:
				raise AssertionError("missing CallInfo for qualified member call (checker bug)")
			if getattr(expr, "kwargs", None) and info.target.kind is not CallTargetKind.CONSTRUCTOR:
				raise AssertionError("keyword arguments reached MIR lowering for a UFCS call (checker bug)")
			if info.target.kind is CallTargetKind.INTRINSIC:
				intrinsic = info.target.intrinsic
				if intrinsic is None:
					raise AssertionError("intrinsic call missing name (typecheck/call-info bug)")
				return self._lower_intrinsic_call_expr(expr, intrinsic, info=info)
			result = self._lower_call_with_info(expr, info)
			if result is None:
				raise AssertionError("Void-returning call used in expression context (checker bug)")
			if info.sig.can_throw:
				ok_tid = info.sig.user_ret_type
				def emit_call() -> M.ValueId:
					return result
				return self._lower_can_throw_call_value(emit_call=emit_call, ok_ty=ok_tid)
			return result
		if isinstance(expr.fn, H.HVar):
			name = expr.fn.name
			info = self._call_info_for_expr_optional(expr)
			if info is None and self._typed_mode != "none":
				raise AssertionError(
					f"missing call info for HCall callsite_id={getattr(expr, 'callsite_id', None)} (typecheck/call-info bug)"
				)
			if info is not None and info.target.kind is CallTargetKind.INTRINSIC:
				intrinsic = info.target.intrinsic
				if intrinsic is None:
					raise AssertionError("intrinsic call missing name (typecheck/call-info bug)")
				return self._lower_intrinsic_call_expr(expr, intrinsic, info=info)
			if info is not None and info.target.kind is CallTargetKind.INDIRECT:
				expected = self._current_expected_type()
				if expected is not None and self._type_table.get(expected).kind is TypeKind.VARIANT:
					inst = self._type_table.get_variant_instance(expected)
					if inst is not None and name in inst.arms_by_name:
						info = None
				if info is not None:
					cur_mod = self._current_module_name()
					fn_module = getattr(expr.fn, "module_id", None)
					if isinstance(fn_module, str):
						struct_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=fn_module, name=name)
					else:
						struct_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=cur_mod, name=name) or self._type_table.find_unique_nominal_by_name(
							kind=TypeKind.STRUCT, name=name
						)
					if struct_ty is not None:
						info = None
			if info is not None:
				if getattr(expr, "kwargs", None) and info.target.kind is not CallTargetKind.CONSTRUCTOR:
					cur_mod = self._current_module_name()
					fn_module = getattr(expr.fn, "module_id", None)
					if isinstance(fn_module, str):
						struct_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=fn_module, name=name)
					else:
						struct_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=cur_mod, name=name) or self._type_table.find_unique_nominal_by_name(
							kind=TypeKind.STRUCT, name=name
						)
					if struct_ty is None:
						raise AssertionError("keyword arguments reached MIR lowering for a normal call (checker bug)")
					info = None
				if info is not None:
					result = self._lower_call_with_info(expr, info)
					if result is None:
						expected = self._current_expected_type()
						if expected is not None and self._type_table.is_void(expected):
							return self._void_value()
						raise AssertionError("Void-returning call used in expression context (checker bug)")
					if info.sig.can_throw:
						ok_tid = info.sig.user_ret_type
						def emit_call() -> M.ValueId:
							return result
						return self._lower_can_throw_call_value(emit_call=emit_call, ok_ty=ok_tid)
					return result
			# Variant constructor call in expression position.
			#
			# MVP rule: constructor calls require an expected variant type from
			# context (annotation, return type, etc.). Stage2 threads that expected
			# type hint through `lower_expr(..., expected_type=...)`.
			if self._typed_mode != "none":
				raise AssertionError("variant constructor reached MIR fallback in typed mode (checker bug)")
			expected = self._current_expected_type()
			if expected is not None:
				td = self._type_table.get(expected)
				if td.kind is TypeKind.VARIANT:
					inst = self._type_table.get_variant_instance(expected)
					if inst is not None and name in inst.arms_by_name:
						arm_def = inst.arms_by_name[name]
						pos_args = list(expr.args)
						kw_pairs = list(getattr(expr, "kwargs", []) or [])
						if self._typed_mode != "none" and kw_pairs:
							raise AssertionError(
								"keyword arguments reached MIR lowering for a constructor in typed mode (checker bug)"
							)
						if pos_args and kw_pairs:
							raise AssertionError("variant constructor does not allow mixing positional and named arguments (checker bug)")

						field_names = list(getattr(arm_def, "field_names", []) or [])
						field_types = list(arm_def.field_types)
						if len(field_names) != len(field_types):
							raise AssertionError("variant ctor schema/type mismatch reached MIR lowering (checker bug)")

						ordered: list[M.ValueId | None] = [None] * len(field_types)
						# Evaluate arguments left-to-right as written, but pass them in field order.
						if kw_pairs:
							for kw in kw_pairs:
								try:
									field_idx = field_names.index(kw.name)
								except ValueError as err:
									raise AssertionError("unknown variant ctor field reached MIR lowering (checker bug)") from err
								if ordered[field_idx] is not None:
									raise AssertionError("duplicate variant ctor field reached MIR lowering (checker bug)")
								ordered[field_idx] = self.lower_expr(kw.value, expected_type=field_types[field_idx])
						else:
							if len(pos_args) != len(field_types):
								raise AssertionError("variant constructor arity mismatch reached MIR lowering (checker bug)")
							for idx, (arg_expr, fty) in enumerate(zip(pos_args, field_types)):
								ordered[idx] = self.lower_expr(arg_expr, expected_type=fty)
						if any(v is None for v in ordered):
							raise AssertionError("missing variant ctor field reached MIR lowering (checker bug)")
						arg_vals = [v for v in ordered if v is not None]
						dest = self.b.new_temp()
						self.b.emit(M.ConstructVariant(dest=dest, variant_ty=expected, ctor=name, args=arg_vals))
						self._local_types[dest] = expected
						return dest
			# Struct constructor: `Point(1, 2)` constructs a struct value.
			#
			# This only triggers when there is no function signature for the same
			# name (to avoid ambiguity in older tests).
			struct_ty: TypeId | None = None
			if self._typed_mode != "none":
				candidate = self._expr_types.get(expr.node_id)
				if candidate is not None:
					if self._type_table.get(candidate).kind is TypeKind.UNKNOWN:
						if self._typed_mode == "strict":
							raise AssertionError("typed_mode strict: struct ctor has Unknown expr type")
					else:
						struct_ty = candidate
			cur_mod = self._current_module_name()
			fn_module = getattr(expr.fn, "module_id", None)
			if struct_ty is None:
				if isinstance(fn_module, str):
					struct_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=fn_module, name=name)
				else:
					struct_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=cur_mod, name=name) or self._type_table.find_unique_nominal_by_name(
						kind=TypeKind.STRUCT, name=name
					)
			if struct_ty is not None:
				if self._typed_mode != "none":
					raise AssertionError("struct constructor reached MIR fallback in typed mode (checker bug)")
				struct_def = self._type_table.get(struct_ty)
				if struct_def.kind is not TypeKind.STRUCT:
					raise AssertionError("struct schema name resolved to non-STRUCT TypeId (checker bug)")
				struct_inst = self._type_table.get_struct_instance(struct_ty)
				if struct_inst is not None:
					field_names = list(struct_inst.field_names)
					field_types = list(struct_inst.field_types)
				else:
					field_names = list(struct_def.field_names or [])
					field_types = list(struct_def.param_types)
				if len(field_names) != len(field_types):
					raise AssertionError("struct schema/type mismatch reached MIR lowering (checker bug)")

				pos_args = list(expr.args)
				kw_pairs = list(getattr(expr, "kwargs", []) or [])
				if len(pos_args) > len(field_types):
					raise AssertionError("struct ctor arg count mismatch reached MIR lowering (checker bug)")

				# Evaluate arguments left-to-right as written, but pass them in field order.
				ordered: list[M.ValueId | None] = [None] * len(field_types)
				for idx, arg_expr in enumerate(pos_args):
					ordered[idx] = self.lower_expr(arg_expr, expected_type=field_types[idx])
				for kw in kw_pairs:
					try:
						field_idx = field_names.index(kw.name)
					except ValueError as err:
						raise AssertionError("unknown struct ctor field reached MIR lowering (checker bug)") from err
					if field_idx < len(pos_args) or ordered[field_idx] is not None:
						raise AssertionError("duplicate struct ctor field reached MIR lowering (checker bug)")
					ordered[field_idx] = self.lower_expr(kw.value, expected_type=field_types[field_idx])
				if any(v is None for v in ordered):
					raise AssertionError("missing struct ctor field reached MIR lowering (checker bug)")
				arg_vals = [v for v in ordered if v is not None]
				dest = self.b.new_temp()
				self.b.emit(M.ConstructStruct(dest=dest, struct_ty=struct_ty, args=arg_vals))
				self._local_types[dest] = struct_ty
				return dest
		if not isinstance(expr.fn, H.HVar):
			raise NotImplementedError("Only direct function-name calls are supported in MIR lowering")
		if getattr(expr, "kwargs", None):
			raise AssertionError("keyword arguments reached MIR lowering for a normal call (checker bug)")
		info = self._call_info_for(expr)
		result = self._lower_call(expr)
		if result is None:
			raise AssertionError("Void-returning call used in expression context (checker bug)")
		# Calls to can-throw functions are always "checked": they either produce the
		# ok payload value or propagate an Error into the nearest try (or out of the
		# current function).
		if info.sig.can_throw:
			ok_tid = info.sig.user_ret_type
			def emit_call() -> M.ValueId:
				return result
			return self._lower_can_throw_call_value(emit_call=emit_call, ok_ty=ok_tid)
		return result

	def _visit_expr_HInvoke(self, expr: H.HInvoke) -> M.ValueId:
		if getattr(expr, "kwargs", None):
			raise AssertionError("keyword arguments are not supported for value calls in MIR lowering (checker bug)")
		info = self._call_info_for_invoke(expr)
		result = self._lower_invoke(expr)
		if result is None:
			raise AssertionError("Void-returning call used in expression context (checker bug)")
		if info.sig.can_throw:
			ok_tid = info.sig.user_ret_type
			def emit_call() -> M.ValueId:
				return result
			return self._lower_can_throw_call_value(emit_call=emit_call, ok_ty=ok_tid)
		return result

	def _lambda_can_throw(self, lam: H.HLambda) -> bool:
		"""
		Conservatively detect whether a lambda body can throw.

		This is intentionally conservative: any throw or can-throw call in the
		lambda body marks the hidden lambda as can-throw so we never lower throws
		to `unreachable` in the hidden function.
		"""
		if getattr(lam, "can_throw_effective", None) is not None:
			return bool(getattr(lam, "can_throw_effective"))
		if self._typed_mode == "strict":
			raise AssertionError("lambda missing can_throw_effective (checker bug)")
		def expr_can_throw(expr: H.HExpr) -> bool:
			if isinstance(expr, H.HCall):
				info = self._call_info_for_expr_optional(expr)
				if info is not None and info.sig.can_throw:
					return True
				if isinstance(expr.fn, H.HLambda):
					return self._lambda_can_throw(expr.fn)
				return any(expr_can_throw(a) for a in expr.args)
			if isinstance(expr, H.HMethodCall):
				info = self._call_info_for_expr_optional(expr)
				if info is not None:
					if info.sig.can_throw:
						return True
				else:
					# Conservatively assume unknown method calls can throw, except for
					# built-in, non-throwing intrinsics handled directly in lowering.
					if expr.method_name not in {"as_int", "as_bool", "as_float", "as_string", "as_object", "get", "dup", "iter", "next", "unwrap_ok", "unwrap_err"}:
						return True
				if expr_can_throw(expr.receiver):
					return True
				return any(expr_can_throw(a) for a in expr.args)
			if isinstance(expr, H.HInvoke):
				info = self._call_info_for_expr_optional(expr)
				if info is not None and info.sig.can_throw:
					return True
				if isinstance(expr.callee, H.HLambda):
					return self._lambda_can_throw(expr.callee)
				if expr_can_throw(expr.callee):
					return True
				return any(expr_can_throw(a) for a in expr.args)
			if isinstance(expr, H.HTryExpr):
				if expr_can_throw(expr.attempt):
					return True
				for arm in expr.arms:
					if block_can_throw(arm.block):
						return True
					if arm.result is not None and expr_can_throw(arm.result):
						return True
				return False
			if isinstance(expr, H.HLambda):
				return self._lambda_can_throw(expr)
			if isinstance(expr, H.HResultOk):
				return expr_can_throw(expr.value)
			if isinstance(expr, H.HTernary):
				return (
					expr_can_throw(expr.cond)
					or expr_can_throw(expr.then_expr)
					or expr_can_throw(expr.else_expr)
				)
			if isinstance(expr, H.HUnary):
				return expr_can_throw(expr.expr)
			if isinstance(expr, H.HBinary):
				return expr_can_throw(expr.left) or expr_can_throw(expr.right)
			if isinstance(expr, H.HField):
				return expr_can_throw(expr.subject)
			if isinstance(expr, H.HIndex):
				return expr_can_throw(expr.subject) or expr_can_throw(expr.index)
			if isinstance(expr, H.HPlaceExpr):
				for proj in expr.projections:
					if isinstance(proj, H.HPlaceIndex) and expr_can_throw(proj.index):
						return True
				return False
			if isinstance(expr, H.HArrayLiteral):
				return any(expr_can_throw(el) for el in expr.elements)
			if hasattr(H, "HMapLiteral") and isinstance(expr, getattr(H, "HMapLiteral")):
				return any(expr_can_throw(e.key) or expr_can_throw(e.value) for e in expr.entries)
			if isinstance(expr, H.HDVInit):
				return any(expr_can_throw(a) for a in expr.args)
			return False

		def stmt_can_throw(stmt: H.HStmt) -> bool:
			if isinstance(stmt, H.HThrow) or isinstance(stmt, H.HRethrow):
				return True
			if isinstance(stmt, H.HExprStmt):
				return expr_can_throw(stmt.expr)
			if isinstance(stmt, H.HLet):
				return expr_can_throw(stmt.value)
			if isinstance(stmt, H.HAssign):
				return expr_can_throw(stmt.value)
			if isinstance(stmt, H.HAugAssign):
				return expr_can_throw(stmt.value) or expr_can_throw(stmt.target)
			if isinstance(stmt, H.HReturn):
				return expr_can_throw(stmt.value) if stmt.value is not None else False
			if isinstance(stmt, H.HIf):
				if expr_can_throw(stmt.cond):
					return True
				if block_can_throw(stmt.then_block):
					return True
				return block_can_throw(stmt.else_block) if stmt.else_block is not None else False
			if isinstance(stmt, H.HLoop):
				return block_can_throw(stmt.body)
			if hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
				return block_can_throw(stmt.block)
			if isinstance(stmt, H.HTry):
				if block_can_throw(stmt.body):
					return True
				return any(block_can_throw(arm.block) for arm in stmt.catches)
			return False

		def block_can_throw(block: H.HBlock | None) -> bool:
			if block is None:
				return False
			return any(stmt_can_throw(stmt) for stmt in block.statements)

		if lam.body_expr is not None:
			return expr_can_throw(lam.body_expr)
		if lam.body_block is not None:
			return block_can_throw(lam.body_block)
		return False

	def _lower_lambda_immediate_call(self, lam: H.HLambda, args: list[H.HExpr]) -> M.ValueId:
		"""Lower an immediate-call lambda via env + hidden function."""
		lam = copy.deepcopy(lam)
		if getattr(lam, "explicit_captures", None) is not None:
			explicit_list: list[C.HCapture] = []
			kind_map = {
				"ref": C.HCaptureKind.REF,
				"ref_mut": C.HCaptureKind.REF_MUT,
				"copy": C.HCaptureKind.COPY,
				"move": C.HCaptureKind.MOVE,
				"auto": C.HCaptureKind.REF,
			}
			for cap in lam.explicit_captures or []:
				if cap.binding_id is None:
					continue
				kind = kind_map.get(cap.kind)
				if kind is None:
					continue
				explicit_list.append(C.HCapture(kind=kind, key=C.HCaptureKey(root_local=cap.binding_id, proj=()), span=cap.span))
			lam.captures = explicit_list
		elif not lam.captures:
			discover_captures(lam)
		if lam.captures:
			# Ensure deterministic capture ordering for env layout and slots.
			lam.captures = sort_captures(lam.captures)

		lambda_id = self._lambda_counter
		self._lambda_counter += 1
		mod = self._current_module_name()
		unknown = self._type_table.ensure_unknown()
		can_throw = self._lambda_can_throw(lam)
		declared_ret_type: TypeId | None = None
		if getattr(lam, "ret_type", None) is not None:
			try:
				declared_ret_type = resolve_opaque_type(lam.ret_type, self._type_table, module_id=mod)
			except Exception:
				declared_ret_type = None

		has_captures = bool(lam.captures)
		capture_map: dict[C.HCaptureKey, int] = {cap.key: idx for idx, cap in enumerate(lam.captures)}
		capture_kinds: list[C.HCaptureKind] = [cap.kind for cap in lam.captures]
		env_ty: TypeId | None = None
		env_field_types: list[TypeId] = []
		env_ptr: M.ValueId | None = None
		if has_captures:
			if self._current_fn_id is not None:
				env_name = f"__lambda_env_{self._current_fn_id.name}_{self._current_fn_id.ordinal}_{lambda_id}"
			else:
				env_name = f"__lambda_env_{lambda_id}"
			field_names = [f"c{i}" for i in range(len(lam.captures))]
			env_ty = self._type_table.declare_struct(module_id=mod, name=env_name, field_names=field_names)
			env_local = f"__imm_env_{lambda_id}"
			self.b.ensure_local(env_local)
			env_vals: list[M.ValueId] = []
			for cap in lam.captures:
				expr = self._expr_from_capture_key(cap.key)
				if cap.kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT):
					place = self._place_from_capture_key(cap.key)
					ptr, inner = self._lower_addr_of_place(place, is_mut=cap.kind is C.HCaptureKind.REF_MUT)
					env_vals.append(ptr)
					inner_ty = inner or unknown
					if cap.kind is C.HCaptureKind.REF_MUT:
						env_field_types.append(self._type_table.ensure_ref_mut(inner_ty))
					else:
						env_field_types.append(self._type_table.ensure_ref(inner_ty))
				elif cap.kind is C.HCaptureKind.MOVE:
					place = self._place_from_capture_key(cap.key)
					if cap.key.proj:
						env_val = self.lower_expr(expr)
						env_vals.append(env_val)
						env_field_types.append(self._local_types.get(env_val) or self._infer_capture_type(expr, cap.key) or unknown)
					else:
						cb_moved = self._move_from_callback_capture_slot(cap.key)
						if cb_moved is not None:
							env_vals.append(cb_moved)
							env_field_types.append(self._local_types.get(cb_moved) or self._infer_capture_type(expr, cap.key) or unknown)
							continue
						if not (hasattr(H, "HPlaceExpr") and isinstance(place, getattr(H, "HPlaceExpr"))):
							raise AssertionError("non-canonical move capture place (compiler bug)")
						if place.projections:
							raise AssertionError("move capture of projected place (compiler bug)")
						subj_name = self._canonical_local(getattr(place.base, "binding_id", None), place.base.name)
						self.b.ensure_local(subj_name)
						inner_ty = self._local_types.get(subj_name) or self._infer_expr_type(expr)
						if inner_ty is None:
							raise AssertionError("move capture operand type unknown in MIR lowering (checker bug)")
						moved_val = self.b.new_temp()
						self.b.emit(M.MoveOut(dest=moved_val, local=subj_name, ty=inner_ty))
						self._local_types[moved_val] = inner_ty
						self._moved_locals.add(subj_name)
						env_vals.append(moved_val)
						env_field_types.append(inner_ty)
				else:
					env_val = self.lower_expr(expr)
					env_vals.append(env_val)
					env_field_types.append(self._local_types.get(env_val) or self._infer_capture_type(expr, cap.key) or unknown)
			for idx, val in enumerate(env_vals):
				val_ty = self._local_types.get(val)
				if val_ty is None or idx >= len(env_field_types):
					continue
				if val_ty != env_field_types[idx]:
					env_field_types[idx] = val_ty
			self._type_table.define_struct_fields(env_ty, env_field_types)
			env_val = self.b.new_temp()
			self.b.emit(M.ConstructStruct(dest=env_val, struct_ty=env_ty, args=env_vals))
			self.b.emit(M.StoreLocal(local=env_local, value=env_val))

			env_ptr = self.b.new_temp()
			self.b.emit(M.AddrOfLocal(dest=env_ptr, local=env_local, is_mut=False))
		arg_vals = [self.lower_expr(a) for a in args]

		if self._current_fn_id is not None:
			base_name = self._current_fn_id.name
			base_ord = self._current_fn_id.ordinal
			hidden_name = f"__lambda_{base_name}_{base_ord}_{lambda_id}"
		else:
			base = self.b.func.name.replace("::", "_").replace("#", "_")
			hidden_name = f"__lambda_{base}_{lambda_id}"
		hidden_fn_id = FunctionId(module=mod, name=hidden_name, ordinal=0)
		hidden_symbol = function_symbol(hidden_fn_id)
		lambda_capture_ref_is_value = getattr(lam, "explicit_captures", None) is None
		hidden_env_local = f"__env_{lambda_id}"
		param_type_ids: list[TypeId] = []
		param_names: list[str] = []
		if has_captures:
			param_type_ids.append(self._type_table.ensure_ref(env_ty))
			param_names.append(hidden_env_local)
		for idx, p in enumerate(lam.params):
			param_names.append(p.name)
			ptype = None
			if getattr(p, "type", None) is not None:
				try:
					ptype = resolve_opaque_type(p.type, self._type_table, module_id=mod)
				except Exception:
					ptype = None
			if ptype is None and idx < len(args):
				ptype = self._infer_expr_type(args[idx])
			param_type_ids.append(ptype if ptype is not None else unknown)

		ret_type: TypeId | None = declared_ret_type
		if lam.body_expr is not None:
			if ret_type is None:
				ret_type = self._infer_expr_type(lam.body_expr)
		elif lam.body_block is not None:
			self._seed_lambda_locals_for_inference(self, lam.body_block)
			if lam.body_block.statements:
				last_stmt = lam.body_block.statements[-1]
				if isinstance(last_stmt, H.HExprStmt):
					if ret_type is None:
						ret_type = self._infer_expr_type(last_stmt.expr)
				elif isinstance(last_stmt, H.HReturn) and last_stmt.value is not None:
					if ret_type is None:
						ret_type = self._infer_expr_type(last_stmt.value)
		else:
			raise AssertionError("lambda missing body reached lowering (bug)")
		if ret_type is None:
			ret_type = unknown
		hidden_sig = FnSignature(
			name=hidden_symbol,
			param_type_ids=param_type_ids,
			param_names=param_names,
			return_type_id=ret_type,
			declared_can_throw=can_throw,
			module=mod,
		)
		self._synth_sig_specs.append(SynthSigSpec(hidden_fn_id, hidden_sig, "hidden_lambda"))
		self._hidden_lambda_specs.append(
			HiddenLambdaSpec(
				fn_id=hidden_fn_id,
				origin_fn_id=self._current_fn_id,
				lambda_expr=lam,
				param_names=list(param_names),
				param_type_ids=list(param_type_ids),
				return_type_id=ret_type,
				can_throw=bool(can_throw),
				has_captures=has_captures,
				env_ty=env_ty,
				env_field_types=list(env_field_types),
				capture_map=dict(capture_map),
				capture_kinds=list(capture_kinds),
				lambda_capture_ref_is_value=lambda_capture_ref_is_value,
				is_callback_lambda=False,
			)
		)

		call_args = [env_ptr] + arg_vals if has_captures else arg_vals
		if can_throw:
			ok_ty = ret_type or unknown
			def emit_call() -> M.ValueId:
				dest = self.b.new_temp()
				self.b.emit(M.Call(dest=dest, fn_id=hidden_fn_id, args=call_args, can_throw=True))
				return dest
			return self._lower_can_throw_call_value(emit_call=emit_call, ok_ty=ok_ty)
		dest = self.b.new_temp()
		self.b.emit(M.Call(dest=dest, fn_id=hidden_fn_id, args=call_args, can_throw=False))
		return dest

	def _lower_lambda_callback(
		self,
		lam: H.HLambda,
		*,
		param_types: list[TypeId],
		ret_type: TypeId,
		can_throw: bool,
	) -> tuple[FunctionRefId, M.ValueId | None, TypeId | None]:
		"""Lower a lambda into a callback thunk + optional heap env."""
		if getattr(lam, "explicit_captures", None) is not None:
			explicit_list: list[C.HCapture] = []
			kind_map = {
				"ref": C.HCaptureKind.REF,
				"ref_mut": C.HCaptureKind.REF_MUT,
				"copy": C.HCaptureKind.COPY,
				"move": C.HCaptureKind.MOVE,
				"auto": C.HCaptureKind.REF,
			}
			for cap in lam.explicit_captures or []:
				if cap.binding_id is None:
					continue
				kind = kind_map.get(cap.kind)
				if kind is None:
					continue
				explicit_list.append(C.HCapture(kind=kind, key=C.HCaptureKey(root_local=cap.binding_id, proj=()), span=cap.span))
			lam.captures = explicit_list
		elif not lam.captures:
			discover_captures(lam)
		if lam.captures:
			lam.captures = sort_captures(lam.captures)
		if any(cap.kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT) for cap in (lam.captures or [])):
			raise AssertionError("borrowed capture in owned callback env (compiler bug)")

		lambda_id = self._lambda_counter
		self._lambda_counter += 1
		mod = self._current_module_name()
		unknown = self._type_table.ensure_unknown()

		has_captures = bool(lam.captures)
		capture_map: dict[C.HCaptureKey, int] = {cap.key: idx for idx, cap in enumerate(lam.captures)}
		capture_kinds: list[C.HCaptureKind] = [cap.kind for cap in lam.captures]
		env_ty: TypeId | None = None
		env_field_types: list[TypeId] = []
		env_ptr: M.ValueId | None = None

		if has_captures:
			if self._current_fn_id is not None:
				env_name = f"__lambda_env_cb_{self._current_fn_id.name}_{self._current_fn_id.ordinal}_{lambda_id}"
			else:
				env_name = f"__lambda_env_cb_{lambda_id}"
			field_names = [f"c{i}" for i in range(len(lam.captures))]
			env_ty = self._type_table.declare_struct(module_id=mod, name=env_name, field_names=field_names)
			env_vals: list[M.ValueId] = []
			for cap in lam.captures:
				expr = self._expr_from_capture_key(cap.key)
				if cap.kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT):
					place = self._place_from_capture_key(cap.key)
					ptr, inner = self._lower_addr_of_place(place, is_mut=cap.kind is C.HCaptureKind.REF_MUT)
					env_vals.append(ptr)
					inner_ty = inner or unknown
					if cap.kind is C.HCaptureKind.REF_MUT:
						env_field_types.append(self._type_table.ensure_ref_mut(inner_ty))
					else:
						env_field_types.append(self._type_table.ensure_ref(inner_ty))
				elif cap.kind is C.HCaptureKind.MOVE:
					place = self._place_from_capture_key(cap.key)
					if cap.key.proj:
						env_val = self.lower_expr(expr)
						env_vals.append(env_val)
						env_field_types.append(self._local_types.get(env_val) or self._infer_capture_type(expr, cap.key) or unknown)
					else:
						cb_moved = self._move_from_callback_capture_slot(cap.key)
						if cb_moved is not None:
							env_vals.append(cb_moved)
							env_field_types.append(self._local_types.get(cb_moved) or self._infer_capture_type(expr, cap.key) or unknown)
							continue
						if not (hasattr(H, "HPlaceExpr") and isinstance(place, getattr(H, "HPlaceExpr"))):
							raise AssertionError("non-canonical move capture place (compiler bug)")
						if place.projections:
							raise AssertionError("move capture of projected place (compiler bug)")
						subj_name = self._canonical_local(getattr(place.base, "binding_id", None), place.base.name)
						self.b.ensure_local(subj_name)
						inner_ty = self._local_types.get(subj_name) or self._infer_expr_type(expr)
						if inner_ty is None:
							raise AssertionError("move capture operand type unknown in MIR lowering (checker bug)")
						moved_val = self.b.new_temp()
						self.b.emit(M.MoveOut(dest=moved_val, local=subj_name, ty=inner_ty))
						self._local_types[moved_val] = inner_ty
						self._moved_locals.add(subj_name)
						env_vals.append(moved_val)
						env_field_types.append(inner_ty)
				else:
					env_val = self.lower_expr(expr)
					env_vals.append(env_val)
					env_field_types.append(self._local_types.get(env_val) or self._infer_capture_type(expr, cap.key) or unknown)
			for idx, val in enumerate(env_vals):
				val_ty = self._local_types.get(val)
				if val_ty is None or idx >= len(env_field_types):
					continue
				if val_ty != env_field_types[idx]:
					env_field_types[idx] = val_ty
			self._type_table.define_struct_fields(env_ty, env_field_types)
			env_val = self.b.new_temp()
			self.b.emit(M.ConstructStruct(dest=env_val, struct_ty=env_ty, args=env_vals))

			raw_base = self._type_table.get_struct_base(module_id="std.mem", name="RawBuffer")
			if raw_base is None:
				raise AssertionError("RawBuffer type missing while lowering callback env (compiler bug)")
			raw_ty = self._type_table.ensure_struct_instantiated(raw_base, [env_ty])
			raw_buf = self.b.new_temp()
			cap = self._const_int(1)
			self.b.emit(M.RawBufferAlloc(dest=raw_buf, raw_ty=raw_ty, elem_ty=env_ty, cap=cap))
			idx = self._const_int(0)
			env_ptr = self.b.new_temp()
			self.b.emit(M.RawBufferPtrAt(dest=env_ptr, buffer=raw_buf, raw_ty=raw_ty, elem_ty=env_ty, index=idx))
			self.b.emit(M.StoreRef(ptr=env_ptr, value=env_val, inner_ty=env_ty))

		if self._current_fn_id is not None:
			base_name = self._current_fn_id.name
			base_ord = self._current_fn_id.ordinal
			hidden_name = f"__lambda_cb_{base_name}_{base_ord}_{lambda_id}"
		else:
			base = self.b.func.name.replace("::", "_").replace("#", "_")
			hidden_name = f"__lambda_cb_{base}_{lambda_id}"
		hidden_fn_id = FunctionId(module=mod, name=hidden_name, ordinal=0)
		hidden_symbol = function_symbol(hidden_fn_id)
		lambda_capture_ref_is_value = getattr(lam, "explicit_captures", None) is None
		hidden_env_local = f"__env_{lambda_id}"
		param_type_ids: list[TypeId] = []
		param_names: list[str] = []
		if has_captures:
			param_type_ids.append(self._type_table.ensure_ref(env_ty))
			param_names.append(hidden_env_local)
		for idx, p in enumerate(lam.params):
			param_names.append(p.name)
			ptype = param_types[idx] if idx < len(param_types) else unknown
			param_type_ids.append(ptype)

		hidden_sig = FnSignature(
			name=hidden_symbol,
			param_type_ids=param_type_ids,
			param_names=param_names,
			return_type_id=ret_type,
			declared_can_throw=can_throw,
			module=mod,
		)
		self._synth_sig_specs.append(SynthSigSpec(hidden_fn_id, hidden_sig, "hidden_lambda"))
		self._hidden_lambda_specs.append(
			HiddenLambdaSpec(
				fn_id=hidden_fn_id,
				origin_fn_id=self._current_fn_id,
				lambda_expr=lam,
				param_names=list(param_names),
				param_type_ids=list(param_type_ids),
				return_type_id=ret_type,
				can_throw=bool(can_throw),
				has_captures=has_captures,
				env_ty=env_ty,
				env_field_types=list(env_field_types),
				capture_map=dict(capture_map),
				capture_kinds=list(capture_kinds),
				lambda_capture_ref_is_value=lambda_capture_ref_is_value,
				is_callback_lambda=True,
			)
		)
		fn_ref = FunctionRefId(fn_id=hidden_fn_id, kind=FunctionRefKind.IMPL, has_wrapper=False)
		return fn_ref, env_ptr, env_ty

	def _lower_lambda_block(self, lower: "HIRToMIR", block: H.HBlock) -> M.ValueId | None:
		"""
		Lower a lambda block body. If the final statement is an ExprStmt, return its value.
		"""
		if not block.statements:
			return None
		lower._push_scope(include_params=True)
		lower._emit_lambda_capture_prologue()
		*prefix, last = block.statements
		for stmt in prefix:
			lower.lower_stmt(stmt)
			if lower.b.block.terminator is not None:
				lower._pop_scope()
				return None
		if isinstance(last, H.HExprStmt):
			val = lower.lower_expr(last.expr)
			if lower.b.block.terminator is None:
				lower._emit_scope_drops(scope_index=len(lower._scope_stack) - 1)
			lower._pop_scope()
			return val
		lower.lower_stmt(last)
		if lower.b.block.terminator is None:
			lower._emit_scope_drops(scope_index=len(lower._scope_stack) - 1)
		lower._pop_scope()
		return None

	def _emit_lambda_capture_prologue(self) -> None:
		if getattr(self, "_lambda_capture_prologue_done", False):
			return
		if self._lambda_capture_slots is None:
			return
		if self._lambda_env_local is None or self._lambda_env_ty is None or self._lambda_env_field_types is None:
			return
		for key, slot in sorted(self._lambda_capture_slots.items(), key=lambda item: item[1]):
			bid = int(key.root_local)
			name = self._binding_names.get(bid, f"__b{bid}")
			local_name = self._canonical_local(bid, name)
			self.b.ensure_local(local_name)
			local_ty = self._local_types.get(local_name)
			if local_ty is None and slot < len(self._lambda_env_field_types):
				local_ty = self._lambda_env_field_types[slot]
				if local_ty is self._unknown_type and self._lambda_env_ty is not None:
					inst = self._type_table.get_struct_instance(self._lambda_env_ty)
					if inst is None:
						schema = self._type_table.get_struct_schema(self._lambda_env_ty)
						if schema is not None and not schema.type_params:
							inst_id = self._type_table.ensure_struct_instantiated(self._lambda_env_ty, [])
							inst = self._type_table.get_struct_instance(inst_id)
					if inst is not None and slot < len(inst.field_types):
						inst_ty = inst.field_types[slot]
						if inst_ty is not self._unknown_type:
							local_ty = inst_ty
				if self._lambda_capture_kinds is not None and slot < len(self._lambda_capture_kinds):
					kind = self._lambda_capture_kinds[slot]
					if kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT) and self._lambda_capture_ref_is_value:
						td = self._type_table.get(local_ty)
						if td.kind is TypeKind.REF and td.param_types:
							local_ty = td.param_types[0]
				self._local_types[local_name] = local_ty
			if local_ty is None:
				local_ty = self._binding_types.get(bid)
				if local_ty is not None:
					self._local_types[local_name] = local_ty
			kind = None
			if self._lambda_capture_kinds is not None and slot < len(self._lambda_capture_kinds):
				kind = self._lambda_capture_kinds[slot]
			if self._lambda_is_callback and kind is C.HCaptureKind.MOVE:
				# Escaping callback lambdas own move captures in heap env storage.
				# Avoid materializing per-invocation locals from env values.
				continue
			val = self._load_capture_from_env(slot)
			self.b.emit(M.StoreLocal(local=local_name, value=val))
			if local_ty is not None:
				# Move-captured values remain owned by the lambda env object and are
				# released by the env drop thunk; registering an additional local drop
				# here double-drops non-Copy captures.
				if kind is not C.HCaptureKind.MOVE:
					self._register_drop_local(local_name, local_ty)
		self._lambda_capture_prologue_done = True

	def _seed_lambda_locals_for_inference(self, lower: "HIRToMIR", block: H.HBlock) -> None:
		"""Seed declared local types for lambda return-type inference."""
		for stmt in block.statements:
			if isinstance(stmt, H.HLet) and getattr(stmt, "declared_type_expr", None) is not None:
				try:
					decl_ty = resolve_opaque_type(
						stmt.declared_type_expr,
						self._type_table,
						module_id=self._current_module_name(),
					)
				except Exception:
					decl_ty = None
				if decl_ty is not None:
					local_name = lower._canonical_local(getattr(stmt, "binding_id", None), stmt.name)
					lower._local_types[local_name] = decl_ty

	def _visit_expr_HMethodCall(self, expr: H.HMethodCall) -> M.ValueId:
		if getattr(expr, "kwargs", None):
			raise AssertionError("keyword arguments for method calls are not supported in MIR lowering (checker bug)")
		if expr.method_name == "dup" and not expr.args:
			recv_ty = self._infer_expr_type(expr.receiver)
			if recv_ty is not None:
				recv_def = self._type_table.get(recv_ty)
				recv_val = self.lower_expr(expr.receiver)
				if recv_def.kind is TypeKind.REF and recv_def.param_types:
					inner_ty = recv_def.param_types[0]
					tmp = self.b.new_temp()
					self.b.emit(M.LoadRef(dest=tmp, ptr=recv_val, inner_ty=inner_ty))
					recv_val = tmp
					recv_ty = inner_ty
					recv_def = self._type_table.get(recv_ty)
				if recv_def.kind is TypeKind.ARRAY and recv_def.param_types:
					elem_ty = recv_def.param_types[0]
					dest = self.b.new_temp()
					self.b.emit(M.ArrayDup(dest=dest, elem_ty=elem_ty, array=recv_val))
					self._local_types[dest] = recv_ty
					return dest
		handled, value = self._lower_array_intrinsic_method(expr, want_value=True)
		if handled:
			if value is None:
				raise AssertionError("Void array method used in expression context (checker bug)")
			return value
		# FnResult intrinsic methods (`is_err`/`unwrap`/`unwrap_err`) lower to
		# dedicated MIR ops so later stages don't need ad-hoc method dispatch.
		if expr.method_name in ("is_err", "unwrap", "unwrap_err") and not expr.args:
			res_val = self.lower_expr(expr.receiver)
			dest = self.b.new_temp()
			if expr.method_name == "is_err":
				self.b.emit(M.ResultIsErr(dest=dest, result=res_val))
				self._local_types[dest] = self._bool_type
				return dest
			if expr.method_name == "unwrap":
				self.b.emit(M.ResultOk(dest=dest, result=res_val))
				# Ok payload type is derived later by SSA/type env; leave unknown here.
				return dest
			if expr.method_name == "unwrap_err":
				self.b.emit(M.ResultErr(dest=dest, result=res_val))
				self._local_types[dest] = self._type_table.ensure_error()
				return dest
		if expr.method_name in ("as_int", "as_bool", "as_float", "as_string", "as_object", "get"):
			recv_ty = self._infer_expr_type(expr.receiver)
			if recv_ty is None:
				recv_ty = self._expr_types.get(expr.receiver.node_id) if self._expr_types else None
			if recv_ty is not None:
				recv_def = self._type_table.get(recv_ty)
				if recv_def.kind is not TypeKind.DIAGNOSTICVALUE:
					recv_ty = None
			if recv_ty is None:
				result, info = self._lower_method_call(expr)
				if result is None:
					if self._type_table.is_void(info.sig.user_ret_type):
						return self._void_value()
					raise AssertionError("Void-returning method call used in expression context (checker bug)")
				if info.sig.can_throw:
					ok_tid = info.sig.user_ret_type
					def emit_call() -> M.ValueId:
						return result
					return self._lower_can_throw_call_value(emit_call=emit_call, ok_ty=ok_tid)
				return result
			if expr.method_name != "get" and expr.args:
				raise NotImplementedError(f"{expr.method_name} takes no arguments")
			if expr.method_name == "get" and len(expr.args) != 1:
				raise NotImplementedError("get takes exactly one key argument")
			dv_val = self.lower_expr(expr.receiver)
			dest = self.b.new_temp()
			if expr.method_name == "as_int":
				self.b.emit(M.DVAsInt(dest=dest, dv=dv_val))
				self._local_types[dest] = self._optional_variant_type(self._int_type)
				return dest
			if expr.method_name == "as_bool":
				self.b.emit(M.DVAsBool(dest=dest, dv=dv_val))
				self._local_types[dest] = self._optional_variant_type(self._bool_type)
				return dest
			if expr.method_name == "as_float":
				self.b.emit(M.DVAsFloat(dest=dest, dv=dv_val))
				self._local_types[dest] = self._optional_variant_type(self._float_type)
				return dest
			if expr.method_name == "as_string":
				self.b.emit(M.DVAsString(dest=dest, dv=dv_val))
				self._local_types[dest] = self._optional_variant_type(self._string_type)
				return dest
			if expr.method_name == "as_object":
				self.b.emit(M.DVAsObject(dest=dest, dv=dv_val))
				self._local_types[dest] = self._optional_variant_type(self._dv_type)
				return dest
			if expr.method_name == "get":
				key_val = self.lower_expr(expr.args[0])
				self.b.emit(M.DVGetField(dest=dest, dv=dv_val, key=key_val))
				self._local_types[dest] = self._optional_variant_type(self._dv_type)
				return dest
		result, info = self._lower_method_call(expr)
		if result is None:
			if self._type_table.is_void(info.sig.user_ret_type):
				return self._void_value()
			raise AssertionError("Void-returning method call used in expression context (checker bug)")
		if info.sig.can_throw:
			ok_tid = info.sig.user_ret_type
			def emit_call() -> M.ValueId:
				return result
			return self._lower_can_throw_call_value(emit_call=emit_call, ok_ty=ok_tid)
		return result

	def _optional_variant_type(self, inner_ty: TypeId) -> TypeId:
		opt_base = self._type_table.ensure_optional_base()
		return self._type_table.ensure_instantiated(opt_base, [inner_ty])

	def _emit_optional_none(self, opt_ty: TypeId) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstructVariant(dest=dest, variant_ty=opt_ty, ctor="None", args=[]))
		self._local_types[dest] = opt_ty
		return dest

	def _emit_optional_some(self, opt_ty: TypeId, value: M.ValueId) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstructVariant(dest=dest, variant_ty=opt_ty, ctor="Some", args=[value]))
		self._local_types[dest] = opt_ty
		return dest

	def _const_int(self, value: int) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstInt(dest=dest, value=value))
		return dest

	def _const_bool(self, value: bool) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstBool(dest=dest, value=value))
		return dest

	def _void_value(self) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstVoid(dest=dest))
		self._local_types[dest] = self._void_type
		return dest

	def _addr_taken_local(self, name_hint: str, ty: TypeId, init_value: M.ValueId) -> str:
		"""
		Create a local and take its address so SSA leaves it as storage.

		This avoids invalid φ nodes for helper locals that do not need SSA
		renaming (e.g., loop indices in intrinsic lowering).
		"""
		local = f"{name_hint}{self.b.new_temp()}"
		self.b.ensure_local(local)
		self._local_types[local] = ty
		self.b.emit(M.StoreLocal(local=local, value=init_value))
		tmp = self.b.new_temp()
		self.b.emit(M.AddrOfLocal(dest=tmp, local=local, is_mut=True))
		return local

	def _array_index_load_value(self, *, elem_ty: TypeId, array: M.ValueId, index: M.ValueId) -> M.ValueId:
		raw = self.b.new_temp()
		self.b.emit(M.ArrayIndexLoad(dest=raw, elem_ty=elem_ty, array=array, index=index))
		copy_status = self._type_table.copy_status(elem_ty)
		if copy_status is True and not self._type_table.is_bitcopy(elem_ty):
			copied = self.b.new_temp()
			self.b.emit(M.CopyValue(dest=copied, value=raw, ty=elem_ty))
			return copied
		return raw

	def _array_elem_take_value(self, *, elem_ty: TypeId, array: M.ValueId, index: M.ValueId) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ArrayElemTake(dest=dest, elem_ty=elem_ty, array=array, index=index))
		self._local_types[dest] = elem_ty
		return dest

	def _lower_array_intrinsic_method(
		self,
		expr: H.HMethodCall,
		*,
		want_value: bool,
	) -> tuple[bool, M.ValueId | None]:
		name = expr.method_name
		if name not in (
			"push",
			"pop",
			"insert",
			"remove",
			"swap_remove",
			"swap",
			"clear",
			"reserve",
			"shrink_to_fit",
			"get",
			"ref_at",
			"set",
			"range",
			"range_mut",
		):
			return False, None

		recv_ty = self._infer_expr_type(expr.receiver)
		if recv_ty is None:
			raise AssertionError("array method receiver type unknown in MIR lowering (checker bug)")
		recv_def = self._type_table.get(recv_ty)
		array_ty = recv_ty
		recv_ptr: M.ValueId | None = None
		recv_is_mut = name not in ("get", "range")
		if name == "range_mut":
			recv_is_mut = True
		if name == "range":
			recv_is_mut = False
		if recv_def.kind is TypeKind.REF and recv_def.param_types:
			array_ty = recv_def.param_types[0]
			recv_ptr = self.lower_expr(expr.receiver)
		else:
			place_expr = place_expr_from_lvalue_expr(expr.receiver)
			if place_expr is None:
				raise NotImplementedError("Array method requires an lvalue receiver in MVP")
			recv_ptr, _inner = self._lower_addr_of_place(
				place_expr,
				is_mut=recv_is_mut,
			)
		if recv_ptr is None:
			raise AssertionError("array method missing receiver address (checker bug)")
		array_def = self._type_table.get(array_ty)
		if array_def.kind is not TypeKind.ARRAY or not array_def.param_types:
			return False, None
		elem_ty = array_def.param_types[0]

		array_val = self.b.new_temp()
		self.b.emit(M.LoadRef(dest=array_val, ptr=recv_ptr, inner_ty=array_ty))

		def _next_gen(array_in: M.ValueId) -> M.ValueId:
			gen_val = self.b.new_temp()
			self.b.emit(M.ArrayGen(dest=gen_val, array=array_in))
			one = self._const_int(1)
			next_val = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=next_val, op=H.BinaryOp.ADD, left=gen_val, right=one))
			return next_val

		def _set_gen(array_in: M.ValueId, gen_val: M.ValueId) -> M.ValueId:
			out = self.b.new_temp()
			self.b.emit(M.ArraySetGen(dest=out, array=array_in, gen=gen_val))
			return out

		if name in ("range", "range_mut"):
			if not want_value:
				return True, None
			ret_ty = self._infer_expr_type(expr)
			if ret_ty is None:
				raise AssertionError("array range type unknown in MIR lowering (checker bug)")
			gen_val = self.b.new_temp()
			self.b.emit(M.ArrayGen(dest=gen_val, array=array_val))
			dest = self.b.new_temp()
			self.b.emit(M.ConstructStruct(dest=dest, struct_ty=ret_ty, args=[recv_ptr, gen_val]))
			self._local_types[dest] = ret_ty
			return True, dest

		if name == "get":
			if not want_value:
				return True, None
			if len(expr.args) != 1:
				raise AssertionError("Array.get arity mismatch reached MIR lowering (checker bug)")
			idx_val = self.lower_expr(expr.args[0], expected_type=self._int_type)
			len_val = self.b.new_temp()
			self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
			zero = self._const_int(0)

			opt_ty = self._optional_variant_type(self._type_table.ensure_ref(elem_ty))
			res_local = f"__array_get_res{self.b.new_temp()}"
			self.b.ensure_local(res_local)
			self._local_types[res_local] = opt_ty

			neg_cond = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=neg_cond, op=H.BinaryOp.LT, left=idx_val, right=zero))
			neg_block = self.b.new_block("array_get_neg")
			check_block = self.b.new_block("array_get_check")
			join_block = self.b.new_block("array_get_join")
			self.b.set_terminator(
				M.IfTerminator(cond=neg_cond, then_target=neg_block.name, else_target=check_block.name)
			)

			self.b.set_block(neg_block)
			none_val = self._emit_optional_none(opt_ty)
			self.b.emit(M.StoreLocal(local=res_local, value=none_val))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(check_block)
			lt_cond = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=lt_cond, op=H.BinaryOp.LT, left=idx_val, right=len_val))
			ok_block = self.b.new_block("array_get_ok")
			bad_block = self.b.new_block("array_get_bad")
			self.b.set_terminator(
				M.IfTerminator(cond=lt_cond, then_target=ok_block.name, else_target=bad_block.name)
			)

			self.b.set_block(ok_block)
			ptr = self.b.new_temp()
			self.b.emit(
				M.AddrOfArrayElem(
					dest=ptr,
					array=array_val,
					index=idx_val,
					inner_ty=elem_ty,
					is_mut=False,
				)
			)
			some_val = self._emit_optional_some(opt_ty, ptr)
			self.b.emit(M.StoreLocal(local=res_local, value=some_val))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(bad_block)
			none_val = self._emit_optional_none(opt_ty)
			self.b.emit(M.StoreLocal(local=res_local, value=none_val))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(join_block)
			out = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=out, local=res_local))
			return True, out

		if name == "ref_at":
			if not want_value:
				return True, None
			if len(expr.args) != 1:
				raise AssertionError("Array.ref_at arity mismatch reached MIR lowering (checker bug)")
			idx_val = self.lower_expr(expr.args[0], expected_type=self._int_type)
			ptr = self.b.new_temp()
			self.b.emit(
				M.AddrOfArrayElem(
					dest=ptr,
					array=array_val,
					index=idx_val,
					inner_ty=elem_ty,
					is_mut=False,
				)
			)
			self._local_types[ptr] = self._type_table.ensure_ref(elem_ty)
			return True, ptr

		if name == "pop":
			if len(expr.args) != 0:
				raise AssertionError("Array.pop arity mismatch reached MIR lowering (checker bug)")
			if not want_value:
				return True, None
			opt_ty = self._optional_variant_type(elem_ty)
			res_local = f"__array_pop_res{self.b.new_temp()}"
			self.b.ensure_local(res_local)
			self._local_types[res_local] = opt_ty

			len_val = self.b.new_temp()
			self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
			zero = self._const_int(0)
			is_empty = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=is_empty, op=H.BinaryOp.EQ, left=len_val, right=zero))
			empty_block = self.b.new_block("array_pop_empty")
			ok_block = self.b.new_block("array_pop_ok")
			join_block = self.b.new_block("array_pop_join")
			self.b.set_terminator(
				M.IfTerminator(cond=is_empty, then_target=empty_block.name, else_target=ok_block.name)
			)

			self.b.set_block(empty_block)
			none_val = self._emit_optional_none(opt_ty)
			self.b.emit(M.StoreLocal(local=res_local, value=none_val))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(ok_block)
			one = self._const_int(1)
			last_idx = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=last_idx, op=H.BinaryOp.SUB, left=len_val, right=one))
			val = self._array_elem_take_value(elem_ty=elem_ty, array=array_val, index=last_idx)
			new_len = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=new_len, op=H.BinaryOp.SUB, left=len_val, right=one))
			new_arr = self.b.new_temp()
			self.b.emit(M.ArraySetLen(dest=new_arr, array=array_val, length=new_len))
			next_gen = _next_gen(array_val)
			new_arr_gen = _set_gen(new_arr, next_gen)
			self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr_gen, inner_ty=array_ty))
			some_val = self._emit_optional_some(opt_ty, val)
			self.b.emit(M.StoreLocal(local=res_local, value=some_val))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(join_block)
			out = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=out, local=res_local))
			return True, out

		def grow_array(array_in: M.ValueId, *, len_val: M.ValueId, cap_val: M.ValueId, need_val: M.ValueId) -> M.ValueId:
			two = self._const_int(2)
			cap_x2 = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=cap_x2, op=H.BinaryOp.MUL, left=cap_val, right=two))
			use_need = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=use_need, op=H.BinaryOp.LT, left=cap_x2, right=need_val))
			new_cap_local = self._addr_taken_local("__array_new_cap", self._int_type, self._const_int(0))
			cap_block = self.b.new_block("array_cap_x2")
			need_block = self.b.new_block("array_cap_need")
			join_block = self.b.new_block("array_cap_join")
			self.b.set_terminator(
				M.IfTerminator(cond=use_need, then_target=need_block.name, else_target=cap_block.name)
			)

			self.b.set_block(cap_block)
			self.b.emit(M.StoreLocal(local=new_cap_local, value=cap_x2))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(need_block)
			self.b.emit(M.StoreLocal(local=new_cap_local, value=need_val))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(join_block)
			new_cap = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=new_cap, local=new_cap_local))

			zero = self._const_int(0)
			new_arr = self.b.new_temp()
			self.b.emit(M.ArrayAlloc(dest=new_arr, elem_ty=elem_ty, length=zero, cap=new_cap))

			idx_local = self._addr_taken_local("__array_copy_i", self._int_type, self._const_int(0))
			self.b.emit(M.StoreLocal(local=idx_local, value=zero))

			cond_block = self.b.new_block("array_copy_cond")
			body_block = self.b.new_block("array_copy_body")
			exit_block = self.b.new_block("array_copy_exit")
			self.b.set_terminator(M.Goto(target=cond_block.name))

			self.b.set_block(cond_block)
			cur = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=cur, local=idx_local))
			lt = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=lt, op=H.BinaryOp.LT, left=cur, right=len_val))
			self.b.set_terminator(M.IfTerminator(cond=lt, then_target=body_block.name, else_target=exit_block.name))

			self.b.set_block(body_block)
			val = self._array_elem_take_value(elem_ty=elem_ty, array=array_in, index=cur)
			self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=new_arr, index=cur, value=val))
			next_i = self.b.new_temp()
			one = self._const_int(1)
			self.b.emit(M.BinaryOpInstr(dest=next_i, op=H.BinaryOp.ADD, left=cur, right=one))
			self.b.emit(M.StoreLocal(local=idx_local, value=next_i))
			self.b.set_terminator(M.Goto(target=cond_block.name))

			self.b.set_block(exit_block)
			new_arr_len = self.b.new_temp()
			self.b.emit(M.ArraySetLen(dest=new_arr_len, array=new_arr, length=len_val))
			old_zero = self.b.new_temp()
			self.b.emit(M.ArraySetLen(dest=old_zero, array=array_in, length=zero))
			self.b.emit(M.ArrayDrop(elem_ty=elem_ty, array=old_zero))
			return new_arr_len

		def shrink_array(array_in: M.ValueId, *, len_val: M.ValueId) -> M.ValueId:
			zero = self._const_int(0)
			new_arr = self.b.new_temp()
			self.b.emit(M.ArrayAlloc(dest=new_arr, elem_ty=elem_ty, length=zero, cap=len_val))

			idx_local = self._addr_taken_local("__array_shrink_i", self._int_type, self._const_int(0))
			self.b.emit(M.StoreLocal(local=idx_local, value=zero))

			cond_block = self.b.new_block("array_shrink_cond")
			body_block = self.b.new_block("array_shrink_body")
			exit_block = self.b.new_block("array_shrink_exit")
			self.b.set_terminator(M.Goto(target=cond_block.name))

			self.b.set_block(cond_block)
			cur = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=cur, local=idx_local))
			lt = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=lt, op=H.BinaryOp.LT, left=cur, right=len_val))
			self.b.set_terminator(M.IfTerminator(cond=lt, then_target=body_block.name, else_target=exit_block.name))

			self.b.set_block(body_block)
			val = self._array_elem_take_value(elem_ty=elem_ty, array=array_in, index=cur)
			self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=new_arr, index=cur, value=val))
			next_i = self.b.new_temp()
			one = self._const_int(1)
			self.b.emit(M.BinaryOpInstr(dest=next_i, op=H.BinaryOp.ADD, left=cur, right=one))
			self.b.emit(M.StoreLocal(local=idx_local, value=next_i))
			self.b.set_terminator(M.Goto(target=cond_block.name))

			self.b.set_block(exit_block)
			new_arr_len = self.b.new_temp()
			self.b.emit(M.ArraySetLen(dest=new_arr_len, array=new_arr, length=len_val))
			old_zero = self.b.new_temp()
			self.b.emit(M.ArraySetLen(dest=old_zero, array=array_in, length=zero))
			self.b.emit(M.ArrayDrop(elem_ty=elem_ty, array=old_zero))
			return new_arr_len

		def ensure_capacity(array_in: M.ValueId, *, len_val: M.ValueId, cap_val: M.ValueId, extra: M.ValueId) -> tuple[M.ValueId, M.ValueId]:
			need = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=need, op=H.BinaryOp.ADD, left=len_val, right=extra))
			# If len < cap and need <= cap, reuse. Otherwise grow.
			need_ok = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=need_ok, op=H.BinaryOp.LE, left=need, right=cap_val))
			ok_block = self.b.new_block("array_cap_ok")
			grow_block = self.b.new_block("array_cap_grow")
			join_block = self.b.new_block("array_cap_join2")
			arr_local = f"__array_cap_arr{self.b.new_temp()}"
			self.b.ensure_local(arr_local)
			self._local_types[arr_local] = array_ty
			grew_local = f"__array_cap_grew{self.b.new_temp()}"
			self.b.ensure_local(grew_local)
			self._local_types[grew_local] = self._bool_type
			self.b.set_terminator(
				M.IfTerminator(cond=need_ok, then_target=ok_block.name, else_target=grow_block.name)
			)

			self.b.set_block(ok_block)
			self.b.emit(M.StoreLocal(local=arr_local, value=array_in))
			grew_false = self.b.new_temp()
			self.b.emit(M.ConstBool(dest=grew_false, value=False))
			self.b.emit(M.StoreLocal(local=grew_local, value=grew_false))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(grow_block)
			new_arr = grow_array(array_in, len_val=len_val, cap_val=cap_val, need_val=need)
			self.b.emit(M.StoreLocal(local=arr_local, value=new_arr))
			grew_true = self.b.new_temp()
			self.b.emit(M.ConstBool(dest=grew_true, value=True))
			self.b.emit(M.StoreLocal(local=grew_local, value=grew_true))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(join_block)
			out = self.b.new_temp()
			self.b.emit(M.MoveOut(dest=out, local=arr_local, ty=array_ty))
			grew = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=grew, local=grew_local))
			return out, grew

		if name in ("push", "insert"):
			if name == "push" and len(expr.args) != 1:
				raise AssertionError("Array.push arity mismatch reached MIR lowering (checker bug)")
			if name == "insert" and len(expr.args) != 2:
				raise AssertionError("Array.insert arity mismatch reached MIR lowering (checker bug)")
			val_arg = expr.args[-1]
			val = self.lower_expr(val_arg, expected_type=elem_ty)
			len_val = self.b.new_temp()
			self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
			cap_val = self.b.new_temp()
			self.b.emit(M.ArrayCap(dest=cap_val, array=array_val))
			next_gen = _next_gen(array_val)
			one = self._const_int(1)
			array_val2, _grew = ensure_capacity(array_val, len_val=len_val, cap_val=cap_val, extra=one)
			array_val = array_val2
			if name == "push":
				self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=array_val, index=len_val, value=val))
				new_len = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=new_len, op=H.BinaryOp.ADD, left=len_val, right=one))
				new_arr = self.b.new_temp()
				self.b.emit(M.ArraySetLen(dest=new_arr, array=array_val, length=new_len))
				new_arr_gen = _set_gen(new_arr, next_gen)
				self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr_gen, inner_ty=array_ty))
				return True, None
			# insert
			idx_val = self.lower_expr(expr.args[0], expected_type=self._int_type)
			eq_len = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=eq_len, op=H.BinaryOp.EQ, left=idx_val, right=len_val))
			push_block = self.b.new_block("array_insert_push")
			shift_block = self.b.new_block("array_insert_shift")
			join_block = self.b.new_block("array_insert_join")
			self.b.set_terminator(
				M.IfTerminator(cond=eq_len, then_target=push_block.name, else_target=shift_block.name)
			)

			self.b.set_block(push_block)
			self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=array_val, index=len_val, value=val))
			new_len = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=new_len, op=H.BinaryOp.ADD, left=len_val, right=one))
			new_arr = self.b.new_temp()
			self.b.emit(M.ArraySetLen(dest=new_arr, array=array_val, length=new_len))
			new_arr_gen = _set_gen(new_arr, next_gen)
			self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr_gen, inner_ty=array_ty))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(shift_block)
			# Bounds-check: index must be < len.
			tmp_ptr = self.b.new_temp()
			self.b.emit(
				M.AddrOfArrayElem(
					dest=tmp_ptr,
					array=array_val,
					index=idx_val,
					inner_ty=elem_ty,
					is_mut=True,
				)
			)
			last_idx = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=last_idx, op=H.BinaryOp.SUB, left=len_val, right=one))
			idx_local = self._addr_taken_local("__array_ins_i", self._int_type, self._const_int(0))
			self.b.emit(M.StoreLocal(local=idx_local, value=last_idx))

			cond_block = self.b.new_block("array_insert_cond")
			body_block = self.b.new_block("array_insert_body")
			exit_block = self.b.new_block("array_insert_exit")
			self.b.set_terminator(M.Goto(target=cond_block.name))

			self.b.set_block(cond_block)
			cur = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=cur, local=idx_local))
			ge = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=ge, op=H.BinaryOp.GE, left=cur, right=idx_val))
			self.b.set_terminator(M.IfTerminator(cond=ge, then_target=body_block.name, else_target=exit_block.name))

			self.b.set_block(body_block)
			val_move = self._array_elem_take_value(elem_ty=elem_ty, array=array_val, index=cur)
			next_i = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=next_i, op=H.BinaryOp.ADD, left=cur, right=one))
			self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=array_val, index=next_i, value=val_move))
			prev_i = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=prev_i, op=H.BinaryOp.SUB, left=cur, right=one))
			self.b.emit(M.StoreLocal(local=idx_local, value=prev_i))
			self.b.set_terminator(M.Goto(target=cond_block.name))

			self.b.set_block(exit_block)
			self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=array_val, index=idx_val, value=val))
			new_len = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=new_len, op=H.BinaryOp.ADD, left=len_val, right=one))
			new_arr = self.b.new_temp()
			self.b.emit(M.ArraySetLen(dest=new_arr, array=array_val, length=new_len))
			new_arr_gen = _set_gen(new_arr, next_gen)
			self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr_gen, inner_ty=array_ty))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(join_block)
			return True, None

		if name in ("remove", "swap_remove"):
			if len(expr.args) != 1:
				raise AssertionError("Array remove/swap_remove arity mismatch reached MIR lowering (checker bug)")
			if not want_value:
				return True, None
			idx_val = self.lower_expr(expr.args[0], expected_type=self._int_type)
			len_val = self.b.new_temp()
			self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
			next_gen = _next_gen(array_val)
			one = self._const_int(1)
			val = self._array_elem_take_value(elem_ty=elem_ty, array=array_val, index=idx_val)
			last_idx = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=last_idx, op=H.BinaryOp.SUB, left=len_val, right=one))
			if name == "swap_remove":
				need_swap = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=need_swap, op=H.BinaryOp.NE, left=idx_val, right=last_idx))
				swap_block = self.b.new_block("array_swaprem_swap")
				skip_block = self.b.new_block("array_swaprem_skip")
				join_block = self.b.new_block("array_swaprem_join")
				self.b.set_terminator(
					M.IfTerminator(cond=need_swap, then_target=swap_block.name, else_target=skip_block.name)
				)

				self.b.set_block(swap_block)
				tmp = self._array_elem_take_value(elem_ty=elem_ty, array=array_val, index=last_idx)
				self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=array_val, index=idx_val, value=tmp))
				self.b.set_terminator(M.Goto(target=join_block.name))

				self.b.set_block(skip_block)
				self.b.set_terminator(M.Goto(target=join_block.name))

				self.b.set_block(join_block)
			else:
				start = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=start, op=H.BinaryOp.ADD, left=idx_val, right=one))
				idx_local = self._addr_taken_local("__array_rem_i", self._int_type, self._const_int(0))
				self.b.emit(M.StoreLocal(local=idx_local, value=start))

				cond_block = self.b.new_block("array_remove_cond")
				body_block = self.b.new_block("array_remove_body")
				exit_block = self.b.new_block("array_remove_exit")
				self.b.set_terminator(M.Goto(target=cond_block.name))

				self.b.set_block(cond_block)
				cur = self.b.new_temp()
				self.b.emit(M.LoadLocal(dest=cur, local=idx_local))
				lt = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=lt, op=H.BinaryOp.LT, left=cur, right=len_val))
				self.b.set_terminator(M.IfTerminator(cond=lt, then_target=body_block.name, else_target=exit_block.name))

				self.b.set_block(body_block)
				tmp = self._array_elem_take_value(elem_ty=elem_ty, array=array_val, index=cur)
				dest_idx = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=dest_idx, op=H.BinaryOp.SUB, left=cur, right=one))
				self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=array_val, index=dest_idx, value=tmp))
				next_i = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=next_i, op=H.BinaryOp.ADD, left=cur, right=one))
				self.b.emit(M.StoreLocal(local=idx_local, value=next_i))
				self.b.set_terminator(M.Goto(target=cond_block.name))

				self.b.set_block(exit_block)
			new_len = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=new_len, op=H.BinaryOp.SUB, left=len_val, right=one))
			new_arr = self.b.new_temp()
			self.b.emit(M.ArraySetLen(dest=new_arr, array=array_val, length=new_len))
			new_arr_gen = _set_gen(new_arr, next_gen)
			self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr_gen, inner_ty=array_ty))
			return True, val

		if name == "swap":
			if len(expr.args) != 2:
				raise AssertionError("Array.swap arity mismatch reached MIR lowering (checker bug)")
			idx_a = self.lower_expr(expr.args[0], expected_type=self._int_type)
			idx_b = self.lower_expr(expr.args[1], expected_type=self._int_type)
			# Bounds-check both indices.
			tmp_a = self.b.new_temp()
			self.b.emit(
				M.AddrOfArrayElem(
					dest=tmp_a,
					array=array_val,
					index=idx_a,
					inner_ty=elem_ty,
					is_mut=True,
				)
			)
			tmp_b = self.b.new_temp()
			self.b.emit(
				M.AddrOfArrayElem(
					dest=tmp_b,
					array=array_val,
					index=idx_b,
					inner_ty=elem_ty,
					is_mut=True,
				)
			)
			is_same = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=is_same, op=H.BinaryOp.EQ, left=idx_a, right=idx_b))
			same_block = self.b.new_block("array_swap_same")
			swap_block = self.b.new_block("array_swap_do")
			join_block = self.b.new_block("array_swap_join")
			self.b.set_terminator(
				M.IfTerminator(cond=is_same, then_target=same_block.name, else_target=swap_block.name)
			)

			self.b.set_block(same_block)
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(swap_block)
			val_a = self._array_elem_take_value(elem_ty=elem_ty, array=array_val, index=idx_a)
			val_b = self._array_elem_take_value(elem_ty=elem_ty, array=array_val, index=idx_b)
			self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=array_val, index=idx_a, value=val_b))
			self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=array_val, index=idx_b, value=val_a))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(join_block)
			return True, None

		if name == "set":
			if len(expr.args) != 2:
				raise AssertionError("Array.set arity mismatch reached MIR lowering (checker bug)")
			idx_val = self.lower_expr(expr.args[0], expected_type=self._int_type)
			val = self.lower_expr(expr.args[1], expected_type=elem_ty)
			self.b.emit(M.ArrayIndexStore(elem_ty=elem_ty, array=array_val, index=idx_val, value=val))
			return True, None

		if name in ("clear", "reserve", "shrink_to_fit"):
			if name == "clear":
				if len(expr.args) != 0:
					raise AssertionError("Array.clear arity mismatch reached MIR lowering (checker bug)")
				len_val = self.b.new_temp()
				self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
				next_gen = _next_gen(array_val)
				zero = self._const_int(0)
				idx_local = self._addr_taken_local("__array_clear_i", self._int_type, self._const_int(0))
				self.b.emit(M.StoreLocal(local=idx_local, value=zero))

				cond_block = self.b.new_block("array_clear_cond")
				body_block = self.b.new_block("array_clear_body")
				exit_block = self.b.new_block("array_clear_exit")
				self.b.set_terminator(M.Goto(target=cond_block.name))

				self.b.set_block(cond_block)
				cur = self.b.new_temp()
				self.b.emit(M.LoadLocal(dest=cur, local=idx_local))
				lt = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=lt, op=H.BinaryOp.LT, left=cur, right=len_val))
				self.b.set_terminator(M.IfTerminator(cond=lt, then_target=body_block.name, else_target=exit_block.name))

				self.b.set_block(body_block)
				self.b.emit(M.ArrayElemDrop(elem_ty=elem_ty, array=array_val, index=cur))
				next_i = self.b.new_temp()
				one = self._const_int(1)
				self.b.emit(M.BinaryOpInstr(dest=next_i, op=H.BinaryOp.ADD, left=cur, right=one))
				self.b.emit(M.StoreLocal(local=idx_local, value=next_i))
				self.b.set_terminator(M.Goto(target=cond_block.name))

				self.b.set_block(exit_block)
				new_arr = self.b.new_temp()
				self.b.emit(M.ArraySetLen(dest=new_arr, array=array_val, length=zero))
				new_arr_gen = _set_gen(new_arr, next_gen)
				self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr_gen, inner_ty=array_ty))
				return True, None

			if name == "reserve":
				if len(expr.args) != 1:
					raise AssertionError("Array.reserve arity mismatch reached MIR lowering (checker bug)")
				add_val = self.lower_expr(expr.args[0], expected_type=self._int_type)
				zero = self._const_int(0)
				neg = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=neg, op=H.BinaryOp.LE, left=add_val, right=zero))
				skip_block = self.b.new_block("array_reserve_skip")
				do_block = self.b.new_block("array_reserve_do")
				join_block = self.b.new_block("array_reserve_join")
				self.b.set_terminator(M.IfTerminator(cond=neg, then_target=skip_block.name, else_target=do_block.name))

				self.b.set_block(skip_block)
				self.b.set_terminator(M.Goto(target=join_block.name))

				self.b.set_block(do_block)
				len_val = self.b.new_temp()
				self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
				cap_val = self.b.new_temp()
				self.b.emit(M.ArrayCap(dest=cap_val, array=array_val))
				next_gen = _next_gen(array_val)
				new_arr, grew = ensure_capacity(array_val, len_val=len_val, cap_val=cap_val, extra=add_val)
				bump_block = self.b.new_block("array_reserve_bump")
				store_block = self.b.new_block("array_reserve_store")
				self.b.set_terminator(
					M.IfTerminator(cond=grew, then_target=bump_block.name, else_target=store_block.name)
				)

				self.b.set_block(bump_block)
				new_arr_gen = _set_gen(new_arr, next_gen)
				self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr_gen, inner_ty=array_ty))
				self.b.set_terminator(M.Goto(target=join_block.name))

				self.b.set_block(store_block)
				self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr, inner_ty=array_ty))
				self.b.set_terminator(M.Goto(target=join_block.name))

				self.b.set_block(join_block)
				return True, None

			# shrink_to_fit
			if len(expr.args) != 0:
				raise AssertionError("Array.shrink_to_fit arity mismatch reached MIR lowering (checker bug)")
			len_val = self.b.new_temp()
			self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
			cap_val = self.b.new_temp()
			self.b.emit(M.ArrayCap(dest=cap_val, array=array_val))
			next_gen = _next_gen(array_val)
			needs = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=needs, op=H.BinaryOp.LT, left=len_val, right=cap_val))
			do_block = self.b.new_block("array_shrink_do")
			skip_block = self.b.new_block("array_shrink_skip")
			join_block = self.b.new_block("array_shrink_join")
			self.b.set_terminator(M.IfTerminator(cond=needs, then_target=do_block.name, else_target=skip_block.name))

			self.b.set_block(do_block)
			new_arr = shrink_array(array_val, len_val=len_val)
			new_arr_gen = _set_gen(new_arr, next_gen)
			self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr_gen, inner_ty=array_ty))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(skip_block)
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(join_block)
			return True, None

		raise AssertionError("unreachable array intrinsic lowering (checker bug)")

	def _recover_unknown_value(self, msg: str) -> M.ValueId:
		if self._typed_mode == "strict":
			raise AssertionError(msg)
		dest = self.b.new_temp()
		self.b.emit(M.ConstInt(dest=dest, value=0))
		self._local_types[dest] = self._unknown_type
		return dest


	def _visit_expr_HDVInit(self, expr: H.HDVInit) -> M.ValueId:
		arg_vals = [self.lower_expr(arg) for arg in expr.args]
		if len(expr.args) == 1 and self._expr_types:
			arg_expr = expr.args[0]
			arg_ty = self._expr_types.get(arg_expr.node_id)
			if arg_ty == self._uint_type:
				src_val = arg_vals[0]
				conv = self.b.new_temp()
				self.b.emit(M.IntFromUint(dest=conv, value=src_val))
				self._local_types[conv] = self._int_type
				arg_vals = [conv]
		dest = self.b.new_temp()
		self.b.emit(M.ConstructDV(dest=dest, dv_type_name=expr.dv_type_name, args=arg_vals))
		return dest

	def _visit_expr_HExceptionInit(self, expr: H.HExceptionInit) -> M.ValueId:
		"""
		Lower exception init into an Error value.
		"""
		return self._construct_error_from_exception_init(expr)

	def _construct_error_from_exception_init(self, expr: H.HExceptionInit) -> M.ValueId:
		from lang.driftc.core.exception_ctor_args import KwArg as _KwArg, resolve_exception_ctor_args

		err_val = self.b.new_temp()
		self._local_types[err_val] = self._type_table.ensure_error()

		code_const = self._lookup_error_code(event_fqn=expr.event_fqn)
		code_val = self.b.new_temp()
		self.b.emit(M.ConstUint64(dest=code_val, value=code_const))
		self._local_types[code_val] = self._uint64_type

		event_fqn_val = self.b.new_temp()
		self.b.emit(M.ConstString(dest=event_fqn_val, value=expr.event_fqn))

		schema = self._exception_schemas.get(expr.event_fqn)
		if schema is None:
			raise AssertionError(f"missing exception schema for {expr.event_fqn!r} (checker bug)")
		_decl_fqn, schema_fields = schema

		resolved, diags = resolve_exception_ctor_args(
			event_fqn=expr.event_fqn,
			declared_fields=schema_fields,
			pos_args=[(a, getattr(a, "loc", Span())) for a in expr.pos_args],
			kw_args=[
				_KwArg(name=kw.name, value=kw.value, name_span=getattr(kw, "loc", Span()))
				for kw in expr.kw_args
			],
			span=getattr(expr, "loc", Span()),
		)
		if diags:
			raise AssertionError("exception ctor args reached MIR lowering with diagnostics (checker bug)")

		if not resolved:
			self.b.emit(
				M.ConstructError(
					dest=err_val,
					code=code_val,
					event_fqn=event_fqn_val,
					payload=None,
					attr_key=None,
				)
			)
			return err_val

		field_dvs: list[tuple[str, M.ValueId]] = []
		for name, field_expr in resolved:
			if isinstance(field_expr, H.HDVInit):
				dv_val = self.lower_expr(field_expr)
			elif isinstance(field_expr, (H.HLiteralInt, H.HLiteralBool, H.HLiteralString)):
				inner_val = self.lower_expr(field_expr)
				dv_val = self.b.new_temp()
				kind_name = "Int" if isinstance(field_expr, H.HLiteralInt) else "Bool"
				if isinstance(field_expr, H.HLiteralString):
					kind_name = "String"
				self.b.emit(M.ConstructDV(dest=dv_val, dv_type_name=kind_name, args=[inner_val]))
			else:
				dv_val = self.lower_expr(field_expr)
				dv_ty = self._local_types.get(dv_val)
				if dv_ty != self._dv_type:
					raise AssertionError(
						f"exception field {name!r} must lower to DiagnosticValue (checker bug)"
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
		return err_val

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
		tern_ty = self._infer_expr_type(expr.then_expr)
		if tern_ty is None:
			tern_ty = self._infer_expr_type(expr.else_expr)
		if tern_ty is None:
			tern_ty = self._unknown_type
		self._local_types[temp_local] = tern_ty

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

	def _visit_expr_HQualifiedMember(self, expr: H.HQualifiedMember) -> M.ValueId:
		base_te = getattr(expr, "base_type_expr", None)
		if base_te is None:
			raise AssertionError("qualified member missing base type (checker bug)")
		cur_mod = self._current_module_name()
		base_tid = resolve_opaque_type(base_te, self._type_table, module_id=getattr(base_te, "module_id", None) or cur_mod, allow_generic_base=True)
		base_def = self._type_table.get(base_tid)
		if base_def.kind is not TypeKind.VARIANT:
			raise AssertionError("qualified member base is not a variant in typed mode (checker bug)")
		schema = self._type_table.get_variant_schema(base_tid)
		if schema is None:
			raise AssertionError("missing variant schema for qualified member (type table bug)")
		type_arg_exprs = list(getattr(base_te, "args", []) or [])
		if schema.type_params:
			if len(type_arg_exprs) != len(schema.type_params):
				raise AssertionError("qualified member missing type arguments in typed mode (checker bug)")
			type_arg_ids = [resolve_opaque_type(a, self._type_table, module_id=getattr(base_te, "module_id", None) or cur_mod) for a in type_arg_exprs]
			inst_id = self._type_table.ensure_variant_instantiated(base_tid, type_arg_ids)
		else:
			if type_arg_exprs:
				raise AssertionError("qualified member has type arguments for non-generic variant (checker bug)")
			inst_id = base_tid
		inst = self._type_table.get_variant_instance(inst_id)
		if inst is None:
			raise AssertionError("variant instance missing for qualified member (type table bug)")
		arm_inst = inst.arms_by_name.get(expr.member)
		if arm_inst is None:
			raise AssertionError("qualified member constructor missing (checker bug)")
		if arm_inst.field_types:
			raise AssertionError("qualified member for non-empty constructor reached MIR lowering (checker bug)")
		dest = self.b.new_temp()
		self.b.emit(M.ConstructVariant(dest=dest, variant_ty=inst_id, ctor=expr.member, args=[]))
		self._local_types[dest] = inst_id
		return dest

	def _visit_expr_HTryExpr(self, expr: H.HTryExpr) -> M.ValueId:
		"""
		Lower expression-form try/catch by desugaring to a try CFG that merges
		values through a hidden local and a join block.
		"""
		# Hidden local for the expression result.
		temp_local = f"__try_expr_tmp{self.b.new_temp()}"
		self.b.ensure_local(temp_local)
		try_ty = self._current_expected_type() or self._infer_expr_type(expr.attempt)
		if try_ty is None:
			try_ty = self._unknown_type
		self._local_types[temp_local] = try_ty

		# Blocks: attempt body, dispatch for errors, catch arms, join for value.
		attempt_block = self.b.new_block("tryexpr_attempt")
		dispatch_block = self.b.new_block("tryexpr_dispatch")
		join_block = self.b.new_block("tryexpr_join")

		# Hidden local to carry the caught Error.
		error_local = f"__try_err{self.b.new_temp()}"
		self.b.ensure_local(error_local)
		error_ty = self._type_table.ensure_error()
		self._local_types[error_local] = error_ty
		self._register_drop_local(error_local, error_ty)
		err_zero = self.b.new_temp()
		self.b.emit(M.ZeroValue(dest=err_zero, ty=error_ty))
		self._local_types[err_zero] = error_ty
		self.b.emit(M.StoreLocal(local=error_local, value=err_zero))

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
		self._local_types[code_tmp] = self._uint64_type

		event_arms = [(arm, cb) for arm, cb in catch_blocks if arm.event_fqn is not None]
		if event_arms:
			current_block = dispatch_block
			for arm, cb in event_arms:
				self.b.set_block(current_block)
				arm_code = self._lookup_catch_event_code(arm.event_fqn)
				arm_code_const = self.b.new_temp()
				self.b.emit(M.ConstUint64(dest=arm_code_const, value=arm_code))
				self._local_types[arm_code_const] = self._uint64_type
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
			should_drop_caught_error = arm.binder is None
			catch_error_local = error_local
			binder_local: str | None = None
			if arm.binder:
				self.b.emit(M.MoveOut(dest=err_again, local=error_local, ty=error_ty))
				self._local_types[err_again] = error_ty
				self._moved_locals.add(error_local)
				binder_id = self._find_binder_binding_id(arm.binder, arm.block, arm.result)
				binder_local = self._canonical_local(binder_id, arm.binder)
				self.b.ensure_local(binder_local)
				self._local_types[binder_local] = self._type_table.ensure_error()
				self.b.emit(M.StoreLocal(local=binder_local, value=err_again))
				catch_error_local = binder_local
			else:
				self.b.emit(M.LoadLocal(dest=err_again, local=error_local))
			prev_catch_err = self._current_catch_error
			self._current_catch_error = catch_error_local
			self.lower_block(arm.block)
			if arm.result is None:
				raise RuntimeError("try/catch expression arm must produce a value")
			arm_val = self.lower_expr(arm.result, expected_type=try_ty)
			self._current_catch_error = prev_catch_err
			if self.b.block.terminator is None and should_drop_caught_error:
				err_done = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=err_done, local=error_local, ty=error_ty))
				self._local_types[err_done] = error_ty
				self._moved_locals.add(error_local)
				self.b.emit(M.DropValue(value=err_done, ty=error_ty))
			if self.b.block.terminator is None and binder_local is not None:
				binder_done = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=binder_done, local=binder_local, ty=error_ty))
				self._local_types[binder_done] = error_ty
				self._moved_locals.add(binder_local)
				self.b.emit(M.DropValue(value=binder_done, ty=error_ty))
			self.b.emit(M.StoreLocal(local=temp_local, value=arm_val))
			if self.b.block.terminator is None:
				self.b.set_terminator(M.Goto(target=join_block.name))

		# Resume at join with the merged value.
		self.b.set_block(join_block)
		dest = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=dest, local=temp_local))
		return dest

	def _visit_expr_HMatchExpr(self, expr: "H.HMatchExpr") -> M.ValueId:
		"""Lower `match` in expression position (value required)."""
		val = self._lower_match(expr, want_value=True)
		assert val is not None
		return val

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
		prev_span = self.b.current_span
		self.b.current_span = Span.from_loc(getattr(stmt, "loc", None))
		prev_stmt_span = self._current_stmt_span
		self._current_stmt_span = self.b.current_span
		try:
			method(stmt)
		finally:
			self.b.current_span = prev_span
			self._current_stmt_span = prev_stmt_span

	def lower_block(self, block: H.HBlock) -> None:
		"""Entry point: lower an HIR block (list of statements) into MIR."""
		self._push_scope(include_params=False)
		for stmt in block.statements:
			self.lower_stmt(stmt)
		if self.b.block.terminator is None:
			self._emit_scope_drops(scope_index=len(self._scope_stack) - 1)
		self._pop_scope()

	def lower_function_body(self, block: H.HBlock) -> None:
		"""
		Lower a full function body block and ensure the function ends in a terminator.

		MIR requires every basic block to end with a terminator. For the entry
		function body, we also want a production-safe invariant:
		  - `-> Void` functions may omit an explicit `return;` and will get an
		    implicit return.
		  - non-Void functions must end in an explicit return (checker responsibility).
		"""
		self._push_scope(include_params=True)
		for stmt in block.statements:
			self.lower_stmt(stmt)
		if self.b.block.terminator is None:
			self._emit_scope_drops(scope_index=len(self._scope_stack) - 1)
		self._pop_scope()
		if self.b.block.terminator is not None:
			return
		can_throw = self._fn_can_throw() is True
		fn_is_void = self._ret_type is not None and self._type_table.is_void(self._ret_type)
		if self._current_catch_error is not None:
			reuses_caught = False
			if isinstance(stmt.value, H.HVar):
				cand = self._canonical_local(getattr(stmt.value, "binding_id", None), stmt.value.name)
				reuses_caught = cand == self._current_catch_error
			elif isinstance(stmt.value, H.HPlaceExpr) and not stmt.value.projections and isinstance(stmt.value.base, H.HVar):
				cand = self._canonical_local(getattr(stmt.value.base, "binding_id", None), stmt.value.base.name)
				reuses_caught = cand == self._current_catch_error
			if not reuses_caught and self._current_catch_error not in self._moved_locals:
				caught_drop = self.b.new_temp()
				error_ty = self._type_table.ensure_error()
				self.b.emit(M.MoveOut(dest=caught_drop, local=self._current_catch_error, ty=error_ty))
				self._local_types[caught_drop] = error_ty
				self._moved_locals.add(self._current_catch_error)
				self.b.emit(M.DropValue(value=caught_drop, ty=error_ty))
		if not fn_is_void:
			raise AssertionError("missing return reached MIR lowering (checker bug)")
		if not can_throw:
			self.b.set_terminator(M.Return(value=None))
			return
		# Can-throw `-> Void` lowers to FnResult<Void, Error>.
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
		if isinstance(stmt.expr, H.HMatchExpr):
			self._lower_match(stmt.expr, want_value=False)
			return
		if isinstance(stmt.expr, H.HCall):
			info = self._call_info_for_expr_optional(stmt.expr)
			if info is not None and info.target.kind is CallTargetKind.INTRINSIC:
				intrinsic = info.target.intrinsic
				if intrinsic is None:
					raise AssertionError("intrinsic call missing name (typecheck/call-info bug)")
				if intrinsic is IntrinsicKind.SWAP:
					if len(stmt.expr.args) != 2:
						raise AssertionError("swap(a, b): arity mismatch reached MIR lowering (checker bug)")
					a_expr = stmt.expr.args[0]
					b_expr = stmt.expr.args[1]
					if isinstance(a_expr, H.HBorrow) and a_expr.is_mut:
						a_expr = place_expr_from_lvalue_expr(a_expr.subject)
					if isinstance(b_expr, H.HBorrow) and b_expr.is_mut:
						b_expr = place_expr_from_lvalue_expr(b_expr.subject)
					if not (
						hasattr(H, "HPlaceExpr")
						and isinstance(a_expr, getattr(H, "HPlaceExpr"))
						and isinstance(b_expr, getattr(H, "HPlaceExpr"))
					):
						raise AssertionError(
							"swap(a, b): non-canonical place reached MIR lowering (normalize/typechecker bug)"
						)
					a_ptr, a_ty = self._lower_addr_of_place(a_expr, is_mut=True)
					b_ptr, b_ty = self._lower_addr_of_place(b_expr, is_mut=True)
					if a_ty != b_ty:
						raise AssertionError("swap(a, b) reached MIR lowering with mismatched types (checker bug)")
					a_val = self.b.new_temp()
					b_val = self.b.new_temp()
					self.b.emit(M.LoadRef(dest=a_val, ptr=a_ptr, inner_ty=a_ty))
					self.b.emit(M.LoadRef(dest=b_val, ptr=b_ptr, inner_ty=b_ty))
					self.b.emit(M.StoreRef(ptr=a_ptr, value=b_val, inner_ty=a_ty))
					self.b.emit(M.StoreRef(ptr=b_ptr, value=a_val, inner_ty=b_ty))
					return
				if intrinsic is IntrinsicKind.RAW_DEALLOC:
					if len(stmt.expr.args) != 1:
						raise AssertionError("dealloc(buf): arity mismatch reached MIR lowering (checker bug)")
					info = self._call_info_for(stmt.expr)
					raw_param = self._unwrap_ref_type(info.sig.param_types[0]) if info.sig.param_types else self._unknown_type
					buf_val = self.lower_expr(stmt.expr.args[0])
					self.b.emit(M.RawBufferDealloc(buffer=buf_val, raw_ty=raw_param))
					return
				if intrinsic is IntrinsicKind.RAW_WRITE:
					if len(stmt.expr.args) != 3:
						raise AssertionError("write(buf, i, v): arity mismatch reached MIR lowering (checker bug)")
					info = self._call_info_for(stmt.expr)
					raw_param = self._unwrap_ref_type(info.sig.param_types[0]) if info.sig.param_types else self._unknown_type
					elem_ty = self._raw_buffer_elem_type(raw_param)
					if elem_ty is self._unknown_type:
						raise AssertionError("write(...) missing RawBuffer element type (checker bug)")
					buf_val = self.lower_expr(stmt.expr.args[0])
					idx_val = self.lower_expr(stmt.expr.args[1])
					val_val = self.lower_expr(stmt.expr.args[2])
					self.b.emit(M.RawBufferWrite(buffer=buf_val, raw_ty=raw_param, elem_ty=elem_ty, index=idx_val, value=val_val))
					return
				if intrinsic is IntrinsicKind.PTR_WRITE:
					if len(stmt.expr.args) != 2:
						raise AssertionError("ptr_write(ptr, v): arity mismatch reached MIR lowering (checker bug)")
					info = self._call_info_for(stmt.expr)
					ptr_param = info.sig.param_types[0] if info.sig.param_types else self._unknown_type
					elem_ty = self._raw_ptr_elem_type(ptr_param)
					if elem_ty is self._unknown_type:
						raise AssertionError("ptr_write(...) missing Ptr<T> element type (checker bug)")
					ptr_val = self.lower_expr(stmt.expr.args[0])
					val_val = self.lower_expr(stmt.expr.args[1])
					self.b.emit(M.PtrWrite(ptr=ptr_val, value=val_val, elem_ty=elem_ty))
					return
				if intrinsic is IntrinsicKind.DROP_VALUE:
					if len(stmt.expr.args) != 1:
						raise AssertionError("drop_value(v): arity mismatch reached MIR lowering (checker bug)")
					info = self._call_info_for(stmt.expr)
					param_ty = info.sig.param_types[0] if info.sig.param_types else self._unknown_type
					val = self.lower_expr(stmt.expr.args[0])
					self.b.emit(M.DropValue(value=val, ty=param_ty))
					return
				self.lower_expr(stmt.expr)
				return
		if (
			isinstance(stmt.expr, H.HCall)
			and hasattr(H, "HQualifiedMember")
			and isinstance(stmt.expr.fn, getattr(H, "HQualifiedMember"))
		):
			info = self._call_info_for_expr_optional(stmt.expr)
			if info is not None:
				if info.sig.can_throw:
					fnres_val = self._lower_call_with_info(stmt.expr, info)
					assert fnres_val is not None

					def emit_call() -> M.ValueId:
						return fnres_val

					self._lower_can_throw_call_stmt(emit_call=emit_call, ok_ty=info.sig.user_ret_type)
					return
				if self._type_table.is_void(info.sig.user_ret_type):
					self._lower_call_with_info(stmt.expr, info)
					return
		if isinstance(stmt.expr, H.HCall):
			info = self._call_info_for_expr_optional(stmt.expr)
			if info is not None:
				if info.sig.can_throw:
					fnres_val = self._lower_call(expr=stmt.expr)
					assert fnres_val is not None

					def emit_call() -> M.ValueId:
						return fnres_val

					self._lower_can_throw_call_stmt(emit_call=emit_call, ok_ty=info.sig.user_ret_type)
					return
				if self._type_table.is_void(info.sig.user_ret_type):
					self._lower_call(expr=stmt.expr)
					return
		if isinstance(stmt.expr, H.HInvoke):
			info = self._call_info_for_expr_optional(stmt.expr)
			if info is not None:
				if info.sig.can_throw:
					fnres_val = self._lower_invoke(expr=stmt.expr)
					assert fnres_val is not None

					def emit_call() -> M.ValueId:
						return fnres_val

					self._lower_can_throw_call_stmt(emit_call=emit_call, ok_ty=info.sig.user_ret_type)
					return
				if self._type_table.is_void(info.sig.user_ret_type):
					self._lower_invoke(expr=stmt.expr)
					return
		if isinstance(stmt.expr, H.HMethodCall):
			handled, _value = self._lower_array_intrinsic_method(stmt.expr, want_value=False)
			if handled:
				return
			# Only special-case method calls when statement semantics differ from
			# expression semantics:
			# - can-throw calls in statement position must be "checked" and propagate,
			# - Void-returning calls in statement position should not produce a value.
			info = self._call_info_for_expr_optional(stmt.expr)
			if info is None:
				self.lower_expr(stmt.expr)
				return
			if info.sig.can_throw:
				fnres_val, _ = self._lower_method_call(expr=stmt.expr)
				assert fnres_val is not None

				def emit_call() -> M.ValueId:
					return fnres_val

				self._lower_can_throw_call_stmt(emit_call=emit_call, ok_ty=info.sig.user_ret_type)
				return
			if self._type_table.is_void(info.sig.user_ret_type):
				self._lower_method_call(expr=stmt.expr)
				return
		if hasattr(H, "HMatchExpr") and isinstance(stmt.expr, getattr(H, "HMatchExpr")):
			self._lower_match(stmt.expr, want_value=False)
			return
		val = self.lower_expr(stmt.expr)
		expr_ty = self._infer_expr_type(stmt.expr)
		if expr_ty is None:
			return
		if self._type_table.is_void(expr_ty):
			return
		if self._type_table.copy_status(expr_ty) is True:
			return
		self.b.emit(M.DropValue(value=val, ty=expr_ty))

	def _visit_stmt_HBlock(self, stmt: H.HBlock) -> None:
		"""
		Lower a block statement by lowering its nested statements in order.

		`HBlock` is used both as a container for structured control-flow bodies
		(`if`/`loop`/`try`) and as an explicit block statement introduced by
		desugarings (e.g., `for` introduces hidden temporaries scoped to the loop).
		"""
		self.lower_block(stmt)

	def _visit_stmt_HUnsafeBlock(self, stmt: H.HUnsafeBlock) -> None:
		self.lower_block(stmt.block)

	def _visit_stmt_HAssert(self, stmt: H.HAssert) -> None:
		cond_val = self.lower_expr(stmt.cond)
		span = getattr(stmt, "loc", Span())
		cond_span = Span.from_loc(getattr(stmt.cond, "loc", None))
		file_str = span.file or "<unknown>"
		if file_str != "<unknown>" and self._current_fn_id is not None:
			base_name = os.path.basename(file_str)
			file_str = f"{self._current_fn_id.module}@{base_name}"
		line_num = span.line or 0
		file_val = self.b.new_temp()
		self.b.emit(M.ConstString(dest=file_val, value=file_str))
		line_val = self.b.new_temp()
		self.b.emit(M.ConstInt(dest=line_val, value=line_num))
		expr_text = None
		if self._type_table is not None:
			expr_text = self._type_table.source_slice_from_span(cond_span)
		if expr_text is None:
			expr_text = ""
		else:
			expr_text = expr_text.strip()
		expr_val = self.b.new_temp()
		self.b.emit(M.ConstString(dest=expr_val, value=expr_text))
		if stmt.msg is None:
			msg_val = self._string_empty_const
		else:
			msg_val = self.lower_expr(stmt.msg)
		instr = M.AssertLoc(cond=cond_val, file=file_val, line=line_val, expr=expr_val, msg=msg_val)
		instr.span = span
		self.b.emit(instr)

	def _visit_stmt_HLet(self, stmt: H.HLet) -> None:
		prev_stmt_span = self._current_stmt_span
		stmt_span = Span.from_loc(getattr(stmt, "loc", None))
		if stmt_span == Span():
			stmt_span = Span.from_loc(getattr(stmt.value, "loc", None))
		self._current_stmt_span = stmt_span
		if getattr(stmt, "binding_id", None) is not None:
			self._local_binding_ids.add(int(stmt.binding_id))
		local_name = self._canonical_local(getattr(stmt, "binding_id", None), stmt.name)
		self.b.ensure_local(local_name)
		if getattr(stmt, "binding_id", None) is not None:
			self._binding_names[int(stmt.binding_id)] = stmt.name
		declared_ty: TypeId | None = None
		if getattr(stmt, "declared_type_expr", None) is not None:
			try:
				declared_ty = resolve_opaque_type(
					stmt.declared_type_expr,
					self._type_table,
					module_id=self._current_module_name(),
					type_params=self._type_param_subst or None,
				)
			except Exception:
				declared_ty = None
		inferred_ty = self._infer_expr_type(stmt.value)
		expected_ty = declared_ty if declared_ty is not None else inferred_ty
		prev_span = self.b.current_span
		self.b.current_span = self._current_stmt_span
		try:
			val = self.lower_expr(stmt.value, expected_type=expected_ty)
		finally:
			self.b.current_span = prev_span
		val_ty = declared_ty if declared_ty is not None else inferred_ty
		bid = getattr(stmt, "binding_id", None)
		if bid is not None:
			bid_ty = self._binding_types.get(int(bid))
			if bid_ty is not None and self._type_table.get(bid_ty).kind is not TypeKind.UNKNOWN:
				val_ty = bid_ty
		if val_ty is not None:
			self._local_types[local_name] = val_ty
			if stmt.name != local_name and stmt.name != "_":
				self._local_types[stmt.name] = val_ty
			self._register_drop_local(local_name, val_ty)
			if drift_debug.enabled("local_types_trace") and (local_name == "done" or stmt.name == "done"):
				import sys
				td = self._type_table.get(val_ty)
				fn = self._current_fn_id
				print(f"[drift:debug][local_types_trace] fn={fn} stmt=HLet local={local_name} name={stmt.name} ty={val_ty}:{td.kind.name}:{td.name}", file=sys.stderr)
		self.b.func.debug_local_names[local_name] = stmt.name
		store = M.StoreLocal(local=local_name, value=val)
		setattr(store, "debug_name", stmt.name)
		span = None
		if hasattr(stmt, "span") and stmt.span is not None:
			span = stmt.span
		elif hasattr(stmt, "loc") and stmt.loc is not None:
			span = stmt.loc
		if span is None or span == Span():
			val_span = None
			if hasattr(stmt.value, "span") and stmt.value.span is not None:
				val_span = stmt.value.span
			elif hasattr(stmt.value, "loc") and stmt.value.loc is not None:
				val_span = stmt.value.loc
			if val_span is not None:
				span = val_span
		if span is not None:
			store.span = span
		self.b.emit(store)
		if bool(getattr(stmt, "capture", False)):
			bid_i = getattr(stmt, "binding_id", None)
			if bid_i is not None:
				cap_name = str(getattr(stmt, "capture_alias", None) or stmt.name)
				self._register_captured_local(
					binding_id=int(bid_i),
					local_name=local_name,
					source_name=stmt.name,
					capture_name=cap_name,
				)
		self._moved_locals.discard(local_name)
		self._current_stmt_span = prev_stmt_span

	def _visit_stmt_HAssign(self, stmt: H.HAssign) -> None:
		val = self.lower_expr(stmt.value)
		# Stage1 normalization must canonicalize assignment targets to `HPlaceExpr`.
		# This keeps stage2 lowering lvalue handling single-path and prevents
		# re-deriving place structure from arbitrary expression trees.
		if not (hasattr(H, "HPlaceExpr") and isinstance(stmt.target, getattr(H, "HPlaceExpr"))):
			raise AssertionError("non-canonical assignment target reached MIR lowering (normalize/typechecker bug)")

		# Canonical place expression: assignments lower through address-of + StoreRef,
		# except for the trivial "local = value" case which keeps the `StoreLocal`
		# primitive (important for SSA/local-type tracking).
		if not stmt.target.projections:
			if self._lambda_capture_slots is not None:
				key = self._capture_key_for_expr(stmt.target.base)
				if key is not None and self._lambda_env_field_types is not None and key in self._lambda_capture_slots:
					slot = self._lambda_capture_slots[key]
					kind = None
					if self._lambda_capture_kinds is not None and slot < len(self._lambda_capture_kinds):
						kind = self._lambda_capture_kinds[slot]
					if kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT):
						field_ty = self._lambda_env_field_types[slot]
						ptr_val = self._load_capture_slot_value(slot)
						inner_ty = field_ty
						td = self._type_table.get(field_ty)
						if td.kind is TypeKind.REF and td.param_types:
							inner_ty = td.param_types[0]
						self._emit_assign_store_ref(ptr=ptr_val, value=val, inner_ty=inner_ty)
						return
			base_ty = self._infer_expr_type(stmt.target.base)
			if base_ty is not None:
				td = self._type_table.get(base_ty)
				if td.kind is TypeKind.REF and td.param_types:
					val_ty = self._infer_expr_type(stmt.value)
					if val_ty is not None and val_ty == base_ty:
						local_name = self._canonical_local(getattr(stmt.target.base, "binding_id", None), stmt.target.base.name)
						self.b.ensure_local(local_name)
						self.b.emit(M.StoreLocal(local=local_name, value=val))
						self._moved_locals.discard(local_name)
						return
					local_name = self._canonical_local(getattr(stmt.target.base, "binding_id", None), stmt.target.base.name)
					self.b.ensure_local(local_name)
					ptr_val = self.b.new_temp()
					self.b.emit(M.LoadLocal(dest=ptr_val, local=local_name))
					self._emit_assign_store_ref(ptr=ptr_val, value=val, inner_ty=td.param_types[0])
					return
			local_name = self._canonical_local(getattr(stmt.target.base, "binding_id", None), stmt.target.base.name)
			self.b.ensure_local(local_name)
			val_ty = self._infer_expr_type(stmt.value)
			if val_ty is not None:
				self._local_types[local_name] = val_ty
				if drift_debug.enabled("local_types_trace") and local_name == "done":
					import sys
					td = self._type_table.get(val_ty)
					fn = self._current_fn_id
					print(f"[drift:debug][local_types_trace] fn={fn} stmt=HAssign local={local_name} ty={val_ty}:{td.kind.name}:{td.name}", file=sys.stderr)
			self.b.emit(M.StoreLocal(local=local_name, value=val))
			self._moved_locals.discard(local_name)
			return
		# Fast path: array element assignment for a direct local binding.
		if (
			len(stmt.target.projections) == 1
			and isinstance(stmt.target.projections[0], H.HPlaceIndex)
			and isinstance(stmt.target.base, H.HVar)
		):
			proj = stmt.target.projections[0]
			array_name = self._canonical_local(getattr(stmt.target.base, "binding_id", None), stmt.target.base.name)
			array_ty = self._local_types.get(array_name) or self._infer_expr_type(stmt.target.base)
			if array_ty is not None:
				td = self._type_table.get(array_ty)
				if td.kind is TypeKind.ARRAY and td.param_types:
					elem_ty = td.param_types[0]
					array_val = self.b.new_temp()
					self.b.emit(M.LoadLocal(dest=array_val, local=array_name))
					index_val = self.lower_expr(proj.index)
					val_ty = self._infer_expr_type(stmt.value)
					if val_ty is not None:
						copy_status = self._type_table.copy_status(val_ty)
						if copy_status is True and not isinstance(stmt.value, H.HMove):
							copy_dest = self.b.new_temp()
							self.b.emit(M.CopyValue(dest=copy_dest, value=val, ty=val_ty))
							self._local_types[copy_dest] = val_ty
							val = copy_dest
					self.b.emit(M.ArrayElemAssign(elem_ty=elem_ty, array=array_val, index=index_val, value=val))
					return
		ptr, inner_ty = self._lower_addr_of_place(stmt.target, is_mut=True)
		self._emit_assign_store_ref(ptr=ptr, value=val, inner_ty=inner_ty)
		return

	def _emit_assign_store_ref(self, *, ptr: M.ValueId, value: M.ValueId, inner_ty: TypeId) -> None:
		# Assignment semantics for lvalue stores are replace, not raw overwrite:
		# drop the previous non-Copy occupant before storing the new value.
		if self._needs_runtime_drop(inner_ty):
			old_val = self.b.new_temp()
			self.b.emit(M.LoadRef(dest=old_val, ptr=ptr, inner_ty=inner_ty))
			self._local_types[old_val] = inner_ty
			zero_val = self.b.new_temp()
			self.b.emit(M.ZeroValue(dest=zero_val, ty=inner_ty))
			self.b.emit(M.StoreRef(ptr=ptr, value=zero_val, inner_ty=inner_ty))
			self.b.emit(M.DropValue(value=old_val, ty=inner_ty))
		self.b.emit(M.StoreRef(ptr=ptr, value=value, inner_ty=inner_ty))

	def _visit_stmt_HAugAssign(self, stmt: "H.HAugAssign") -> None:
		"""
		Lower augmented assignment (`+=`) as a read-modify-write.

		This preserves correct semantics for complex places:
		- evaluate the target address once,
		- load the current value,
		- compute the new value,
		- store it back.

		We intentionally avoid early desugaring to `x = x + y` which would
		duplicate evaluation of the lvalue (e.g., `arr[i] += 1` would evaluate `i`
		twice).
		"""
		op_map = {
			"+=": H.BinaryOp.ADD,
			"-=": H.BinaryOp.SUB,
			"*=": H.BinaryOp.MUL,
			"/=": H.BinaryOp.DIV,
			"%=": H.BinaryOp.MOD,
			"&=": H.BinaryOp.BIT_AND,
			"|=": H.BinaryOp.BIT_OR,
			"^=": H.BinaryOp.BIT_XOR,
			"<<=": H.BinaryOp.SHL,
			">>=": H.BinaryOp.SHR,
		}
		if stmt.op not in op_map:
			raise AssertionError(f"unsupported augmented assignment operator '{stmt.op}' reached MIR lowering")
		bin_op = op_map[stmt.op]

		# Stage1 normalization must canonicalize augmented assignment targets to `HPlaceExpr`.
		if not (hasattr(H, "HPlaceExpr") and isinstance(stmt.target, getattr(H, "HPlaceExpr"))):
			raise AssertionError("non-canonical augmented assignment target reached MIR lowering (normalize/typechecker bug)")

		rhs = self.lower_expr(stmt.value)
		inner_ty = self._infer_expr_type(stmt.target) or self._unknown_type

		# Trivial local case: keep locals in SSA-friendly `LoadLocal`/`StoreLocal` form.
		if not stmt.target.projections:
			local_name = self._canonical_local(getattr(stmt.target.base, "binding_id", None), stmt.target.base.name)
			self.b.ensure_local(local_name)
			base_ty = self._infer_expr_type(stmt.target.base)
			if base_ty is not None:
				td = self._type_table.get(base_ty)
				if td.kind is TypeKind.REF and td.param_types:
					ptr_val = self.b.new_temp()
					self.b.emit(M.LoadLocal(dest=ptr_val, local=local_name))
					old = self.b.new_temp()
					self.b.emit(M.LoadRef(dest=old, ptr=ptr_val, inner_ty=td.param_types[0]))
					new = self.b.new_temp()
					self.b.emit(M.BinaryOpInstr(dest=new, op=bin_op, left=old, right=rhs))
					self.b.emit(M.StoreRef(ptr=ptr_val, value=new, inner_ty=td.param_types[0]))
					return
			old = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=old, local=local_name))
			new = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=new, op=bin_op, left=old, right=rhs))
			self._local_types[local_name] = inner_ty
			self.b.emit(M.StoreLocal(local=local_name, value=new))
			return

		ptr, elem_ty = self._lower_addr_of_place(stmt.target, is_mut=True)
		old = self.b.new_temp()
		self.b.emit(M.LoadRef(dest=old, ptr=ptr, inner_ty=elem_ty))
		new = self.b.new_temp()
		self.b.emit(M.BinaryOpInstr(dest=new, op=bin_op, left=old, right=rhs))
		self.b.emit(M.StoreRef(ptr=ptr, value=new, inner_ty=elem_ty))
		return

	def _visit_stmt_HReturn(self, stmt: H.HReturn) -> None:
		if self.b.block.terminator is not None:
			return
		ret_span = Span.from_loc(getattr(stmt, "loc", None))
		if drift_debug.enabled("stage2") and getattr(self._current_fn_id, "module", None) == "main":
			import sys
			print(f"[drift:debug][stage2] return loc={ret_span}", file=sys.stderr)
		if ret_span == Span() and stmt.value is not None:
			ret_span = Span.from_loc(getattr(stmt.value, "loc", None))
		can_throw = self._fn_can_throw() is True
		fn_is_void = self._ret_type is not None and self._type_table.is_void(self._ret_type)

		if not can_throw:
			if fn_is_void:
				if stmt.value is not None:
					val_ty = self._infer_expr_type(stmt.value)
					if val_ty is None or self._type_table.is_void(val_ty):
						self._emit_scope_drops(scope_index=0)
						term = M.Return(value=None)
						if ret_span != Span():
							term.span = ret_span
						self.b.set_terminator(term)
						return
					raise AssertionError("Void function must not have a return value (checker bug)")
				self._emit_scope_drops(scope_index=0)
				term = M.Return(value=None)
				if ret_span != Span():
					term.span = ret_span
				self.b.set_terminator(term)
				return
			if stmt.value is None:
				raise AssertionError("non-void bare return reached MIR lowering (checker bug)")
			val = self.lower_expr(stmt.value, expected_type=self._ret_type)
			self._emit_scope_drops(scope_index=0)
			term = M.Return(value=val)
			if ret_span != Span():
				term.span = ret_span
			self.b.set_terminator(term)
			return

		# Can-throw function: surface `-> T` lowers to an internal
		# `FnResult<T, Error>` return. Wrap normal returns into Ok.
		if fn_is_void:
			if stmt.value is not None:
				val_ty = self._infer_expr_type(stmt.value)
				if val_ty is None or self._type_table.is_void(val_ty):
					res_val = self.b.new_temp()
					self.b.emit(M.ConstructResultOk(dest=res_val, value=None))
					self._emit_scope_drops(scope_index=0)
					term = M.Return(value=res_val)
					if ret_span != Span():
						term.span = ret_span
					self.b.set_terminator(term)
					return
				raise AssertionError("Void function must not have a return value (checker bug)")
			res_val = self.b.new_temp()
			self.b.emit(M.ConstructResultOk(dest=res_val, value=None))
			self._emit_scope_drops(scope_index=0)
			term = M.Return(value=res_val)
			if ret_span != Span():
				term.span = ret_span
			self.b.set_terminator(term)
			return
		if stmt.value is None:
			raise AssertionError("non-void bare return reached MIR lowering (checker bug)")
		val = self.lower_expr(stmt.value, expected_type=self._ret_type)
		res_val = self.b.new_temp()
		self.b.emit(M.ConstructResultOk(dest=res_val, value=val))
		self._emit_scope_drops(scope_index=0)
		term = M.Return(value=res_val)
		if ret_span != Span():
			term.span = ret_span
		self.b.set_terminator(term)

	def _visit_stmt_HBreak(self, stmt: H.HBreak) -> None:
		# Break jumps to the innermost loop's break target.
		if not self._loop_stack:
			raise NotImplementedError("break outside of loop not supported yet")
		_, break_target, loop_scope_index = self._loop_stack[-1]
		self._emit_scope_drops(scope_index=loop_scope_index)
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=break_target))

	def _visit_stmt_HContinue(self, stmt: H.HContinue) -> None:
		# Continue jumps to the innermost loop's continue target (loop header).
		if not self._loop_stack:
			raise NotImplementedError("continue outside of loop not supported yet")
		continue_target, _, loop_scope_index = self._loop_stack[-1]
		self._emit_scope_drops(scope_index=loop_scope_index)
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
		loop_scope_index = len(self._scope_stack)
		self._loop_stack.append((header.name, exit_block.name, loop_scope_index))

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

		if isinstance(stmt.value, H.HExceptionInit):
			err_val = self._construct_error_from_exception_init(stmt.value)
		else:
			# Throwing an existing Error value (e.g., from try-result sugar unwrap_err).
			err_val = self.lower_expr(stmt.value)

		# When throwing a new error from inside a catch arm, release the currently
		# caught error unless this throw is directly reusing that same local.
		if self._current_catch_error is not None:
			reuses_caught = False
			if isinstance(stmt.value, H.HVar):
				cand = self._canonical_local(getattr(stmt.value, "binding_id", None), stmt.value.name)
				reuses_caught = cand == self._current_catch_error
			elif isinstance(stmt.value, H.HPlaceExpr) and not stmt.value.projections and isinstance(stmt.value.base, H.HVar):
				cand = self._canonical_local(getattr(stmt.value.base, "binding_id", None), stmt.value.base.name)
				reuses_caught = cand == self._current_catch_error
			if not reuses_caught and self._current_catch_error not in self._moved_locals:
				caught_drop = self.b.new_temp()
				error_ty = self._type_table.ensure_error()
				self.b.emit(M.MoveOut(dest=caught_drop, local=self._current_catch_error, ty=error_ty))
				self._local_types[caught_drop] = error_ty
				self._moved_locals.add(self._current_catch_error)
				self.b.emit(M.DropValue(value=caught_drop, ty=error_ty))
		self._emit_captured_locals(err_val)

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
			self._emit_scope_drops(scope_index=0)
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
		error_ty = self._type_table.ensure_error()
		err_val = self.b.new_temp()
		self.b.emit(M.MoveOut(dest=err_val, local=self._current_catch_error, ty=error_ty))
		self._local_types[err_val] = error_ty
		self._moved_locals.add(self._current_catch_error)
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
			self.lower_block(stmt.body)
			return

		body_block = self.b.new_block("try_body")
		dispatch_block = self.b.new_block("try_dispatch")
		cont_block = self.b.new_block("try_cont")

		# Hidden local to carry the Error into the dispatch/catch blocks.
		error_local = f"__try_err{self.b.new_temp()}"
		self.b.ensure_local(error_local)
		# Track the hidden error slot type so downstream inference/codegen has a concrete type.
		error_ty = self._type_table.ensure_error()
		self._local_types[error_local] = error_ty
		self._register_drop_local(error_local, error_ty)
		err_zero = self.b.new_temp()
		self.b.emit(M.ZeroValue(dest=err_zero, ty=error_ty))
		self._local_types[err_zero] = error_ty
		self.b.emit(M.StoreLocal(local=error_local, value=err_zero))

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

		cont_reachable = False
		# Lower try body.
		self.b.set_block(body_block)
		self.lower_block(stmt.body)
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=cont_block.name))
			cont_reachable = True

		# Pop context before lowering dispatch so throws in catch bodies route to the outer try.
		# Rethrow reads the caught error from `_current_catch_error` (set while lowering each catch body).
		self._try_stack.pop()

		# Dispatch: load error, project event code, branch to arms.
		self.b.set_block(dispatch_block)
		err_tmp = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=err_tmp, local=error_local))
		code_tmp = self.b.new_temp()
		self.b.emit(M.ErrorEvent(dest=code_tmp, error=err_tmp))
		self._local_types[code_tmp] = self._uint64_type

		# Chain event-specific arms with IfTerminator, else falling through.
		event_arms = [(arm, cb) for arm, cb in catch_blocks if arm.event_fqn is not None]
		if event_arms:
			# We build a chain of Ifs; the final else falls through to the final resolution.
			current_block = dispatch_block
			for arm, cb in event_arms:
				self.b.set_block(current_block)
				arm_code = self._lookup_catch_event_code(arm.event_fqn)
				arm_code_const = self.b.new_temp()
				self.b.emit(M.ConstUint64(dest=arm_code_const, value=arm_code))
				self._local_types[arm_code_const] = self._uint64_type
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
			should_drop_caught_error = arm.binder is None
			catch_error_local = error_local
			binder_local: str | None = None
			if arm.binder:
				self.b.emit(M.MoveOut(dest=err_again, local=error_local, ty=error_ty))
				self._local_types[err_again] = error_ty
				self._moved_locals.add(error_local)
				binder_id = self._find_binder_binding_id(arm.binder, arm.block)
				binder_local = self._canonical_local(binder_id, arm.binder)
				self.b.ensure_local(binder_local)
				self._local_types[binder_local] = self._type_table.ensure_error()
				self.b.emit(M.StoreLocal(local=binder_local, value=err_again))
				catch_error_local = binder_local
			else:
				self.b.emit(M.LoadLocal(dest=err_again, local=error_local))
			code_again = self.b.new_temp()
			self.b.emit(M.ErrorEvent(dest=code_again, error=err_again))
			# Make the caught error available to `rethrow` inside this catch arm.
			prev_catch_err = self._current_catch_error
			self._current_catch_error = catch_error_local
			self.lower_block(arm.block)
			self._current_catch_error = prev_catch_err
			if self.b.block.terminator is None and should_drop_caught_error:
				err_done = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=err_done, local=error_local, ty=error_ty))
				self._local_types[err_done] = error_ty
				self._moved_locals.add(error_local)
				self.b.emit(M.DropValue(value=err_done, ty=error_ty))
			if self.b.block.terminator is None and binder_local is not None:
				binder_done = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=binder_done, local=binder_local, ty=error_ty))
				self._local_types[binder_done] = error_ty
				self._moved_locals.add(binder_local)
				self.b.emit(M.DropValue(value=binder_done, ty=error_ty))
			if self.b.block.terminator is None:
				self.b.set_terminator(M.Goto(target=cont_block.name))
				cont_reachable = True

		# Continue in cont.
		self.b.set_block(cont_block)
		if not cont_reachable and self.b.block.terminator is None:
			self.b.set_terminator(M.Unreachable())

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
		known_types = [t for t in elem_types if self._type_table.get(t).kind is not TypeKind.UNKNOWN]
		if known_types:
			first = known_types[0]
			if all(t == first for t in known_types):
				return first
			return self._unknown_type
		first = elem_types[0]
		if all(t == first for t in elem_types):
			return first
		return self._unknown_type

	def _unwrap_ref_type(self, ty: TypeId) -> TypeId:
		td = self._type_table.get(ty)
		if td.kind is TypeKind.REF and td.param_types:
			return td.param_types[0]
		return ty

	def _raw_buffer_elem_type(self, raw_ty: TypeId) -> TypeId:
		inst = self._type_table.get_struct_instance(raw_ty)
		if inst is None:
			return self._unknown_type
		base_td = self._type_table.get(inst.base_id)
		if base_td.kind is TypeKind.STRUCT and base_td.module_id == "std.mem" and base_td.name == "RawBuffer":
			if inst.type_args:
				return inst.type_args[0]
		return self._unknown_type

	def _raw_ptr_elem_type(self, ptr_ty: TypeId) -> TypeId:
		td = self._type_table.get(ptr_ty)
		if td.kind is TypeKind.RAW_PTR and td.param_types:
			return td.param_types[0]
		return self._unknown_type

	def _call_info_for_expr_optional(self, expr: H.HExpr) -> CallInfo | None:
		csid = getattr(expr, "callsite_id", None)
		if isinstance(csid, int):
			return self._call_info_by_callsite_id.get(csid)
		return None

	def _call_info_for(self, expr: H.HCall) -> CallInfo:
		info = self._call_info_for_expr_optional(expr)
		if info is None:
			raise AssertionError(
				f"missing call info for HCall callsite_id={getattr(expr, 'callsite_id', None)} (typecheck/call-info bug)"
			)
		return info

	def _call_info_for_method(self, expr: H.HMethodCall) -> CallInfo:
		info = self._call_info_for_expr_optional(expr)
		if info is None:
			raise AssertionError(
				f"missing call info for HMethodCall callsite_id={getattr(expr, 'callsite_id', None)} (typecheck/call-info bug)"
			)
		return info

	def _call_info_for_invoke(self, expr: H.HInvoke) -> CallInfo:
		info = self._call_info_for_expr_optional(expr)
		if info is None:
			raise AssertionError(
				f"missing call info for HInvoke callsite_id={getattr(expr, 'callsite_id', None)} (typecheck/call-info bug)"
			)
		return info

	def _resolve_map_insert_call_info(self, *, map_ty: TypeId, key_ty: TypeId, value_ty: TypeId) -> CallInfo:
		def _same_type_shape(expected: TypeId, actual: TypeId) -> bool:
			if expected == actual:
				return True
			exp = self._type_table.get(expected)
			act = self._type_table.get(actual)
			if exp.kind is not act.kind:
				return False
			if exp.kind is TypeKind.SCALAR:
				return exp.module_id == act.module_id and exp.name == act.name
			if exp.kind is TypeKind.REF:
				if bool(getattr(exp, "ref_mut", False)) != bool(getattr(act, "ref_mut", False)):
					return False
				if not exp.param_types or not act.param_types:
					return False
				return _same_type_shape(exp.param_types[0], act.param_types[0])
			if exp.kind is TypeKind.STRUCT:
				exp_inst = self._type_table.get_struct_instance(expected)
				act_inst = self._type_table.get_struct_instance(actual)
				if exp_inst is not None and act_inst is not None:
					exp_base = self._type_table.get(exp_inst.base_id)
					act_base = self._type_table.get(act_inst.base_id)
					if exp_base.module_id != act_base.module_id or exp_base.name != act_base.name:
						return False
					if len(exp_inst.type_args) != len(act_inst.type_args):
						return False
					return all(_same_type_shape(e, a) for e, a in zip(exp_inst.type_args, act_inst.type_args))
				return exp.module_id == act.module_id and exp.name == act.name
			return False

		matches: list[tuple[FunctionId, FnSignature]] = []
		for fn_id, sig in self._signatures_by_id.items():
			if not getattr(sig, "is_method", False):
				continue
			if getattr(sig, "method_name", None) != "insert":
				continue
			if bool(getattr(sig, "is_wrapper", False)):
				continue
			params = list(sig.param_type_ids or [])
			if len(params) != 3:
				continue
			recv = self._type_table.get(params[0])
			if recv.kind is not TypeKind.REF or not recv.param_types or not bool(getattr(recv, "ref_mut", False)):
				continue
			if not _same_type_shape(map_ty, recv.param_types[0]):
				continue
			if not _same_type_shape(key_ty, params[1]) or not _same_type_shape(value_ty, params[2]):
				continue
			if sig.return_type_id is None:
				continue
			matches.append((fn_id, sig))
		if not matches:
			raise AssertionError("map literal insert call target not found (checker bug)")
		if len(matches) > 1:
			inst = [m for m in matches if "__inst__" in m[0].name]
			if inst:
				matches = inst
		if len(matches) > 1:
			raise AssertionError("map literal insert call target is ambiguous (checker bug)")
		fn_id, sig = matches[0]
		can_throw = self._can_throw_by_id.get(fn_id)
		if can_throw is None:
			can_throw = True if sig.declared_can_throw is None else bool(sig.declared_can_throw)
		return CallInfo(
			target=CallTarget.direct(fn_id),
			sig=CallSig(
				param_types=tuple(sig.param_type_ids or []),
				user_ret_type=sig.return_type_id,
				can_throw=bool(can_throw),
			),
		)

	def _call_returns_void(self, expr: H.HExpr) -> bool:
		if isinstance(expr, H.HCall):
			info = self._call_info_for_expr_optional(expr)
			if info is not None:
				if info.sig.can_throw:
					# Can-throw calls return an internal FnResult value, even when the
					# surface ok type is Void.
					return False
				return self._type_table.is_void(info.sig.user_ret_type)
		if isinstance(expr, H.HMethodCall):
			info = self._call_info_for_expr_optional(expr)
			if info is not None:
				if info.sig.can_throw:
					return False
				return self._type_table.is_void(info.sig.user_ret_type)
		if isinstance(expr, H.HInvoke):
			info = self._call_info_for_expr_optional(expr)
			if info is not None:
				if info.sig.can_throw:
					return False
				return self._type_table.is_void(info.sig.user_ret_type)
		return False

	def _lower_call(self, expr: H.HCall) -> M.ValueId | None:
		# Invariant: all direct calls from HIR must have CallInfo and produce MIR
		# Call instructions with an explicit can_throw flag.
		info = self._call_info_for(expr)
		if info.target.kind is CallTargetKind.CONSTRUCTOR:
			return self._lower_constructor_call(expr, info)
		if info.target.kind is CallTargetKind.INTRINSIC:
			raise AssertionError("intrinsic call reached _lower_call (typecheck/call-info bug)")
		if info.target.kind is CallTargetKind.INDIRECT:
			return self._lower_indirect_call(expr.fn, expr.args, info)
		if info.target.kind is not CallTargetKind.DIRECT or not info.target.symbol:
			raise AssertionError("call missing direct CallTarget (typecheck/call-info bug)")
		target_fn_id = info.target.symbol
		if not isinstance(expr.fn, H.HVar):
			raise NotImplementedError("Only direct function-name calls are supported in MIR lowering")
		arg_vals = [self.lower_expr(a) for a in expr.args]
		expr_span = Span.from_loc(getattr(expr, "loc", None))
		cur_span = self._current_stmt_span
		call_span = expr_span if expr_span != Span() else (cur_span if cur_span is not None else Span())
		if drift_debug.enabled("stage2"):
			import sys
			print(f"[drift:debug][stage2] call spans fn={getattr(expr.fn, 'name', None)} expr={expr_span} stmt={cur_span} call={call_span}", file=sys.stderr)
		if drift_debug.enabled("stage2") and expr_span != Span() and call_span != expr_span:
			import sys
			print(f"[drift:debug][stage2] call span mismatch: expr={expr_span} call={call_span} fn={getattr(expr.fn, 'name', None)}", file=sys.stderr)
		prev_span = self.b.current_span
		if call_span != Span():
			self.b.current_span = call_span
		try:
			# Can-throw calls always return an internal FnResult value, even when the
			# surface ok type is Void.
			if info.sig.can_throw:
				dest = self.b.new_temp()
				call = M.Call(dest=dest, fn_id=target_fn_id, args=arg_vals, can_throw=True)
				if call_span != Span():
					call.span = call_span
				self.b.emit(call)
				self._local_types[dest] = call_abi_ret_type(info.sig, self._type_table)
				return dest
			if self._type_table.is_void(info.sig.user_ret_type):
				call = M.Call(dest=None, fn_id=target_fn_id, args=arg_vals, can_throw=False)
				if call_span != Span():
					call.span = call_span
				self.b.emit(call)
				return None
			dest = self.b.new_temp()
			call = M.Call(dest=dest, fn_id=target_fn_id, args=arg_vals, can_throw=False)
			if call_span != Span():
				call.span = call_span
			self.b.emit(call)
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		finally:
			self.b.current_span = prev_span

	def _lower_call_with_info(self, expr: H.HCall, info: CallInfo) -> M.ValueId | None:
		if info.target.kind is CallTargetKind.CONSTRUCTOR:
			return self._lower_constructor_call(expr, info)
		if info.target.kind is CallTargetKind.INTRINSIC:
			raise AssertionError("intrinsic call reached _lower_call_with_info (typecheck/call-info bug)")
		if info.target.kind is CallTargetKind.INDIRECT:
			return self._lower_indirect_call(expr.fn, expr.args, info)
		if info.target.kind is not CallTargetKind.DIRECT or not info.target.symbol:
			raise AssertionError("call missing direct CallTarget (typecheck/call-info bug)")
		target_fn_id = info.target.symbol
		arg_vals: list[M.ValueId] = []
		for idx, arg in enumerate(expr.args):
			param_ty = info.sig.param_types[idx] if idx < len(info.sig.param_types) else None
			arg_vals.append(self._lower_call_arg(arg, param_ty))
		expr_span = Span.from_loc(getattr(expr, "loc", None))
		cur_span = self._current_stmt_span
		call_span = expr_span if expr_span != Span() else (cur_span if cur_span is not None else Span())
		if drift_debug.enabled("stage2"):
			import sys
			print(f"[drift:debug][stage2] call spans fn={getattr(expr.fn, 'name', None)} expr={expr_span} stmt={cur_span} call={call_span}", file=sys.stderr)
		if drift_debug.enabled("stage2") and expr_span != Span() and call_span != expr_span:
			import sys
			print(f"[drift:debug][stage2] call span mismatch: expr={expr_span} call={call_span} fn={getattr(expr.fn, 'name', None)}", file=sys.stderr)
		prev_span = self.b.current_span
		if call_span != Span():
			self.b.current_span = call_span
		try:
			if info.sig.can_throw:
				dest = self.b.new_temp()
				call = M.Call(dest=dest, fn_id=target_fn_id, args=arg_vals, can_throw=True)
				if call_span != Span():
					call.span = call_span
				self.b.emit(call)
				self._local_types[dest] = call_abi_ret_type(info.sig, self._type_table)
				return dest
			if self._type_table.is_void(info.sig.user_ret_type):
				call = M.Call(dest=None, fn_id=target_fn_id, args=arg_vals, can_throw=False)
				if call_span != Span():
					call.span = call_span
				self.b.emit(call)
				return None
			dest = self.b.new_temp()
			call = M.Call(dest=dest, fn_id=target_fn_id, args=arg_vals, can_throw=False)
			if call_span != Span():
				call.span = call_span
			self.b.emit(call)
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		finally:
			self.b.current_span = prev_span

	def _lower_call_arg(self, arg: H.HExpr, param_ty: TypeId | None) -> M.ValueId:
		"""
		Lower a call argument, moving out of locals for non-Copy params.

		MVP rule: passing a non-Copy local by value consumes it. We only move
		out of plain locals (HVar) here; projected places must be moved
		explicitly via `move`.
		"""
		if param_ty is not None:
			ptd = self._type_table.get(param_ty)
			if ptd.kind is TypeKind.REF:
				return self.lower_expr(arg, expected_type=param_ty)
		if isinstance(arg, H.HVar) or (hasattr(H, "HPlaceExpr") and isinstance(arg, getattr(H, "HPlaceExpr"))):
			base = arg
			if hasattr(H, "HPlaceExpr") and isinstance(arg, getattr(H, "HPlaceExpr")):
				if arg.projections:
					return self.lower_expr(arg)
				base = arg.base
			if isinstance(base, H.HVar):
				arg_ty = self._infer_expr_type(base)
				if param_ty is not None:
					param_copy = self._type_table.copy_status(param_ty)
					if param_copy is False or param_copy is None:
						arg_ty = param_ty
				if arg_ty is not None:
					arg_copy = self._type_table.copy_status(arg_ty)
					if arg_copy is False or arg_copy is None:
						subj_name = self._canonical_local(getattr(base, "binding_id", None), base.name)
						self.b.ensure_local(subj_name)
						moved_val = self.b.new_temp()
						self.b.emit(M.MoveOut(dest=moved_val, local=subj_name, ty=arg_ty))
						self._local_types[moved_val] = arg_ty
						self._moved_locals.add(subj_name)
						return moved_val
		return self.lower_expr(arg, expected_type=param_ty)

	def _lower_constructor_call(self, expr: H.HCall, info: CallInfo) -> M.ValueId:
		variant_ty = info.target.variant_type_id
		struct_ty = info.target.struct_type_id
		ctor_name = info.target.ctor_name
		ctor_arg_field_indices = info.target.ctor_arg_field_indices
		pos_args = list(expr.args)
		kw_pairs = list(getattr(expr, "kwargs", []) or [])
		if pos_args and kw_pairs:
			raise AssertionError("constructor does not allow mixing positional and named arguments (checker bug)")
		if variant_ty is not None:
			if ctor_name is None:
				raise AssertionError("constructor call missing variant metadata (typecheck/call-info bug)")
			inst = self._type_table.get_variant_instance(variant_ty)
			if inst is None:
				raise AssertionError("variant instance missing for constructor call (type table bug)")
			arm_def = inst.arms_by_name.get(ctor_name)
			if arm_def is None:
				raise AssertionError("unknown constructor reached MIR lowering (checker bug)")
			field_names = list(getattr(arm_def, "field_names", []) or [])
			field_types = list(arm_def.field_types)
			if len(field_names) != len(field_types):
				raise AssertionError("variant ctor schema/type mismatch reached MIR lowering (checker bug)")
		elif struct_ty is not None:
			struct_def = self._type_table.get(struct_ty)
			if struct_def.kind is not TypeKind.STRUCT:
				raise AssertionError("constructor call resolved to non-STRUCT (checker bug)")
			struct_inst = self._type_table.get_struct_instance(struct_ty)
			if struct_inst is not None:
				field_names = list(struct_inst.field_names)
				field_types = list(struct_inst.field_types)
			else:
				field_names = list(struct_def.field_names or [])
				field_types = list(struct_def.param_types)
			if len(field_names) != len(field_types):
				raise AssertionError("struct ctor schema/type mismatch reached MIR lowering (checker bug)")
		else:
			raise AssertionError("constructor call missing variant/struct metadata (typecheck/call-info bug)")
		ordered: list[M.ValueId | None] = [None] * len(field_types)
		if kw_pairs:
			raise AssertionError("keyword arguments reached MIR lowering for a constructor (checker bug)")
		if ctor_arg_field_indices is not None:
			if len(pos_args) != len(ctor_arg_field_indices):
				raise AssertionError("constructor arg mapping arity mismatch reached MIR lowering (checker bug)")
			ordered = [None] * len(field_types)
			for arg_expr, field_idx in zip(pos_args, ctor_arg_field_indices):
				if field_idx < 0 or field_idx >= len(field_types):
					raise AssertionError("constructor field index out of range (checker bug)")
				if ordered[field_idx] is not None:
					raise AssertionError("constructor arg mapping duplicates field (checker bug)")
				field_ty = field_types[field_idx]
				arg_val = self._lower_call_arg(arg_expr, field_ty)
				if arg_val is None:
					if self._type_table.is_void(field_ty):
						ordered[field_idx] = None
						continue
					raise AssertionError("Void-returning call used in expression context (checker bug)")
				ordered[field_idx] = arg_val
		else:
			if len(pos_args) != len(field_types):
				raise AssertionError("constructor arity mismatch reached MIR lowering (checker bug)")
			for idx, (arg_expr, fty) in enumerate(zip(pos_args, field_types)):
				arg_val = self._lower_call_arg(arg_expr, fty)
				if arg_val is None:
					if self._type_table.is_void(fty):
						ordered[idx] = None
						continue
					raise AssertionError("Void-returning call used in expression context (checker bug)")
				ordered[idx] = arg_val
		for idx, v in enumerate(ordered):
			if v is None and not self._type_table.is_void(field_types[idx]):
				raise AssertionError("missing constructor field reached MIR lowering (checker bug)")
		arg_vals = [v for v in ordered if v is not None]
		dest = self.b.new_temp()
		if variant_ty is not None:
			self.b.emit(M.ConstructVariant(dest=dest, variant_ty=variant_ty, ctor=ctor_name, args=arg_vals))
			self._local_types[dest] = variant_ty
		else:
			self.b.emit(M.ConstructStruct(dest=dest, struct_ty=struct_ty, args=arg_vals))
			self._local_types[dest] = struct_ty
		return dest

	def _call_info_from_resolution(self, expr: H.HExpr) -> CallInfo | None:
		if self._typed_mode != "none":
			raise AssertionError(
				"call_resolutions-based CallInfo is not allowed in typed mode (typecheck/call-info bug)"
			)
		res = self._call_resolutions.get(expr.node_id)
		decl = getattr(res, "decl", None)
		if decl is None:
			return None
		target_fn_id = getattr(decl, "fn_id", None)
		if target_fn_id is None:
			return None
		sig = getattr(decl, "signature", None)
		if sig is None:
			return None
		params = list(getattr(sig, "param_types", []) or [])
		result_type = getattr(res, "result_type", None) or getattr(sig, "result_type", None)
		if result_type is None:
			return None
		call_can_throw = bool(self._can_throw_by_id.get(target_fn_id, True))
		info = CallInfo(
			target=CallTarget.direct(target_fn_id),
			sig=CallSig(
				param_types=tuple(params),
				user_ret_type=result_type,
				can_throw=bool(call_can_throw),
			),
		)
		csid = getattr(expr, "callsite_id", None)
		if isinstance(csid, int):
			self._call_info_by_callsite_id[csid] = info
		return info

	def _call_info_from_ufcs(self, expr: H.HCall) -> CallInfo | None:
		info = self._call_info_from_resolution(expr)
		if info is not None:
			return info
		return None

	def _lower_invoke(self, expr: H.HInvoke) -> M.ValueId | None:
		info = self._call_info_for_invoke(expr)
		return self._lower_indirect_call(expr.callee, expr.args, info)

	def _lower_indirect_call(
		self,
		callee_expr: H.HExpr,
		args: list[H.HExpr],
		info: CallInfo,
	) -> M.ValueId | None:
		callee_ty = self._infer_expr_type(callee_expr)
		if callee_ty is not None and self._type_table.get(callee_ty).kind is TypeKind.INTERFACE:
			raise AssertionError("interface calls must lower to CallIface, not CallIndirect")
		callee_val = self.lower_expr(callee_expr)
		arg_vals: list[M.ValueId] = []
		for idx, arg in enumerate(args):
			param_ty = info.sig.param_types[idx] if idx < len(info.sig.param_types) else None
			arg_vals.append(self._lower_call_arg(arg, param_ty))
		param_types = list(info.sig.param_types)
		if info.sig.can_throw:
			dest = self.b.new_temp()
			self.b.emit(
				M.CallIndirect(
					dest=dest,
					callee=callee_val,
					args=arg_vals,
					param_types=param_types,
					user_ret_type=info.sig.user_ret_type,
					can_throw=True,
				)
			)
			self._local_types[dest] = call_abi_ret_type(info.sig, self._type_table)
			return dest
		if self._type_table.is_void(info.sig.user_ret_type):
			self.b.emit(
				M.CallIndirect(
					dest=None,
					callee=callee_val,
					args=arg_vals,
					param_types=param_types,
					user_ret_type=info.sig.user_ret_type,
					can_throw=False,
				)
			)
			return None
		dest = self.b.new_temp()
		self.b.emit(
			M.CallIndirect(
				dest=dest,
				callee=callee_val,
				args=arg_vals,
				param_types=param_types,
				user_ret_type=info.sig.user_ret_type,
				can_throw=False,
			)
		)
		self._local_types[dest] = info.sig.user_ret_type
		return dest

	def _lower_iface_call(
		self,
		iface_expr: H.HExpr,
		args: list[H.HExpr],
		method_name: str,
		info: CallInfo,
	) -> M.ValueId | None:
		iface_val = self.lower_expr(iface_expr)
		arg_vals = [self.lower_expr(a) for a in args]
		param_types = list(info.sig.param_types)
		iface_ty = self._infer_expr_type(iface_expr)
		if iface_ty is None:
			raise AssertionError("interface call missing receiver type (checker bug)")
		if self._type_table.get(iface_ty).kind is not TypeKind.INTERFACE:
			raise AssertionError("interface call expects interface receiver (checker bug)")
		iface_inst = self._type_table.get_interface_instance(iface_ty)
		iface_base = iface_inst.base_id if iface_inst is not None else iface_ty
		owner_base, _method_schema = self._type_table.interface_method_lookup(iface_base, method_name)
		if owner_base != iface_base:
			offsets = self._type_table.interface_segment_offsets(iface_base)
			if owner_base not in offsets:
				raise AssertionError("interface method owner not in linearization (checker bug)")
			dest = self.b.new_temp()
			self.b.emit(M.IfaceUpcast(dest=dest, iface=iface_val, slot_offset=offsets[owner_base]))
			view_map = self._type_table.interface_instance_view_map(iface_ty)
			self._local_types[dest] = view_map.get(owner_base, iface_ty)
			iface_val = dest
		slot_index = self._type_table.interface_method_vtable_slot(iface_base, owner_base, method_name)
		if self._type_table.is_void(info.sig.user_ret_type):
			self.b.emit(
				M.CallIface(
					dest=None,
					iface=iface_val,
					args=arg_vals,
					param_types=param_types,
					user_ret_type=info.sig.user_ret_type,
					can_throw=info.sig.can_throw,
					slot_index=slot_index,
				)
			)
			return None
		dest = self.b.new_temp()
		self.b.emit(
			M.CallIface(
				dest=dest,
				iface=iface_val,
				args=arg_vals,
				param_types=param_types,
				user_ret_type=info.sig.user_ret_type,
				can_throw=info.sig.can_throw,
				slot_index=slot_index,
			)
		)
		self._local_types[dest] = info.sig.user_ret_type
		return dest

	def _lower_method_call_with_info(self, expr: H.HMethodCall, info: CallInfo) -> tuple[M.ValueId | None, CallInfo]:
		"""
		Lower a method call to a plain function call.

		We do not keep a distinct MIR method-call instruction in the v1 backend;
		it complicates codegen and duplicates resolution logic. Instead we resolve
		the method to a concrete symbol (e.g. `m.geom::Point::move_by`) and call it
		with the receiver as the first argument.

		Method calls are resolved using CallInfo (typed HIR); we do not re-resolve
		or guess can-throw in stage2.
		"""
		if getattr(expr, "kwargs", None):
			raise AssertionError("keyword arguments for method calls are not supported in MIR lowering (checker bug)")

		if info.target.kind is CallTargetKind.INDIRECT:
			recv_ty = self._infer_expr_type(expr.receiver)
			if recv_ty is None and self._expr_types and getattr(expr.receiver, "node_id", None) is not None:
				recv_ty = self._expr_types.get(expr.receiver.node_id)
			if recv_ty is not None and self._type_table.get(recv_ty).kind is TypeKind.INTERFACE:
				return self._lower_iface_call(expr.receiver, expr.args, expr.method_name, info), info
			return self._lower_indirect_call(expr.receiver, expr.args, info), info
		if info.target.kind is not CallTargetKind.DIRECT or not info.target.symbol:
			raise AssertionError(
				"method call missing direct CallTarget (typecheck/call-info bug): "
				f"{expr.method_name} callsite={getattr(expr, 'callsite_id', None)}"
			)
		target_fn_id = info.target.symbol
		symbol_name = function_symbol(target_fn_id)
		sig = self._signatures_by_id.get(target_fn_id)
		if sig is None or sig.self_mode is None:
			raise AssertionError(f"missing method signature/self_mode for '{symbol_name}' (typecheck bug)")
		self_mode = sig.self_mode
		recv_ty = self._infer_expr_type(expr.receiver)
		if recv_ty is None:
			raise AssertionError(
				"method receiver type unknown in MIR lowering (typecheck/inference bug): "
				f"{expr.method_name}(...)"
			)

		# Compute the receiver argument according to the method's receiver mode.
		#
		# - value: pass the receiver value as-is.
		# - ref/ref_mut: pass a pointer (`&T` / `&mut T`). If the receiver is a
		#   reference already, pass it directly; otherwise take the address of the
		#   receiver place (auto-borrow from lvalues only).
		param_types = list(getattr(info.sig, "param_types", []) or [])
		receiver_param_ty = param_types[0] if param_types else None
		receiver_arg: M.ValueId
		if self_mode == "value":
			receiver_arg = self._lower_call_arg(expr.receiver, receiver_param_ty)
		else:
			recv_def = self._type_table.get(recv_ty)
			if recv_def.kind is TypeKind.REF:
				receiver_arg = self.lower_expr(expr.receiver)
			else:
				# Auto-borrow from an lvalue receiver. We support `HVar` and canonical
				# `HPlaceExpr` receivers; other receiver expressions are not addressable
				# in MVP.
				place_expr = None
				if hasattr(H, "HPlaceExpr") and isinstance(expr.receiver, getattr(H, "HPlaceExpr")):
					place_expr = expr.receiver
				elif isinstance(expr.receiver, H.HVar):
					place_expr = H.HPlaceExpr(base=expr.receiver, projections=[], loc=Span())
				if place_expr is None:
					raise NotImplementedError("method auto-borrow requires an lvalue receiver in MVP")
				receiver_arg, _inner = self._lower_addr_of_place(place_expr, is_mut=(self_mode == "ref_mut"))

		arg_vals: list[M.ValueId] = [receiver_arg]
		for idx, arg in enumerate(expr.args):
			param_ty = param_types[idx + 1] if idx + 1 < len(param_types) else None
			arg_vals.append(self._lower_call_arg(arg, param_ty))

		# Can-throw calls always return an internal FnResult carrier value.
		if info.sig.can_throw:
			dest = self.b.new_temp()
			self.b.emit(M.Call(dest=dest, fn_id=target_fn_id, args=arg_vals, can_throw=True))
			return dest, info

		if self._type_table.is_void(info.sig.user_ret_type):
			self.b.emit(M.Call(dest=None, fn_id=target_fn_id, args=arg_vals, can_throw=False))
			return None, info

		dest = self.b.new_temp()
		self.b.emit(M.Call(dest=dest, fn_id=target_fn_id, args=arg_vals, can_throw=False))
		self._local_types[dest] = info.sig.user_ret_type
		return dest, info

	def _lower_method_call(self, expr: H.HMethodCall) -> tuple[M.ValueId | None, CallInfo]:
		info = self._call_info_for_method(expr)
		return self._lower_method_call_with_info(expr, info)

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
		ok_copy = self._type_table.copy_status(ok_ty)
		if ok_copy is True:
			self.b.emit(M.LoadLocal(dest=dest, local=ok_local))
		else:
			self.b.emit(M.MoveOut(dest=dest, local=ok_local, ty=ok_ty))
			self._moved_locals.add(ok_local)
		self._local_types[dest] = ok_ty
		return dest

	def _lower_can_throw_call_stmt(
		self,
		*,
		emit_call: callable,
		ok_ty: TypeId,
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
		if not self._type_table.is_void(ok_ty):
			ok_val = self.b.new_temp()
			self.b.emit(M.ResultOk(dest=ok_val, result=fnres_val))
			if self._type_table.copy_status(ok_ty) is not True:
				self.b.emit(M.DropValue(value=ok_val, ty=ok_ty))
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
			known = None
			if self._expr_types and self._typed_mode != "none":
				known = self._expr_types.get(expr.node_id)
			if known is not None:
				known_def = self._type_table.get(known)
				if known_def.kind is TypeKind.UNKNOWN and self._typed_mode == "strict":
					raise AssertionError("typed_mode strict: Unknown expr type encountered")
				if known_def.kind is TypeKind.SCALAR and known_def.name in ("Int", "Uint", "Uint64", "Byte"):
					return known
			return self._int_type
		if isinstance(expr, H.HCast):
			if self._expr_types and self._typed_mode != "none":
				known = self._expr_types.get(expr.node_id)
				if known is not None and self._type_table.get(known).kind is not TypeKind.UNKNOWN:
					return known
			te = getattr(expr, "target_type_expr", None)
			if te is not None:
				try:
					mod_id = getattr(te, "module_id", None) or self._current_module_name()
					tid = resolve_opaque_type(te, self._type_table, module_id=mod_id)
					if tid is not None and self._type_table.get(tid).kind is not TypeKind.UNKNOWN:
						return tid
				except Exception:
					return None
			return None
		if isinstance(expr, H.HVar):
			if self._lambda_capture_slots is not None:
				key = self._capture_key_for_expr(expr)
				if key is not None and self._lambda_env_field_types is not None and key in self._lambda_capture_slots:
					slot = self._lambda_capture_slots[key]
					field_ty = self._lambda_env_field_types[slot]
					if self._lambda_capture_kinds is not None and slot < len(self._lambda_capture_kinds):
						kind = self._lambda_capture_kinds[slot]
						if kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT):
							if not self._lambda_capture_ref_is_value:
								return field_ty
							td = self._type_table.get(field_ty)
							if td.kind is TypeKind.REF and td.param_types:
								return td.param_types[0]
					return field_ty
			local_name = self._canonical_local(getattr(expr, "binding_id", None), expr.name)
			local_ty = self._local_types.get(local_name)
			if local_ty is not None:
				# Prefer a concrete expr type over a placeholder local type.
				if self._expr_types and self._typed_mode != "none":
					known = self._expr_types.get(expr.node_id)
					if known is not None:
						known_def = self._type_table.get(known)
						local_def = self._type_table.get(local_ty)
						if local_def.kind in (TypeKind.UNKNOWN, TypeKind.TYPEVAR) and known_def.kind not in (
							TypeKind.UNKNOWN,
							TypeKind.TYPEVAR,
						):
							return known
				if getattr(expr, "binding_id", None) is not None:
					bid_ty = self._binding_types.get(int(expr.binding_id))
					if bid_ty is not None and self._type_table.get(bid_ty).kind not in (TypeKind.UNKNOWN, TypeKind.TYPEVAR):
						local_def = self._type_table.get(local_ty)
						bid_def = self._type_table.get(bid_ty)
						if local_def.kind in (TypeKind.UNKNOWN, TypeKind.TYPEVAR):
							return bid_ty
						if local_def.kind is TypeKind.SCALAR and bid_def.kind is TypeKind.SCALAR and local_ty != bid_ty:
							return bid_ty
				return local_ty
			if self._expr_types and self._typed_mode != "none":
				known = self._expr_types.get(expr.node_id)
				if known is not None:
					known_def = self._type_table.get(known)
					if known_def.kind is not TypeKind.UNKNOWN:
						return known
			if getattr(expr, "binding_id", None) is not None:
				bid_ty = self._binding_types.get(int(expr.binding_id))
				if bid_ty is not None:
					return bid_ty
			if getattr(expr, "binding_id", None) is None:
				const_mod = getattr(expr, "module_id", None) or self._current_module_name()
				const_val = self._type_table.lookup_const(f"{const_mod}::{expr.name}")
				if const_val is not None:
					const_ty, _ = const_val
					return const_ty
		if self._expr_types and self._typed_mode != "none":
			known = self._expr_types.get(expr.node_id)
			if known is not None:
				if self._type_table.get(known).kind is TypeKind.UNKNOWN:
					if self._typed_mode == "strict":
						raise AssertionError("typed_mode strict: Unknown expr type encountered")
				else:
					if drift_debug.enabled("local_types_trace") and isinstance(expr, H.HLiteralBool) and known != self._bool_type:
						import sys
						td = self._type_table.get(known)
						fn = self._current_fn_id
						print(f"[drift:debug][local_types_trace] fn={fn} expr=HLiteralBool node_id={expr.node_id} known={known}:{td.kind.name}:{td.name}", file=sys.stderr)
					return known
		if isinstance(expr, H.HLiteralFloat):
			return self._float_type
		if isinstance(expr, H.HLiteralBool):
			return self._bool_type
		if isinstance(expr, H.HLiteralString):
			return self._string_type
		if isinstance(expr, H.HDVInit):
			return self._dv_type
		if isinstance(expr, H.HFString):
			return self._string_type
		if hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
			# Best-effort: infer match result type from arm result expressions when
			# they are locally inferrable and identical.
			arm_tys: list[TypeId] = []
			for arm in expr.arms:
				if getattr(arm, "result", None) is None:
					return None
				ty = self._infer_expr_type(arm.result)  # type: ignore[arg-type]
				if ty is None:
					return None
				arm_tys.append(ty)
			if not arm_tys:
				return None
			first = arm_tys[0]
			if all(t == first for t in arm_tys):
				return first
			return None
		if isinstance(expr, H.HCall) and hasattr(H, "HQualifiedMember") and isinstance(expr.fn, getattr(H, "HQualifiedMember")):
			info = self._call_info_for_expr_optional(expr)
			if info is not None:
				return info.sig.user_ret_type
			return self._infer_qualified_ctor_variant_type(expr.fn, expr.args)
		if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
			info = self._call_info_for_expr_optional(expr)
			if info is not None:
				return info.sig.user_ret_type
			name = expr.fn.name
			# Struct constructor call: result is the struct TypeId.
			struct_ty: TypeId | None = None
			cur_mod = self._current_module_name()
			if "::" in name:
				parts = name.split("::")
				if len(parts) == 2:
					struct_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=parts[0], name=parts[1])
			else:
				struct_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=cur_mod, name=name) or self._type_table.find_unique_nominal_by_name(
					kind=TypeKind.STRUCT, name=name
				)
			if struct_ty is not None:
				return struct_ty
			if name == "string_concat":
				return self._string_type
			if name == "string_eq":
				return self._bool_type
			if name == "len" and expr.args:
				arg_ty = self._infer_expr_type(expr.args[0])
				if arg_ty is not None:
					td = self._type_table.get(arg_ty)
					if td.kind is TypeKind.ARRAY or (td.kind is TypeKind.SCALAR and td.name == "String"):
						return self._int_type
		if isinstance(expr, H.HInvoke):
			info = self._call_info_for_expr_optional(expr)
			if info is not None:
				return info.sig.user_ret_type
		if isinstance(expr, H.HFnPtrConst):
			return self._type_table.ensure_function(
				list(expr.call_sig.param_types),
				expr.call_sig.user_ret_type,
				can_throw=bool(expr.call_sig.can_throw),
			)
		if isinstance(expr, H.HField) and expr.name in ("len", "cap", "capacity"):
			subj_ty = self._infer_expr_type(expr.subject)
			if subj_ty is None:
				return None
			ty_def = self._type_table.get(subj_ty)
			if ty_def.kind is TypeKind.ARRAY or (ty_def.kind is TypeKind.SCALAR and ty_def.name == "String"):
				return self._int_type
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
		if hasattr(H, "HMapLiteral") and isinstance(expr, getattr(H, "HMapLiteral")):
			return self._expr_types.get(expr.node_id) if self._expr_types and getattr(expr, "node_id", None) is not None else None
		if isinstance(expr, H.HField):
			if self._lambda_capture_slots is not None:
				key = self._capture_key_for_expr(expr)
				if key is not None and self._lambda_env_field_types is not None and key in self._lambda_capture_slots:
					slot = self._lambda_capture_slots[key]
					field_ty = self._lambda_env_field_types[slot]
					if self._lambda_capture_kinds is not None and slot < len(self._lambda_capture_kinds):
						kind = self._lambda_capture_kinds[slot]
						if kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT):
							if not self._lambda_capture_ref_is_value:
								return field_ty
							td = self._type_table.get(field_ty)
							if td.kind is TypeKind.REF and td.param_types:
								return td.param_types[0]
					return field_ty
		if isinstance(expr, H.HUnary):
			# Unary ops preserve the numeric type when it can be inferred locally.
			inner = self._infer_expr_type(expr.expr)
			if inner is None:
				return None
			if expr.op is H.UnaryOp.NEG:
				return inner if inner in (self._int_type, self._float_type) else None
			if expr.op is H.UnaryOp.BIT_NOT:
				return inner if inner == self._uint_type else None
		if isinstance(expr, H.HBinary):
			# Minimal numeric/boolean inference to support:
			#   - materialized temporaries (`val tmp = 1 + 2; &tmp`)
			#   - basic arithmetic and comparisons in MIR typing.
			left = self._infer_expr_type(expr.left)
			right = self._infer_expr_type(expr.right)
			if left is None or right is None:
				return None
			# Arithmetic/bitwise operators return the operand type when both sides match.
			if expr.op in (
				H.BinaryOp.ADD,
				H.BinaryOp.SUB,
				H.BinaryOp.MUL,
				H.BinaryOp.DIV,
				H.BinaryOp.MOD,
				H.BinaryOp.BIT_AND,
				H.BinaryOp.BIT_OR,
				H.BinaryOp.BIT_XOR,
				H.BinaryOp.SHL,
				H.BinaryOp.SHR,
			):
				if left == right and left in (self._int_type, self._float_type, self._uint_type):
					return left
				return None
			# Comparisons return Bool when both sides are comparable scalars.
			if expr.op in (
				H.BinaryOp.EQ,
				H.BinaryOp.NE,
				H.BinaryOp.LT,
				H.BinaryOp.LE,
				H.BinaryOp.GT,
				H.BinaryOp.GE,
			):
				if left == right and left in (self._int_type, self._float_type, self._bool_type, self._string_type):
					return self._bool_type
				return None
			# Boolean logic returns Bool.
			if expr.op in (H.BinaryOp.AND, H.BinaryOp.OR):
				return self._bool_type if left == right == self._bool_type else None

		if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
			# Canonical place expression: its type is the type of the referenced
			# storage location (same as reading the lvalue).
			cur = self._infer_expr_type(expr.base)
			if cur is None:
				return None
			for proj in expr.projections:
				if isinstance(proj, H.HPlaceDeref):
					td = self._type_table.get(cur)
					if td.kind is not TypeKind.REF or not td.param_types:
						return None
					cur = td.param_types[0]
					continue
				if isinstance(proj, H.HPlaceField):
					info = self._type_table.struct_field(cur, proj.name)
					if info is None:
						return None
					_, cur = info
					continue
				if isinstance(proj, H.HPlaceIndex):
					td = self._type_table.get(cur)
					if td.kind is not TypeKind.ARRAY or not td.param_types:
						return None
					cur = td.param_types[0]
					continue
				return None
			return cur
		if isinstance(expr, H.HBorrow):
			inner = self._infer_expr_type(expr.subject)
			inner = inner if inner is not None else self._unknown_type
			return self._type_table.ensure_ref_mut(inner) if expr.is_mut else self._type_table.ensure_ref(inner)
		if hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
			# `move <place>` yields the underlying value type.
			return self._infer_expr_type(expr.subject)
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
			info = self._call_info_for_expr_optional(expr)
			if info is not None:
				return info.sig.user_ret_type
			recv_ty = self._infer_expr_type(expr.receiver)
			if recv_ty is not None:
				recv_def = self._type_table.get(recv_ty)
				if recv_def.kind is TypeKind.DIAGNOSTICVALUE:
					if expr.method_name == "as_int":
						return self._optional_variant_type(self._int_type)
					if expr.method_name == "as_bool":
						return self._optional_variant_type(self._bool_type)
					if expr.method_name == "as_float":
						return self._optional_variant_type(self._float_type)
					if expr.method_name == "as_string":
						return self._optional_variant_type(self._string_type)
					if expr.method_name == "as_object":
						return self._optional_variant_type(self._dv_type)
					if expr.method_name == "get":
						return self._optional_variant_type(self._dv_type)
		if hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
			return self._infer_expr_type(expr.attempt)
		return None

	def _infer_capture_type(self, expr: H.HExpr, key: C.HCaptureKey) -> TypeId | None:
		ty = self._infer_expr_type(expr)
		if ty is not None:
			return ty
		if self._expr_types and self._typed_mode != "none":
			known = self._expr_types.get(expr.node_id)
			if known is not None and self._type_table.get(known).kind is not TypeKind.UNKNOWN:
				return known
		return self._binding_types.get(int(key.root_local))

	def _find_binder_binding_id(self, binder: str, block: H.HBlock, result_expr: H.HExpr | None = None) -> int | None:
		found: int | None = None

		def walk_expr(expr: H.HExpr) -> None:
			nonlocal found
			if found is not None:
				return
			if isinstance(expr, H.HVar) and expr.name == binder and expr.binding_id is not None:
				found = int(expr.binding_id)
				return
			if isinstance(expr, H.HPlaceExpr):
				base = expr.base
				if isinstance(base, H.HVar) and base.name == binder and base.binding_id is not None:
					found = int(base.binding_id)
					return
				for proj in expr.projections:
					if isinstance(proj, H.HPlaceIndex):
						walk_expr(proj.index)
				return
			if isinstance(expr, H.HCall):
				walk_expr(expr.fn)
				for a in expr.args:
					walk_expr(a)
				for kw in expr.kwargs:
					walk_expr(kw.value)
				return
			if isinstance(expr, H.HMethodCall):
				walk_expr(expr.receiver)
				for a in expr.args:
					walk_expr(a)
				for kw in expr.kwargs:
					walk_expr(kw.value)
				return
			if isinstance(expr, H.HField):
				walk_expr(expr.subject)
				return
			if isinstance(expr, H.HIndex):
				walk_expr(expr.subject)
				walk_expr(expr.index)
				return
			if isinstance(expr, H.HBorrow):
				walk_expr(expr.subject)
				return
			if isinstance(expr, H.HTryExpr):
				walk_expr(expr.attempt)
				for arm in expr.arms:
					walk_block(arm.block)
					if arm.result is not None:
						walk_expr(arm.result)
				return
			if isinstance(expr, H.HMatchExpr):
				walk_expr(expr.subject)
				for arm in expr.arms:
					walk_block(arm.block)
					if arm.result is not None:
						walk_expr(arm.result)
				return
			if isinstance(expr, H.HUnary):
				walk_expr(expr.expr)
				return
			if isinstance(expr, H.HBinary):
				walk_expr(expr.left)
				walk_expr(expr.right)
				return
			if isinstance(expr, H.HTernary):
				walk_expr(expr.cond)
				walk_expr(expr.then_expr)
				walk_expr(expr.else_expr)
				return
			if isinstance(expr, H.HArrayLiteral):
				for e in expr.elements:
					walk_expr(e)
				return
			if hasattr(H, "HMapLiteral") and isinstance(expr, getattr(H, "HMapLiteral")):
				for e in expr.entries:
					walk_expr(e.key)
					walk_expr(e.value)
				return
			if isinstance(expr, H.HFString):
				for h in expr.holes:
					walk_expr(h.expr)
				return

		def walk_stmt(stmt: H.HStmt) -> None:
			nonlocal found
			if found is not None:
				return
			if isinstance(stmt, H.HExprStmt):
				walk_expr(stmt.expr)
				return
			if isinstance(stmt, H.HReturn) and stmt.value is not None:
				walk_expr(stmt.value)
				return
			if isinstance(stmt, H.HLet) and stmt.value is not None:
				walk_expr(stmt.value)
				return
			if isinstance(stmt, H.HAssign):
				walk_expr(stmt.target)
				walk_expr(stmt.value)
				return
			if isinstance(stmt, H.HAugAssign):
				walk_expr(stmt.target)
				walk_expr(stmt.value)
				return
			if isinstance(stmt, H.HIf):
				walk_expr(stmt.cond)
				walk_block(stmt.then_block)
				if stmt.else_block is not None:
					walk_block(stmt.else_block)
				return
			if isinstance(stmt, H.HLoop):
				walk_block(stmt.body)
				return
			if isinstance(stmt, H.HTry):
				walk_block(stmt.body)
				for arm in stmt.catches:
					walk_block(arm.block)
				return
			if isinstance(stmt, H.HBlock):
				walk_block(stmt)
				return
			if hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
				walk_block(stmt.block)
				return

		def walk_block(b: H.HBlock) -> None:
			for st in b.statements:
				walk_stmt(st)

		walk_block(block)
		if result_expr is not None:
			walk_expr(result_expr)
		return found

	def _infer_qualified_ctor_variant_type(
		self,
		qm: H.HQualifiedMember,
		args: list[H.HExpr],
		kwargs: list[H.HKeywordArg] | None = None,
		*,
		expected_type: TypeId | None = None,
	) -> TypeId | None:
		"""
		Best-effort inference of the concrete variant TypeId for a qualified ctor call.

		This supports `TypeRef::Ctor(args...)` lowering in stage2 without relying
		on typed-checker annotations. The typed checker is responsible for user-
		facing diagnostics; this helper returns None on underconstrained cases.
		"""
		base_te = getattr(qm, "base_type_expr", None)
		cur_mod = self._current_module_name()
		base_tid = resolve_opaque_type(base_te, self._type_table, module_id=cur_mod, allow_generic_base=True)
		td = self._type_table.get(base_tid)
		if td.kind is not TypeKind.VARIANT:
			# `resolve_opaque_type` is conservative for bare generic variant names;
			# prefer a declared variant base when present.
			name = getattr(base_te, "name", None)
			if isinstance(name, str):
				vb = self._type_table.get_variant_base(module_id=cur_mod, name=name) or self._type_table.get_variant_base(
					module_id="lang.core", name=name
				)
				if vb is not None:
					base_tid = vb
					td = self._type_table.get(base_tid)
		if td.kind is not TypeKind.VARIANT:
			return None

		# If an expected type exists and it is an instantiation of the same base
		# variant, prefer it. This allows underconstrained constructor calls like
		# `Optional::None()` to be typed via context (`val x: Optional<Int> = ...`).
		if expected_type is not None and self._type_table.get(expected_type).kind is TypeKind.VARIANT:
			inst = self._type_table.get_variant_instance(expected_type)
			if inst is not None and inst.base_id == base_tid:
				return expected_type
			if inst is None and expected_type == base_tid:
				return expected_type

		schema = self._type_table.get_variant_schema(base_tid)
		if schema is None:
			return None

		has_explicit_args = bool(getattr(base_te, "args", []) or [])
		if schema.type_params and not has_explicit_args:
			arm_schema = next((a for a in schema.arms if a.name == qm.member), None)
			if arm_schema is None:
				return None
			kw_pairs = list(kwargs or [])
			if kw_pairs and args:
				return None

			ordered_args: list[H.HExpr] = []
			if kw_pairs:
				by_name: dict[str, H.HExpr] = {}
				for kw in kw_pairs:
					if kw.name in by_name:
						return None
					by_name[kw.name] = kw.value
				for f in arm_schema.fields:
					if f.name not in by_name:
						return None
					ordered_args.append(by_name[f.name])
				if len(by_name) != len(arm_schema.fields):
					return None
			else:
				if len(args) != len(arm_schema.fields):
					return None
				ordered_args = list(args)

			inferred: list[TypeId | None] = [None for _ in schema.type_params]

			def unify(gexpr: GenericTypeExpr, actual: TypeId) -> None:
				if gexpr.param_index is not None:
					idx = int(gexpr.param_index)
					prev = inferred[idx]
					if prev is None:
						inferred[idx] = actual
					return
				name = gexpr.name
				sub = list(gexpr.args or [])
				if not sub:
					return
				td2 = self._type_table.get(actual)
				if name in {"&", "&mut"} and td2.kind is TypeKind.REF and td2.param_types:
					if name == "&mut" and not td2.ref_mut:
						return
					unify(sub[0], td2.param_types[0])
					return
				if name == "Array" and td2.kind is TypeKind.ARRAY and td2.param_types:
					unify(sub[0], td2.param_types[0])
					return
				if td2.kind is TypeKind.VARIANT and len(td2.param_types) == len(sub):
					for gsub, tsub in zip(sub, td2.param_types):
						unify(gsub, tsub)

			for f, arg_expr in zip(arm_schema.fields, ordered_args):
				arg_ty = self._infer_expr_type(arg_expr)
				if arg_ty is None:
					return None
				unify(f.type_expr, arg_ty)

			if any(t is None for t in inferred):
				return None
			return self._type_table.ensure_instantiated(base_tid, [t for t in inferred if t is not None])

		return base_tid

	def _lower_addr_of_place(self, expr: H.HPlaceExpr, *, is_mut: bool) -> tuple[M.ValueId, TypeId]:
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
		if self._lambda_capture_slots is not None:
			key = self._capture_key_for_expr(expr)
			if key is not None and self._lambda_env_field_types is not None and key in self._lambda_capture_slots:
				slot = self._lambda_capture_slots[key]
				field_ty = self._lambda_env_field_types[slot]
				kind = None
				if self._lambda_capture_kinds is not None and slot < len(self._lambda_capture_kinds):
					kind = self._lambda_capture_kinds[slot]
				if kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT):
					ptr_val = self._load_capture_slot_value(slot)
					td = self._type_table.get(field_ty)
					inner_ty = field_ty
					if td.kind is TypeKind.REF and td.param_types:
						inner_ty = td.param_types[0]
					return ptr_val, inner_ty
				env_ptr = self.b.new_temp()
				self.b.emit(M.LoadLocal(dest=env_ptr, local=self._lambda_env_local))
				addr = self.b.new_temp()
				self.b.emit(
					M.AddrOfField(
						dest=addr,
						base_ptr=env_ptr,
						struct_ty=self._lambda_env_ty,
						field_index=slot,
						field_ty=field_ty,
						is_mut=is_mut,
					)
				)
				return addr, field_ty
		# Canonical place expression (stage1→stage2 boundary).
		if self._typed_mode == "strict" and isinstance(expr.base, H.HVar) and expr.base.binding_id is None:
			raise AssertionError("typed_mode strict: missing binding_id for place base (checker bug)")
		base_name = self._canonical_local(getattr(expr.base, "binding_id", None), expr.base.name)
		self.b.ensure_local(base_name)
		cur_ty = self._infer_expr_type(expr.base)
		if cur_ty is None and isinstance(expr.base, H.HVar) and expr.base.binding_id is not None:
			bid_ty = self._binding_types.get(int(expr.base.binding_id))
			if bid_ty is not None:
				cur_ty = bid_ty
		if cur_ty is None and not expr.projections:
			cur_ty = self._type_table.ensure_unknown()
		if cur_ty is None:
			raise AssertionError("address-of place base type unknown in MIR lowering (checker bug)")
		addr = self.b.new_temp()
		self.b.emit(M.AddrOfLocal(dest=addr, local=base_name, is_mut=is_mut))

		# Apply projections left-to-right, maintaining the invariant:
		#   `addr` is a pointer to a value of type `cur_ty`.
		for proj in expr.projections:
			# Deref projection: load a reference value (pointer) from storage and
			# treat it as the new address.
			if isinstance(proj, H.HPlaceDeref):
				td = self._type_table.get(cur_ty)
				if td.kind is not TypeKind.REF or not td.param_types:
					raise AssertionError("deref place of non-ref reached MIR lowering (checker bug)")
				if is_mut and not td.ref_mut:
					raise AssertionError("mutable deref place without &mut reached MIR lowering (checker bug)")
				loaded_ptr = self.b.new_temp()
				self.b.emit(M.LoadRef(dest=loaded_ptr, ptr=addr, inner_ty=cur_ty))
				addr = loaded_ptr
				cur_ty = td.param_types[0]
				continue

			# Field projection: compute field address from a struct address.
			if isinstance(proj, H.HPlaceField):
				base_def = self._type_table.get(cur_ty)
				if base_def.kind is TypeKind.REF and base_def.param_types:
					if is_mut and not base_def.ref_mut:
						raise AssertionError("mutable field place without &mut reached MIR lowering (checker bug)")
					loaded_ptr = self.b.new_temp()
					self.b.emit(M.LoadRef(dest=loaded_ptr, ptr=addr, inner_ty=cur_ty))
					addr = loaded_ptr
					cur_ty = base_def.param_types[0]
					base_def = self._type_table.get(cur_ty)
				if base_def.kind is not TypeKind.STRUCT:
					fn_name = getattr(self.b.func, "name", None)
					raise AssertionError(
						"field place base is not a struct (checker bug): "
						f"{base_def.kind} for {cur_ty} base={base_name} field={proj.name} fn={fn_name}"
					)
				info = self._type_table.struct_field(cur_ty, proj.name)
				if info is None:
					raise AssertionError("unknown struct field reached MIR lowering (checker bug)")
				field_idx, field_ty = info
				dest = self.b.new_temp()
				self.b.emit(
					M.AddrOfField(
						dest=dest,
						base_ptr=addr,
						struct_ty=cur_ty,
						field_index=field_idx,
						field_ty=field_ty,
						is_mut=is_mut,
					)
				)
				addr = dest
				cur_ty = field_ty
				continue

			# Index projection: load the array value then compute element address.
			if isinstance(proj, H.HPlaceIndex):
				array_def = self._type_table.get(cur_ty)
				if array_def.kind is TypeKind.REF and array_def.param_types:
					if is_mut and not array_def.ref_mut:
						raise AssertionError("mutable index place without &mut reached MIR lowering (checker bug)")
					loaded_ptr = self.b.new_temp()
					self.b.emit(M.LoadRef(dest=loaded_ptr, ptr=addr, inner_ty=cur_ty))
					addr = loaded_ptr
					cur_ty = array_def.param_types[0]
					array_def = self._type_table.get(cur_ty)
				if array_def.kind is not TypeKind.ARRAY or not array_def.param_types:
					raise AssertionError("index place of non-array reached MIR lowering (checker bug)")
				elem_ty = array_def.param_types[0]
				array_val = self.b.new_temp()
				self.b.emit(M.LoadRef(dest=array_val, ptr=addr, inner_ty=cur_ty))
				index_val = self.lower_expr(proj.index)
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
				addr = dest
				cur_ty = elem_ty
				continue

			raise AssertionError("unsupported place projection reached MIR lowering (checker bug)")

		return addr, cur_ty

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


@dataclass
class _CapturedLocal:
	binding_id: int
	local_name: str
	source_name: str
	capture_name: str
