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
from lang.driftc.call_contract import intrinsic_call_issues, CtorFieldSpec, ctor_call_issues, array_method_arity_issues, call_kwargs_issues

# Callback / CallbackThrow intrinsic kind sets — derived from the
# central `call_resolver._CALLBACK_ARITIES` enumeration.  Adding a
# new arity is a one-line change in `call_resolver._CALLBACK_ARITY_MAX`
# plus the matching `IntrinsicKind` enum rows.
from lang.driftc.checker.call_resolver import _CALLBACK_ARITIES as _CB_ARITIES  # noqa: E402
_CALLBACK_INTRINSIC_KINDS = frozenset(
	getattr(IntrinsicKind, f"CALLBACK{n}") for n in _CB_ARITIES
) | frozenset(
	getattr(IntrinsicKind, f"CALLBACK_THROW{n}") for n in _CB_ARITIES
)
_CALLBACK_THROW_INTRINSIC_KINDS = frozenset(
	getattr(IntrinsicKind, f"CALLBACK_THROW{n}") for n in _CB_ARITIES
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
from lang.driftc.call_contract import call_contract_issues, repair_named_hcall_callinfo
from . import mir_nodes as M
from . import ownership_ledger_events as _ledger_events
from .ownership_ledger import DropVerdict as _DropVerdict


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


@dataclass(frozen=True, slots=True)
class DropPolicy:
	"""Single-source-of-truth drop/copy classification for a `TypeId`
	at the `hir_to_mir` layer.

	**Phase 1 contract** (part of the
	`fix/ownership-drop-ledger` track): every ownership-question
	consumer inside `HIRToMIR` — should a value be moved or copied at
	a transfer boundary? does a local need a scope-exit drop? can a
	type's bits be bitcopied freely? — reads `DropPolicy` via
	`HIRToMIR._drop_policy(ty)` rather than calling the underlying
	`TypeTable.copy_status` / `has_drop` / `is_bitcopy` /
	`is_destructible` queries directly.  The five axes below
	(`needs_drop`, `is_bitcopy`, `is_cheap_copy`, `is_destructible`,
	`has_structural_drop`) are the *only* dimensions emission sites
	are permitted to depend on; asking for anything finer than these
	must be expressed as a derived predicate on `DropPolicy`, not a
	bypass query to `TypeTable`.

	Direct `copy_status` / `has_drop` / `is_bitcopy` /
	`is_destructible` calls on `self._type_table` inside `HIRToMIR`
	are a Phase 1 contract violation and are flagged in review, with
	one explicit exception: a small set of **Phase 1 residuals** —
	sites that combine the raw queries in ways the five current
	`DropPolicy` axes don't express, and the typevar "unknown" branch
	in `_classify_value_transfer` that has no concrete-type analogue
	in `DropPolicy`.  Every residual site carries an inline comment
	of the form `# PHASE 1 RESIDUAL (kind).` naming the residual
	class — enumerate them with `rg "PHASE 1 RESIDUAL" lang/driftc/stage2/hir_to_mir.py`.
	(Line numbers aren't included in this docstring on purpose; in
	a file this long they rot on the next edit.)  Any NEW direct
	call to the underlying queries that is NOT labelled with that
	inline marker is a contract violation.  Phase 2 (per-program-
	point ownership ledger) subsumes every residual — either by
	adding a missing policy axis or by making the underlying query
	unreachable through a ledger-driven rewrite.  The computation of
	`_drop_policy` itself is NOT a residual (it IS the funnel) and
	is the one site where raw queries are not just permitted but
	required.

	The current semantics intentionally mirror the pre-Phase-1
	behaviour — this is a funnel, not a fix.  The actual semantic
	fix for the `match Optional<String>` double-drop UAF lives in
	Phase 2 (see Phase 0's fail-stop assertion in
	`_ensure_arm_scrut_ptr` for the current guardrail).  Phase 1
	buys: future tweaks (including Phase 2's) change one function,
	and contract tests pin the policy output per canonical type so
	those tweaks are loud instead of silent.
	"""
	# True iff scope-exit must emit a runtime drop operation for a
	# local of this type.  POD types are False.  Refcounted scalar
	# (`String`), structural-with-drop (`Optional<String>`,
	# `Array<String>`, structs with droppable fields), and
	# user-`Destructible` types are True.
	needs_drop: bool
	# True iff the value's bits are semantically an independent
	# owner with no shared refcounts or pointers-to-shared.  POD
	# types (`Int`, `Bool`, bitcopy structs of PODs) are True;
	# refcounted and structural-with-drop types are False.  The
	# compiler is allowed to bitcopy any value whose policy has
	# `is_bitcopy=True` at any ownership-transfer boundary.
	is_bitcopy: bool
	# True iff the compiler can emit a cheap Copy for this type at
	# an ownership-transfer boundary — a single bitcopy for POD
	# types, or a single refcount retain for refcounted scalar
	# types.  Structural-with-drop and user-Destructible types are
	# False (a correct copy would require a per-field traversal
	# that is not "cheap" in the Phase-1 sense).  This is the
	# question that governs the `match` scrutinee MoveOut-vs-Copy
	# decision in `_ensure_arm_scrut_ptr` and the binder path
	# decision in the match-arm binder loop.
	#
	# NOTE (Phase 1 semantic-preserving note): today's computation
	# faithfully mirrors the pre-Phase-1 `_should_copy_value` —
	# i.e. "Copy trait marker is True AND the type does not need
	# runtime drop."  That rule is what permitted the
	# `match Optional<String>` UAF when a packaged callee resolved
	# `copy_status` eagerly; the actual fix is Phase 2's ledger.
	# Phase 1 documents the behaviour and pins it under test so
	# Phase 2's tightening is visible as a diff here.
	is_cheap_copy: bool
	# True iff the type is `core.Destructible` (has a user
	# `destroy(self)` impl) OR its drop model otherwise requires a
	# Destructible-aware cleanup epilogue.  Governs
	# `param_drop_status` assignment and the `_emit_scope_drops`
	# cleanup path for locals that are destructible but may not
	# need a "bare" runtime drop.  Distinct from `needs_drop` —
	# some Destructible types are also `needs_drop`, but the
	# `_emit_scope_drops` walk cleans up destructible-but-not-
	# drop-needed locals via a dedicated branch.
	is_destructible: bool
	# True iff the type's STRUCTURE contains a drop-bearing child,
	# independent of the Copy-trait shortcut that `needs_drop`
	# observes.  Equivalent to `TypeTable.has_drop(ty)`: walks the
	# variant/struct/array structure and returns True if any
	# transitive field requires runtime drop (refcount release,
	# destructor call, etc.).
	#
	# This axis exists specifically because the Phase 0 fail-stop
	# in `_ensure_arm_scrut_ptr` must diverge from `needs_drop` on
	# exactly the bug shape: a type can have
	# `has_structural_drop=True` AND `needs_drop=False` when
	# `copy_status=True` (the packaged-load miscalculation) — that
	# is the precondition the fail-stop targets.  Routing the
	# fail-stop through `needs_drop` would silently suppress it
	# under the same shortcut that causes the original UAF, which
	# is what this axis avoids.  Phase 2 (the ledger) will
	# reconcile `needs_drop` with `has_structural_drop` so the
	# divergence becomes impossible at the policy layer; until
	# then, `has_structural_drop` is the correct query for any
	# invariant that must NOT be fooled by the Copy-trait shortcut.
	has_structural_drop: bool


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
		# Stack of (continue_target, break_target, scope_index, break_seen) for nested loops.
		self._loop_stack: list[tuple[str, str, int, bool]] = []
		# Stack of scopes; each scope stores locals that need drop at scope exit.
		self._scope_stack: list[list[str]] = []
		# Phase 4 site-1 patch 6c (2026-04-24): `_moved_locals`,
		# `_moved_at_scope_index`, `_local_decl_scope_index`,
		# `_mark_moved`, `_scope_drop_verdict`, and `_emit_scope_drops`
		# all retired.  Site 1's drop authority is now `verdict_at` on
		# the post-build `_ownership_ledger` via the `M.CleanupHook` +
		# `cleanup_authoring` path; HIR-side move bookkeeping is no
		# longer consulted by any production cleanup decision.
		# SSA temps produced by reading a non-bitcopy field from a &T
		# (aliased pointers into the borrowed struct's memory).  These MUST
		# be deep-copied before any ownership transfer (struct/variant
		# construction, return, variable binding) to prevent double-free.
		self._ref_field_temps: set[str] = set()
		self._current_stmt_span: Span | None = None
		# Stack of try contexts for nested try/catch (innermost on top).
		self._try_stack: list["_TryCtx"] = []
		# Error value bound by the innermost catch block (if any) for rethrow.
		self._current_catch_error: M.ValueId | None = None
		# Lazily-materialized typed-catch-binder storage.  Keyed by the
		# binder's binding_id (globally unique per fn); value is
		# (storage_local_name, struct_type_id).  Populated on first
		# field projection via `_get_or_materialize_typed_catch_storage`;
		# enables `&e.field` (HBorrow path) by giving the field a real
		# struct local to take the address of.  HField direct-read path
		# (existing legacy per-access JSON-decode helper) is unchanged
		# for now; future unification into the storage model is a
		# follow-up.
		self._typed_catch_binder_storage: dict[int, tuple[str, "TypeId"]] = {}
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
		# Slice 7c-3 (ABI 14): `_dv_type` field deleted along with
		# `TypeKind.DIAGNOSTICVALUE`.
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
		# In destroy(), self IS included in _param_drop_locals so that
		# scope-exit drops self using the LIVE post-mutation value.
		# The codegen guard (fn_id match) prevents recursive destroy()
		# and instead emits plain field drops for non-Destructible fields.
		self._param_drop_locals: list[str] = []
		# Detect if this function is a Destructible::destroy impl method.
		# If so, `self` MUST be in _param_drop_locals regardless of what
		# _needs_runtime_drop returns.  During package builds, the type
		# table may not have complete struct instances or trait resolution
		# for cross-package generic instantiations (e.g. Arc<AtomicBool>),
		# causing _needs_runtime_drop to return False for self.  But by
		# definition, destroy() is called TO drop self — omitting the
		# scope drop causes the codegen field-cleanup epilogue to never run.
		_is_destroy_method = (
			current_fn_id is not None
			and "std.core.Destructible::destroy" in getattr(current_fn_id, "name", "")
		)
		if param_types:
			for name, ty in param_types.items():
				# Phase 1 contract: every axis read by the param-drop
				# classifier goes through the policy funnel, including
				# the `string_arc_managed` branch that previously read
				# `TypeTable.has_drop` directly.  Compute once per
				# param so the classification never sees two different
				# snapshots of the underlying queries (the exact
				# predicate-drift class the funnel was built to
				# prevent).
				param_policy = self._drop_policy(ty)
				if _is_destroy_method and name == "self":
					self._param_drop_locals.append(name)
					self.b.func.param_drop_status[name] = "scope_exit_drop"
				elif param_policy.needs_drop:
					self._param_drop_locals.append(name)
					self.b.func.param_drop_status[name] = "scope_exit_drop"
				elif param_policy.is_destructible:
					# Destructible types that are also Copy still need
					# scope-exit drops so the codegen inside-destroy
					# epilogue can drop their fields.
					self._param_drop_locals.append(name)
					self.b.func.param_drop_status[name] = "scope_exit_drop"
				elif param_policy.has_structural_drop:
					# Structural drop needed but the generic path
					# returned `needs_drop=False` — this type is
					# handled by string_arc (e.g. String, Array,
					# Error, Interface) on a parallel track.  Use
					# the shortcut-free axis so
					# we classify correctly even under the packaged-
					# load `copy_status` resolution that flips
					# `needs_drop` to False.
					self.b.func.param_drop_status[name] = "string_arc_managed"
				else:
					self.b.func.param_drop_status[name] = "no_drop"
		# Block-scope constants: binding_id → (TypeId, value).
		# Populated by _visit_stmt_HLocalConst; consulted by _visit_expr_HVar.
		self._local_consts: dict[int, tuple[TypeId, object]] = {}
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
		# Phase 3A ownership-ledger observational recording.  Gated once
		# per HIRToMIR instance: when disabled, `_drop_decision_log` is
		# None and `_record_drop_decision` is a single-attribute-read
		# no-op on the hot path.  When enabled, the log is shared by
		# reference with `builder.func` so the driver can drain without
		# holding a HIRToMIR handle.
		if drift_debug.enabled("ownership_ledger"):
			self._drop_decision_log: _ledger_events.DropDecisionLog | None = (
				_ledger_events.DropDecisionLog(fn_name=self.b.func.name)
			)
			setattr(self.b.func, "_drop_decision_log", self._drop_decision_log)
		else:
			self._drop_decision_log = None
		# Phase 4 site-1 patch 1: per-function counter for
		# `M.CleanupHook.scope_id`.  Used for telemetry correlation
		# only; the authoring pass does not consume it for emission
		# decisions.
		self._cleanup_hook_counter: int = 0
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

	def _drop_policy(self, ty: TypeId) -> DropPolicy:
		"""Compute the Phase-1 single-source-of-truth drop/copy
		policy for `ty` — see the `DropPolicy` docstring for the
		contract.

		This function is the canonical site for `TypeTable.copy_status`
		/ `has_drop` / `is_bitcopy` / `is_destructible` queries inside
		`HIRToMIR`.  Every plain policy-axis read must go through the
		resulting `DropPolicy` via this function or the thin wrappers
		(`_needs_runtime_drop`, `_should_copy_value`,
		`_classify_value_transfer`, `_type_is_destructible`).

		The class docstring enumerates a small set of **documented
		Phase 1 residuals** — sites that still read the underlying
		queries directly because they combine them in ways the five
		current `DropPolicy` axes don't express (e.g. the "Copy but
		non-bitcopy" predicate used in `ArrayElemTake`/`CopyValue`
		dispatchers, and the typevar "unknown" branch in
		`_classify_value_transfer`).  Enumerate the live residuals
		with `rg "PHASE 1 RESIDUAL" lang/driftc/stage2/hir_to_mir.py`
		— line numbers aren't quoted here because they rot on every
		surrounding edit.  Residuals MUST carry an inline comment
		naming them and MUST be subsumed by Phase 2 (the per-
		program-point ownership ledger), either by adding the missing
		policy axis or by making the underlying query unreachable
		through a ledger-driven rewrite.  Any NEW direct call to
		`copy_status` / `has_drop` / `is_bitcopy` /
		`is_destructible` on `self._type_table` inside `HIRToMIR`
		that is NOT on the documented-residuals list is a Phase 1
		contract violation and is rejected at review.

		Semantics are intentionally identical to the pre-Phase-1
		behaviour.  The actual semantic fix for the
		`match Optional<String>` double-drop is Phase 2 (the per-
		program-point ownership ledger); Phase 1 just centralises
		the computation so Phase 2's change is a one-function edit
		and the pinned contract tests make the diff loud.
		"""
		# Query every underlying fact EXACTLY ONCE per call.  The
		# whole point of the funnel is that one interpretation of
		# the type-table snapshot drives every policy axis — querying
		# `copy_status` twice (or `has_drop` twice) in the same
		# function would re-open the predicate-drift class the funnel
		# was built to close, even if the type-table is currently
		# stable.  Each of the five axes below is a pure derivation
		# from these four snapshots plus the `_unknown_type` check
		# and the DV transitive walk.
		if ty == self._unknown_type:
			# Unknown-type short-circuit: no meaningful policy; return
			# the safe-conservative baseline (no drop, move-transfer).
			return DropPolicy(
				needs_drop=False,
				is_bitcopy=False,
				is_cheap_copy=False,
				is_destructible=False,
				has_structural_drop=False,
			)

		try:
			is_bitcopy = bool(self._type_table.is_bitcopy(ty))
		except Exception:
			is_bitcopy = False
		try:
			copy_status = self._type_table.copy_status(ty)
		except Exception:
			copy_status = None
		try:
			raw_has_drop = bool(self._type_table.has_drop(ty))
		except Exception:
			raw_has_drop = False
		try:
			raw_is_destructible = bool(self._type_table.is_destructible(ty))
		except Exception:
			raw_is_destructible = False
		# Slice 7c-3 (ABI 14): `_contains_dv_transitive` is gone with
		# `TypeKind.DIAGNOSTICVALUE`.  needs_drop / has_structural_drop
		# now collapse to the underlying `has_drop` / `is_destructible`
		# signals.

		# --- needs_drop ----------------------------------------
		# Driven by destruction reality.  A type with `has_drop=True`
		# (refcount release, user destructor, structural drop) MUST
		# be dropped at scope exit, regardless of `copy_status`.  The
		# pre-fix Copy short-circuit treated `Copy` as "no drop
		# needed" — wrong for refcounted scalars like `String`
		# (copy_status=True AND has_drop=True; bytes are bitcopyable
		# but the refcount must be released) and for variants/
		# structs that contain `String`/`Array<…>`/destructible
		# fields.  Pinned by
		# `lang/tests/driver/test_drop_policy_copy_short_circuit_bug.py`.
		needs_drop = bool(raw_has_drop or raw_is_destructible)

		# --- has_structural_drop -------------------------------
		# Shortcut-free drop query: `raw_has_drop` direct.  The
		# phase-0 fail-stop reads THIS axis, not `needs_drop`,
		# because it must NOT be fooled by the Copy-trait shortcut
		# that the UAF exploited.
		has_structural_drop = bool(raw_has_drop)

		# --- is_cheap_copy -------------------------------------
		# Decoupled from `needs_drop`.  A type is "cheap copy" when
		# its Copy semantics can be implemented with a single bitcopy
		# or a single retain: POD bitcopy types, refcounted SCALAR
		# types (`String` — one retain), and Copy structural types
		# whose payload has no drop work (POD variants/structs).
		# Structural-with-drop (`Optional<String>`, `Array<String>`,
		# struct-with-droppable-fields) requires per-field traversal
		# and is NOT cheap.
		td_for_kind = self._type_table.get(ty)
		is_scalar_kind = td_for_kind.kind is TypeKind.SCALAR
		is_cheap_copy = (copy_status is True) and (
			is_bitcopy or is_scalar_kind or not has_structural_drop
		)

		# --- is_destructible -----------------------------------
		is_destructible = raw_is_destructible

		return DropPolicy(
			needs_drop=needs_drop,
			is_bitcopy=is_bitcopy,
			is_cheap_copy=is_cheap_copy,
			is_destructible=is_destructible,
			has_structural_drop=has_structural_drop,
		)

	def _type_is_destructible(self, ty: TypeId) -> bool:
		"""Thin wrapper over `_drop_policy` — Phase 1 contract.

		Kept as a named predicate so ~40 downstream call sites don't
		all have to be rewritten in one change.  New code should
		prefer `self._drop_policy(ty).is_destructible` directly when
		the call site also inspects other axes.
		"""
		return self._drop_policy(ty).is_destructible

	def _needs_runtime_drop(self, ty: TypeId) -> bool:
		"""Thin wrapper over `_drop_policy` — Phase 1 contract.

		See `DropPolicy` + `_drop_policy` for the semantics and the
		Phase 1 funnel rationale.
		"""
		return self._drop_policy(ty).needs_drop

	# Slice 7c-3 (ABI 14): `_contains_dv_transitive` deleted along
	# with `TypeKind.DIAGNOSTICVALUE`.

	def _can_inline_copy(self, ty: TypeId, visiting: set[TypeId]) -> bool:
		"""Check whether a type's copy can be fully inlined at compile time.
		Returns False for self-referential types that would require a
		recursive runtime clone (which we don't yet support)."""
		if self._drop_policy(ty).is_bitcopy:
			return True
		if ty in visiting:
			return False
		visiting.add(ty)
		td = self._type_table.get(ty)
		if td.kind is TypeKind.SCALAR and td.name == "String":
			return True
		if td.kind is TypeKind.ARRAY and td.param_types:
			return self._can_inline_copy(td.param_types[0], visiting)
		if td.kind is TypeKind.STRUCT:
			inst = self._type_table.get_struct_instance(ty)
			if inst is not None:
				for ft in inst.field_types:
					if not self._can_inline_copy(ft, visiting):
						return False
			return True
		if td.kind is TypeKind.VARIANT:
			inst = self._type_table.get_variant_instance(ty)
			if inst is not None:
				for arm in inst.arms:
					for ft in arm.field_types:
						if not self._can_inline_copy(ft, visiting):
							return False
			return True
		# For other types (FORWARD_NOMINAL, TYPEVAR, etc.) be conservative.
		return False

	def _should_copy_value(self, ty: TypeId) -> bool:
		"""Thin wrapper over `_drop_policy` — Phase 1 contract.

		Returns True iff the type supports a cheap-copy transfer
		(POD bitcopy or refcount retain); False otherwise.  The
		unresolved-typevar branch lives in
		`_classify_value_transfer` — callers of `_should_copy_value`
		treat typevar as "False (move-safe)" implicitly.
		"""
		return self._drop_policy(ty).is_cheap_copy

	def _classify_value_transfer(self, ty: TypeId, *, allow_unknown_typevar: bool = False) -> str:
		"""Single-source ownership transfer decision for lowered values.

		Returns:
		- "copy": value can be semantically copied at this boundary.
		- "move": value must be moved (or consumed) to preserve ownership.
		- "unknown": unresolved typevar (allowed only when explicitly requested).

		Phase 1 note: the copy/move decision now flows through
		`_drop_policy(ty).is_cheap_copy`; the "unknown" branch for
		typevars remains out-of-band because `DropPolicy` is
		intentionally a concrete-type answer.  If a future phase
		needs typevar-aware policy, extend `DropPolicy` rather than
		re-introducing a parallel classification path here.
		"""
		td = self._type_table.get(ty)
		# Typevar handling stays outside the policy funnel — the
		# policy answers for concrete types only; typevar classification
		# is a call-site contract (the caller explicitly opts in to
		# "unknown" via `allow_unknown_typevar`).
		if td.kind is TypeKind.TYPEVAR and allow_unknown_typevar:
			# PHASE 1 RESIDUAL (typevar-unknown).  `DropPolicy` is a
			# concrete-type answer; there is no axis that distinguishes
			# "unknown because typevar unresolved" from "decided False".
			# Preserve the pre-Phase-1 behaviour of returning "unknown"
			# only when `copy_status` is itself None (i.e. the typevar
			# is genuinely unresolved); a typevar whose `copy_status`
			# is already decided by a proof falls through to the
			# concrete branch.  Phase 2 subsumes this by either adding
			# a typevar-unknown axis to `DropPolicy` or by ensuring the
			# ledger-driven rewrite never asks classification for an
			# unresolved typevar.
			try:
				cs = self._type_table.copy_status(ty)
			except Exception:
				cs = None
			if cs is None:
				return "unknown"
		policy = self._drop_policy(ty)
		return "copy" if policy.is_cheap_copy else "move"

	def _copy_if_ref_alias(self, value: M.ValueId, ty: TypeId) -> M.ValueId:
		"""If *value* is an aliased temp from a &T field read, emit a deep copy
		so the caller receives a freshly-owned value.  Otherwise return *value*
		unchanged.  This must be called at every ownership-transfer boundary
		(struct/variant construction, return, variable binding, call args)."""
		if value not in self._ref_field_temps:
			return value
		self._ref_field_temps.discard(value)
		if self._drop_policy(ty).is_bitcopy:
			return value
		td = self._type_table.get(ty)
		if td.kind is TypeKind.ARRAY and td.param_types:
			dup = self.b.new_temp()
			self.b.emit(M.ArrayDup(dest=dup, elem_ty=td.param_types[0], array=value))
			self._local_types[dup] = ty
			return dup
		# Any type that requires a runtime clone-on-read-from-ref must
		# go through CopyValue here.  STRUCT/VARIANT contain refcounted
		# fields (e.g. String) and a raw LoadRef from &T produces a
		# bitwise alias, not an owned copy.
		if td.kind in (TypeKind.STRUCT, TypeKind.VARIANT) or (
			td.kind is TypeKind.SCALAR and td.name == "String"
		):
			copy = self.b.new_temp()
			self.b.emit(M.CopyValue(dest=copy, value=value, ty=ty))
			self._local_types[copy] = ty
			return copy
		return value

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
		if not self._scope_stack:
			return
		if not self._needs_runtime_drop(ty) and not self._type_is_destructible(ty):
			return
		scope = self._scope_stack[-1]
		if local_name in scope:
			return
		scope.append(local_name)

	def _materialize_owned_temp(
		self,
		*,
		name_prefix: str,
		ty: TypeId,
		value: "M.ValueId | Callable[[], M.ValueId]",
	) -> str:
		"""The blessed way to allocate a synthetic local that stores
		an OWNED value across any control flow.  Encapsulates the
		four steps that must always go together:

		  1. `ensure_local(local)`              -- alloca'd in the function
		  2. `_local_types[local] = ty`         -- type table for SSA/codegen
		  3. `_register_drop_local(local, ty)`  -- scope-exit cleanup
		  4. `emit(StoreLocal(local, value))`   -- initialize the slot

		Returns the synthesized local name.  Caller emits
		`AddrOfLocal` or further reads/writes against the returned
		name as needed.

		`value` may be either:
		  - an `M.ValueId` (a `str`): used directly as the
		    StoreLocal value;
		  - a zero-arg callable that produces an `M.ValueId`:
		    invoked AFTER the temp slot is allocated (steps 1-3)
		    but BEFORE the StoreLocal emit.  Use this when the
		    value computation needs `b.new_temp()` calls of its
		    own -- e.g. `lambda: self.lower_expr(expr.subject)`
		    in the HBorrow rvalue fallback.  Preserves
		    byte-identical IR with the pre-helper hand-rolled
		    pattern (slot id allocated FIRST, then value's temps).

		Use this anywhere the synthesized local will hold a
		materialized owned value that must participate in scope
		cleanup (i.e. its inner refcounted fields must be released
		when the enclosing scope exits).  Do NOT use for ephemeral
		SSA-style temps that are consumed immediately within the
		same expression -- those have no scope-cleanup concern and
		use `b.new_temp()` directly.

		Forgetting `_register_drop_local` at hand-rolled sites is
		the recurring leak class fixed across `__borrow_tmp`
		(2026-05-16), `__exc_params_view_*` (Cluster 1, 2026-05-06),
		and `__cap_move_*` -- the helper makes it structurally
		impossible to forget.  Adoption is enforced by
		`lang/tests/driver/test_owned_temp_materialization_audit.py`.

		`_register_drop_local` is internally idempotent and gated
		by `_needs_runtime_drop` / `_type_is_destructible`, so
		unconditional call here is safe for trivially-droppable `ty`
		(Int, Bool, etc.) -- the registration just no-ops.
		"""
		local = f"{name_prefix}{self.b.new_temp()}"
		self.b.ensure_local(local)
		self._local_types[local] = ty
		self._register_drop_local(local, ty)
		val: M.ValueId = value() if callable(value) else value
		self.b.emit(M.StoreLocal(local=local, value=val))
		return local

	def _materialize_owned_temp_for_borrow(
		self,
		*,
		ty: TypeId,
		value: "M.ValueId | Callable[[], M.ValueId]",
		is_mut: bool = False,
	) -> M.ValueId:
		"""`_materialize_owned_temp` + `AddrOfLocal`.  Returns the
		borrow pointer.  This is the HBorrow rvalue fallback's whole
		job -- the canonical use case this helper exists for.  Also
		used by the Arc-fat and Arc-as-interface chained-rvalue
		receiver materialization sites.

		`value` accepts the same forms as `_materialize_owned_temp`
		(eager `ValueId` or zero-arg callable for lazy
		evaluation between slot alloc and StoreLocal)."""
		local = self._materialize_owned_temp(
			name_prefix="__borrow_tmp", ty=ty, value=value,
		)
		ptr = self.b.new_temp()
		self.b.emit(M.AddrOfLocal(dest=ptr, local=local, is_mut=is_mut))
		return ptr

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

	def _emit_captured_locals(self, err_val: M.ValueId) -> None:
		"""Slice 7b (2026-05-05): emit the captured-frame JSON object for
		the current function's `^`-captured locals at the throw site.

		Pre-7b this site dual-wrote the frame: a legacy DV path
		(`M.ErrorAddLocalDV` per key) AND the JSON frame consumed by
		`<error>.context.encode_compact()`.  Slice 7b deletes the legacy
		DV path (the captures DV map was already unreadable from user
		code post-Slice 7a — `e.captures[k]` is rejected with
		`E_EXC_CAPTURES_REMOVED`); only the JSON frame remains.

		Frame shape:
		    `{"fn":"<fqn>","locals":{<key>:<value>,...}}`

		`^`-keys are sorted lex-utf8 at compile time for determinism.
		Per-capture value text is emitted directly via
		`core.diagnostic_json_<scalar>` helpers — no DV intermediate.
		The set of supported capture types (Int/Uint/Bool/Float/String)
		is enforced upstream by the checker.

		Slice 7b (K, 2026-05-06; negative-space audit): production
		compilation always has std.core loaded, so the per-scalar
		JSON helpers must be resolvable.  Reaching this site with
		active captures and a missing helper ICEs as a contract
		failure rather than silently skipping the frame (which would
		drop captured-frame data from `<error>.context.encode_compact()`
		without any signal).  Stage2 unit tests do not construct
		active captures without loading std.core, so the ICE never
		fires there.
		"""
		if not self._active_captured_locals:
			return
		frame_name = function_symbol(self._current_fn_id) if self._current_fn_id is not None else self.b.func.name

		# Snapshot the captured locals in lex-utf8 order of capture_name
		# so the inner JSON {"<key>":<value>,...} is deterministic.
		caps_sorted = sorted(
			self._active_captured_locals.values(),
			key=lambda c: c.capture_name,
		)

		# Slice 7b contract guard (K, 2026-05-06): production
		# compilation always has std.core loaded, so the per-scalar
		# JSON helpers must be resolvable.  Reaching this site with
		# active captures and a missing helper is a contract failure
		# (would silently drop captured-frame data from
		# `<error>.context.encode_compact()`); ICE so the bug
		# surfaces.  Stage2 unit tests don't construct active captures
		# without loading std.core.
		json_helpers: dict[str, "FunctionId"] = {}
		for hkey, hname in (
			("int", "diagnostic_json_int"),
			("uint", "diagnostic_json_uint"),
			("bool", "diagnostic_json_bool"),
			("float", "diagnostic_json_float"),
			("string", "diagnostic_json_string"),
		):
			fn_id = self._find_free_fn_id("std.core", hname)
			if fn_id is None:
				raise AssertionError(
					f"`^`-captured-locals frame builder reached HIR→MIR "
					f"with active captures but `core.{hname}` is not in "
					"signatures — std.core not loaded.  Contract failure: "
					"production compilation always has std.core; silently "
					"skipping the frame would drop captured-frame data "
					"from `<error>.context.encode_compact()`."
				)
			json_helpers[hkey] = fn_id

		frame_json_val = self._build_throw_context_frame_json(
			frame_name=frame_name,
			caps_sorted=caps_sorted,
			json_helpers=json_helpers,
		)
		# Innermost-first ordering: each function's frame is appended
		# at the throw site (inner function unwinds first → appended
		# first in the context_json array).
		self.b.emit(M.ExcAppendContextFrame(error=err_val, frame_json=frame_json_val))

	def _record_drop_decision(
		self,
		*,
		site: str,
		local: str,
		verdict: str,
		reason: str,
		field_path: tuple[tuple[str, int], ...] = (),
	) -> None:
		"""Phase 3A observational hook.  No-op when the ledger flag is off.

		Program point is the current insertion cursor: `(block_name,
		len(instructions))` — i.e. the index at which a drop emission
		(if the site chose MustDrop) would land, or the "hypothetical
		next instruction" the ledger reads pre-state for when the site
		chose MustNotDrop.  The reporter queries `state_pre(point)`,
		which for this cursor is `post_instr[(block, len-1)]` and
		therefore reflects the local's state immediately before the
		decision — not affected by the site's own subsequent emissions.

		`field_path` (Phase 4 step 3b): empty tuple `()` for whole-
		local records (existing); non-empty tuple of `(ctor_name,
		field_index)` projections for per-field records.  When non-
		empty, the reporter compares against `field_verdict_at`
		instead of `verdict_at`."""
		if self._drop_decision_log is None:
			return
		self._drop_decision_log.record(
			site=site,
			program_point=(self.b.block.name, len(self.b.block.instructions)),
			local=local,
			verdict=verdict,
			reason=reason,
			field_path=field_path,
		)

	def _emit_scope_cleanup_hook(self, *, scope_index: int) -> None:
		"""Phase 4 site-1 cleanup-authoring marker emission.

		Generalises patch 1's `_emit_function_exit_cleanup_hook`
		(scope_index=0) to any scope_index.  Walks
		`self._scope_stack[scope_index:]` in legacy emission order
		(reversed scopes, then reversed locals) and emits a single
		`M.CleanupHook` whose `candidates` list captures every
		(local, type) pair the legacy `_emit_scope_drops` would
		have considered at the same point.

		The candidates list is unfiltered by drop verdict on purpose:
		`cleanup_authoring.py` queries `verdict_at` for each
		candidate and decides emission.  The hook carries the full
		legacy consideration set so authoring can produce the same
		MIR legacy would have, modulo the authority shift from
		`_moved_locals` to `verdict_at`.

		Production call sites (all migrated 2026-04-24):
		- function-exit: the seven `_visit_stmt_HReturn` /
		  `_visit_stmt_HThrow` slots (via the patch-1 compat shim
		  `_emit_function_exit_cleanup_hook(scope_index=0)`).
		- `lower_function_body` fall-through (patch 4a).
		- `lower_block` end-of-block fall-through (patch 3).
		- lambda-block exits (patch 4b.1) — both expression-returning
		  and statement-terminated forms.
		- `HBreak` / `HContinue` (patch 4b.2).

		Site 1 emission decisions are now driven by `verdict_at`
		across all sites; `_moved_locals` retirement is patch 6 in
		the Path Y plan (still queried by the orphan
		`_emit_scope_drops` definition / its `_scope_drop_verdict`
		helper, neither of which has production callers).
		"""
		if not self._scope_stack:
			return
		if scope_index < 0:
			scope_index = 0
		candidates: list[tuple] = []
		for scope in reversed(self._scope_stack[scope_index:]):
			for local in reversed(scope):
				ty = self._local_types.get(local)
				if ty is None:
					# Matches legacy `_emit_scope_drops`'s silent skip
					# for unknown-type locals; the authoring pass also
					# skips unknown-type candidates.
					continue
				candidates.append((local, ty))
		if not candidates:
			return
		scope_id = self._cleanup_hook_counter
		self._cleanup_hook_counter += 1
		self.b.emit(M.CleanupHook(scope_id=scope_id, candidates=candidates))

	def _emit_function_exit_cleanup_hook(self) -> None:
		"""Patch 1 compat shim around `_emit_scope_cleanup_hook(scope_index=0)`."""
		self._emit_scope_cleanup_hook(scope_index=0)

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

	def _lower_share_capture(
		self,
		*,
		cap: "C.HExplicitCapture",
		hir_cap: "H.HExplicitCapture",
		env_vals: list[M.ValueId],
		env_field_types: list[TypeId],
	) -> None:
		"""Lower a `captures(share x)` slot in a lambda capture list.

		The synthesized `Share::share(&x)` HCall was built at AST→HIR
		time and lives on `hir_cap.share_value`.  We lower it INLINE
		at the env-construction site (NOT as a pre-statement) so the
		call evaluates exactly when the lambda expression itself
		evaluates — preserving user-visible left-to-right argument
		order in shapes like
		`foo(side_effect(), || captures(share x) => ...)`.

		Trait dispatch routes through
		`HQualifiedMember(std.core.shareable.Share, "share")` which
		the call-resolver resolved to a monomorphized
		`Share::share__inst__<...>` at type-check time; lowering
		`cap.share_value` here just emits the resolved Call.

		Ownership invariant (pinned by
		`lang/tests/stage2/test_share_capture_ownership.py`):
		  - `x` is borrowed by `Share::share(&x)` — stays LIVE.
		  - The call returns a fresh +1 owner; ownership transfers
		    directly into the env field via the slot append below.
		"""
		share_value = getattr(hir_cap, "share_value", None)
		if share_value is None:
			raise AssertionError(
				"share capture missing synthesized share_value — "
				"AST→HIR `_visit_expr_Lambda` should have populated it"
			)
		val = self.lower_expr(share_value)
		val_ty = self._local_types.get(val)
		if val_ty is None:
			val_ty = self._infer_expr_type(share_value)
		if val_ty is None:
			raise AssertionError(
				"share-capture call result has unknown type in MIR "
				"lowering — checker should have resolved Share::share "
				"before this point"
			)
		env_vals.append(val)
		env_field_types.append(val_ty)

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
		# The match machinery already handles aliased owned-local field reads
		# via tombstoning (zeroing the field in the source struct, lines below).
		# We only need to deep-copy when that mechanism cannot apply — i.e.,
		# when the scrutinee was read from a ref (&T), not an owned local.
		if scrut_ty is not None:
			_scrut_tombstone_applies = False
			if (
				isinstance(expr.scrutinee, H.HField)
				and isinstance(expr.scrutinee.subject, H.HVar)
			):
				_subj_ty = self._infer_expr_type(expr.scrutinee.subject)
				if _subj_ty is not None:
					_subj_def = self._type_table.get(_subj_ty)
					if _subj_def.kind is TypeKind.STRUCT:
						_scrut_tombstone_applies = True
			if not _scrut_tombstone_applies:
				scrut_val = self._copy_if_ref_alias(scrut_val, scrut_ty)
		if scrut_ty is None or self._type_table.get(scrut_ty).kind is not TypeKind.VARIANT:
			raise AssertionError("match scrutinee must have a concrete variant type (checker bug)")
		inst = self._type_table.get_variant_instance(scrut_ty)
		if inst is None:
			raise AssertionError("match scrutinee variant instance missing (type table bug)")
		scrut_source_local: str | None = None
		if isinstance(expr.scrutinee, H.HVar):
			scrut_source_local = self._canonical_local(getattr(expr.scrutinee, "binding_id", None), expr.scrutinee.name)

		# When the scrutinee is a field access on a local (e.g. `match re.root`),
		# the StructGetField extracted the variant as an SSA copy without moving
		# ownership from the struct local.  Each match arm will copy the scrutinee
		# value into its own temp for binder extraction and drop, but the original
		# struct local still holds the same variant value.  To prevent double-free
		# when the struct local is later dropped, tombstone the field in the
		# struct's local storage now that the value has been extracted.
		if (
			isinstance(expr.scrutinee, H.HField)
			and isinstance(expr.scrutinee.subject, H.HVar)
			and scrut_ty is not None
			and not scrut_is_ref
			and not self._should_copy_value(scrut_ty)
		):
			owner_var = expr.scrutinee.subject
			owner_local = self._canonical_local(getattr(owner_var, "binding_id", None), owner_var.name)
			owner_ty = self._local_types.get(owner_local)
			if owner_ty is not None:
				owner_def = self._type_table.get(owner_ty)
				if owner_def.kind is TypeKind.STRUCT:
					field_info = self._type_table.struct_field(owner_ty, expr.scrutinee.name)
					if field_info is not None:
						field_idx, field_ty = field_info
						owner_ptr = self.b.new_temp()
						self.b.emit(M.AddrOfLocal(dest=owner_ptr, local=owner_local, is_mut=True))
						self._local_types[owner_ptr] = self._type_table.ensure_ref_mut(owner_ty)
						field_ptr = self.b.new_temp()
						self.b.emit(M.AddrOfField(dest=field_ptr, base_ptr=owner_ptr, struct_ty=owner_ty, field_index=field_idx, field_ty=field_ty, is_mut=True))
						self._local_types[field_ptr] = self._type_table.ensure_ref_mut(field_ty)
						zero_val = self.b.new_temp()
						self.b.emit(M.ZeroValue(dest=zero_val, ty=field_ty))
						self._local_types[zero_val] = field_ty
						self.b.emit(M.StoreRef(ptr=field_ptr, value=zero_val, inner_ty=field_ty))

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

		# Enter dispatch.
		self.b.set_terminator(M.Goto(target=dispatch_block.name))

		for arm in expr.arms:
			if arm.ctor is None:
				continue
			if inst.arms_by_name.get(arm.ctor) is None:
				raise AssertionError("unknown constructor in match reached MIR lowering (checker bug)")
		arm_blocks: list[tuple[H.HMatchArm, M.BasicBlock]] = [
			(arm, self.b.new_block(f"match_arm_{idx}")) for idx, arm in enumerate(expr.arms)
		]

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
			arm_scrut_payload_moved = False
			# Phase 4 site-2 patch 5: hook reference used to fill in the
			# arm_end program point once arm-body lowering completes.
			match_cleanup_hook_obj: M.MatchCleanupHook | None = None
			# `arm_scrut_payload_moved` is the partial-move/whole-scrutinee
			# MECHANISM SELECTOR — when True, the per-field cleanup hook
			# fires; when False, the whole-scrutinee elif emits a
			# single-candidate `M.CleanupHook` (the whole-variant drop
			# path).  No HIR-side per-field filter exists post-0.31.13;
			# `match_cleanup_authoring` + the chain-aware ledger are the
			# sole authority on emit-vs-skip per candidate.
			try:
				def _ensure_arm_scrut_ptr(*, mark_source_moved: bool = False) -> None:
					# When mark_source_moved is True, the caller guarantees
					# that EVERY arm of this match will also call this
					# helper (so `scrut_source_local` is moved on every CFG
					# path out of the match).  Only then is it correct to
					# globally record the source local as moved — otherwise
					# arms that skip the helper would leave the scrutinee
					# live on their CFG edge, and `_moved_locals`
					# accounting would suppress a scope-drop that those
					# paths legitimately need.
					nonlocal arm_scrut_local, arm_scrut_ptr
					if arm_scrut_ptr is not None:
						return
					# materialize-audit: allow registered via explicit
					# M.CleanupHook emission at line ~1990 of this file
					# (whole-scrutinee drop path).  Match-arm machinery
					# tracks scrutinee + source-local cleanup through the
					# match_cleanup_authoring pass, not scope_stack.
					arm_scrut_local = f"__match_scrut_tmp{self.b.new_temp()}"
					self.b.ensure_local(arm_scrut_local)
					self._local_types[arm_scrut_local] = scrut_ty
					source_local = scrut_source_local
					if source_local is None and not self._should_copy_value(scrut_ty):
						# materialize-audit: allow registered same match-
						# arm CleanupHook path as arm_scrut_local above;
						# source_local is moved out into arm_scrut_local
						# before the hook fires.
						source_local = f"__match_scrut_src{self.b.new_temp()}"
						self.b.ensure_local(source_local)
						self._local_types[source_local] = scrut_ty
						self.b.emit(M.StoreLocal(local=source_local, value=scrut_val))
					if source_local is not None and not self._should_copy_value(scrut_ty):
						arm_scrut_moved_in = self.b.new_temp()
						self.b.emit(M.MoveOut(dest=arm_scrut_moved_in, local=source_local, ty=scrut_ty))
						self._local_types[arm_scrut_moved_in] = scrut_ty
						self.b.emit(M.StoreLocal(local=arm_scrut_local, value=arm_scrut_moved_in))
						if mark_source_moved:
							# Patch 6c: HIR-side `_mark_moved` retired —
							# the lattice tracks the consumption via the
							# MoveOut emitted at line 1450.
							pass
						else:
							# Value-producing match: we cannot globally
							# mark `source_local` moved, because sibling
							# arms that do not invoke this helper (e.g.
							# `default` or bare-ctor arms) legitimately
							# leave the scrutinee live on their CFG edges
							# and rely on scope-drop for cleanup.
							# Instead, write a DROP-SAFE TOMBSTONE back
							# into the source local on THIS path — the
							# same pattern `ArrayElemTake` uses for array
							# slot cleanup.  At runtime the enclosing
							# scope-drop reads tombstone bytes
							# (reserved `__drift_internal_tombstone` tag
							# or a user-declared `@tombstone` ctor) and
							# becomes a provable no-op on this path,
							# while untaken-arm paths leave the original
							# storage intact.  Path-sensitive via
							# runtime state, no `_moved_locals` mutation.
							tomb = self.b.new_temp()
							self.b.emit(M.TombstoneValue(dest=tomb, ty=scrut_ty))
							self._local_types[tomb] = scrut_ty
							self.b.emit(M.StoreLocal(local=source_local, value=tomb))
					else:
						# Copy-store branch.  Reached when EITHER (a)
						# `source_local is None` (the scrutinee is a
						# transient SSA value with no named local to
						# scope-drop, so dual-ownership cannot arise)
						# or (b) `_should_copy_value(scrut_ty)` is
						# True (the type-system claim is "this
						# scrutinee supports a cheap copy").
						#
						# Phase 2 fix for the `match Optional<String>`
						# double-drop UAF (fix/ownership-drop-ledger
						# track, 0.31.0): case (b) was historically
						# emitted as a bare `StoreLocal(arm_scrut_local,
						# scrut_val)`.  For a POD scrutinee (bitcopy)
						# that's correct — bits are self-contained,
						# the source local and arm_scrut_local each
						# own an independent value, per-drop is a
						# no-op.  For a refcount-bearing scrutinee
						# (e.g. `Optional<String>` under the packaged-
						# load `copy_status = True` resolution), the
						# bare StoreLocal is a bitcopy of the variant
						# bits WITHOUT running the per-arm retain
						# traversal — so both the source local and
						# `arm_scrut_local` end up with pointers into
						# the same refcount header, each claiming
						# ownership.  When `string_arc` later emits
						# scope-exit drops for both, the refcount is
						# released twice → UAF.  Phase 0 landed a
						# compile-time fail-stop on this shape;
						# Phase 2 removes the fail-stop by emitting
						# the semantically correct operation at this
						# site.
						#
						# Dispatch — scoped to the DUAL-OWNER case:
						#
						#   dual_owner = (source_local is not None)
						#                AND has_structural_drop
						#
						# Only dual-owner reaches `CopyValue`.  The
						# other subcases of this else-branch (reached
						# when `_should_copy_value(scrut_ty) == True`
						# OR `source_local is None`) keep the bare
						# `StoreLocal`.
						#
						# **Why the `source_local is not None` half of
						# the guard is load-bearing.**  A transient
						# rvalue scrutinee (`match make_opt() { ... }`
						# where `make_opt()` returns an owning refcount-
						# bearing Variant by value) has
						# `source_local is None` — `scrut_val` is an
						# SSA owning the freshly-produced refcount
						# and has no named local tracking it.  Bare
						# `StoreLocal(arm_scrut_local, scrut_val)`
						# transfers that single refcount owner into
						# the arm temp; the arm-end drop releases it;
						# balanced.  If we instead emitted
						# `CopyValue` here, we'd retain the
						# refcount (+1), leaving the original
						# `scrut_val` as a second owner — but with no
						# named local to scope-drop, the original
						# refcount leaks for the lifetime of the
						# function.  Regression pinned by
						# `test_match_scrut_copy_store_inline_rvalue_does_not_copy`
						# in
						# `test_match_scrut_copy_store_emits_copyvalue.py`.
						#
						# **Why the `has_structural_drop` half of the
						# guard is load-bearing.**  A POD variant
						# (`V<Int>`) with `has_structural_drop=False`
						# has no refcount or shared-owner
						# substructure; bitcopying into
						# `arm_scrut_local` is semantically
						# indistinguishable from a per-field copy.
						# Running the per-arm `CopyValue` traversal
						# here would be pure overhead without fixing
						# any bug.
						#
						# **Why both halves together, exactly, are the
						# Phase 2a fix.**  The original UAF shape
						# (TLS's `_base_port()` under packaged
						# stdlib) is specifically: named source
						# local `opt`, refcount-bearing scrutinee,
						# Copy-store branch reached.  That triplet
						# IS the dual-owner condition.  Bare
						# `StoreLocal` bitcopies the variant bits
						# into `arm_scrut_local` without retaining;
						# `opt` and `arm_scrut_local` each claim
						# ownership of the same refcount; scope-exit
						# drops on both produce the double-release.
						# `CopyValue` fixes it by giving
						# `arm_scrut_local` its own retained copy so
						# each drop releases exactly one refcount.
						# Transient rvalues and POD variants were
						# never part of the bug; over-reaching the
						# CopyValue to them is the Phase 2a review
						# finding this guard responds to.
						#
						# `_drop_policy` is read once per scrut —
						# Phase 1 funnel contract.
						_scrut_policy = self._drop_policy(scrut_ty)
						_dual_owner = (source_local is not None) and _scrut_policy.has_structural_drop
						if _dual_owner:
							copy_dest = self.b.new_temp()
							self.b.emit(M.CopyValue(dest=copy_dest, value=scrut_val, ty=scrut_ty))
							self._local_types[copy_dest] = scrut_ty
							self.b.emit(M.StoreLocal(local=arm_scrut_local, value=copy_dest))
						else:
							self.b.emit(M.StoreLocal(local=arm_scrut_local, value=scrut_val))
					arm_scrut_ptr = self.b.new_temp()
					self.b.emit(M.AddrOfLocal(dest=arm_scrut_ptr, local=arm_scrut_local, is_mut=True))
					self._local_types[arm_scrut_ptr] = self._type_table.ensure_ref_mut(scrut_ty)

				if not want_value and not scrut_is_ref:
					# Statement-context match: `_ensure_arm_scrut_ptr`
					# is invoked unconditionally here, so EVERY arm moves
					# the source local into its arm_scrut_local.  Safe
					# to record the move globally — scope-drop at the
					# enclosing block end correctly skips the already-
					# consumed named local.
					_ensure_arm_scrut_ptr(mark_source_moved=True)

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
						need_addr_binders = False
						for fidx in field_indices:
							if fidx < 0 or fidx >= len(arm_def.field_types):
								raise AssertionError("match binder field index out of range (checker bug)")
							f_ty = arm_def.field_types[fidx]
							if not self._should_copy_value(f_ty):
								need_addr_binders = True
								break
							# Copy-classified payload that still has refcount
							# (or other runtime-drop) semantics — e.g. String,
							# Array<X>, struct-with-drop — must take the
							# materialization path so the rvalue variant temp
							# gets a scope drop after the match.  Without
							# this, in a value-producing inline match like
							#
							#     val s = match env.get("X") { Some(v) => move v, ... };
							#
							# the SSA variant temp has its +1 on the payload
							# field but is never released, leaking the
							# payload buffer.  Pinned by
							# `lang/tests/memcheck/test_inline_match_rvalue_string_payload_leak.py`.
							if self._needs_runtime_drop(f_ty):
								need_addr_binders = True
								break
						if (not scrut_is_ref) and arm.binders and need_addr_binders:
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
								# materialize-audit: allow non-owning
								# Ref-typed binder (scrut_is_ref branch:
								# binder_ty is Ref<bty> at line 1741);
								# Ref locals are not droppable so no
								# scope cleanup needed.
								self.b.ensure_local(bname)
								self._local_types[bname] = binder_ty
								self.b.emit(M.StoreLocal(local=bname, value=field_val))
							else:
								if arm_scrut_ptr is not None:
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
									payload_is_copy = self._should_copy_value(bty)
									if payload_is_copy:
										# Copy-classified payload: emit `CopyValue`,
										# which dispatches per-type (bitcopy for POD,
										# retain for refcounted scalars).  PARTIAL-
										# MOVE CLEANUP below does not apply: this
										# path does not set `arm_scrut_payload_moved`,
										# so the variant slot retains its original
										# +1 and is released by whole-variant scope
										# drop.  The binder owns an independent +1
										# (via `CopyValue`) and is released by its
										# own scope drop.
										copy_dest = self.b.new_temp()
										self.b.emit(M.CopyValue(dest=copy_dest, value=field_moved, ty=bty))
										self._local_types[copy_dest] = bty
										field_moved = copy_dest
									else:
										# materialize-audit: allow consumed
										# tmp_local is immediately MoveOut'd
										# on the next line; the value transfers
										# into move_dest and tmp_local is no
										# longer live for scope cleanup.
										tmp_local = f"__match_field_move_{self.b.new_temp()}"
										self.b.ensure_local(tmp_local)
										self._local_types[tmp_local] = bty
										self.b.emit(M.StoreLocal(local=tmp_local, value=field_moved))
										move_dest = self.b.new_temp()
										self.b.emit(M.MoveOut(dest=move_dest, local=tmp_local, ty=bty))
										self._local_types[move_dest] = bty
										field_moved = move_dest
										arm_scrut_payload_moved = True
										# Per-field MovedOut tracking lives in the
										# chain-aware ledger walker
										# (`_apply_field_state` in
										# `ownership_ledger.py`); the MoveOut emitted
										# above transitions the field to MOVED_OUT,
										# and `match_cleanup_authoring`'s
										# `field_verdict_at` query at the cleanup
										# hook position correctly returns
										# MUST_NOT_DROP for it — no HIR-side
										# `moved_field_indices` set is needed.
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
									# Phase 4 site-1 patch 6a: arm-end binder
									# drainage migrated to ledger-authored
									# emission via the per-arm CleanupHook;
									# `arm_drop_locals` mirror retired.  The
									# scope-stack registration alone makes
									# the binder a CleanupHook candidate.
									self._register_drop_local(bname, binder_ty)
								self.b.emit(M.StoreLocal(local=bname, value=field_moved))

				# Consume and drop by-value scrutinee before arm body so cleanup runs
				# even when the arm terminates early (e.g., return/throw).
				if arm_scrut_payload_moved:
					# PARTIAL-MOVE CLEANUP: at least one droppable field of
					# the matched ctor was moved out by the binder loop.
					# Dropping the whole variant (via
					# `DropValue(arm_scrut_local)`) would re-drop the moved
					# field — unsafe.  Dropping nothing at all would leak
					# every OTHER field whose storage-ref remained in the
					# variant.
					#
					# **Authority boundary (post-0.31.13)**: HIR proposes
					# the FULL `arm_def.field_types` set as candidates.
					# `match_cleanup_authoring` queries `field_verdict_at`
					# per candidate and the chain-aware ledger walker +
					# `compute_drop_policy.needs_drop` are the sole
					# authority on emit-vs-skip.  Three categories the
					# authoring pass resolves:
					#  - Unbound droppable field: slot still owns its
					#    +1; lattice state LIVE → MUST_DROP → emit.
					#    Closes the named-subset bind leak, pinned by
					#    `lang/tests/codegen/e2e/match_subset_bind_leaves_unbound_fields_dropped/`.
					#  - Bound MOVED field: binder consumed the +1 via
					#    `MoveOut`; lattice marks the field MOVED_OUT
					#    (chain-aware `_apply_field_state` rule);
					#    `field_verdict_at` returns MUST_NOT_DROP → no
					#    emission.
					#  - Non-droppable / POD field: `compute_drop_policy
					#    (ty).needs_drop=False` collapses the verdict to
					#    MUST_NOT_DROP regardless of state → no emission.
					#  - Bound COPIED droppable field (refcounted scalar
					#    like String): `MoveFromRef` ownership-transfer
					#    primitive lands the slot's +1 into drop_tmp; the
					#    binder owns its own +1 (CopyValue retain).  The
					#    chain-aware walker keeps the field LIVE (the
					#    Copy chain breaks at `CopyValue`); authoring
					#    emits the per-field cleanup chain to release the
					#    slot's stake.  See
					#    `lang/tests/memcheck/test_partial_move_copy_binder_string_slot_leak.py`.
					# Phase 4 step 3b: replace the single whole-scrutinee
					# REASON_FIELD_MOVED record with per-field records
					# emitted inside the cleanup loop below.  The
					# whole-scrutinee record was always a summary of
					# "some field was moved"; per-field records carry
					# the same information at finer grain and let the
					# aggregator distinguish per_field_still_disagrees
					# (per-field comparison failed) from per_field_gap
					# (ledger could not classify; whole-local-only
					# records fall here).  Removing the summary lets
					# bucket 1 drop materially as the per-field
					# records flow into agree (most cases) or
					# per_field_still_disagrees (the residual K wants
					# visible).
					# Phase 4 site-2 patch 5: per-field cleanup is
					# authored by `match_cleanup_authoring` post-ledger.
					# HIR→MIR pre-allocates `drop_tmp` locals and
					# registers them via `_register_drop_local` so later
					# site-1 `CleanupHook` markers (for early return /
					# throw inside the arm body) capture them as
					# candidates, then emits a single
					# `M.MatchCleanupHook` carrying the candidate list.
					# The authoring pass queries `field_verdict_at` per
					# candidate and emits the canonical chain only for
					# `MUST_DROP` verdicts; non-emitted drop_tmps stay
					# `UNINIT`, so site-1 `verdict_at` sees
					# `classify(UNINIT) = MUST_NOT_DROP` and skips
					# cleanly.  See `work/ownership-ledger/patch-5-design.md`
					# and `lang/driftc/stage2/match_cleanup_authoring.py`.
					match_cleanup_hook_point: tuple[str, int] | None = None
					match_cleanup_candidates: list[tuple] = []
					if (
						arm.ctor is not None
						and not scrut_is_ref
						and arm_scrut_ptr is not None
					):
						arm_def = inst.arms_by_name[arm.ctor]
						# **Authority boundary (post-0.31.13)**: HIR
						# proposes the FULL ctor field set as
						# candidates; `match_cleanup_authoring` queries
						# `field_verdict_at` per candidate and the
						# ledger / `compute_drop_policy` are the sole
						# emit-vs-skip authority.  No HIR-side filters.
						#
						# History: Filter A (skip moved fields via
						# `moved_field_indices`) and Filter B (skip
						# non-drop-needing fields) were retired with
						# the chain-aware per-field walker landing in
						# 0.31.12 (`MoveFromRef` +
						# `_apply_field_state`'s tightened MovedOut
						# detection).  Both are now structurally
						# redundant: the walker marks Move-chain fields
						# MOVED_OUT (so authoring's `field_verdict_at`
						# returns MUST_NOT_DROP for them); the
						# `needs_drop=False` path in `classify`
						# collapses POD candidates to MUST_NOT_DROP
						# regardless of state.
						#
						# Pinned by
						# `lang/tests/stage2/test_match_cleanup_full_candidate_set.py`.
						for cleanup_fidx, cleanup_fty in enumerate(arm_def.field_types):
							# Capture hook point before any per-candidate
							# emission (no MIR instrs emitted per candidate
							# in the patch-5 shape, so this is stable across
							# the loop body).
							if match_cleanup_hook_point is None:
								match_cleanup_hook_point = (self.b.block.name, len(self.b.block.instructions))
							# Pre-allocate drop_tmp local.  `_register_drop_local`
							# self-filters non-droppable types (so POD
							# drop_tmps don't enter scope_stack and
							# site-1 hooks correctly skip them), but the
							# drop_tmp itself is allocated and added to
							# the MatchCleanupHook candidate list so
							# authoring can make the emit-vs-skip
							# decision uniformly.
							drop_tmp = f"__match_partial_drop_{self.b.new_temp()}"
							self.b.ensure_local(drop_tmp)
							self._local_types[drop_tmp] = cleanup_fty
							self._register_drop_local(drop_tmp, cleanup_fty)
							match_cleanup_candidates.append(
								(drop_tmp, int(cleanup_fidx), cleanup_fty)
							)
						if match_cleanup_candidates and match_cleanup_hook_point is not None:
							hook_block, hook_idx = match_cleanup_hook_point
							match_cleanup_hook_obj = M.MatchCleanupHook(
								scope_id=self._cleanup_hook_counter,
								arm_scrut_local=arm_scrut_local if arm_scrut_local is not None else "",
								arm_scrut_ptr_local=arm_scrut_ptr,
								variant_ty=scrut_ty,
								ctor=arm.ctor,
								candidates=list(match_cleanup_candidates),
								arm_end_block="",
								arm_end_index=-1,
							)
							self._cleanup_hook_counter += 1
							self.b.func.blocks[hook_block].instructions.insert(hook_idx, match_cleanup_hook_obj)
					arm_scrut_local = None
				elif arm_scrut_local is not None:
					# Phase 4 whole-scrutinee migration (post-Copy-shortcut-fix):
					# emit a `M.CleanupHook` with `arm_scrut_local` as the sole
					# candidate.  `cleanup_authoring` queries `verdict_at`
					# and emits `MoveOut + DropValue` iff the lattice says
					# `MUST_DROP` (with `compute_drop_policy.needs_drop` as
					# the type-level needs_drop axis — now driven by
					# destruction reality, not gated by `copy_status`).
					#
					# This subsumes the inline `MoveOut + DropValue`
					# emission and the `_match_scrutinee_drop_verdict`
					# HIR-side helper.  Telemetry is emitted by
					# `cleanup_authoring._emit_decision_record` with
					# `classification=agree` pre-baked (authoring IS the
					# ledger consultation).
					#
					# Pre-fix this migration leaked 66 bytes against
					# signed-package stdlib because
					# `compute_drop_policy(Optional<String>).needs_drop`
					# short-circuited to False on `copy_status=True` from
					# the .dmp-side Copy-impl resolution.  Post-policy-fix
					# (cba71410) and Bug 1 fix (79a0f87c), `needs_drop=True`
					# uniformly across raw and pkg builds.
					scope_id = self._cleanup_hook_counter
					self._cleanup_hook_counter += 1
					self.b.emit(M.CleanupHook(scope_id=scope_id, candidates=[(arm_scrut_local, scrut_ty)]))
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
						# Value-producing match arm's result expression is an
						# OWNING-CONSUMPTION boundary (ownership of the arm
						# value transfers from wherever it lives INTO
						# `result_local`).  Same family as
						# `return <expr>` / `dst = <expr>` — for a
						# move-classified HVar (arm binder local or any
						# outer owning local), raw `lower_expr` emits a
						# plain load that leaves the source live, which
						# then (a) gets dropped at arm-end by
						# `arm_drop_locals` for a binder, or (b) gets
						# dropped at scope-exit for an outer local, and
						# in either case the `result_local` now holds a
						# dangling view of released storage.  Exposed by
						# `om_match_bind_{diag_entry, string_heap_concat}`
						# scenario_value_producing_match (ASAN UAF /
						# memcheck leak respectively) when the arm result
						# is the bound binder `v`.
						#
						# `_lower_owning_consume` is the canonical path:
						# MoveOut for move-classified HVar / projection-
						# free HPlaceExpr (and marks `_moved_locals` so
						# `arm_drop_locals` cleanup below skips the
						# consumed local); raw `lower_expr` otherwise.
						val = self._lower_owning_consume(arm.result, self._local_types.get(result_local))
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

				if self.b.block.terminator is None and match_cleanup_hook_obj is not None:
					# Phase 4 site-2 patch 5: fix up the hook's arm_end
					# program point now that arm-body lowering is
					# complete and the arm-end drainage emission is
					# about to run.  match_cleanup_authoring will emit
					# `MoveOut(drop_tmp) + DropValue` at this position
					# for each MUST_DROP candidate.  If the arm instead
					# early-returned/threw (terminator set), arm_end
					# stays as the placeholder and authoring skips the
					# tail chain — site-1 CleanupHooks at the early-exit
					# point handle drop_tmp cleanup via their own
					# verdict_at queries.
					match_cleanup_hook_obj.arm_end_block = self.b.block.name
					match_cleanup_hook_obj.arm_end_index = len(self.b.block.instructions)

				if self.b.block.terminator is None:
					# Phase 4 site-1 patch 6a: arm-end drainage migrated
					# to the per-arm `M.CleanupHook` — site-1 authoring
					# queries `verdict_at` per binder/drop_tmp at this
					# position and emits the canonical `MoveOut +
					# DropValue` for live candidates.  drop_tmps with
					# match_cleanup_authoring-emitted arm-end chains are
					# already MOVED_OUT here; verdict_at returns
					# MUST_NOT_DROP and skips them — no double-drop.
					self._emit_scope_cleanup_hook(scope_index=len(self._scope_stack) - 1)
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

	def _visit_expr_HLiteralUint(self, expr) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstUint(dest=dest, value=expr.value))
		self._local_types[dest] = self._uint_type
		return dest

	def _visit_expr_HLiteralUint64(self, expr) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstUint64(dest=dest, value=expr.value))
		self._local_types[dest] = self._uint64_type
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
		allowed_scalar_names = ("Int", "Uint", "Uint64", "Int32", "Uint32", "Byte", "Bool")

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

		def _is_uint_scalar_type(ty: TypeId) -> bool:
			td = self._type_table.get(ty)
			return td.kind is TypeKind.SCALAR and td.name == "Uint"

		def _is_raw_ptr_type(ty: TypeId) -> bool:
			td = self._type_table.get(ty)
			return td.kind is TypeKind.RAW_PTR

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

		def _resolve_ptr_target() -> TypeId | None:
			te = getattr(expr, "target_type_expr", None)
			if te is None:
				return None
			module_id = getattr(te, "module_id", None) or self._current_module_name()
			try:
				tid = resolve_opaque_type(te, self._type_table, module_id=module_id)
			except Exception:
				return None
			td = self._type_table.get(tid)
			if td.kind is TypeKind.RAW_PTR:
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
		if resolved_target is None:
			resolved_target = _resolve_ptr_target()
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
			if (_is_raw_ptr_type(val_ty) and (_is_raw_ptr_type(target_ty) or _is_uint_scalar_type(target_ty))) or (_is_uint_scalar_type(val_ty) and _is_raw_ptr_type(target_ty)):
				dest = self.b.new_temp()
				self.b.emit(M.CastScalar(dest=dest, value=val, src_ty=val_ty, dst_ty=target_ty))
				self._local_types[dest] = target_ty
				return dest
		if src_ty == target_ty:
			return val
		if (_is_raw_ptr_type(src_ty) and (_is_raw_ptr_type(target_ty) or _is_uint_scalar_type(target_ty))) or (_is_uint_scalar_type(src_ty) and _is_raw_ptr_type(target_ty)):
			dest = self.b.new_temp()
			self.b.emit(M.CastScalar(dest=dest, value=val, src_ty=src_ty, dst_ty=target_ty))
			self._local_types[dest] = target_ty
			return dest
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
		# Block-scope constants: emit a fresh MIR literal at each use site.
		bid = getattr(expr, "binding_id", None)
		if bid is not None and int(bid) in self._local_consts:
			return self._emit_local_const(int(bid))
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
			if ty_id == self._byte_type:
				self.b.emit(M.ConstByte(dest=dest, value=int(val)))
				return dest
			if isinstance(val, list):
				td = self._type_table.get(ty_id)
				if td.kind is TypeKind.ARRAY and td.param_types:
					elem_ty = td.param_types[0]
					self.b.emit(M.ConstArray(dest=dest, elem_ty=elem_ty, values=list(val)))
					self._local_types[dest] = ty_id
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
			# LANGUAGE_BUG fix (2026-05-15): if the subject is an
			# HField (or HField chain) rooted at an rvalue call that
			# returns `&T`, the whole-expression materialization below
			# would COPY the leaf field value (an AtomicBool, a Mutex
			# guard, …) into the temp and return `&temp` — silently
			# breaking `&self` interior-mutation methods on the leaf.
			# This is the `arc.get().flag.store(true, Release)` shape
			# pinned at
			# `lang/tests/driver/test_arc_get_field_atomic_store_persistence_blocker.py`.
			#
			# Instead: walk the projection chain looking for the
			# deepest rvalue base.  If that base is a call that
			# returns `&T`, lower the call once to its `&T` value
			# and walk the remaining HField chain emitting
			# `AddrOfField` (and `LoadRef` for any intermediate REF
			# field) directly against that pointer — the same place-
			# chain shape `_lower_addr_of_place` produces, but
			# rooted at a call result instead of a local's address.
			# (`_lower_addr_of_place` itself is not reused: it takes
			# an `HPlaceExpr` and starts from `AddrOfLocal`, which
			# this rvalue-rooted subject doesn't have.)
			# `_lift_rvalue_ref_base_for_borrow` splits this into a
			# pure-inspection validator (`_validate_lifted_chain`)
			# and an emitter, so a structural mismatch returns None
			# without leaving any MIR behind — the caller falls back
			# to the whole-expression materialization below over the
			# same untouched subject.  This mirrors the stage1
			# `BorrowMaterializeRewriter._split_lift_place_chain`
			# strategy for `&w.get().handle`, applied here at MIR
			# lowering because the type checker injects this
			# specific HBorrow shape AFTER stage1 normalization runs
			# (see `type_checker.py` autoborrow-on-rvalue-call
			# branch ~line 8704).
			lifted = self._lift_rvalue_ref_base_for_borrow(
				expr.subject, is_mut=expr.is_mut
			)
			if lifted is not None:
				return lifted

			if expr.is_mut:
				raise AssertionError("non-canonical &mut borrow operand reached MIR lowering (normalize/typechecker bug)")
			inner_ty = self._infer_expr_type(expr.subject)
			if inner_ty is None:
				raise AssertionError("borrow operand type unknown in MIR lowering (checker bug)")
			# The synthetic local holds the materialized owned value of
			# the rvalue subject; the borrow result is its address.  Use
			# the canonical materialize helper so steps stay together
			# (alloc + type + register-drop + store + addr).  Lazy `value`
			# callable preserves the byte-identical IR shape: the temp
			# slot id is allocated FIRST (counter call before lower_expr),
			# then lower_expr's own temps are emitted, then the StoreLocal.
			return self._materialize_owned_temp_for_borrow(
				ty=inner_ty,
				value=lambda: self.lower_expr(expr.subject),
				is_mut=False,
			)
		ptr, _inner = self._lower_addr_of_place(expr.subject, is_mut=expr.is_mut)
		return ptr

	def _lift_rvalue_ref_base_for_borrow(
		self, subject: H.HExpr, *, is_mut: bool
	) -> "M.ValueId | None":
		"""LANGUAGE_BUG-fix helper (2026-05-15).  Detect the
		`HField(... HCall/HMethodCall_returning_ref, field1, ...)`
		shape — a borrow subject that's a field-projection chain
		rooted at a call returning `&T` — and lower it correctly so
		`&self` interior-mutation methods on the leaf field mutate
		the actual storage instead of a temp-local copy.

		## Atomicity contract (K-review, 2026-05-15)

		On `None` return, this function MUST have emitted zero MIR
		instructions.  All structural validation (the call's return
		type, every intermediate field type, mutability against
		each REF intermediate) happens BEFORE `lower_expr` is
		called on the base or any `AddrOfField` is emitted.  This
		keeps the caller's fallback path (whole-expression
		materialization) clean: when this returns None, the
		fallback emits its own MIR over the same untouched
		subject, with no stranded prefix instructions.

		## Supported chain shape

		Validated by `_validate_lifted_chain`:

		1. The outermost subject is `HField`, repeatedly walked
		   inward through nested `HField` projections.
		2. The innermost base is `HCall` / `HMethodCall` /
		   `HInvoke` whose inferred type is `&T` (or `&mut T`).
		3. Every intermediate field's type, after auto-dereffing
		   any ref hops, resolves to a `TypeKind.STRUCT` so the
		   next field name can be looked up.
		4. Every field name is found by `struct_field` on its
		   containing struct.
		5. For `&mut` borrows: every REF hop must be `ref_mut`
		   (mirrors `_lower_addr_of_place`'s mutability rule at
		   line ~10468).

		Anything that fails validation returns None without
		emission.  Caller's fallback handles those subjects via
		the legacy whole-expr materialization.

		## Mirrored auto-deref / mutability rules

		Mirrors the field-projection auto-deref at
		`_lower_addr_of_place` lines ~10466-10475: if a field's
		current type is `Ref<T>`, emit a `LoadRef` to follow the
		ref before projecting the next field.  The mutability
		check at line ~10469 (`is_mut and not td.ref_mut →
		AssertionError`) is replaced here by a soft-fail return-
		None in the validator (the caller's fallback can still
		handle the operand, even if it ultimately also rejects
		it; emission-side bugs masquerade as miscompiles, so we
		prefer to fall through than to assert mid-emission).
		"""
		plan = self._validate_lifted_chain(subject, is_mut=is_mut)
		if plan is None:
			return None
		base_call_expr, projections = plan
		# Validation passed.  Safe to emit.
		ref_val = self.lower_expr(base_call_expr)
		cur_ptr = ref_val
		for step in projections:
			# A step is either a "deref" (REF intermediate) or a
			# "field" (struct field projection).  Deref emits
			# LoadRef; field emits AddrOfField.
			step_kind, step_arg = step
			if step_kind == "deref":
				inner_ref_ty = step_arg  # the Ref<T> type at this point
				loaded = self.b.new_temp()
				self.b.emit(M.LoadRef(dest=loaded, ptr=cur_ptr, inner_ty=inner_ref_ty))
				cur_ptr = loaded
			elif step_kind == "field":
				struct_ty, field_idx, field_ty = step_arg
				next_ptr = self.b.new_temp()
				self.b.emit(M.AddrOfField(
					dest=next_ptr,
					base_ptr=cur_ptr,
					struct_ty=struct_ty,
					field_index=field_idx,
					field_ty=field_ty,
					is_mut=is_mut,
				))
				cur_ptr = next_ptr
			else:
				# Defensive: validator only emits the two step kinds
				# above.  An unknown kind here means the validator
				# and the emitter are out of sync — fail loud
				# (we're past the no-emission boundary already).
				raise AssertionError(
					f"_lift_rvalue_ref_base_for_borrow: unknown step kind "
					f"{step_kind!r} from validator (compiler bug)"
				)
		return cur_ptr

	def _validate_lifted_chain(
		self, subject: H.HExpr, *, is_mut: bool
	) -> "tuple[H.HExpr, list[tuple[str, object]]] | None":
		"""Validate the projection chain for
		`_lift_rvalue_ref_base_for_borrow`.  Pure inspection — emits
		zero MIR instructions.  On success returns
		`(base_call_expr, projections)` where `projections` is the
		ordered list of (`"deref"|"field"`, payload) steps the
		emitter executes against the base's `&T` result.  On any
		structural mismatch — non-field outer chain, non-call
		innermost, non-ref call return, non-struct intermediate,
		unknown field name, &mut through non-&mut REF hop —
		returns None and leaves the MIR state untouched.

		Walks types, never expressions.  `self._infer_expr_type` is
		the only type-table consultation; it does not emit MIR.
		`self._type_table.get` / `.struct_field` are pure lookups.
		"""
		field_names: list[str] = []
		cur = subject
		while isinstance(cur, H.HField):
			field_names.append(cur.name)
			cur = cur.subject
		if not field_names:
			return None
		# Innermost base must be a callable rvalue.  HVar /
		# HPlaceExpr already-place cases are handled by the
		# caller's HPlaceExpr branch.
		if not isinstance(cur, (H.HCall, H.HMethodCall, getattr(H, "HInvoke", ()))):
			return None
		# Base must return a reference type.  Owned-rvalue bases
		# fall through to whole-expr materialization — see
		# `lang/tests/codegen/e2e/autoborrow_owned_rvalue_field_method_unchanged/`
		# for the pinned over-fire guard.
		base_ty = self._infer_expr_type(cur)
		if base_ty is None:
			return None
		base_def = self._type_table.get(base_ty)
		if base_def.kind is not TypeKind.REF or not base_def.param_types:
			return None
		if is_mut and not getattr(base_def, "ref_mut", False):
			# `&mut place.field` over a base that returned `&T`
			# (not `&mut T`) cannot produce a `&mut` field
			# pointer.  Defer to caller's fallback / diagnostic.
			return None
		# Walk the field chain innermost-first against the
		# pointed-to struct type.  Build the step list for the
		# emitter as we go.
		field_names.reverse()
		projections: list[tuple[str, object]] = []
		cur_pointee_ty = base_def.param_types[0]
		for field_name in field_names:
			# Auto-deref ref intermediates: mirrors
			# `_lower_addr_of_place`'s REF-then-field branch.
			pointee_def = self._type_table.get(cur_pointee_ty)
			while pointee_def.kind is TypeKind.REF and pointee_def.param_types:
				if is_mut and not getattr(pointee_def, "ref_mut", False):
					return None
				projections.append(("deref", cur_pointee_ty))
				cur_pointee_ty = pointee_def.param_types[0]
				pointee_def = self._type_table.get(cur_pointee_ty)
			if pointee_def.kind is not TypeKind.STRUCT:
				return None
			field_info = self._type_table.struct_field(cur_pointee_ty, field_name)
			if field_info is None:
				return None
			field_idx, field_ty = field_info
			projections.append(
				("field", (cur_pointee_ty, field_idx, field_ty))
			)
			cur_pointee_ty = field_ty
		return cur, projections

	def _visit_expr_HCopy(self, expr: H.HCopy) -> M.ValueId:
		"""
		Lower explicit `copy <expr>`.

		Evaluates the subject expression, then emits a CopyValue instruction
		to produce an owned deep copy.  When the subject is a reference (&T),
		the ref is first dereferenced via LoadRef to get the inner value,
		then the inner value is copied.
		"""
		val = self.lower_expr(expr.subject)
		val_ty = self._infer_expr_type(expr.subject)
		if val_ty is None:
			raise AssertionError("copy operand type unknown in MIR lowering (checker bug)")
		td = self._type_table.get(val_ty)
		# If the subject is a reference, deref first to get the inner value.
		if td.kind is TypeKind.REF and td.param_types:
			inner_ty = td.param_types[0]
			derefed = self.b.new_temp()
			self.b.emit(M.LoadRef(dest=derefed, ptr=val, inner_ty=inner_ty))
			self._local_types[derefed] = inner_ty
			val = derefed
			val_ty = inner_ty
		copy_dest = self.b.new_temp()
		self.b.emit(M.CopyValue(dest=copy_dest, value=val, ty=val_ty))
		self._local_types[copy_dest] = val_ty
		return copy_dest

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
		# materialize-audit: allow consumed
		# tmp_local is immediately MoveOut'd two lines below; the
		# value transfers into moved_val and tmp_local is no longer
		# live for scope cleanup.
		tmp_local = f"__cap_move_{self.b.new_temp()}"
		self.b.ensure_local(tmp_local)
		self._local_types[tmp_local] = inner_ty
		self.b.emit(M.StoreLocal(local=tmp_local, value=loaded))
		moved_val = self.b.new_temp()
		self.b.emit(M.MoveOut(dest=moved_val, local=tmp_local, ty=inner_ty))
		self._local_types[moved_val] = inner_ty
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
			# Mark as ref-aliased if the inner type requires runtime
			# clone-on-read-from-ref (contains refcounted fields).
			# _copy_if_ref_alias will emit CopyValue at ownership
			# transfer boundaries (return, binding, call arg).
			if not self._drop_policy(inner_ty).is_bitcopy:
				self._ref_field_temps.add(dest)
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
			# materialize-audit: allow non-owning Bool short-circuit
			# holds the AND/OR result.  Bool is bitcopy + non-droppable;
			# no scope cleanup needed.
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
		# Field projection through array indexing on a non-Copy element:
		# entries[i].name should borrow the element, not copy it.
		# Emit AddrOfArrayElem + StructGetField instead of copying the whole element.
		if isinstance(expr.subject, H.HIndex):
			idx_subj_ty = self._infer_expr_type(expr.subject.subject)
			if idx_subj_ty is not None:
				idx_subj_def = self._type_table.get(idx_subj_ty)
				# Unwrap reference: &Array<T> → Array<T>
				is_ref = idx_subj_def.kind is TypeKind.REF and idx_subj_def.param_types
				if is_ref:
					inner_arr_ty = idx_subj_def.param_types[0]
					inner_arr_def = self._type_table.get(inner_arr_ty)
				else:
					inner_arr_ty = idx_subj_ty
					inner_arr_def = idx_subj_def
				if inner_arr_def.kind is TypeKind.ARRAY and inner_arr_def.param_types:
					elem_ty = inner_arr_def.param_types[0]
					elem_def = self._type_table.get(elem_ty)
					if elem_def.kind is TypeKind.STRUCT and not self._should_copy_value(elem_ty):
						field_info = self._type_table.struct_field(elem_ty, expr.name)
						if field_info is not None:
							field_idx, field_ty = field_info
							arr_val = self.lower_expr(expr.subject.subject)
							if is_ref:
								arr_loaded = self.b.new_temp()
								self.b.emit(M.LoadRef(dest=arr_loaded, ptr=arr_val, inner_ty=inner_arr_ty))
								arr_val = arr_loaded
							idx_val = self.lower_expr(expr.subject.index)
							elem_ptr = self.b.new_temp()
							self.b.emit(M.AddrOfArrayElem(dest=elem_ptr, array=arr_val, index=idx_val, inner_ty=elem_ty))
							field_ptr = self.b.new_temp()
							self.b.emit(M.AddrOfField(dest=field_ptr, base_ptr=elem_ptr, struct_ty=elem_ty, field_index=field_idx, field_ty=field_ty))
							dest = self.b.new_temp()
							self.b.emit(M.LoadRef(dest=dest, ptr=field_ptr, inner_ty=field_ty))
							if self._should_copy_value(field_ty):
								copy_dest = self.b.new_temp()
								self.b.emit(M.CopyValue(dest=copy_dest, value=dest, ty=field_ty))
								self._local_types[copy_dest] = field_ty
								return copy_dest
							self._local_types[dest] = field_ty
							return dest
		subject = self.lower_expr(expr.subject)
		subj_ty = self._infer_expr_type(expr.subject)
		if subj_ty is None:
			raise AssertionError("field subject type unknown in MIR lowering (checker bug)")
		sub_def = self._type_table.get(subj_ty)
		loaded_from_ref = False
		if sub_def.kind is TypeKind.REF and sub_def.param_types:
			inner_ty = sub_def.param_types[0]
			loaded = self.b.new_temp()
			self.b.emit(M.LoadRef(dest=loaded, ptr=subject, inner_ty=inner_ty))
			subject = loaded
			subj_ty = inner_ty
			sub_def = self._type_table.get(subj_ty)
			loaded_from_ref = True
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
		if expr.name == "params" and sub_def.kind is TypeKind.ERROR:
			# Slice 1 of the DV→JSON diagnostics-context migration:
			# `<error>.params` lowers to ExcGetParamsJson + ConstructStruct
			# of `core.ErrorParamsView { json_text: <retained-canonical-JSON> }`.
			# The runtime helper returns a retained DriftString per ABI
			# spec §2.3; string_arc tracks the dest as an owned string
			# result (see stage2/string_arc.py).
			json_dest = self.b.new_temp()
			self.b.emit(M.ExcGetParamsJson(dest=json_dest, error=subject))
			self._local_types[json_dest] = self._string_type
			epv_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id="std.core", name="ErrorParamsView")
			if epv_ty is None:
				raise AssertionError("std.core:ErrorParamsView not found in type table (compiler invariant)")
			view_dest = self.b.new_temp()
			self.b.emit(M.ConstructStruct(dest=view_dest, struct_ty=epv_ty, args=[json_dest]))
			self._local_types[view_dest] = epv_ty
			return view_dest
		if expr.name == "context" and sub_def.kind is TypeKind.ERROR:
			# Slice 2 mirror of the params lowering: ExcGetContextJson +
			# ConstructStruct of `core.ErrorContextView`.
			json_dest = self.b.new_temp()
			self.b.emit(M.ExcGetContextJson(dest=json_dest, error=subject))
			self._local_types[json_dest] = self._string_type
			ecv_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id="std.core", name="ErrorContextView")
			if ecv_ty is None:
				raise AssertionError("std.core:ErrorContextView not found in type table (compiler invariant)")
			view_dest = self.b.new_temp()
			self.b.emit(M.ConstructStruct(dest=view_dest, struct_ty=ecv_ty, args=[json_dest]))
			self._local_types[view_dest] = ecv_ty
			return view_dest
		if expr.name == "attrs":
			raise NotImplementedError("attrs view must be indexed: Error.attrs[\"key\"]")
		if expr.name == "captures":
			raise NotImplementedError(
				"captures view must be indexed: Error.captures[\"frame\"][\"key\"]"
			)
		# Slice 5 (slice 3 of impl): typed catch binder field projection.
		# When the type-checker recognized this HField as a typed
		# projection on a typed catch binder (Error envelope viewed
		# through the matched event's schema), it set
		# `expr.typed_proj_event_fqn`.  Lower as a compiler-owned
		# call to the appropriate `_typed_params_field_<scalar>`
		# helper in std.core — bypasses the public ErrorParamsView /
		# JsonCursor surface so the typed projection's behavior
		# (deterministic missing-key / wrong-type → default) is
		# fixed by spec, not by user-visible cursor ergonomics.
		typed_proj_fqn = getattr(expr, "typed_proj_event_fqn", None)
		if typed_proj_fqn is not None and sub_def.kind is TypeKind.ERROR:
			# Invariant: the type-checker only sets `typed_proj_event_fqn`
			# after validating the binder + schema field + supported scalar
			# type.  Lowering returning None here means a checker/lowering
			# boundary mismatch — fail loud, do NOT fall through to the
			# default lowering (which would emit garbage at runtime).
			result = self._lower_typed_catch_field_proj(
				envelope=subject,
				event_fqn=typed_proj_fqn,
				field_name=expr.name,
			)
			if result is None:
				raise AssertionError(
					f"typed_proj_event_fqn={typed_proj_fqn!r} field={expr.name!r}: "
					f"_lower_typed_catch_field_proj returned None for an annotated "
					f"projection — checker/lowering boundary mismatch (slice 3 "
					f"first pass invariant)"
				)
			return result
		if sub_def.kind is TypeKind.FORWARD_NOMINAL:
			resolved_ty: TypeId | None = None
			alias_def = self._type_table.lookup_type_alias(module_id=sub_def.module_id, name=sub_def.name)
			if alias_def is not None:
				alias_params, alias_target, _loc = alias_def
				if not alias_params:
					cand = resolve_opaque_type(alias_target, self._type_table, module_id=sub_def.module_id, type_params=None, allow_generic_base=True)
					cand_def = self._type_table.get(cand)
					if cand_def.kind is TypeKind.STRUCT:
						resolved_ty = cand
			if resolved_ty is None:
				resolved_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=sub_def.module_id, name=sub_def.name)
			if resolved_ty is None:
				resolved_ty = self._type_table.find_unique_nominal_by_name(kind=TypeKind.STRUCT, name=sub_def.name)
			if resolved_ty is not None:
				subj_ty = resolved_ty
				sub_def = self._type_table.get(subj_ty)
		# Struct field access.
		if sub_def.kind is not TypeKind.STRUCT:
			raise NotImplementedError(f"field access is only supported on structs in v1 (have {sub_def.kind})")
		info = self._type_table.struct_field(subj_ty, expr.name)
		if info is None:
			_fn_name = getattr(self.b.func, "name", None)
			_sd = self._type_table.get(subj_ty)
			_field_list = [getattr(f, "name", None) for f in getattr(_sd, "fields", []) or []]
			raise AssertionError(
				"unknown struct field reached MIR lowering (checker bug): "
				f"field={expr.name!r} subj_ty={subj_ty} struct={getattr(_sd, 'name', None)!r} "
				f"module={getattr(_sd, 'module_id', None)!r} known_fields={_field_list} fn={_fn_name!r}"
			)
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
		# StructGetField (LLVM extractvalue) produces an aliased bitcopy of
		# the source's data.  For non-bitcopy fields this means the result
		# shares the backing allocation with the source.  If the source
		# will be dropped (it's a local/param, a &T deref, or itself an
		# aliased temp), the extracted value is a dangerous alias that must
		# be deep-copied before any ownership transfer.
		#
		# We mark the temp here so that consumption sites (struct/variant
		# construction, return, variable binding, call args) emit a copy.
		# Transient uses (chained .len, comparisons) are safe without a
		# copy and would otherwise leak the intermediate allocation.
		if not self._drop_policy(field_ty).is_bitcopy:
			subject_is_alias = loaded_from_ref or subject in self._ref_field_temps
			if not subject_is_alias:
				# Check if the subject was loaded from a local/param that
				# will be dropped at scope exit (owned struct field read).
				if isinstance(expr.subject, H.HVar):
					subject_is_alias = True
				elif hasattr(H, "HPlaceExpr") and isinstance(expr.subject, getattr(H, "HPlaceExpr")) and not expr.subject.projections:
					subject_is_alias = True
			if subject_is_alias:
				self._ref_field_temps.add(dest)
		return dest

	def _visit_expr_HIndex(self, expr: H.HIndex) -> M.ValueId:
		# Slice 7b (2026-05-05): user-source `e.attrs[k]` / `e.captures[k]`
		# is rejected at the checker boundary with `E_EXC_ATTRS_REMOVED`
		# / `E_EXC_CAPTURES_REMOVED`; the legacy DV-bridge lowering paths
		# that emitted `M.ErrorAttrsGetDV` / `M.ErrorCapturesGetDV` are
		# retired here.  The corresponding MIR ops + runtime exports are
		# slated for deletion later in this slice.
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

		# materialize-audit: allow registered explicit
		# _register_drop_local call below
		# tmp_local is alloc'd up-top and stored INSIDE the ok_block
		# (after the bounds-check branch), so the helper's
		# all-in-one alloc+store doesn't fit cleanly.  Hand-roll
		# the 4 steps with explicit register-drop.
		tmp_local = f"__idx_tmp{self.b.new_temp()}"
		self.b.ensure_local(tmp_local)
		self._local_types[tmp_local] = elem_ty
		self._register_drop_local(tmp_local, elem_ty)

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

		transfer = self._classify_value_transfer(elem_ty, allow_unknown_typevar=True)
		# PHASE 1 RESIDUAL (Copy-trait-claim-with-transfer-disposition).
		# The combined predicate "transfer is copy OR move AND the
		# Copy-trait claim is decided True" isn't expressible as a
		# pure `DropPolicy` axis — it straddles the policy (`transfer`)
		# and the raw trait claim.  The "move AND copy_status=True"
		# subcase is specifically the `Optional<String>`-shape where
		# the Copy trait resolves True but the policy correctly
		# classifies as move due to drop-bearing substructure.  Phase
		# 2 subsumes this by adding a typevar-aware axis or by
		# driving array-element classification from the ledger
		# directly.
		if transfer in ("copy", "move") and self._type_table.copy_status(elem_ty) is True:
			copy_dest = self.b.new_temp()
			self.b.emit(M.CopyValue(dest=copy_dest, value=loaded, ty=elem_ty))
			self._local_types[copy_dest] = elem_ty
			return copy_dest
		if transfer == "unknown":
			return loaded
		td = self._type_table.get(elem_ty)
		if td.kind is not TypeKind.TYPEVAR:
			# PHASE 1 RESIDUAL (v1 array-read guard).  Dead-branch
			# assertion: reached when the element is neither Copy
			# nor typevar.  Kept as a `copy_status` direct query
			# because it's an invariant check, not an ownership
			# decision; Phase 2 replaces it with a ledger-driven
			# classification once array-element policy becomes a
			# first-class axis.
			if self._type_table.copy_status(elem_ty) is True:
				raise NotImplementedError("array index read requires trivially-copyable element type in v1")
			raise NotImplementedError("array index read requires Copy element type; borrow not supported in v1")
		return loaded

	def _emit_index_error_throw(self, *, index_val: M.ValueId) -> None:
		"""Compiler-synthesized inline throw for the array bounds-check.

		Slice 7b (2026-05-05): routed through the same unified
		Diagnostic owning-throw path as user-source `throw E(...)` —
		constructs the `std.err:IndexError` Path-A struct from MIR
		values, calls its synthesized `to_json_text(&IndexError)`, and
		emits ConstructError(empty) + ExcSetParamsJson.  No legacy DV
		attachment.
		"""
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

		# Slice 7b: stage2-unit-test environments stub out std.err — no
		# Path-A struct is registered in the synthetic type table.
		# Fall through to empty-envelope ConstructError when missing.
		# (Production compilation always has the struct registered by
		# parser-side parallel struct synthesis.)
		struct_ty = self._type_table.get_nominal(
			kind=TypeKind.STRUCT, module_id="std.err", name="IndexError",
		)

		code_const = self._lookup_error_code(event_fqn=event_fqn)
		code_val = self.b.new_temp()
		self.b.emit(M.ConstUint64(dest=code_val, value=code_const))
		self._local_types[code_val] = self._uint64_type
		event_fqn_val = self.b.new_temp()
		self.b.emit(M.ConstString(dest=event_fqn_val, value=event_fqn))

		container_const = self.b.new_temp()
		self.b.emit(M.ConstString(dest=container_const, value=ARRAY_CONTAINER_ID))
		self._local_types[container_const] = self._string_type

		# Build positional MIR values in declaration order.
		name_to_mir = {"container_id": container_const, "index": index_val}
		field_mir_vals = [name_to_mir[name] for name in schema_fields]

		err_val = self.b.new_temp()
		self._local_types[err_val] = self._type_table.ensure_error()
		# Slice 7b contract guard (K, 2026-05-06):
		#   - Stage2-unit-test environments stub out std.err — no
		#     Path-A struct registered — fall through to empty-envelope.
		#   - Production / package-consumer compilation MUST have the
		#     synthesized `to_json_text` reachable; missing it is a
		#     compiler/package-loader bug, not a valid `{}` payload.
		#     ICE so the bug surfaces clearly.
		if struct_ty is None:
			self.b.emit(M.ConstructError(
				dest=err_val,
				code=code_val,
				event_fqn=event_fqn_val,
				payload=None,
				attr_key=None,
			))
		else:
			to_json_fn_id = self._lookup_diagnostic_to_json_text_fn_id(struct_ty)
			if to_json_fn_id is None:
				raise AssertionError(
					f"`pub error {event_fqn}` has its Path-A struct registered "
					"but its synthesized `core.Diagnostic.to_json_text` impl "
					"method is not in `_signatures_by_id` — compiler / "
					"package-loader bug.  The synthesized impl should always "
					"be reachable for production `pub error` types (including "
					"package-consumer compilation that imports std.err)."
				)
			self._emit_diagnostic_owning_throw(
				err_val=err_val,
				code_val=code_val,
				event_fqn_val=event_fqn_val,
				event_fqn=event_fqn,
				struct_ty=struct_ty,
				field_mir_vals=field_mir_vals,
			)

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
			if val_ty is not None and not isinstance(elem_expr, H.HMove):
				# Step 1: deep-copy any ref-alias temp into an owned
				# value at the ownership boundary.  `_copy_if_ref_alias`
				# is the standard mechanism the rest of the lowering
				# uses (call args, struct ctors, returns, var bindings)
				# to upgrade a HField/HPlaceExpr-projection alias of an
				# owning local's storage into an independent owned temp.
				# Pre-fix the ArrayLit elem loop skipped this step, so
				# downstream CopyValue saw an aliased struct value
				# whose nested fields still belonged to the source
				# local — releasing that alias later would UAF.
				val = self._copy_if_ref_alias(val, val_ty)

				# Step 2: K28-aftermath / drift-net-tls v0.3.14
				# alignment.  ArrayLit's per-element store is subject
				# to the same MIR contract as ArrayElemInit* /
				# ArrayIndexStore at push/insert/set sites — Copy
				# non-bitcopy values need explicit CopyValue (with a
				# paired DropValue when the source is an OWNED rvalue
				# temp).
				#
				# Pre-fix this branch only fired for `_should_copy_value`
				# (= Copy AND no runtime-owned substructure), missing
				# Copy non-bitcopy structs like core.DiagnosticEntry
				# whose `_needs_runtime_drop` returns True (because
				# they transitively contain DV) and so are classified
				# as "move" rather than "copy".  That tripped the
				# MIR-validate "must use CopyValue or MoveOut for Copy
				# element type" invariant.  See
				# issues/array_lit_copy_nonbitcopy_struct_mir_invariant/.
				#
				# PHASE 1 RESIDUAL (Copy-but-non-bitcopy).  This asks
				# a predicate the five current `DropPolicy` axes don't
				# express: "the Copy-trait claim is decided True for
				# this type AND the bits are not self-contained."
				# `is_cheap_copy` collapses to False for any
				# `needs_drop=True` type (including DV-bearing
				# structs like `core.DiagnosticEntry`), so it isn't
				# the right axis here.  Phase 2 adds the missing axis
				# (or retires this branch via a ledger-driven
				# rewrite); until then the raw queries are intentional.
				copy_status = self._type_table.copy_status(val_ty)
				if copy_status is True and not self._type_table.is_bitcopy(val_ty):
					copy_dest = self.b.new_temp()
					self.b.emit(M.CopyValue(dest=copy_dest, value=val, ty=val_ty))
					self._local_types[copy_dest] = val_ty
					# Source-ownership classification, mirroring
					# `_call_arg_yields_owned_temp`'s rules adapted for
					# `lower_expr` (which handles HVar/HPlaceExpr as
					# plain loads without a MoveOut wrapper):
					# - `H.HVar` / projection-free `HPlaceExpr` → borrowed
					#   view sharing refcount with the source local;
					#   dropping it would UAF on the next store into
					#   that local (the drift-net-tls Array<String>
					#   regression family).
					# - Everything else (HCall, HPlaceExpr with
					#   projections after `_copy_if_ref_alias` upgraded
					#   them to owned temps, …) → owned rvalue temp;
					#   the paired DropValue releases its own ref so
					#   the cloned `copy_dest` is the lone owner.
					HPlaceExpr = getattr(H, "HPlaceExpr", None)
					is_place = isinstance(elem_expr, H.HVar) or (
						HPlaceExpr is not None
						and isinstance(elem_expr, HPlaceExpr)
						and not getattr(elem_expr, "projections", None)
					)
					if not is_place:
						self.b.emit(M.DropValue(value=val, ty=val_ty))
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

		# materialize-audit: allow consumed
		# map_local is built up via iterative inserts then MoveOut'd
		# to the caller via the explicit MoveOut at the end of this
		# function; the slot's stake transfers out, no scope cleanup
		# needed.  Cleanup_authoring's ledger walker sees the MoveOut
		# and would suppress any drop anyway.
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
		# Pre-flight: validate arity/kwargs via call_contract (single seam).
		_kwargs = getattr(expr, "kwargs", None) or []
		_shape_issues = [i for i in intrinsic_call_issues(intrinsic, expr, kwargs=_kwargs) if "MUT_BORROW_REQUIRED" not in i.code]
		if _shape_issues:
			raise AssertionError(f"{_shape_issues[0].message} reached MIR lowering (checker bug)")
		if intrinsic is IntrinsicKind.REPLACE:
			place_expr = expr.args[0]
			if isinstance(place_expr, H.HBorrow) and place_expr.is_mut:
				place_expr = place_expr_from_lvalue_expr(place_expr.subject)
			new_expr = expr.args[1]
			# Order is load-bearing: address first, then lower/consume
			# the replacement value, THEN MoveFromRef the old owner out
			# (atomically tombstoning the slot), THEN StoreRef the new
			# value, THEN MoveOut the temp local as the SSA result.
			#
			# Consuming `new_expr` BEFORE MoveFromRef matters because
			# MoveFromRef is mutating (it tombstones *ptr).  If a
			# throwing or otherwise abandoned replacement expression
			# could leave the place tombstoned, callers would see a
			# slot in an inconsistent state.  Lowering `new_expr` first
			# guarantees the replacement is fully realised before any
			# mutation reaches the destination.
			#
			# MoveFromRef vs LoadRef: LoadRef is a raw pointer copy
			# with no ownership transfer.  string_arc's late-rewrite
			# at `string_arc.py:StoreRef` synthesises a
			# drop-before-overwrite (LoadRef + StringRelease) on every
			# StoreRef to a String place — for non-Copy refcount types
			# that release would free the original allocation while the
			# caller still holds the LoadRef'd pointer (use-after-free;
			# carrier `lang/tests/memcheck/test_mem_replace_string_uaf.py`).
			# MoveFromRef tombstones the slot atomically, so the
			# subsequent StoreRef rewrite's release fires on null bytes
			# (`drift_string_release(null)` is a runtime no-op; see
			# `string_arc.py:1097-1099`).
			#
			# Two argument shapes are accepted, matching the checker's
			# acceptance criterion (resolved type == &mut T):
			#   1. Inline borrow form (`&mut <place>`) or a bare
			#      HPlaceExpr — gives us a place to lower as an
			#      address via _lower_addr_of_place.
			#   2. Any other expression that resolves to &mut T —
			#      named local / parameter / method-call return.
			#      The expression's lowered value IS the pointer
			#      (refs are pointers at the MIR boundary); inner_ty
			#      comes from the call's recorded signature.  This
			#      is the customer-facing fix for the 0.31.80 bug
			#      where named &mut T values were rejected with
			#      "replace expects &mut T as the first argument"
			#      even though the type matched.
			if hasattr(H, "HPlaceExpr") and isinstance(place_expr, getattr(H, "HPlaceExpr")):
				ptr, inner_ty = self._lower_addr_of_place(place_expr, is_mut=True)
			else:
				if info is None or info.sig is None:
					raise AssertionError("replace(named-ref, v): missing CallInfo (checker bug)")
				inner_ty = info.sig.user_ret_type
				ptr = self.lower_expr(expr.args[0])
			new_val = self._lower_owning_consume(new_expr, expected=inner_ty)
			tmp_local = f"__replace_old_{self.b.new_temp()}"
			self.b.ensure_local(tmp_local)
			self._local_types[tmp_local] = inner_ty
			self.b.emit(M.MoveFromRef(local=tmp_local, ptr=ptr, inner_ty=inner_ty))
			self.b.emit(M.StoreRef(ptr=ptr, value=new_val, inner_ty=inner_ty))
			old_val = self.b.new_temp()
			self.b.emit(M.MoveOut(dest=old_val, local=tmp_local, ty=inner_ty))
			self._local_types[old_val] = inner_ty
			return old_val
		if intrinsic is IntrinsicKind.MAYBE_UNINIT:
			if info is None:
				raise AssertionError("maybe_uninit(...) missing CallInfo (checker bug)")
			ret_ty = info.sig.user_ret_type
			dest = self.b.new_temp()
			self.b.emit(M.ZeroValue(dest=dest, ty=ret_ty))
			self._local_types[dest] = ret_ty
			return dest
		if intrinsic is IntrinsicKind.MAYBE_WRITE:
			if info is None:
				raise AssertionError("maybe_write(...) missing CallInfo (checker bug)")
			ret_ty = info.sig.user_ret_type
			inner_ty = self._unwrap_ref_type(ret_ty)
			if inner_ty is None:
				raise AssertionError("maybe_write(...) missing &mut T return type (checker bug)")
			slot = self.lower_expr(expr.args[0])
			val = self._lower_owning_consume(expr.args[1], expected=inner_ty)
			raw_ptr = self.b.new_temp()
			ptr_ty = self._type_table.new_ptr(inner_ty)
			self.b.emit(M.PtrFromRef(dest=raw_ptr, src=slot, ptr_ty=ptr_ty))
			self.b.emit(M.PtrWrite(ptr=raw_ptr, value=val, elem_ty=inner_ty))
			self._local_types[raw_ptr] = ptr_ty
			self._local_types[slot] = ret_ty
			return slot
		if intrinsic in (IntrinsicKind.MAYBE_ASSUME_INIT_REF, IntrinsicKind.MAYBE_ASSUME_INIT_MUT):
			if info is None:
				raise AssertionError(f"{intrinsic.value}(...) missing CallInfo (checker bug)")
			ret_ty = info.sig.user_ret_type
			slot = self.lower_expr(expr.args[0])
			self._local_types[slot] = ret_ty
			return slot
		if intrinsic is IntrinsicKind.MAYBE_ASSUME_INIT_READ:
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
			if info is None:
				raise AssertionError("rawbuffer_from_parts(...) missing CallInfo (checker bug)")
			ptr_val = self.lower_expr(expr.args[0])
			cap_val = self.lower_expr(expr.args[1])
			dest = self.b.new_temp()
			self.b.emit(M.ConstructStruct(dest=dest, struct_ty=info.sig.user_ret_type, args=[ptr_val, cap_val]))
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		if intrinsic is IntrinsicKind.RAWBUFFER_EMPTY:
			# Drained-state sentinel for `RawBuffer<T>` — null ptr,
			# zero cap.  Lowers to a single `ZeroValue` (LLVM
			# zeroinitializer for the aggregate).  No runtime helper.
			# Used by stdlib code that needs to mark a `RawBuffer<T>`
			# field as drained without inventing a parallel flag —
			# `TypeBox.take<T>` is the reference user.
			if info is None:
				raise AssertionError("rawbuffer_empty(...) missing CallInfo (checker bug)")
			dest = self.b.new_temp()
			self.b.emit(M.ZeroValue(dest=dest, ty=info.sig.user_ret_type))
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		if intrinsic in (IntrinsicKind.RAW_PTR_AT_REF, IntrinsicKind.RAW_PTR_AT_MUT):
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
			if info is None:
				raise AssertionError("ptr_from_ref(...) missing CallInfo (checker bug)")
			src_val = self.lower_expr(expr.args[0])
			dest = self.b.new_temp()
			self.b.emit(M.PtrFromRef(dest=dest, src=src_val, ptr_ty=info.sig.user_ret_type))
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		if intrinsic is IntrinsicKind.PTR_OFFSET:
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
			if info is None or not info.sig.param_types:
				raise AssertionError("ptr_is_null(...) missing CallInfo (checker bug)")
			ptr_val = self.lower_expr(expr.args[0])
			dest = self.b.new_temp()
			self.b.emit(M.PtrIsNull(dest=dest, ptr=ptr_val, ptr_ty=info.sig.param_types[0]))
			self._local_types[dest] = self._bool_type
			return dest
		if intrinsic is IntrinsicKind.PTR_AS_MUT_REF:
			if info is None:
				raise AssertionError("ptr_as_mut_ref(...) missing CallInfo (checker bug)")
			src_val = self.lower_expr(expr.args[0])
			dest = self.b.new_temp()
			self.b.emit(M.PtrAsMutRef(dest=dest, src=src_val, ref_ty=info.sig.user_ret_type))
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		if intrinsic is IntrinsicKind.BYTE_LENGTH:
			name = intrinsic.value
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
		if intrinsic in _CALLBACK_INTRINSIC_KINDS:
			arg = expr.args[0]
			if info is None:
				raise AssertionError(f"{intrinsic.value}(...) missing CallInfo (checker bug)")
			can_throw = intrinsic in _CALLBACK_THROW_INTRINSIC_KINDS
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
				raise NotImplementedError(f"{intrinsic.value} requires a function pointer constant or lambda in v1")
			self._local_types[dest] = info.sig.user_ret_type
			return dest
		if intrinsic is IntrinsicKind.TYPE_ID:
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
			if info.target.kind is not CallTargetKind.CONSTRUCTOR:
				_kw_issues = call_kwargs_issues("UFCS call", getattr(expr, "kwargs", None))
				if _kw_issues:
					raise AssertionError(f"{_kw_issues[0].message} reached MIR lowering")
			if info.target.kind is CallTargetKind.INTRINSIC:
				intrinsic = info.target.intrinsic
				if intrinsic is None:
					raise AssertionError("intrinsic call missing name (typecheck/call-info bug)")
				return self._lower_intrinsic_call_expr(expr, intrinsic, info=info)
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
				if info.target.kind is not CallTargetKind.CONSTRUCTOR and call_kwargs_issues("a normal call", getattr(expr, "kwargs", None)):
					cur_mod = self._current_module_name()
					fn_module = getattr(expr.fn, "module_id", None)
					if isinstance(fn_module, str):
						struct_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=fn_module, name=name)
					else:
						struct_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=cur_mod, name=name) or self._type_table.find_unique_nominal_by_name(
							kind=TypeKind.STRUCT, name=name
						)
					if struct_ty is None:
						_kw_issues = call_kwargs_issues("a normal call", getattr(expr, "kwargs", None))
						raise AssertionError(f"{_kw_issues[0].message} reached MIR lowering")
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

						field_names = list(getattr(arm_def, "field_names", []) or [])
						field_types = list(arm_def.field_types)
						if len(field_names) != len(field_types):
							raise AssertionError("variant ctor schema/type mismatch reached MIR lowering (checker bug)")

						_ctor_issues = ctor_call_issues(len(pos_args), tuple(kw.name for kw in kw_pairs), CtorFieldSpec(field_names=tuple(field_names)), ctor_label="variant", span=getattr(expr, "loc", None))
						if _ctor_issues:
							raise AssertionError(f"{_ctor_issues[0].message} reached MIR lowering")

						ordered: list[M.ValueId | None] = [None] * len(field_types)
						# Evaluate arguments left-to-right as written, but pass them in field order.
						if kw_pairs:
							for kw in kw_pairs:
								field_idx = field_names.index(kw.name)
								ordered[field_idx] = self.lower_expr(kw.value, expected_type=field_types[field_idx])
						else:
							for idx, (arg_expr, fty) in enumerate(zip(pos_args, field_types)):
								ordered[idx] = self.lower_expr(arg_expr, expected_type=fty)
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

				_ctor_issues = ctor_call_issues(len(pos_args), tuple(kw.name for kw in kw_pairs), CtorFieldSpec(field_names=tuple(field_names)), ctor_label="struct", span=getattr(expr, "loc", None))
				if _ctor_issues:
					raise AssertionError(f"{_ctor_issues[0].message} reached MIR lowering")

				# Evaluate arguments left-to-right as written, but pass them in field order.
				ordered: list[M.ValueId | None] = [None] * len(field_types)
				for idx, arg_expr in enumerate(pos_args):
					val = self.lower_expr(arg_expr, expected_type=field_types[idx])
					ordered[idx] = self._copy_if_ref_alias(val, field_types[idx])
				for kw in kw_pairs:
					field_idx = field_names.index(kw.name)
					val = self.lower_expr(kw.value, expected_type=field_types[field_idx])
					ordered[field_idx] = self._copy_if_ref_alias(val, field_types[field_idx])
				arg_vals = [v for v in ordered if v is not None]
				dest = self.b.new_temp()
				self.b.emit(M.ConstructStruct(dest=dest, struct_ty=struct_ty, args=arg_vals))
				self._local_types[dest] = struct_ty
				return dest
		if not isinstance(expr.fn, H.HVar):
			raise NotImplementedError("Only direct function-name calls are supported in MIR lowering")
		_kw_issues = call_kwargs_issues("a normal call", getattr(expr, "kwargs", None), span=getattr(expr, "loc", None))
		if _kw_issues:
			raise AssertionError(f"{_kw_issues[0].message}")
		info = self._call_info_for(expr)
		result = self._lower_call(expr)
		if result is None:
			expected = self._current_expected_type()
			if expected is not None and self._type_table.is_void(expected):
				return self._void_value()
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
		_kw_issues = call_kwargs_issues("value calls", getattr(expr, "kwargs", None), span=getattr(expr, "loc", None))
		if _kw_issues:
			raise AssertionError(f"{_kw_issues[0].message}")
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
					if expr.method_name not in {"as_int", "as_bool", "as_float", "as_string", "as_object", "get", "index", "kind", "len", "entries", "dup", "iter", "next", "unwrap_ok", "unwrap_err"}:
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
			if hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
				return block_can_throw(expr.body) or expr_can_throw(expr.result)
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
			return False

		def stmt_can_throw(stmt: H.HStmt) -> bool:
			if isinstance(stmt, H.HThrow) or isinstance(stmt, H.HRethrow):
				return True
			if isinstance(stmt, H.HExprStmt):
				return expr_can_throw(stmt.expr)
			if isinstance(stmt, H.HLocalConst):
				return False  # literal initializer, never throws
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
			# Capture-mode keywords map 1-to-1 onto HCaptureKind.
			# The legacy `"auto": REF` mapping (bareword
			# `captures(x)` silently lowering to a borrowed-cell
			# capture) was removed in 0.31.22 — see
			# `lang/tests/driver/test_bareword_capture_rejected.py`
			# and `parser/parser.py::_build_lambda_capture`.
			kind_map = {
				"ref": C.HCaptureKind.REF,
				"ref_mut": C.HCaptureKind.REF_MUT,
				"copy": C.HCaptureKind.COPY,
				"move": C.HCaptureKind.MOVE,
				"share": C.HCaptureKind.SHARE,
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
			# Build a binding_id → HExplicitCapture map so the SHARE
			# branch can read `share_value` (synthesized in
			# `ast_to_hir.py::_visit_expr_Lambda`).  `lam.captures` is
			# the post-discovery `C.HCapture` list; `lam.explicit_captures`
			# is the source-spelled HIR list — both share binding_ids.
			hir_cap_by_bid: dict[int, "H.HExplicitCapture"] = {}
			for ec in (getattr(lam, "explicit_captures", None) or []):
				if ec.binding_id is not None:
					hir_cap_by_bid[int(ec.binding_id)] = ec
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
						env_vals.append(moved_val)
						env_field_types.append(inner_ty)
				elif cap.kind is C.HCaptureKind.SHARE:
					hir_cap = hir_cap_by_bid.get(int(cap.key.root_local))
					if hir_cap is None:
						raise AssertionError(
							"SHARE capture has no matching HExplicitCapture with share_value"
						)
					self._lower_share_capture(
						cap=cap,
						hir_cap=hir_cap,
						env_vals=env_vals,
						env_field_types=env_field_types,
					)
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
			# Capture-mode keywords map 1-to-1 onto HCaptureKind.
			# The legacy `"auto": REF` mapping (bareword
			# `captures(x)` silently lowering to a borrowed-cell
			# capture) was removed in 0.31.22 — see
			# `lang/tests/driver/test_bareword_capture_rejected.py`
			# and `parser/parser.py::_build_lambda_capture`.
			kind_map = {
				"ref": C.HCaptureKind.REF,
				"ref_mut": C.HCaptureKind.REF_MUT,
				"copy": C.HCaptureKind.COPY,
				"move": C.HCaptureKind.MOVE,
				"share": C.HCaptureKind.SHARE,
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
		# B4: borrowed captures in callback env are allowed — the borrow checker
		# validates escape levels (SCOPED/LOCAL accept, THREAD/STATIC reject).
		# The env code at lines 3000-3008 handles ref field storage/loading.

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
			# Build a binding_id → HExplicitCapture map so the SHARE
			# branch can read `share_value` (synthesized in
			# `ast_to_hir.py::_visit_expr_Lambda`).  See the matching
			# block in `_lower_lambda_immediate_call`.
			hir_cap_by_bid: dict[int, "H.HExplicitCapture"] = {}
			for ec in (getattr(lam, "explicit_captures", None) or []):
				if ec.binding_id is not None:
					hir_cap_by_bid[int(ec.binding_id)] = ec
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
						env_vals.append(moved_val)
						env_field_types.append(inner_ty)
				elif cap.kind is C.HCaptureKind.SHARE:
					hir_cap = hir_cap_by_bid.get(int(cap.key.root_local))
					if hir_cap is None:
						raise AssertionError(
							"SHARE capture has no matching HExplicitCapture with share_value"
						)
					self._lower_share_capture(
						cap=cap,
						hir_cap=hir_cap,
						env_vals=env_vals,
						env_field_types=env_field_types,
					)
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
		lam_is_void = lower._ret_type is not None and lower._type_table.is_void(lower._ret_type)
		if isinstance(last, H.HExprStmt) and not lam_is_void:
			val = lower.lower_expr(last.expr)
			if lower.b.block.terminator is None:
				# Phase 4 site-1 patch 4b.1: lambda-block exit
				# (expression-returning form) migrated to
				# `M.CleanupHook` + `cleanup_authoring`.  Mirror of
				# patch 4a for `lower_function_body` fall-through:
				# scope drops run immediately before the synthetic
				# function's Return terminator emitted by the
				# driftc.py caller.
				lower._emit_scope_cleanup_hook(scope_index=len(lower._scope_stack) - 1)
			lower._pop_scope()
			return val
		# Void-returning lambda/callback: the tail expression is in
		# statement context (side effects only), exactly like a named
		# function body's final ExprStmt (`lower_function_body` discards
		# the result of a void fn's trailing expression).  Lowering it as
		# a *value* would route void-producing tails — a Void free-call
		# `f(x)` or a `match` with a Void/empty arm — through value-context
		# paths in `_visit_expr_HCall`/`_lower_match` that assert
		# ("Void-returning call used in expression context" /
		# "value-producing match arm must yield a value or terminate").
		# `lower_stmt` lowers the expression with `want_value=False`, which
		# both forms already handle, so the two ICEs disappear and behavior
		# matches the named-function tail (see the `disp(ev)` / inline-match
		# regressions in test_closure_void_tail_lowering.py).
		lower.lower_stmt(last)
		if lower.b.block.terminator is None:
			# Phase 4 site-1 patch 4b.1: lambda-block exit
			# (statement-terminated form, e.g. final HReturn or fall-
			# through with no value).  Same migration as above.
			lower._emit_scope_cleanup_hook(scope_index=len(lower._scope_stack) - 1)
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
			if self._lambda_is_callback and kind in (C.HCaptureKind.MOVE, C.HCaptureKind.SHARE):
				# Escaping callback lambdas own MOVE and SHARE captures in
				# the heap env storage — the env field is the canonical
				# owner and is dropped via the cb env-drop thunk.
				# Materializing a per-invocation local AND registering it
				# for cleanup would double-drop the same Arc / shared
				# resource (the body local would scope-exit-drop it AND
				# cb_drop would drop the env field).
				continue
			val = self._load_capture_from_env(slot)
			self.b.emit(M.StoreLocal(local=local_name, value=val))
			if local_ty is not None:
				# Move/share-captured values remain owned by the lambda
				# env object and are released by the env drop thunk;
				# registering an additional local drop here double-drops
				# non-Copy captures.  SHARE follows the same shape as
				# MOVE: the env field is the +1 owner.
				if kind not in (C.HCaptureKind.MOVE, C.HCaptureKind.SHARE):
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
		_kw_issues = call_kwargs_issues("method calls", getattr(expr, "kwargs", None), span=getattr(expr, "loc", None))
		if _kw_issues:
			raise AssertionError(f"{_kw_issues[0].message}")
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
		# Slice 3 DV→JSON: `<Error>.encode_compact()` returns the
		# canonical full-envelope JSON document.  Compiler-handled
		# (no real method body); the type-checker recognized this
		# call shape and tagged it with an intrinsic-method fn-id.
		# Lower the entire envelope assembly here.
		if expr.method_name == "encode_compact" and not expr.args:
			recv_ty = self._infer_expr_type(expr.receiver)
			if recv_ty is not None:
				recv_def = self._type_table.get(recv_ty)
				while recv_def.kind is TypeKind.REF and recv_def.param_types:
					recv_ty = recv_def.param_types[0]
					recv_def = self._type_table.get(recv_ty)
				if recv_def.kind is TypeKind.ERROR:
					return self._lower_error_encode_compact(expr.receiver)
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
		# Slice 7c-3 (ABI 14, 2026-05-06): the DV-intrinsic dispatch
		# guard is gone with `TypeKind.DIAGNOSTICVALUE`.  Method
		# names like `as_int` / `as_bool` / `get` resolve through
		# normal method dispatch (e.g. `core.JsonCursor.get(k)
		# .as_int()` from Slice 4A).
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
		# materialize-audit: allow non-owning
		# Every caller of this helper passes `self._int_type` (loop
		# counters for array intrinsic lowering); Int is non-droppable
		# so scope cleanup is moot.  If a future caller needs to use
		# this helper for a droppable type, route through
		# `_materialize_owned_temp` instead.
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
		# Snapshot both axes from one policy read — cheap-copy
		# classification combined with bitcopy shape.  This is a
		# pure policy-axis decision (no `copy_status is True AND
		# ...` escape hatch); routed through the funnel.
		elem_policy = self._drop_policy(elem_ty)
		if elem_policy.is_cheap_copy and not elem_policy.is_bitcopy:
			copied = self.b.new_temp()
			self.b.emit(M.CopyValue(dest=copied, value=raw, ty=elem_ty))
			return copied
		return raw

	def _array_elem_take_value(self, *, elem_ty: TypeId, array: M.ValueId, index: M.ValueId) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ArrayElemTake(dest=dest, elem_ty=elem_ty, array=array, index=index))
		self._local_types[dest] = elem_ty
		return dest

	# Slice 7c-2 (ABI 14, 2026-05-06): `_DV_RVALUE_RECV_HEXPRS`
	# allow-list and the surrounding ownership comment block
	# deleted along with `_dv_method_recv_is_rvalue` and the DV-
	# intrinsic dispatch path.

	def _lower_error_encode_compact(self, receiver: H.HExpr) -> M.ValueId:
		"""Build the canonical full-envelope JSON document for an
		`Error` value at the `<Error>.encode_compact()` call site.

		Slice 3 DV→JSON migration — produces a String of the form:

		    `{"event_code":<uint>,"event_fqn":"<escaped>","params":<params_json>,"context":<context_json>,"stack":null}`

		`params` and `context` are spliced as already-canonical JSON
		segments (NOT re-quoted as strings); `event_fqn` is JSON-
		escaped via `core._json_quote_string`; `stack` is the literal
		`null` (full backtrace serialization deferred).  No new
		runtime helpers — reuses `M.ErrorEvent` (event_code via
		extractvalue), `M.ErrorEventFqn` (event_fqn via extractvalue
		+ retain), `M.ExcGetParamsJson`, `M.ExcGetContextJson`, and
		`M.StringFromUint` for the numeric event_code.

		The receiver is an `Error` (or a `&Error` borrow that we
		auto-deref).  Caller is `_visit_expr_HMethodCall`'s
		`encode_compact` special-case.
		"""
		# Lower receiver, deref if it's &Error.
		recv_val = self.lower_expr(receiver)
		recv_ty = self._infer_expr_type(receiver)
		if recv_ty is not None:
			recv_def = self._type_table.get(recv_ty)
			if recv_def.kind is TypeKind.REF and recv_def.param_types:
				inner_ty = recv_def.param_types[0]
				loaded = self.b.new_temp()
				self.b.emit(M.LoadRef(dest=loaded, ptr=recv_val, inner_ty=inner_ty))
				recv_val = loaded

		# Resolve the JSON-escape helper (private; std.core).
		quote_fn = self._find_free_fn_id("std.core", "_json_quote_string")
		if quote_fn is None:
			raise AssertionError(
				"std.core:_json_quote_string not found in signatures table — "
				"Error.encode_compact requires the JSON-escape helper from "
				"stdlib/std/core/core.drift"
			)

		# Extract event_code (Uint64) and format as decimal.
		code_val = self.b.new_temp()
		self.b.emit(M.ErrorEvent(dest=code_val, error=recv_val))
		code_str = self.b.new_temp()
		self.b.emit(M.StringFromUint(dest=code_str, value=code_val))
		self._local_types[code_str] = self._string_type

		# Extract event_fqn (retained owned String) and JSON-escape.
		fqn_val = self.b.new_temp()
		self.b.emit(M.ErrorEventFqn(dest=fqn_val, error=recv_val))
		self._local_types[fqn_val] = self._string_type
		# Materialize fqn for `&String` borrow into _json_quote_string.
		# Use the canonical owned-temp helper so the slot's String
		# stake is auto-released at scope exit.
		fqn_local = self._materialize_owned_temp(
			name_prefix="__exc_fqn_",
			ty=self._string_type,
			value=fqn_val,
		)
		fqn_addr = self.b.new_temp()
		self.b.emit(M.AddrOfLocal(dest=fqn_addr, local=fqn_local, is_mut=False))
		fqn_quoted = self.b.new_temp()
		self.b.emit(M.Call(dest=fqn_quoted, fn_id=quote_fn, args=[fqn_addr], can_throw=False))
		self._local_types[fqn_quoted] = self._string_type

		# Get params_json + context_json (both retained owned Strings).
		params_str = self.b.new_temp()
		self.b.emit(M.ExcGetParamsJson(dest=params_str, error=recv_val))
		self._local_types[params_str] = self._string_type
		context_str = self.b.new_temp()
		self.b.emit(M.ExcGetContextJson(dest=context_str, error=recv_val))
		self._local_types[context_str] = self._string_type

		# Concat the envelope:
		#   `{"event_code":<code>,"event_fqn":<quoted_fqn>,"params":<params>,"context":<context>,"stack":null}`
		def _emit_lit(text: str) -> M.ValueId:
			tmp = self.b.new_temp()
			self.b.emit(M.ConstString(dest=tmp, value=text))
			return tmp

		def _concat(left: M.ValueId, right: M.ValueId) -> M.ValueId:
			tmp = self.b.new_temp()
			self.b.emit(M.StringConcat(dest=tmp, left=left, right=right))
			return tmp

		result = _emit_lit('{"event_code":')
		result = _concat(result, code_str)
		result = _concat(result, _emit_lit(',"event_fqn":'))
		result = _concat(result, fqn_quoted)
		result = _concat(result, _emit_lit(',"params":'))
		result = _concat(result, params_str)
		result = _concat(result, _emit_lit(',"context":'))
		result = _concat(result, context_str)
		result = _concat(result, _emit_lit(',"stack":null}'))
		self._local_types[result] = self._string_type
		return result

	def _build_throw_context_frame_json(
		self,
		*,
		frame_name: str,
		caps_sorted: list,
		json_helpers: dict,
	) -> M.ValueId:
		"""Slice 7b (2026-05-05): build a single context-frame JSON
		object for the current function's `^`-captured locals.

		Output shape:
		    `{"fn":"<frame_name>","locals":{<k1>:<v1>,...}}`

		Inner locals are lex-utf8 sorted by capture_name (caller passes
		`caps_sorted` already in that order).  Each captured local's
		value is projected to JSON text by direct dispatch to the
		appropriate `core.diagnostic_json_<scalar>` helper — no DV
		intermediate.  The set of supported capture types
		(Int/Uint/Bool/Float/String) is enforced by the checker.

		`json_helpers` is a `{"int","uint","bool","float","string"} →
		FunctionId` map pre-resolved by the caller.
		"""
		# Open: `{"fn":"<frame_name>","locals":{`
		# We embed frame_name as a JSON-quoted string at compile time
		# (frame names are FQNs of compiled functions — ASCII-clean
		# Drift identifiers separated by `.` and `:`; safe to bake as
		# a literal without runtime escape).
		open_lit = self.b.new_temp()
		open_text = '{"fn":"' + frame_name + '","locals":{'
		self.b.emit(M.ConstString(dest=open_lit, value=open_text))
		result = open_lit
		for i, cap in enumerate(caps_sorted):
			# Comma separator between locals (after the first).
			if i > 0:
				comma = self.b.new_temp()
				self.b.emit(M.ConstString(dest=comma, value=","))
				next_result = self.b.new_temp()
				self.b.emit(M.StringConcat(dest=next_result, left=result, right=comma))
				result = next_result
			# `"<key>":` literal — capture_name is user-supplied;
			# Drift identifiers + the optional `as "key"` syntax allow
			# a wider char set, but in v1 capture keys are restricted
			# to JSON-safe printable ASCII (no embedded quotes/escapes).
			# Bake the quoted key+colon as a compile-time literal.
			key_lit = self.b.new_temp()
			self.b.emit(M.ConstString(dest=key_lit, value='"' + cap.capture_name + '":'))
			next_result = self.b.new_temp()
			self.b.emit(M.StringConcat(dest=next_result, left=result, right=key_lit))
			result = next_result
			# Load the captured local and project to JSON text.
			val = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=val, local=cap.local_name))
			val_ty = self._local_types.get(cap.local_name) or self._binding_types.get(cap.binding_id)
			if val_ty is None:
				raise AssertionError("captured local type missing in MIR lowering (checker bug)")
			value_text = self._project_capture_to_json_text(
				value=val,
				value_ty=val_ty,
				json_helpers=json_helpers,
			)
			next_result = self.b.new_temp()
			self.b.emit(M.StringConcat(dest=next_result, left=result, right=value_text))
			result = next_result
		# Close: `}}`
		close_lit = self.b.new_temp()
		self.b.emit(M.ConstString(dest=close_lit, value="}}"))
		final = self.b.new_temp()
		self.b.emit(M.StringConcat(dest=final, left=result, right=close_lit))
		self._local_types[final] = self._string_type
		return final

	def _project_capture_to_json_text(
		self,
		*,
		value: M.ValueId,
		value_ty: TypeId,
		json_helpers: dict,
	) -> M.ValueId:
		"""Slice 7b: dispatch a captured-local scalar value to the
		matching `core.diagnostic_json_<scalar>` helper.  Returns a MIR
		value of String type holding the JSON text.

		String values pass via `&String` (AddrOfLocal); other scalars
		pass by value.  Capture types are restricted to scalars by the
		checker; reaching this helper with a non-scalar is a checker /
		lowering boundary mismatch.
		"""
		json_text = self.b.new_temp()
		self._local_types[json_text] = self._string_type
		if value_ty == self._int_type:
			self.b.emit(M.Call(dest=json_text, fn_id=json_helpers["int"], args=[value], can_throw=False))
			return json_text
		if value_ty == self._uint_type:
			self.b.emit(M.Call(dest=json_text, fn_id=json_helpers["uint"], args=[value], can_throw=False))
			return json_text
		if value_ty == self._bool_type:
			self.b.emit(M.Call(dest=json_text, fn_id=json_helpers["bool"], args=[value], can_throw=False))
			return json_text
		if value_ty == self._float_type:
			self.b.emit(M.Call(dest=json_text, fn_id=json_helpers["float"], args=[value], can_throw=False))
			return json_text
		if value_ty == self._string_type:
			# diagnostic_json_string takes &String — materialize a
			# named local and pass its address.  We must explicitly
			# drop the local after the call: throw-site unwind bypasses
			# normal scope-drop, so the LoadLocal-staked refcount on
			# the captured local would otherwise leak.
			# materialize-audit: allow consumed
			# Inline DropValue ~10 lines below handles release at the
			# throw-bypass site; scope cleanup is intentionally
			# bypassed here (would not fire on the unwind path anyway).
			str_local = f"__throw_ctx_str_{self.b.new_temp()}"
			self.b.ensure_local(str_local)
			self._local_types[str_local] = self._string_type
			self.b.emit(M.StoreLocal(local=str_local, value=value))
			str_ref = self.b.new_temp()
			self._local_types[str_ref] = self._type_table.ensure_ref(self._string_type)
			self.b.emit(M.AddrOfLocal(dest=str_ref, local=str_local, is_mut=False))
			self.b.emit(M.Call(dest=json_text, fn_id=json_helpers["string"], args=[str_ref], can_throw=False))
			str_for_drop = self.b.new_temp()
			self._local_types[str_for_drop] = self._string_type
			self.b.emit(M.LoadLocal(dest=str_for_drop, local=str_local))
			self.b.emit(M.DropValue(value=str_for_drop, ty=self._string_type))
			return json_text
		raise AssertionError(
			"captured local type must be a supported scalar "
			"(Int/Uint/Bool/Float/String) — checker should have rejected"
		)

	def _lower_typed_catch_field_proj(
		self,
		*,
		envelope: M.ValueId,
		event_fqn: str,
		field_name: str,
	) -> M.ValueId:
		"""Slice 5 (slice 3 of impl): lower a typed catch binder field
		access (`e.<declared_field>`) as a compiler-owned typed
		projection from the Error envelope's params JSON.

		**Invariant** (K, 2026-05-04): this helper is only called by
		the HField branch when the type-checker has set
		`expr.typed_proj_event_fqn`, which only happens AFTER the
		checker validated that the binder is a typed catch binder,
		the field is declared on the matched event's schema, AND the
		declared field type is one of the supported scalars
		(Int/Uint/Bool/Float/String).  Every path below that would
		otherwise return None is therefore a checker/lowering
		boundary mismatch and aborts compilation — there is NO
		fall-through to default field lowering, since that route
		would emit garbage at runtime for an annotated typed
		projection.  Non-scalar field types are rejected at the
		checker via `E_TYPED_CATCH_FIELD_UNSUPPORTED_TYPE` before
		annotation; aliased binders are rejected via
		`E_UNKNOWN_FIELD_ON_ERROR`.

		Steps:
		  1. Look up the field's declared type from the Path-A
		     co-registered struct.
		  2. Pick the appropriate `core._typed_params_field_<scalar>`
		     helper.
		  3. Extract `params_json` from the envelope via
		     `M.ExcGetParamsJson` (returns retained DriftString).
		  4. Materialize the params text + key into locals so we can
		     borrow them as `&String` for the helper call.
		  5. Emit `M.Call`.

		Behavior on missing-key / wrong-type / malformed JSON is
		deterministic (default value for the scalar type) — see the
		helpers in `stdlib/std/core/core.drift`.
		"""
		# 1. Look up the field's declared type from the Path-A co-
		# registered struct (slice 1 / Path A).
		if ":" not in event_fqn:
			raise AssertionError(
				f"typed projection event_fqn {event_fqn!r} is not in canonical "
				f"`module:Name` shape — checker invariant violation"
			)
		mod_id, type_name = event_fqn.split(":", 1)
		struct_id = self._type_table.get_nominal(
			kind=TypeKind.STRUCT,
			module_id=mod_id,
			name=type_name,
		)
		if struct_id is None:
			raise AssertionError(
				f"typed projection {event_fqn!r}: Path-A co-registered struct "
				f"missing from type table — Path-A invariant violation"
			)
		field_info = self._type_table.struct_field(struct_id, field_name)
		if field_info is None:
			raise AssertionError(
				f"typed projection {event_fqn}.{field_name}: field absent from "
				f"Path-A struct schema — checker invariant violation"
			)
		_field_idx, field_ty = field_info

		# 2. Pick the helper based on the declared field type.
		#    Slice 3 first-pass scope: scalars only.  The checker
		#    rejects non-scalar field types upstream with
		#    E_TYPED_CATCH_FIELD_UNSUPPORTED_TYPE, so reaching here
		#    with a non-scalar type is a boundary mismatch.
		int_ty = self._type_table.ensure_int()
		uint_ty = self._type_table.ensure_uint()
		bool_ty = self._type_table.ensure_bool()
		float_ty = self._type_table.ensure_float()
		string_ty = self._type_table.ensure_string()
		if field_ty == int_ty:
			helper_name = "_typed_params_field_int"
		elif field_ty == uint_ty:
			helper_name = "_typed_params_field_uint"
		elif field_ty == bool_ty:
			helper_name = "_typed_params_field_bool"
		elif field_ty == float_ty:
			helper_name = "_typed_params_field_float"
		elif field_ty == string_ty:
			helper_name = "_typed_params_field_string"
		else:
			raise AssertionError(
				f"typed projection {event_fqn}.{field_name}: declared field type "
				f"is not one of the supported scalars (Int/Uint/Bool/Float/String) — "
				f"checker should have rejected with E_TYPED_CATCH_FIELD_UNSUPPORTED_TYPE"
			)
		helper_fn_id = self._find_free_fn_id("std.core", helper_name)
		if helper_fn_id is None:
			raise AssertionError(
				f"std.core function {helper_name!r} not loaded — typed catch "
				f"projection requires std.core in the build (the helper is "
				f"compiler-emitted; stage2 unit tests must pre-stage signatures "
				f"if they reach this lowering path)"
			)

		# 3. Extract params_json text from the envelope.
		json_text = self.b.new_temp()
		self.b.emit(M.ExcGetParamsJson(dest=json_text, error=envelope))
		self._local_types[json_text] = self._string_type

		# 4. Materialize params text into a local for `&String` borrow.
		json_local = f"__typed_proj_json_{json_text}"
		self.b.ensure_local(json_local)
		self.b.emit(M.StoreLocal(local=json_local, value=json_text))
		json_addr = self.b.new_temp()
		self.b.emit(M.AddrOfLocal(dest=json_addr, local=json_local, is_mut=False))

		# 5. Build key string + materialize for `&String` borrow.
		key_text = self.b.new_temp()
		self.b.emit(M.ConstString(dest=key_text, value=field_name))
		self._local_types[key_text] = self._string_type
		key_local = f"__typed_proj_key_{key_text}"
		self.b.ensure_local(key_local)
		self.b.emit(M.StoreLocal(local=key_local, value=key_text))
		key_addr = self.b.new_temp()
		self.b.emit(M.AddrOfLocal(dest=key_addr, local=key_local, is_mut=False))

		# 6. Call helper.  Helpers are nothrow.
		result = self.b.new_temp()
		self.b.emit(M.Call(
			dest=result,
			fn_id=helper_fn_id,
			args=[json_addr, key_addr],
			can_throw=False,
		))
		self._local_types[result] = field_ty
		return result

	def _get_or_materialize_typed_catch_storage(
		self,
		*,
		binding_id: int,
		binder_local: str,
		event_fqn: str,
	) -> tuple[str, "TypeId"]:
		"""Return (storage_local_name, struct_type_id) for a typed
		catch binder, materializing the storage local lazily on
		first use.

		The storage is a parallel struct local of the `pub error`'s
		Path-A co-registered struct type.  Each declared scalar
		field is decoded once (via `_typed_params_field_<scalar>`
		helpers) and stored in the corresponding struct slot at
		materialization time.  Subsequent field reads / borrows
		project from the storage rather than re-decoding from the
		envelope JSON.

		Allocation strategy: lazy on first projection through this
		helper.  The storage local is registered for normal scope
		cleanup at materialization time, so its lifetime matches
		the catch arm scope -- no envelope-owned double-drop (the
		envelope and the storage own independent +1s of each
		String field; both drop independently).

		Returns the existing entry if already materialized for this
		binding_id (binding_ids are globally unique per fn).
		"""
		existing = self._typed_catch_binder_storage.get(binding_id)
		if existing is not None:
			return existing

		if ":" not in event_fqn:
			raise AssertionError(
				f"typed catch binder event_fqn {event_fqn!r} is not in "
				f"canonical `module:Name` shape (checker invariant violation)"
			)
		mod_id, type_name = event_fqn.split(":", 1)
		struct_id = self._type_table.get_nominal(
			kind=TypeKind.STRUCT,
			module_id=mod_id,
			name=type_name,
		)
		if struct_id is None:
			raise AssertionError(
				f"typed catch binder {event_fqn!r}: Path-A co-registered "
				f"struct missing from type table (Path-A invariant violation)"
			)

		struct_inst = self._type_table.get_struct_instance(struct_id)
		if struct_inst is None or not struct_inst.field_types:
			raise AssertionError(
				f"typed catch binder {event_fqn!r}: struct instance has no "
				f"field types"
			)
		field_types = list(struct_inst.field_types)
		field_names = list(struct_inst.field_names or [])
		if len(field_names) != len(field_types):
			raise AssertionError(
				f"typed catch binder {event_fqn!r}: field-name/type arity "
				f"mismatch ({len(field_names)} vs {len(field_types)})"
			)

		# Allocate the storage local typed as the struct so AddrOfField
		# projections downstream see a STRUCT base.
		storage_local = f"__typed_catch_view_{binder_local}_{self.b.new_temp()}"
		self.b.ensure_local(storage_local)
		self._local_types[storage_local] = struct_id

		# Decode each declared field from the envelope.  Scalar fields
		# go through the same `_typed_params_field_<scalar>` helpers
		# the legacy per-access path uses.  Non-scalar fields are
		# UNREACHABLE here: the type-checker rejects any borrow on a
		# typed catch binder whose schema contains a non-scalar field
		# with `E_TYPED_CATCH_BORROW_MIXED_SCHEMA` BEFORE
		# materialization runs.  Reaching the non-scalar branch below
		# would mean a checker-rule regression -- we hard-assert
		# rather than silently zero-init, because zero-initializing a
		# non-scalar slot and then registering the whole struct for
		# scope drop would run that field type's destructor on
		# garbage memory at scope exit (silent UB).  See K-review
		# 2026-05-17 HIGH: an earlier draft of this helper used
		# `ZeroValue` here, and the resulting drop-of-garbage was
		# exactly the soundness hole the assertion now prevents.
		int_ty = self._type_table.ensure_int()
		uint_ty = self._type_table.ensure_uint()
		bool_ty = self._type_table.ensure_bool()
		float_ty = self._type_table.ensure_float()
		string_ty = self._type_table.ensure_string()
		scalar_to_helper = {
			int_ty: "_typed_params_field_int",
			uint_ty: "_typed_params_field_uint",
			bool_ty: "_typed_params_field_bool",
			float_ty: "_typed_params_field_float",
			string_ty: "_typed_params_field_string",
		}

		# Load envelope from binder local; extract params_json once
		# for the whole field-decode batch.
		envelope_val = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=envelope_val, local=binder_local))
		self._local_types[envelope_val] = self._type_table.ensure_error()
		json_text = self.b.new_temp()
		self.b.emit(M.ExcGetParamsJson(dest=json_text, error=envelope_val))
		self._local_types[json_text] = self._string_type
		json_local = f"__typed_catch_view_json_{self.b.new_temp()}"
		self.b.ensure_local(json_local)
		self.b.emit(M.StoreLocal(local=json_local, value=json_text))
		json_addr = self.b.new_temp()
		self.b.emit(M.AddrOfLocal(dest=json_addr, local=json_local, is_mut=False))

		field_values: list[M.ValueId] = []
		for f_name, f_ty in zip(field_names, field_types):
			helper_name = scalar_to_helper.get(f_ty)
			if helper_name is None:
				# Defense in depth: the type-checker rejects typed-catch
				# borrow on schemas with any non-scalar field via
				# E_TYPED_CATCH_BORROW_MIXED_SCHEMA before this
				# materialization runs.  Reaching here means a checker
				# bug -- silently zero-initializing the slot and then
				# registering the whole struct for scope drop would
				# invoke the field's destructor on garbage memory (UB).
				# Refuse to emit unsafe IR; surface as compiler bug.
				raise AssertionError(
					f"typed catch binder {event_fqn!r}: schema field "
					f"{f_name!r} has non-scalar type {f_ty} but reached "
					f"borrow-path storage materialization.  Checker should "
					f"have rejected with E_TYPED_CATCH_BORROW_MIXED_SCHEMA."
				)
			helper_fn_id = self._find_free_fn_id("std.core", helper_name)
			if helper_fn_id is None:
				raise AssertionError(
					f"std.core function {helper_name!r} not loaded -- typed "
					f"catch projection requires std.core in the build"
				)
			key_text = self.b.new_temp()
			self.b.emit(M.ConstString(dest=key_text, value=f_name))
			self._local_types[key_text] = self._string_type
			key_local = f"__typed_catch_view_key_{self.b.new_temp()}"
			self.b.ensure_local(key_local)
			self.b.emit(M.StoreLocal(local=key_local, value=key_text))
			key_addr = self.b.new_temp()
			self.b.emit(M.AddrOfLocal(dest=key_addr, local=key_local, is_mut=False))
			result = self.b.new_temp()
			self.b.emit(M.Call(
				dest=result,
				fn_id=helper_fn_id,
				args=[json_addr, key_addr],
				can_throw=False,
			))
			self._local_types[result] = f_ty
			field_values.append(result)

		struct_val = self.b.new_temp()
		self.b.emit(M.ConstructStruct(dest=struct_val, struct_ty=struct_id, args=field_values))
		self._local_types[struct_val] = struct_id
		self.b.emit(M.StoreLocal(local=storage_local, value=struct_val))
		# Register for normal scope cleanup at catch-arm exit.  Fields
		# are independently owned (Strings carry their own +1 from the
		# decode helper); dropping storage releases them without
		# touching envelope-owned copies.
		self._register_drop_local(storage_local, struct_id)

		self._typed_catch_binder_storage[binding_id] = (storage_local, struct_id)
		return (storage_local, struct_id)


	def _lower_synthesized_error_params_view(self, recv: H.HExpr, view_ty: TypeId) -> M.ValueId:
		"""Build an `ErrorParamsView` value from a synthesized
		`<error>.params` receiver, returning the new MIR ValueId.

		For the HField shape, `_visit_expr_HField` already special-
		cases `params` on Error.  For the HPlaceExpr shape we
		construct the equivalent here by lowering the parent place
		(everything except the trailing HPlaceField("params")) into
		an Error value, then emitting ExcGetParamsJson + ConstructStruct.
		"""
		if isinstance(recv, H.HField) and recv.name == "params":
			return self.lower_expr(recv)
		HPlaceExpr = getattr(H, "HPlaceExpr", None)
		if HPlaceExpr is None or not isinstance(recv, HPlaceExpr):
			raise AssertionError("_lower_synthesized_error_params_view: unexpected receiver shape")
		if not recv.projections or not isinstance(recv.projections[-1], H.HPlaceField) or recv.projections[-1].name != "params":
			raise AssertionError("_lower_synthesized_error_params_view: receiver must end in HPlaceField('params')")
		# Lower the parent: HPlaceExpr with all but the last projection.
		# When projections is just [HPlaceField("params")], the parent is
		# the bare HVar `recv.base` — lower it directly.
		if len(recv.projections) == 1:
			err_val = self.lower_expr(recv.base)
		else:
			parent = HPlaceExpr(base=recv.base, projections=recv.projections[:-1], loc=getattr(recv, "loc", Span()))
			# Parent HPlaceExpr value-lowering is unsupported in v1
			# (no `_visit_expr_HPlaceExpr`).  In Slice 1 the only
			# observed shape is `e.params` directly off a catch
			# binder (HVar), so a deeper place chain hitting this
			# branch indicates a use case beyond Slice 1's scope.
			raise NotImplementedError(
				"`<error>.params` through a multi-step place chain is not "
				"supported in Slice 1; use `e.params` directly off a catch "
				"binder."
			)
		# Unwrap reference if `err_val` is &Error.
		err_ty = self._infer_expr_type(recv.base) if len(recv.projections) == 1 else None
		if err_ty is not None:
			err_def = self._type_table.get(err_ty)
			if err_def.kind is TypeKind.REF and err_def.param_types:
				inner_ty = err_def.param_types[0]
				loaded = self.b.new_temp()
				self.b.emit(M.LoadRef(dest=loaded, ptr=err_val, inner_ty=inner_ty))
				err_val = loaded
		json_dest = self.b.new_temp()
		self.b.emit(M.ExcGetParamsJson(dest=json_dest, error=err_val))
		self._local_types[json_dest] = self._string_type
		view_dest = self.b.new_temp()
		self.b.emit(M.ConstructStruct(dest=view_dest, struct_ty=view_ty, args=[json_dest]))
		self._local_types[view_dest] = view_ty
		return view_dest

	def _is_synthesized_error_params_place(self, recv: H.HExpr) -> bool:
		"""Detect `<error>.params` as a method-call receiver.

		The expression has no real backing field on Error — it's
		synthesized at HField/HPlaceField lowering into an
		`ErrorParamsView` value via `ExcGetParamsJson + ConstructStruct`.
		Method calls on it (currently just `encode_compact()`) cannot
		auto-borrow through `_lower_addr_of_place` because the
		"projection" doesn't correspond to a real struct address.
		Caller materializes the value to a named local and takes
		`AddrOfLocal` for the `&ErrorParamsView` self argument.

		Recognizes both shapes the parser may produce:
		  - `H.HField(subject=..., name="params")`
		  - `H.HPlaceExpr(base=..., projections=[..., HPlaceField("params")])`
		Subject must type to `TypeKind.ERROR` (after ref-unwrap).
		"""
		# HField shape.
		if isinstance(recv, H.HField) and recv.name == "params":
			subj_ty = self._infer_expr_type(recv.subject)
			if subj_ty is None:
				return False
			td = self._type_table.get(subj_ty)
			if td.kind is TypeKind.REF and td.param_types:
				td = self._type_table.get(td.param_types[0])
			return td.kind is TypeKind.ERROR
		# HPlaceExpr shape — last projection must be HPlaceField("params")
		# and the type up to (but excluding) that projection must be Error.
		HPlaceExpr = getattr(H, "HPlaceExpr", None)
		if HPlaceExpr is not None and isinstance(recv, HPlaceExpr):
			if not recv.projections:
				return False
			last_proj = recv.projections[-1]
			if not isinstance(last_proj, H.HPlaceField) or last_proj.name != "params":
				return False
			# Walk projections (excluding the last) to find the parent type.
			cur_ty = self._infer_expr_type(recv.base)
			if cur_ty is None:
				return False
			for proj in recv.projections[:-1]:
				if isinstance(proj, H.HPlaceDeref):
					td = self._type_table.get(cur_ty)
					if td.kind is not TypeKind.REF or not td.param_types:
						return False
					cur_ty = td.param_types[0]
				elif isinstance(proj, H.HPlaceField):
					info = self._type_table.struct_field(cur_ty, proj.name)
					if info is None:
						return False
					_, cur_ty = info
				elif isinstance(proj, H.HPlaceIndex):
					td = self._type_table.get(cur_ty)
					if td.kind is not TypeKind.ARRAY or not td.param_types:
						return False
					cur_ty = td.param_types[0]
			td = self._type_table.get(cur_ty)
			if td.kind is TypeKind.REF and td.param_types:
				td = self._type_table.get(td.param_types[0])
			return td.kind is TypeKind.ERROR
		return False

	# Slice 7c-2 (ABI 14, 2026-05-06): `_dv_method_recv_is_rvalue`
	# helper deleted along with the DV-intrinsic dispatch path that
	# was its only caller.

	def _call_arg_yields_owned_temp(self, arg: H.HExpr, param_ty: TypeId | None) -> bool:
		"""Mirror of `_lower_call_arg`'s ownership decision for use by
		`_lower_array_intrinsic_method` at push/insert/set sites.

		Returns True if `_lower_call_arg(arg, param_ty)` yields a MIR
		value that OWNS its inner refcounted storage, False if it yields
		a borrowed/shared view whose storage is still owned by a source
		local.

		The rules mirror `_lower_call_arg` exactly:
		- HVar / projection-free HPlaceExpr with a MOVE-classified type
		  → MoveOut → owned temp.
		- HVar / projection-free HPlaceExpr with a COPY-classified type
		  → plain load (borrowed view sharing refcount with the local).
		- HPlaceExpr with projections → `lower_expr` deep-copy → owned.
		- Anything else (HCall, HMethodCall, …) → `lower_expr`
		  on an rvalue expression → owned temp.

		This distinction matters for `_ensure_array_elem_copy`:
		`drop_source=True` is only safe when `val` is an OWNED temp,
		because the paired `DropValue` releases the inner refcounted
		storage.  Releasing a shared view would decrement the refcount
		out from under the source local (observed as heap-use-after-free
		in `Array<String>.push(name)` where `name` is a local String
		reporter: drift-net-tls v0.3.14 certification).
		"""
		is_place = isinstance(arg, H.HVar) or (hasattr(H, "HPlaceExpr") and isinstance(arg, getattr(H, "HPlaceExpr")))
		if not is_place:
			return True  # rvalue expressions lower to owned temps.
		base = arg
		if hasattr(H, "HPlaceExpr") and isinstance(arg, getattr(H, "HPlaceExpr")):
			if getattr(arg, "projections", None):
				return True  # projected reads lower through lower_expr.
			base = arg.base
		if not isinstance(base, H.HVar):
			return True
		arg_ty = self._infer_expr_type(base)
		if param_ty is not None and not self._should_copy_value(param_ty):
			arg_ty = param_ty
		if arg_ty is None:
			return True
		# MOVE-classified → MoveOut path → owned.
		# COPY-classified → fall-through to lower_expr, which for HVar
		# produces a borrowed view.
		return not self._should_copy_value(arg_ty)

	def _ensure_array_elem_copy(self, val: M.ValueId, elem_ty: TypeId, *, drop_source: bool) -> M.ValueId:
		"""Wrap *val* in CopyValue when the element type is Copy but non-bitcopy.

		Array store MIR instructions (ArrayElemInit*, ArrayIndexStore) require
		that Copy non-bitcopy values are explicitly copied so the runtime can
		perform the correct retain/refcount operation.  Bitcopy types and
		non-Copy types do not need this wrapping.

		Source-temp release (K28-aftermath Leak B):
		`drop_source=True` means the caller guarantees `val` is an OWNED
		MIR temp — either a direct rvalue owned by this function or a
		semantically-copied view whose inner refcounted storage is
		independent of the caller's original owner.  `CopyValue`
		deep-clones inner refcounted storage (String retain, DV clone)
		into the array's element copy, leaving `val`'s inner storage
		untouched; without an explicit drop, those inner refs leak
		(observed for `Array<DiagnosticEntry>` with heap-allocated keys).
		A paired `DropValue(value=val)` is emitted so the source temp's
		owned substructure is released and the returned clone is the
		lone owner of the element storage.

		`drop_source=False` is REQUIRED when `val` is a borrowed / shared
		view — typically a plain load from a COPY-classified local
		(e.g. `Array<String>.push(local_string)` where `_lower_call_arg`
		falls through to `lower_expr(HVar)` because String is
		classified as "copy").  Releasing a shared view would decrement
		the source local's refcount, leaving the array slot and the
		local both pointing to data that's freed on the next store into
		the local (drift-net-tls v0.3.14 certification UAF reporter).
		Callers MUST determine ownership via `_call_arg_yields_owned_temp`
		(or equivalent reasoning) and pass the correct value.

		`extend(&src)`'s element is an owned temp despite the MIR shape
		being `ArrayIndexLoad`: `_lower_array_index_load`
		(llvm_codegen.py) calls `_emit_copy_value` on the loaded element
		so the MIR-level `elem` is already an independent owned temp
		with its own retain (for String) or its own deep-cloned fields
		(for struct/DV elements).  See
		`array_extend_borrowed_source_string_no_uaf`.

		PHASE 1 RESIDUAL (Copy-but-non-bitcopy).  Same predicate shape
		as the sibling residual in the `ArrayLit` element loop —
		"Copy trait decided True AND bits not self-contained."  No
		current `DropPolicy` axis expresses it; Phase 2 either adds
		the axis or retires the branch.  Enumerate with
		`rg "PHASE 1 RESIDUAL" lang/driftc/stage2/hir_to_mir.py`.
		"""
		copy_status = self._type_table.copy_status(elem_ty)
		if copy_status is True and not self._type_table.is_bitcopy(elem_ty):
			copy_dest = self.b.new_temp()
			self.b.emit(M.CopyValue(dest=copy_dest, value=val, ty=elem_ty))
			self._local_types[copy_dest] = elem_ty
			if drop_source:
				# K28-aftermath Leak B: release owned source-temp inner refs
				# after clone.  `drop_source=True` means the caller has
				# proven `val` is owned — see the docstring and
				# `_call_arg_yields_owned_temp` for the classification
				# rules.  Passing `drop_source=True` when `val` is a
				# borrowed view (COPY-classified HVar load) will UAF the
				# source local; see
				# `array_push_copy_local_string_no_uaf` for that shape.
				self.b.emit(M.DropValue(value=val, ty=elem_ty))
			return copy_dest
		return val

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
			"extend",
			"truncate",
			"remove_range",
			"len",
			"cap",
			"capacity",
			"gen",
		):
			return False, None

		recv_ty = self._infer_expr_type(expr.receiver)
		if recv_ty is None:
			raise AssertionError("array method receiver type unknown in MIR lowering (checker bug)")
		recv_def = self._type_table.get(recv_ty)
		array_ty = recv_ty
		# Resolve through one REF hop without emitting any MIR.  The
		# receiver type can be `&Array<T>` when the typechecker autoborrows
		# a value-context receiver; we still need to inspect the underlying
		# `Array<T>` here to decide whether this method actually targets an
		# array intrinsic.
		if recv_def.kind is TypeKind.REF and recv_def.param_types:
			array_ty = recv_def.param_types[0]
		# Pre-emission kind guard: the intrinsic-name set above includes
		# names that also exist as user-defined methods on non-Array types
		# (e.g. `FileBuilder::truncate`).  Any MIR emitted before this guard
		# would be left dead in the function body when we bail out — for
		# chained owned-builder receivers (`builder().truncate(...)`) that
		# means the entire receiver chain gets lowered twice, leaking each
		# allocation in the abandoned prefix.  Determine eligibility from
		# the type alone, then lower the receiver only when committed.
		array_def = self._type_table.get(array_ty)
		if array_def.kind is not TypeKind.ARRAY or not array_def.param_types:
			return False, None
		recv_ptr: M.ValueId | None = None
		# Read-only intrinsic methods: do not take an exclusive borrow
		# of the receiver place.  `len`/`cap`/`capacity`/`gen` are
		# field-access magic in the type checker and produce Int with
		# no array mutation; lowering them with `is_mut=True` would
		# reject `val arr` receivers and conflict with any outstanding
		# shared borrow.  Matches the field-access form (`arr.len`)
		# which never takes an exclusive borrow.
		recv_is_mut = name not in ("get", "range", "len", "cap", "capacity", "gen")
		if name == "range_mut":
			recv_is_mut = True
		if name == "range":
			recv_is_mut = False
		if recv_def.kind is TypeKind.REF and recv_def.param_types:
			recv_ptr = self.lower_expr(expr.receiver)
		else:
			place_expr = place_expr_from_lvalue_expr(expr.receiver)
			if place_expr is None:
				raise NotImplementedError("Array method requires an lvalue receiver in v1")
			recv_ptr, _inner = self._lower_addr_of_place(
				place_expr,
				is_mut=recv_is_mut,
			)
		if recv_ptr is None:
			raise AssertionError("array method missing receiver address (checker bug)")
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

		# Bug 1 fix: method-call form of the Array length / capacity /
		# generation accessors lowers identically to the field-access
		# form (see the `expr.name in ("len","cap","capacity","gen")`
		# field-lowering branch above in this file).  All four return
		# Int and read-only.  The checker side at
		# `call_resolver.py` (the `len/cap/capacity/gen` arm just
		# above the `push/pop/...` arm) already typed this as Int.
		if name == "len":
			if not want_value:
				return True, None
			dest = self.b.new_temp()
			self.b.emit(M.ArrayLen(dest=dest, array=array_val))
			return True, dest
		if name in ("cap", "capacity"):
			if not want_value:
				return True, None
			dest = self.b.new_temp()
			self.b.emit(M.ArrayCap(dest=dest, array=array_val))
			return True, dest
		if name == "gen":
			if not want_value:
				return True, None
			dest = self.b.new_temp()
			self.b.emit(M.ArrayGen(dest=dest, array=array_val))
			return True, dest

		if name == "get":
			if not want_value:
				return True, None
			_arr_issues = array_method_arity_issues("get", len(expr.args), span=getattr(expr, "loc", None))
			if _arr_issues:
				raise AssertionError(_arr_issues[0].message)
			idx_val = self.lower_expr(expr.args[0], expected_type=self._int_type)
			len_val = self.b.new_temp()
			self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
			zero = self._const_int(0)

			opt_ty = self._optional_variant_type(self._type_table.ensure_ref(elem_ty))
			# Ownership of the Optional<&T> payload transfers outward
			# through the SSA value produced by `LoadLocal` at the
			# join below; the staging local must NOT be registered
			# for scope-exit drop, or its payload double-drops (one
			# drop here, one through whatever consumes the loaded
			# value).  Conditional stores across negative / in-range /
			# out-of-range branches mean the helper's all-in-one
			# alloc+store doesn't fit; bare `ensure_local + StoreLocal`
			# is the intentional shape.
			# materialize-audit: allow result-staging Optional<&T> loaded out via LoadLocal at the join
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
			_arr_issues = array_method_arity_issues("ref_at", len(expr.args), span=getattr(expr, "loc", None))
			if _arr_issues:
				raise AssertionError(_arr_issues[0].message)
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
			_arr_issues = array_method_arity_issues("pop", len(expr.args), span=getattr(expr, "loc", None))
			if _arr_issues:
				raise AssertionError(_arr_issues[0].message)
			if not want_value:
				return True, None
			opt_ty = self._optional_variant_type(elem_ty)
			# Same shape as `__array_get_res` above: conditional
			# stores across empty / non-empty branches, final
			# `LoadLocal` at the join transfers the popped Optional<T>
			# payload outward via the SSA value.  Registering the
			# staging local for drop was the 2026-05-16 array_pop /
			# std_regex_* memcheck regression -- caused a double-free
			# (scope cleanup AND the consumer both freed the popped
			# element's inner refcounted content).  Bare ensure_local
			# + StoreLocal is intentional; do not register.
			# materialize-audit: allow result-staging Optional<T> loaded out via LoadLocal at the join
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
			# materialize-audit: allow registered explicit
			# _register_drop_local call below
			# arr_local holds the (possibly grown) Array<T>; stored
			# conditionally across ok/grow branches, then loaded after
			# the join.  Hand-roll with explicit register so the slot
			# participates in scope cleanup.
			arr_local = f"__array_cap_arr{self.b.new_temp()}"
			self.b.ensure_local(arr_local)
			self._local_types[arr_local] = array_ty
			self._register_drop_local(arr_local, array_ty)
			# materialize-audit: allow non-owning Bool grew-flag local
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
			_arr_issues = array_method_arity_issues(name, len(expr.args), span=getattr(expr, "loc", None))
			if _arr_issues:
				raise AssertionError(_arr_issues[0].message)
			val_arg = expr.args[-1]
			val_owned = self._call_arg_yields_owned_temp(val_arg, elem_ty)
			val = self._lower_call_arg(val_arg, elem_ty)
			# drop_source is only safe when `_lower_call_arg` yields an OWNED
			# temp (MoveOut for move-classified HVar, or lower_expr for
			# rvalues).  For COPY-classified HVar (e.g. `Array<String>.push(local)`)
			# `_lower_call_arg` falls through to a plain `lower_expr` load —
			# a borrowed view — and dropping it would decrement the source
			# local's refcount, causing UAF at the next store into that
			# local.  See drift-net-tls v0.3.14 certification regression.
			val = self._ensure_array_elem_copy(val, elem_ty, drop_source=val_owned)
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
			_arr_issues = array_method_arity_issues(name, len(expr.args), span=getattr(expr, "loc", None))
			if _arr_issues:
				raise AssertionError(_arr_issues[0].message)
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
			_arr_issues = array_method_arity_issues("swap", len(expr.args), span=getattr(expr, "loc", None))
			if _arr_issues:
				raise AssertionError(_arr_issues[0].message)
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
			_arr_issues = array_method_arity_issues("set", len(expr.args), span=getattr(expr, "loc", None))
			if _arr_issues:
				raise AssertionError(_arr_issues[0].message)
			idx_val = self.lower_expr(expr.args[0], expected_type=self._int_type)
			# Same ownership reasoning AND the same lowering machinery
			# as push/insert: route the value through `_lower_call_arg`
			# (not raw `lower_expr`) so HVar / projection / rvalue all
			# end up at the SSA ownership state that
			# `_call_arg_yields_owned_temp` predicts.  Pre-fix `set`
			# called `lower_expr` directly, which produced a different
			# ownership state for move-classified HVars — combined with
			# `drop_source=True` from the helper, that double-released
			# the source local on scope drop (UAF in
			# om_array_set_diag_entry hvar_local scenario).
			val_owned = self._call_arg_yields_owned_temp(expr.args[1], elem_ty)
			val = self._lower_call_arg(expr.args[1], elem_ty)
			val = self._ensure_array_elem_copy(val, elem_ty, drop_source=val_owned)
			self.b.emit(M.ArrayIndexStore(elem_ty=elem_ty, array=array_val, index=idx_val, value=val))
			return True, None

		if name in ("clear", "reserve", "shrink_to_fit"):
			_arr_issues = array_method_arity_issues(name, len(expr.args), span=getattr(expr, "loc", None))
			if _arr_issues:
				raise AssertionError(_arr_issues[0].message)
			if name == "clear":
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
				# reserve(n) = ensure total capacity >= n (no-op if cap already >= n)
				requested_cap = self.lower_expr(expr.args[0], expected_type=self._int_type)
				cap_val = self.b.new_temp()
				self.b.emit(M.ArrayCap(dest=cap_val, array=array_val))
				already_ok = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=already_ok, op=H.BinaryOp.LE, left=requested_cap, right=cap_val))
				skip_block = self.b.new_block("array_reserve_skip")
				do_block = self.b.new_block("array_reserve_do")
				join_block = self.b.new_block("array_reserve_join")
				self.b.set_terminator(M.IfTerminator(cond=already_ok, then_target=skip_block.name, else_target=do_block.name))

				self.b.set_block(skip_block)
				self.b.set_terminator(M.Goto(target=join_block.name))

				self.b.set_block(do_block)
				len_val = self.b.new_temp()
				self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
				next_gen = _next_gen(array_val)
				extra = self.b.new_temp()
				self.b.emit(M.BinaryOpInstr(dest=extra, op=H.BinaryOp.SUB, left=requested_cap, right=len_val))
				new_arr, grew = ensure_capacity(array_val, len_val=len_val, cap_val=cap_val, extra=extra)
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

		if name == "extend":
			# Copy-only bulk append: extend(src: &Array<T>)
			_arr_issues = array_method_arity_issues("extend", len(expr.args), span=getattr(expr, "loc", None))
			if _arr_issues:
				raise AssertionError(_arr_issues[0].message)
			src_val = self.lower_expr(expr.args[0])
			src_len = self.b.new_temp()
			self.b.emit(M.ArrayLen(dest=src_len, array=src_val))
			zero = self._const_int(0)
			is_empty = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=is_empty, op=H.BinaryOp.EQ, left=src_len, right=zero))
			skip_block = self.b.new_block("array_extend_skip")
			do_block = self.b.new_block("array_extend_do")
			join_block = self.b.new_block("array_extend_join")
			self.b.set_terminator(M.IfTerminator(cond=is_empty, then_target=skip_block.name, else_target=do_block.name))

			self.b.set_block(skip_block)
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(do_block)
			len_val = self.b.new_temp()
			self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
			cap_val = self.b.new_temp()
			self.b.emit(M.ArrayCap(dest=cap_val, array=array_val))
			next_gen = _next_gen(array_val)
			array_val2, _grew = ensure_capacity(array_val, len_val=len_val, cap_val=cap_val, extra=src_len)
			# Copy loop: i from 0 to src_len
			idx_local = self._addr_taken_local("__array_ext_i", self._int_type, self._const_int(0))
			self.b.emit(M.StoreLocal(local=idx_local, value=zero))
			cond_block = self.b.new_block("array_extend_cond")
			body_block = self.b.new_block("array_extend_body")
			exit_block = self.b.new_block("array_extend_exit")
			self.b.set_terminator(M.Goto(target=cond_block.name))

			self.b.set_block(cond_block)
			cur = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=cur, local=idx_local))
			lt = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=lt, op=H.BinaryOp.LT, left=cur, right=src_len))
			self.b.set_terminator(M.IfTerminator(cond=lt, then_target=body_block.name, else_target=exit_block.name))

			self.b.set_block(body_block)
			elem = self.b.new_temp()
			self.b.emit(M.ArrayIndexLoad(dest=elem, elem_ty=elem_ty, array=src_val, index=cur))
			self._local_types[elem] = elem_ty
			# `extend(&src)`: at the MIR level `elem` looks like a borrowed
			# load from src, but the LLVM lowering of `ArrayIndexLoad`
			# (`_lower_array_index_load`) calls `_emit_copy_value` on the
			# loaded element — for non-bitcopy types like String /
			# `Array<DiagnosticEntry>` this performs the retain (or deep
			# field clone) so the MIR-level `elem` is in fact an INDEPENDENT
			# owned temp distinct from src's storage.  CopyValue then takes
			# ANOTHER retain for the destination's element copy, leaving
			# `elem`'s extra ref unbalanced unless we drop it.  See
			# `array_extend_borrowed_source_string_no_uaf` (memcheck-clean
			# only with the paired drop emitted here).
			elem = self._ensure_array_elem_copy(elem, elem_ty, drop_source=True)
			dest_idx = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=dest_idx, op=H.BinaryOp.ADD, left=len_val, right=cur))
			self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=array_val2, index=dest_idx, value=elem))
			one = self._const_int(1)
			next_i = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=next_i, op=H.BinaryOp.ADD, left=cur, right=one))
			self.b.emit(M.StoreLocal(local=idx_local, value=next_i))
			self.b.set_terminator(M.Goto(target=cond_block.name))

			self.b.set_block(exit_block)
			new_len = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=new_len, op=H.BinaryOp.ADD, left=len_val, right=src_len))
			new_arr = self.b.new_temp()
			self.b.emit(M.ArraySetLen(dest=new_arr, array=array_val2, length=new_len))
			new_arr_gen = _set_gen(new_arr, next_gen)
			self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr_gen, inner_ty=array_ty))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(join_block)
			return True, None

		if name == "truncate":
			_arr_issues = array_method_arity_issues("truncate", len(expr.args), span=getattr(expr, "loc", None))
			if _arr_issues:
				raise AssertionError(_arr_issues[0].message)
			new_len_arg = self.lower_expr(expr.args[0], expected_type=self._int_type)
			len_val = self.b.new_temp()
			self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
			next_gen = _next_gen(array_val)
			zero = self._const_int(0)
			# Clamp: new_len = max(0, min(new_len_arg, len_val))
			clamped_local = self._addr_taken_local("__array_trunc_nl", self._int_type, self._const_int(0))
			neg_cond = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=neg_cond, op=H.BinaryOp.LT, left=new_len_arg, right=zero))
			clamp_zero_block = self.b.new_block("array_trunc_clamp0")
			clamp_min_block = self.b.new_block("array_trunc_clampmin")
			clamp_join_block = self.b.new_block("array_trunc_clampjoin")
			self.b.set_terminator(M.IfTerminator(cond=neg_cond, then_target=clamp_zero_block.name, else_target=clamp_min_block.name))

			self.b.set_block(clamp_zero_block)
			self.b.emit(M.StoreLocal(local=clamped_local, value=zero))
			self.b.set_terminator(M.Goto(target=clamp_join_block.name))

			self.b.set_block(clamp_min_block)
			gt_len = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=gt_len, op=H.BinaryOp.GT, left=new_len_arg, right=len_val))
			use_len_block = self.b.new_block("array_trunc_uselen")
			use_arg_block = self.b.new_block("array_trunc_usearg")
			self.b.set_terminator(M.IfTerminator(cond=gt_len, then_target=use_len_block.name, else_target=use_arg_block.name))

			self.b.set_block(use_len_block)
			self.b.emit(M.StoreLocal(local=clamped_local, value=len_val))
			self.b.set_terminator(M.Goto(target=clamp_join_block.name))

			self.b.set_block(use_arg_block)
			self.b.emit(M.StoreLocal(local=clamped_local, value=new_len_arg))
			self.b.set_terminator(M.Goto(target=clamp_join_block.name))

			self.b.set_block(clamp_join_block)
			clamped_val = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=clamped_val, local=clamped_local))
			# If clamped == len, no-op
			eq_len = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=eq_len, op=H.BinaryOp.EQ, left=clamped_val, right=len_val))
			noop_block = self.b.new_block("array_trunc_noop")
			drop_block = self.b.new_block("array_trunc_drop")
			join_block = self.b.new_block("array_trunc_join")
			self.b.set_terminator(M.IfTerminator(cond=eq_len, then_target=noop_block.name, else_target=drop_block.name))

			self.b.set_block(noop_block)
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(drop_block)
			# Drop loop: i from clamped_val to len_val
			idx_local = self._addr_taken_local("__array_trunc_i", self._int_type, self._const_int(0))
			self.b.emit(M.StoreLocal(local=idx_local, value=clamped_val))
			cond_block = self.b.new_block("array_trunc_cond")
			body_block = self.b.new_block("array_trunc_body")
			exit_block = self.b.new_block("array_trunc_exit")
			self.b.set_terminator(M.Goto(target=cond_block.name))

			self.b.set_block(cond_block)
			cur = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=cur, local=idx_local))
			lt = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=lt, op=H.BinaryOp.LT, left=cur, right=len_val))
			self.b.set_terminator(M.IfTerminator(cond=lt, then_target=body_block.name, else_target=exit_block.name))

			self.b.set_block(body_block)
			self.b.emit(M.ArrayElemDrop(elem_ty=elem_ty, array=array_val, index=cur))
			one = self._const_int(1)
			next_i = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=next_i, op=H.BinaryOp.ADD, left=cur, right=one))
			self.b.emit(M.StoreLocal(local=idx_local, value=next_i))
			self.b.set_terminator(M.Goto(target=cond_block.name))

			self.b.set_block(exit_block)
			new_arr = self.b.new_temp()
			self.b.emit(M.ArraySetLen(dest=new_arr, array=array_val, length=clamped_val))
			new_arr_gen = _set_gen(new_arr, next_gen)
			self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr_gen, inner_ty=array_ty))
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(join_block)
			return True, None

		if name == "remove_range":
			_arr_issues = array_method_arity_issues("remove_range", len(expr.args), span=getattr(expr, "loc", None))
			if _arr_issues:
				raise AssertionError(_arr_issues[0].message)
			start_val = self.lower_expr(expr.args[0], expected_type=self._int_type)
			self._local_types[start_val] = self._int_type
			end_val = self.lower_expr(expr.args[1], expected_type=self._int_type)
			self._local_types[end_val] = self._int_type
			len_val = self.b.new_temp()
			self.b.emit(M.ArrayLen(dest=len_val, array=array_val))
			self._local_types[len_val] = self._int_type
			zero = self._const_int(0)

			def _rr_abort(msg: str) -> None:
				false_val = self.b.new_temp()
				self.b.emit(M.ConstBool(dest=false_val, value=False))
				file_val = self.b.new_temp()
				self.b.emit(M.ConstString(dest=file_val, value="<array>"))
				line_val = self._const_int(0)
				expr_val = self.b.new_temp()
				self.b.emit(M.ConstString(dest=expr_val, value="remove_range"))
				msg_val = self.b.new_temp()
				self.b.emit(M.ConstString(dest=msg_val, value=msg))
				self.b.emit(M.AssertLoc(cond=false_val, file=file_val, line=line_val, expr=expr_val, msg=msg_val))
				self.b.set_terminator(M.Unreachable())

			# Validate: start >= 0
			bad_start_neg = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=bad_start_neg, op=H.BinaryOp.LT, left=start_val, right=zero))
			abort1_block = self.b.new_block("array_rr_abort1")
			check2_block = self.b.new_block("array_rr_check2")
			self.b.set_terminator(M.IfTerminator(cond=bad_start_neg, then_target=abort1_block.name, else_target=check2_block.name))
			self.b.set_block(abort1_block)
			_rr_abort("start must be >= 0")

			# Validate: end >= 0
			self.b.set_block(check2_block)
			bad_end_neg = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=bad_end_neg, op=H.BinaryOp.LT, left=end_val, right=zero))
			abort2_block = self.b.new_block("array_rr_abort2")
			check3_block = self.b.new_block("array_rr_check3")
			self.b.set_terminator(M.IfTerminator(cond=bad_end_neg, then_target=abort2_block.name, else_target=check3_block.name))
			self.b.set_block(abort2_block)
			_rr_abort("end must be >= 0")

			# Validate: start <= end
			self.b.set_block(check3_block)
			bad_order = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=bad_order, op=H.BinaryOp.GT, left=start_val, right=end_val))
			abort3_block = self.b.new_block("array_rr_abort3")
			check4_block = self.b.new_block("array_rr_check4")
			self.b.set_terminator(M.IfTerminator(cond=bad_order, then_target=abort3_block.name, else_target=check4_block.name))
			self.b.set_block(abort3_block)
			_rr_abort("start must be <= end")

			# Validate: end <= len
			self.b.set_block(check4_block)
			bad_end_len = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=bad_end_len, op=H.BinaryOp.GT, left=end_val, right=len_val))
			abort4_block = self.b.new_block("array_rr_abort4")
			valid_block = self.b.new_block("array_rr_valid")
			self.b.set_terminator(M.IfTerminator(cond=bad_end_len, then_target=abort4_block.name, else_target=valid_block.name))
			self.b.set_block(abort4_block)
			_rr_abort("end must be <= len")

			self.b.set_block(valid_block)
			# If start == end, no-op
			eq_empty = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=eq_empty, op=H.BinaryOp.EQ, left=start_val, right=end_val))
			noop_block = self.b.new_block("array_rr_noop")
			work_block = self.b.new_block("array_rr_work")
			join_block = self.b.new_block("array_rr_join")
			self.b.set_terminator(M.IfTerminator(cond=eq_empty, then_target=noop_block.name, else_target=work_block.name))

			self.b.set_block(noop_block)
			self.b.set_terminator(M.Goto(target=join_block.name))

			self.b.set_block(work_block)
			next_gen = _next_gen(array_val)
			# Drop loop: i from start to end
			drop_idx_local = self._addr_taken_local("__array_rr_di", self._int_type, self._const_int(0))
			self.b.emit(M.StoreLocal(local=drop_idx_local, value=start_val))
			drop_cond = self.b.new_block("array_rr_dcond")
			drop_body = self.b.new_block("array_rr_dbody")
			drop_exit = self.b.new_block("array_rr_dexit")
			self.b.set_terminator(M.Goto(target=drop_cond.name))

			self.b.set_block(drop_cond)
			dcur = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=dcur, local=drop_idx_local))
			dlt = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=dlt, op=H.BinaryOp.LT, left=dcur, right=end_val))
			self.b.set_terminator(M.IfTerminator(cond=dlt, then_target=drop_body.name, else_target=drop_exit.name))

			self.b.set_block(drop_body)
			self.b.emit(M.ArrayElemDrop(elem_ty=elem_ty, array=array_val, index=dcur))
			one = self._const_int(1)
			dnext = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=dnext, op=H.BinaryOp.ADD, left=dcur, right=one))
			self._local_types[dnext] = self._int_type
			self.b.emit(M.StoreLocal(local=drop_idx_local, value=dnext))
			self.b.set_terminator(M.Goto(target=drop_cond.name))

			self.b.set_block(drop_exit)
			# Shift loop: i from end to len, move elem to start + (i - end)
			shift_idx_local = self._addr_taken_local("__array_rr_si", self._int_type, self._const_int(0))
			self.b.emit(M.StoreLocal(local=shift_idx_local, value=end_val))
			shift_cond = self.b.new_block("array_rr_scond")
			shift_body = self.b.new_block("array_rr_sbody")
			shift_exit = self.b.new_block("array_rr_sexit")
			self.b.set_terminator(M.Goto(target=shift_cond.name))

			self.b.set_block(shift_cond)
			scur = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=scur, local=shift_idx_local))
			slt = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=slt, op=H.BinaryOp.LT, left=scur, right=len_val))
			self.b.set_terminator(M.IfTerminator(cond=slt, then_target=shift_body.name, else_target=shift_exit.name))

			self.b.set_block(shift_body)
			elem = self._array_elem_take_value(elem_ty=elem_ty, array=array_val, index=scur)
			offset = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=offset, op=H.BinaryOp.SUB, left=scur, right=end_val))
			self._local_types[offset] = self._int_type
			dest_idx = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=dest_idx, op=H.BinaryOp.ADD, left=start_val, right=offset))
			self._local_types[dest_idx] = self._int_type
			self.b.emit(M.ArrayElemInitUnchecked(elem_ty=elem_ty, array=array_val, index=dest_idx, value=elem))
			one_s = self._const_int(1)
			snext = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=snext, op=H.BinaryOp.ADD, left=scur, right=one_s))
			self._local_types[snext] = self._int_type
			self.b.emit(M.StoreLocal(local=shift_idx_local, value=snext))
			self.b.set_terminator(M.Goto(target=shift_cond.name))

			self.b.set_block(shift_exit)
			removed_count = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=removed_count, op=H.BinaryOp.SUB, left=end_val, right=start_val))
			self._local_types[removed_count] = self._int_type
			new_len = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=new_len, op=H.BinaryOp.SUB, left=len_val, right=removed_count))
			self._local_types[new_len] = self._int_type
			new_arr = self.b.new_temp()
			self.b.emit(M.ArraySetLen(dest=new_arr, array=array_val, length=new_len))
			new_arr_gen = _set_gen(new_arr, next_gen)
			self.b.emit(M.StoreRef(ptr=recv_ptr, value=new_arr_gen, inner_ty=array_ty))
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


	# Slice 7c-2 (ABI 14, 2026-05-06): `_visit_expr_HDVInit` deleted
	# along with `H.HDVInit` and `M.ConstructDV`.  No production
	# path emits HDVInit nodes — the dispatch via
	# `getattr(self, f"_visit_expr_{type(expr).__name__}")` will
	# raise NotImplementedError if anything ever does, which is
	# the correct contract failure shape.

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

		# Slice 7b unification (2026-05-05): all `error E` throws —
		# pub or non-pub, manual `implement core.Diagnostic` or
		# compiler-synthesized — route through the same
		# `_construct_error_via_diagnostic` path.  The path builds the
		# Path-A struct from resolved field values, calls the type's
		# `to_json_text(&E)` impl, and hands the resulting String to
		# `ExcSetParamsJson`.  There is no DV-attachment
		# (`ConstructError(payload=DV)` / `ErrorAddAttrDV`) on the
		# production throw lowering anymore — Slice 6's K-rule
		# ("Diagnostic impl owns the whole JSON shape") now applies
		# uniformly.
		#
		# Slice 7b (K, 2026-05-06; LANGUAGE_BUG fix): synthesis fires
		# for non-pub `error E` too — pre-fix, non-pub error throws
		# silently emitted an empty params envelope.  The empty-
		# envelope branch below is now reachable only for empty
		# throws (`throw E()` with no fields) or stage2-stub test
		# environments (no Path-A struct registered); a non-empty
		# throw of an `error E` whose Diagnostic impl is missing ICEs
		# in the contract guard (compiler / package metadata bug).
		struct_ty: TypeId | None = None
		if ":" in expr.event_fqn:
			_mod_id, _type_name = expr.event_fqn.split(":", 1)
			struct_ty = self._type_table.get_nominal(
				kind=TypeKind.STRUCT, module_id=_mod_id, name=_type_name,
			)
		diag_fn_id: "FunctionId | None" = None
		if struct_ty is not None:
			diag_fn_id = self._lookup_diagnostic_to_json_text_fn_id(struct_ty)
		if diag_fn_id is not None and resolved:
			return self._construct_error_via_diagnostic(
				err_val=err_val,
				code_val=code_val,
				event_fqn_val=event_fqn_val,
				event_fqn=expr.event_fqn,
				resolved=resolved,
				schema_fields=schema_fields,
			)
		# Slice 7b contract guard (K, 2026-05-06): a non-empty throw
		# of any `error E` (pub OR non-pub) MUST route through the
		# Diagnostic impl.  Synthesis fires for every `error` decl
		# with projectable fields, so reaching this fall-through with
		# `resolved` non-empty AND a Path-A struct registered AND the
		# event known as an `error` means `to_json_text` is missing
		# from `_signatures_by_id` — a compiler / package metadata
		# bug, not a valid `{}` payload.  ICE so the bug surfaces
		# clearly instead of silently dropping field values from
		# `e.params.get(...)`.
		if resolved and struct_ty is not None and diag_fn_id is None:
			exc_kinds = getattr(self._type_table, "exception_kinds", None) or {}
			if exc_kinds.get(expr.event_fqn) == "error":
				raise AssertionError(
					f"`error {expr.event_fqn}` reached HIR→MIR throw "
					"lowering with fields but `core.Diagnostic.to_json_text` "
					"impl method is not in signatures — compiler / package "
					"metadata bug; the synthesized impl should always be "
					"reachable for any `error` type with projectable fields"
				)
		# Empty-envelope shape: reachable in two cases:
		#   1. The throw has zero fields (`throw E()` with empty
		#      schema) — `to_json_text` would return the literal
		#      `"{}"`, but we skip the call and let the runtime's
		#      default `"{}"` stand.
		#   2. Stage2-stub test environments where the synthetic type
		#      table has no Path-A struct registered for this
		#      event_fqn.  Production compilation always registers the
		#      struct (parser-side parallel struct synthesis); the ICE
		#      above guards the production path.
		# No params JSON is attached — the runtime default `"{}"` is
		# returned by `e.params.encode_compact()`.
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

	def _construct_error_via_diagnostic(
		self,
		*,
		err_val: M.ValueId,
		code_val: M.ValueId,
		event_fqn_val: M.ValueId,
		event_fqn: str,
		resolved: list[tuple[str, "H.HExpr"]],
		schema_fields: list[str] | None,
	) -> M.ValueId:
		"""Slice 6/7b unified-Diagnostic owning lowering for `throw E(...)`.

		Build the Path-A struct E from the resolved field values, call
		the type's `to_json_text(&E)` impl to project the JSON, and
		hand the result to `ExcSetParamsJson`.  No DV-attachment
		(`ConstructError(payload=...)` / `ErrorAddAttrDV`) — the
		Diagnostic impl owns the whole JSON shape, regardless of
		whether the impl is user-manual (Slice 6) or compiler-
		synthesized (Slice 7b — same code path).

		Slice 7b unification (2026-05-05): the synthesized
		`to_json_text` body emits canonical JSON via per-field
		`<self>.<field>.to_json_text()` dispatch; routing synthesized
		throws through the same path as manual throws fixes the
		variant-field double-quoting bug (synthesized was wrapping
		non-scalar field values in `DV::String(value.to_json_text())`,
		which then JSON-quoted the already-canonical body).

		`resolved` is in declaration order (matching `schema_fields`),
		so positional `M.ConstructStruct` field order is correct.

		Ownership invariant: each field value is consumed exactly once
		— by the struct constructor.  `to_json_text` takes `&Self`
		(reference borrow), so the struct stays live for the projection
		and gets dropped at scope exit through the ordinary local
		drop path.  String / Array fields therefore round-trip without
		double-free OR leak (heap-string e2e
		`pub_error_manual_diagnostic_string_field` is the regression
		anchor).
		"""
		# Resolve event_fqn → (module_id, type_name) → Path-A struct TypeId.
		if ":" not in event_fqn:
			raise AssertionError(
				f"event_fqn {event_fqn!r} missing module qualifier; "
				"manual-Diagnostic lowering requires a module-qualified pub error"
			)
		mod_id, type_name = event_fqn.split(":", 1)
		struct_ty = self._type_table.get_nominal(
			kind=TypeKind.STRUCT, module_id=mod_id, name=type_name,
		)
		if struct_ty is None:
			raise AssertionError(
				f"no Path-A struct registered for `pub error {event_fqn}` "
				"(parser-side parallel struct synthesis bug)"
			)
		# Lower each resolved field value to a MIR temp.  ConstructStruct
		# expects positional args in declaration order — `resolved` is
		# already in that order.
		field_vals: list[M.ValueId] = [self.lower_expr(val) for _name, val in resolved]
		return self._emit_diagnostic_owning_throw(
			err_val=err_val,
			code_val=code_val,
			event_fqn_val=event_fqn_val,
			event_fqn=event_fqn,
			struct_ty=struct_ty,
			field_mir_vals=field_vals,
		)

	def _emit_diagnostic_owning_throw(
		self,
		*,
		err_val: M.ValueId,
		code_val: M.ValueId,
		event_fqn_val: M.ValueId,
		event_fqn: str,
		struct_ty: TypeId,
		field_mir_vals: list[M.ValueId],
	) -> M.ValueId:
		"""Slice 7b shared post-lowering owning-throw emission.

		Given pre-lowered MIR values for the Path-A struct's fields (in
		declaration order), construct the struct, call its
		`to_json_text(&E)` Diagnostic impl, and emit ConstructError +
		ExcSetParamsJson.  Used by both the user-source `throw E(...)`
		path (`_construct_error_via_diagnostic`) and compiler-synthesized
		inline throws (e.g. the array bounds-check IndexError site).
		"""
		# Locate the `to_json_text` method on this struct type.  Slice 7b
		# invariant: every reachable `pub error E` has either a manual or
		# compiler-synthesized `core.Diagnostic for E`; the lookup keys
		# on (struct_ty, std.core.Diagnostic) and finds both.
		to_json_fn_id = self._lookup_diagnostic_to_json_text_fn_id(struct_ty)
		if to_json_fn_id is None:
			raise AssertionError(
				f"`core.Diagnostic for {event_fqn}` recorded but "
				f"its `to_json_text` impl method is missing from signatures "
				f"(manual or synthesized)"
			)
		struct_val = self.b.new_temp()
		self._local_types[struct_val] = struct_ty
		self.b.emit(M.ConstructStruct(
			dest=struct_val,
			struct_ty=struct_ty,
			args=field_mir_vals,
		))
		# Materialize the struct in a named local so we can take its
		# address (`AddrOfLocal` requires a named local, not a temp).
		# materialize-audit: allow consumed
		# Inline DropValue ~30 lines below handles release at the
		# throw-bypass site (throw propagation unwinds via gotos --
		# normal scope-drop does NOT fire on throw-lowered locals).
		# See the leak-fix comment block below the DropValue.
		struct_local = f"__throw_diag_{self.b.new_temp()}"
		self.b.ensure_local(struct_local)
		self._local_types[struct_local] = struct_ty
		self.b.emit(M.StoreLocal(local=struct_local, value=struct_val))
		struct_ref = self.b.new_temp()
		self._local_types[struct_ref] = self._type_table.ensure_ref(struct_ty)
		self.b.emit(M.AddrOfLocal(dest=struct_ref, local=struct_local, is_mut=False))
		json_str_val = self.b.new_temp()
		self._local_types[json_str_val] = self._string_type
		self.b.emit(M.Call(
			dest=json_str_val,
			fn_id=to_json_fn_id,
			args=[struct_ref],
			can_throw=False,
		))
		self.b.emit(M.ConstructError(
			dest=err_val,
			code=code_val,
			event_fqn=event_fqn_val,
			payload=None,
			attr_key=None,
		))
		self.b.emit(M.ExcSetParamsJson(error=err_val, json_text=json_str_val))
		# Slice 7b leak fix (K, 2026-05-06; surfaced by negative-space
		# audit): explicit DropValue on the throw-site struct local.
		# Throw propagation unwinds via gotos to the dispatch block —
		# normal scope-drop does NOT fire on locals materialized inside
		# the throw lowering itself, so heap-backed fields (String,
		# Array, Arc, ...) on the struct would leak one slot per
		# throw.  Same explicit-drop pattern as the captured-locals
		# JSON frame builder uses for borrowed Strings.  Pre-fix this
		# matched valgrind clean only for string-literal fields (Const
		# strings have no heap allocation); heap-allocated String
		# fields leaked silently.
		struct_for_drop = self.b.new_temp()
		self._local_types[struct_for_drop] = struct_ty
		self.b.emit(M.LoadLocal(dest=struct_for_drop, local=struct_local))
		self.b.emit(M.DropValue(value=struct_for_drop, ty=struct_ty))
		return err_val

	def _lookup_diagnostic_to_json_text_fn_id(self, struct_ty: TypeId) -> "FunctionId | None":
		"""Find the `implement core.Diagnostic for E` impl's `to_json_text`
		method by scanning signatures for a TRAIT IMPL method on
		`struct_ty` for the canonical `std.core.Diagnostic` trait.
		Filtering on the trait identity (not just method name) avoids
		dispatching to an inherent `to_json_text` or to a different-trait
		`to_json_text` that happens to share the method name.  See
		`FnSignature.impl_trait_{module,name}`.

		Slice 7b (2026-05-05): renamed from
		`_lookup_manual_diagnostic_to_json_text_fn_id` — the helper has
		always keyed on (target type, std.core.Diagnostic trait
		identity), which is shared between user-manual impls and
		compiler-synthesized impls.  The synthesized impl is registered
		via the same `ImplementDef` shape (`trait=TypeExpr(name="Diagnostic",
		module_id="std.core")`) so this lookup finds both equivalently.
		"""
		for fn_id, sig in self._signatures_by_id.items():
			if not bool(getattr(sig, "is_method", False)):
				continue
			if getattr(sig, "method_name", None) != "to_json_text":
				continue
			if getattr(sig, "impl_target_type_id", None) != struct_ty:
				continue
			# Canonical std.core.Diagnostic gating.
			if getattr(sig, "impl_trait_module", None) != "std.core":
				continue
			if getattr(sig, "impl_trait_name", None) != "Diagnostic":
				continue
			return fn_id
		return None

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
		# materialize-audit: allow registered explicit
		# _register_drop_local call below
		# temp_local holds the ternary result -- stored conditionally
		# across then/else branches, loaded at the join.  Helper's
		# all-in-one alloc+store doesn't fit (conditional store).
		# Register so droppable result types participate in cleanup.
		temp_local = f"__tern_tmp{self.b.new_temp()}"
		self.b.ensure_local(temp_local)
		tern_ty = self._infer_expr_type(expr.then_expr)
		if tern_ty is None:
			tern_ty = self._infer_expr_type(expr.else_expr)
		if tern_ty is None:
			tern_ty = self._unknown_type
		self._local_types[temp_local] = tern_ty
		self._register_drop_local(temp_local, tern_ty)

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
		# Prefer the checker's recorded expression type.  The checker accepts a
		# no-fields qualified-member ref by short-circuiting to `expected_type`
		# (see `type_checker.py` HQualifiedMember branch, the short-circuit
		# guarded on `expected_type is not None and not arm_schema.fields`).
		# Re-resolving `base_te → base_tid` and calling
		# `ensure_variant_instantiated` here would arrive at a *different* type
		# identity than the one the checker handed back — and for a
		# user-declared generic variant whose base is not pre-registered in the
		# generic-instance machinery, `ensure_variant_instantiated` will
		# ValueError.  Consume the recorded type when it's shaped as a VARIANT
		# instance over the same base; fall through to the re-resolve path only
		# when no recorded type applies (untyped lowering / recover mode).
		# Compute the schema base id for this qualified-member's variant base.
		# `base_tid` from `resolve_opaque_type(base_te, allow_generic_base=True)`
		# can be either:
		#   - the schema base TypeId (when base_te has no args), or
		#   - an already-instantiated variant TypeId (when base_te has args —
		#     `resolve_opaque_type` eagerly instantiates).
		# `variant_instances[<inst_id>].base_id` is the canonical schema base.
		schema_base_id: TypeId | None = None
		if base_tid in self._type_table.variant_schemas:
			schema_base_id = base_tid
		else:
			base_inst = self._type_table.get_variant_instance(base_tid)
			if base_inst is not None:
				schema_base_id = base_inst.base_id
		inst_id: TypeId | None = None
		recorded = self._expr_types.get(expr.node_id) if self._typed_mode != "none" else None
		if recorded is not None and schema_base_id is not None:
			try:
				recorded_def = self._type_table.get(recorded)
			except Exception:
				recorded_def = None
			if recorded_def is not None and recorded_def.kind is TypeKind.VARIANT:
				recorded_inst = self._type_table.get_variant_instance(recorded)
				if recorded_inst is not None and recorded_inst.base_id == schema_base_id:
					inst_id = recorded
		if inst_id is None:
			type_arg_exprs = list(getattr(base_te, "args", []) or [])
			if schema.type_params:
				if len(type_arg_exprs) != len(schema.type_params):
					raise AssertionError("qualified member missing type arguments in typed mode (checker bug)")
				type_arg_ids = [resolve_opaque_type(a, self._type_table, module_id=getattr(base_te, "module_id", None) or cur_mod) for a in type_arg_exprs]
				# Use the canonical schema base for instantiation — `base_tid`
				# may already be the instance TypeId (when `base_te` carries
				# args, `resolve_opaque_type` eagerly instantiates), and
				# `ensure_variant_instantiated` requires a *declared schema
				# base* TypeId (ValueErrors otherwise).  This matches the
				# happy-path schema-base derivation above.
				if schema_base_id is None:
					raise AssertionError("qualified member base has no derivable schema base id (type table bug)")
				inst_id = self._type_table.ensure_variant_instantiated(schema_base_id, type_arg_ids)
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
				scope_index_at_entry=len(self._scope_stack),
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
				# Consume `error_local` via MoveOut before propagating.
				# `err_tmp` above was a LoadLocal SNAPSHOT used only for
				# the event-code comparison; ownership is still nominally
				# in `error_local`.  See the parallel comment in
				# `_visit_stmt_HTry` -- without this MoveOut, the
				# function-exit cleanup hook (or an outer-try error_local
				# store) double-drops the same envelope pointer that the
				# caller's FnResult.Err release path will hit.  Expression-
				# form mirror of the statement-form fix.  Reproducer:
				#   pub error A {} pub error B {}
				#   fn fail() -> Int { throw A(); }
				#   fn inner() -> Int { val x = try fail() catch B(e) { 999 }; return x; }
				#   fn main() nothrow -> Int { return try inner() catch { 0 }; }
				# Fixed 2026-05-18.
				consumed_tmp = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=consumed_tmp, local=error_local, ty=error_ty))
				self._local_types[consumed_tmp] = error_ty
				self._propagate_error(consumed_tmp)
		else:
			self.b.set_block(dispatch_block)
			if catch_all_block is not None:
				self.b.set_terminator(M.Goto(target=catch_all_block.name))
			else:
				# Same ownership transfer as the event-arms branch above.
				consumed_tmp = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=consumed_tmp, local=error_local, ty=error_ty))
				self._local_types[consumed_tmp] = error_ty
				self._propagate_error(consumed_tmp)

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
				binder_id = self._find_binder_binding_id(arm.binder, arm.block, arm.result)
				binder_local = self._canonical_local(binder_id, arm.binder)
				# materialize-audit: allow consumed
				# Explicit MoveOut + DropValue at lines ~7295-7299
				# below handles release at try-expr arm exit; scope
				# cleanup would not fire on the arm-exit path anyway
				# (try-expr semantics route through the join block).
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
				self.b.emit(M.DropValue(value=err_done, ty=error_ty))
			if self.b.block.terminator is None and binder_local is not None:
				binder_done = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=binder_done, local=binder_local, ty=error_ty))
				self._local_types[binder_done] = error_ty
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
			# Phase 4 site-1 patch 3: nested-scope fall-through cleanup
			# migrated to `M.CleanupHook`.  Re-enabled after the
			# `core.drop_value` intrinsic lowering was fixed to emit
			# `MoveOut + DropValue` (so the ledger sees the consumption
			# and `cleanup_authoring`'s `verdict_at` returns
			# MUST_NOT_DROP rather than authoring a redundant drop).
			# Pinned by `test_drop_value_intrinsic_ownership.py` and
			# `test_patch3_nested_scope_uaf_regression.py`.  See
			# `work/ownership-ledger/patch-3-diagnosis.md`.
			self._emit_scope_cleanup_hook(scope_index=len(self._scope_stack) - 1)
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
			# Phase 4 site-1 patch 4a: `lower_function_body`
			# fall-through cleanup migrated to `M.CleanupHook` +
			# `cleanup_authoring`.  Structurally the closest
			# remaining shape to the patch-1 function-exit slice
			# (cleanup runs immediately before the implicit Return
			# terminator, no mid-function downstream ledger
			# queries).  Covered by the existing patch-3 carrier +
			# the consume-via-intrinsic invariant pins.
			self._emit_scope_cleanup_hook(scope_index=len(self._scope_stack) - 1)
		self._pop_scope()
		if self.b.block.terminator is not None:
			return
		can_throw = self._fn_can_throw() is True
		fn_is_void = self._ret_type is not None and self._type_table.is_void(self._ret_type)
		# Phase 4 site-1 patch 6b-B: the legacy inline `caught_error`
		# drop here was dead code — `_current_catch_error` is only set
		# inside `_visit_expr_HTryExpr`'s catch arm lowering (set at
		# line ~6394, restored at line ~6399).  By the time this
		# post-loop check ran, the catch arm body had completed and
		# `_current_catch_error` was always None.  The function-exit
		# `_emit_scope_cleanup_hook(scope_index=...)` above (patch 4a,
		# line ~6490) covers `caught_error` as a candidate via the
		# `_register_drop_local` registration at try-expression
		# lowering time.  Dead-code site removed.
		if not fn_is_void:
			# Defensive invariant: the checker's terminal-flow pass
			# (`Checker._check_terminal_returns`) is responsible for rejecting
			# any non-Void function whose body falls off the end. If we reach
			# here, that pass missed a case — file a checker bug.
			raise AssertionError(
				"missing return reached MIR lowering — terminal-flow checker should "
				"have rejected this (see Checker._check_terminal_returns)"
			)
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
				# Pre-flight: validate arity/kwargs via call_contract (single seam).
				_kwargs = getattr(stmt.expr, "kwargs", None) or []
				_shape_issues = [i for i in intrinsic_call_issues(intrinsic, stmt.expr, kwargs=_kwargs) if "MUT_BORROW_REQUIRED" not in i.code]
				if _shape_issues:
					raise AssertionError(f"{_shape_issues[0].message} reached MIR lowering (checker bug)")
				if intrinsic is IntrinsicKind.SWAP:
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
					info = self._call_info_for(stmt.expr)
					raw_param = self._unwrap_ref_type(info.sig.param_types[0]) if info.sig.param_types else self._unknown_type
					buf_val = self.lower_expr(stmt.expr.args[0])
					self.b.emit(M.RawBufferDealloc(buffer=buf_val, raw_ty=raw_param))
					return
				if intrinsic is IntrinsicKind.RAW_WRITE:
					info = self._call_info_for(stmt.expr)
					raw_param = self._unwrap_ref_type(info.sig.param_types[0]) if info.sig.param_types else self._unknown_type
					elem_ty = self._raw_buffer_elem_type(raw_param)
					if elem_ty is self._unknown_type:
						raise AssertionError("write(...) missing RawBuffer element type (checker bug)")
					buf_val = self.lower_expr(stmt.expr.args[0])
					idx_val = self.lower_expr(stmt.expr.args[1])
					val_val = self._lower_owning_consume(stmt.expr.args[2], expected=elem_ty)
					self.b.emit(M.RawBufferWrite(buffer=buf_val, raw_ty=raw_param, elem_ty=elem_ty, index=idx_val, value=val_val))
					return
				if intrinsic is IntrinsicKind.PTR_WRITE:
					info = self._call_info_for(stmt.expr)
					ptr_param = info.sig.param_types[0] if info.sig.param_types else self._unknown_type
					elem_ty = self._raw_ptr_elem_type(ptr_param)
					if elem_ty is self._unknown_type:
						raise AssertionError("ptr_write(...) missing Ptr<T> element type (checker bug)")
					ptr_val = self.lower_expr(stmt.expr.args[0])
					val_val = self._lower_owning_consume(stmt.expr.args[1], expected=elem_ty)
					self.b.emit(M.PtrWrite(ptr=ptr_val, value=val_val, elem_ty=elem_ty))
					return
				if intrinsic is IntrinsicKind.DROP_VALUE:
					info = self._call_info_for(stmt.expr)
					param_ty = info.sig.param_types[0] if info.sig.param_types else self._unknown_type
					# Lower through the owning-consume helper so a non-
					# Copy local arg becomes `MoveOut(t, x, ty) + DropValue(t)`
					# instead of `LoadLocal + DropValue`.  Without the
					# explicit MoveOut, MIR carries no ownership transition
					# for `x` and the ledger reports `x` as LIVE after the
					# DropValue — patch-1 cleanup_authoring (function-exit)
					# and patch-3 cleanup_authoring (nested-scope) would
					# both query `verdict_at(x)` and authorize a redundant
					# drop, double-running the destructor (manifested as
					# UAF on the fat-Arc-interface-views driver carrier;
					# see `work/ownership-ledger/patch-3-diagnosis.md`).
					# `_lower_owning_consume` also handles `_mark_moved`
					# for the HVar / projection-free HPlaceExpr cases, so
					# the explicit `_mark_moved` block below is no longer
					# needed.
					val = self._lower_owning_consume(stmt.expr.args[0], expected=param_ty)
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

					self._lower_can_throw_call_stmt(emit_call=emit_call, ok_ty=info.sig.user_ret_type, is_terminal_throws=self._is_call_terminal_throws(info))
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

					self._lower_can_throw_call_stmt(emit_call=emit_call, ok_ty=info.sig.user_ret_type, is_terminal_throws=self._is_call_terminal_throws(info))
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

					self._lower_can_throw_call_stmt(emit_call=emit_call, ok_ty=info.sig.user_ret_type, is_terminal_throws=self._is_call_terminal_throws(info))
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

				self._lower_can_throw_call_stmt(emit_call=emit_call, ok_ty=info.sig.user_ret_type, is_terminal_throws=self._is_call_terminal_throws(info))
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
		if self._should_copy_value(expr_ty):
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

	def _visit_expr_HUnsafeExpr(self, expr: H.HUnsafeExpr) -> "M.ValueId":
		self.lower_block(expr.body)
		# If the result expression is a void-returning call, lower it for
		# side effects only and return a void value.  This avoids the
		# "Void-returning call used in expression context" assertion when
		# an unsafe block wraps a void extern-C call in statement position.
		if isinstance(expr.result, H.HCall):
			info = self._call_info_for_expr_optional(expr.result)
			if info is None:
				info = self._call_info_for(expr.result)
			if info is not None and self._type_table.is_void(info.sig.user_ret_type):
				self._lower_call(expr.result)
				return self._void_value()
		return self.lower_expr(expr.result)

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

	def _emit_local_const(self, bid: int) -> M.ValueId:
		"""Emit a fresh MIR literal for a block-scope constant."""
		ty_id, val = self._local_consts[bid]
		dest = self.b.new_temp()
		if isinstance(val, list):
			td = self._type_table.get(ty_id)
			if td.kind is TypeKind.ARRAY and td.param_types:
				elem_ty = td.param_types[0]
				self.b.emit(M.ConstArray(dest=dest, elem_ty=elem_ty, values=list(val)))
				self._local_types[dest] = ty_id
				return dest
			raise AssertionError(f"unsupported local const array type (bid={bid})")
		if ty_id == self._int_type:
			self.b.emit(M.ConstInt(dest=dest, value=int(val)))
		elif ty_id == self._uint_type:
			self.b.emit(M.ConstUint(dest=dest, value=int(val)))
		elif ty_id == self._uint64_type:
			self.b.emit(M.ConstUint64(dest=dest, value=int(val)))
		elif ty_id == self._bool_type:
			self.b.emit(M.ConstBool(dest=dest, value=bool(val)))
		elif ty_id == self._string_type:
			self.b.emit(M.ConstString(dest=dest, value=str(val)))
		elif ty_id == self._float_type:
			self.b.emit(M.ConstFloat(dest=dest, value=float(val)))
		elif ty_id == self._byte_type:
			self.b.emit(M.ConstByte(dest=dest, value=int(val)))
		else:
			raise AssertionError(f"unsupported local const type (bid={bid})")
		return dest

	def _visit_stmt_HLocalConst(self, stmt: H.HLocalConst) -> None:
		"""Record a block-scope constant for later inlining at use sites.

		No local slot is allocated. Each HVar reference to this binding_id
		will emit a fresh MIR literal (ConstInt/ConstString/etc.).
		"""
		from lang.driftc.checker import _eval_hir_const_value
		val = _eval_hir_const_value(stmt.value)
		if val is None:
			return  # checker already diagnosed
		decl_ty: TypeId | None = None
		if getattr(stmt, "declared_type_expr", None) is not None:
			try:
				decl_ty = resolve_opaque_type(
					stmt.declared_type_expr,
					self._type_table,
					module_id=self._current_module_name(),
					type_params=self._type_param_subst or None,
				)
			except Exception:
				decl_ty = None
		if decl_ty is None:
			return
		bid = getattr(stmt, "binding_id", None)
		if bid is not None:
			self._local_consts[int(bid)] = (decl_ty, val)

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
		# Deep-copy aliased ref-field temps before binding to a local
		# (the local will be dropped at scope exit).
		if val_ty is not None:
			val = self._copy_if_ref_alias(val, val_ty)
		if val_ty is not None:
			self._local_types[local_name] = val_ty
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
		self._current_stmt_span = prev_stmt_span

	def _visit_stmt_HAssign(self, stmt: H.HAssign) -> None:
		# Use `_lower_owning_consume` so HVar / projection-free
		# HPlaceExpr sources of move-classified types are MoveOut'd
		# (marking the source local as moved) instead of being
		# loaded.  Pre-fix `dst = src;` for `src: DiagnosticEntry`
		# loaded src and StoreLocal'd into dst — both src and dst then
		# held the same data ptr and double-released at scope drop
		# (om_local_assign_diag_entry hvar_local UAF, same family as
		# the return-value bug).
		assign_ty = self._infer_expr_type(stmt.value)
		val = self._lower_owning_consume(stmt.value, assign_ty)
		# Deep-copy aliased ref-field temps before assignment.
		if assign_ty is not None:
			val = self._copy_if_ref_alias(val, assign_ty)
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
						# materialize-audit: allow registered via HLet
						# path (_visit_stmt_HLet calls
						# _register_drop_local at the original
						# declaration); HAssign just stores into the
						# existing slot.
						local_name = self._canonical_local(getattr(stmt.target.base, "binding_id", None), stmt.target.base.name)
						self.b.ensure_local(local_name)
						self.b.emit(M.StoreLocal(local=local_name, value=val))
						return
					# materialize-audit: allow registered via HLet path
					# (same rationale -- existing slot, declared earlier)
					local_name = self._canonical_local(getattr(stmt.target.base, "binding_id", None), stmt.target.base.name)
					self.b.ensure_local(local_name)
					ptr_val = self.b.new_temp()
					self.b.emit(M.LoadLocal(dest=ptr_val, local=local_name))
					self._emit_assign_store_ref(ptr=ptr_val, value=val, inner_ty=td.param_types[0])
					return
			# materialize-audit: allow registered via HLet path (same)
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
						if self._should_copy_value(val_ty) and not isinstance(stmt.value, H.HMove):
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
		# Replace-store semantics for an owning slot: drop the previous
		# non-Copy occupant exactly once before storing the new value.
		#
		# The canonical sequence — `MoveFromRef → MoveOut → DropValue
		# → StoreRef` — uses the explicit ownership-transfer primitive
		# `MoveFromRef` to atomically tombstone the slot AND capture
		# the old value into a local in one operation, drains the
		# local into an SSA value via `MoveOut`, drops it once, and
		# stores the new value into the now-tombstoned slot.
		#
		# Why MoveFromRef and not the legacy `LoadRef + ZeroValue +
		# StoreRef(zero) + DropValue` shape: `string_arc.py:1108-1121`
		# rewrites every `StoreRef` to a String place as
		# `LoadRef + StringRelease + StoreRef`.  In the legacy shape
		# the zero-tombstone `StoreRef` therefore became a real
		# release of the old slot value, and the explicit `DropValue`
		# released the same allocation a second time via the SSA
		# loaded BEFORE the tombstone — UAF + leak.  See
		# `lang/tests/memcheck/test_mut_struct_string_field_self_concat.py`
		# for the runtime carrier and
		# `lang/tests/stage2/test_assign_store_ref_drop_bearing_lowering.py`
		# for the post-string_arc MIR pin.
		#
		# Caller ordering: `_visit_stmt_HAssign` lowers the RHS into
		# `value` BEFORE invoking this helper (see `_lower_owning_consume`
		# at the call site), so any self-referential read like
		# `ctx.s = ctx.s + "A"` is fully evaluated and materialized in
		# `value` by the time the slot is tombstoned here.  Tombstoning
		# at this point cannot break a still-pending RHS load.
		if self._needs_runtime_drop(inner_ty):
			tmp_local = f"__assign_old_{self.b.new_temp()}"
			self.b.ensure_local(tmp_local)
			self._local_types[tmp_local] = inner_ty
			self.b.emit(M.MoveFromRef(local=tmp_local, ptr=ptr, inner_ty=inner_ty))
			old_val = self.b.new_temp()
			self._local_types[old_val] = inner_ty
			self.b.emit(M.MoveOut(dest=old_val, local=tmp_local, ty=inner_ty))
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
			# materialize-audit: allow registered via HLet path
			# (HAugAssign rewrites x op= rhs as x = x op rhs into
			# the existing local; declaration site _visit_stmt_HLet
			# already called _register_drop_local).
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

	def _lower_owning_consume(self, value_expr: H.HExpr, expected: TypeId | None) -> M.ValueId:
		"""Lower an expression at an OWNING-CONSUMPTION boundary.

		Used by `return <expr>;` and local-reassignment (`x = <expr>;`)
		— both of which (1) consume the source value into the
		destination, and (2) trigger downstream scope-drops that must
		see the source local as MOVED if the value type is
		move-classified.  Mirrors `_lower_call_arg`'s ownership
		decision: HVar / projection-free HPlaceExpr of a
		move-classified type → `MoveOut` (so scope-drops skip the
		local); all other shapes → `lower_expr` (and the caller's
		`_copy_if_ref_alias` upgrade for projection aliases).

		Pre-fix both paths used raw `lower_expr` — for `return src;`
		(or `dst = src;`) where `src` is an HVar of e.g.
		`core.DiagnosticEntry`, the load left the local live; the
		subsequent scope-drop / dst-drop sequence then double-released
		the inner refcounted storage (caller saw a use-after-free in
		`m::scenario_hvar_local` for both om_return_value_diag_entry
		and om_local_assign_diag_entry).
		"""
		base = value_expr
		HPlaceExpr = getattr(H, "HPlaceExpr", None)
		if HPlaceExpr is not None and isinstance(value_expr, HPlaceExpr):
			if getattr(value_expr, "projections", None):
				return self.lower_expr(value_expr, expected_type=expected)
			base = value_expr.base
		if isinstance(base, H.HVar):
			arg_ty = self._infer_expr_type(base)
			if expected is not None and not self._should_copy_value(expected):
				arg_ty = expected
			# LANGUAGE_BUG (consume-intrinsic ownership, 2026-05-06):
			# At an OWNING-CONSUMPTION boundary, a value with a custom
			# `core.Destructible` impl MUST move, never cheap-copy.
			# Otherwise the source local stays LIVE in the ledger,
			# `cleanup_authoring` queries `verdict_at(local)` at scope
			# exit, sees `MUST_DROP`, and authors a redundant
			# scope-exit destructor — running the user `destroy()` body
			# twice for the same logical instance (UAF when destroy
			# touches heap; latent for empty destructors).
			#
			# The pre-fix gate (`not _should_copy_value(arg_ty)`) only
			# moved when `_drop_policy.is_cheap_copy` was False.  But a
			# plain struct with all-`ConstShare` fields AND a
			# user-declared `core.Destructible for T` impl can have
			# `is_cheap_copy=True` (auto-`ConstShare` is currently not
			# disqualified by the presence of `Destructible`).  At
			# consume sites that's the source of the double-destroy
			# class K flagged across `core.drop_value`, `mem.write`,
			# `mem.maybe_write`, `mem.replace`, `mem.ptr_write`, and
			# the `MaybeUninit` standalone-local lowering.
			#
			# Pinned by `test_drop_value_intrinsic_ownership.py::
			# test_lowering_drop_value_emits_moveout_for_non_copy_local`
			# and the consume-intrinsic / maybe-uninit siblings.  The
			# auto-`ConstShare`-with-`Destructible` gap remains a
			# separate phase (synthesis-side disqualification); this
			# patch hardens the consume-side decision so the gap does
			# not leak into runtime double-destroy.
			must_move = arg_ty is not None and (
				not self._should_copy_value(arg_ty)
				or self._drop_policy(arg_ty).is_destructible
			)
			if must_move:
				subj_name = self._canonical_local(getattr(base, "binding_id", None), base.name)
				self.b.ensure_local(subj_name)
				moved_val = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=moved_val, local=subj_name, ty=arg_ty))
				self._local_types[moved_val] = arg_ty
				return moved_val
		return self.lower_expr(value_expr, expected_type=expected)

	def _lower_return_value(self, value_expr: H.HExpr) -> M.ValueId:
		"""Backward-compatible shim around `_lower_owning_consume` for
		return-statement use.  See `_lower_owning_consume` for details.
		"""
		return self._lower_owning_consume(value_expr, self._ret_type)

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
						self.lower_stmt(H.HExprStmt(expr=stmt.value))
						self._emit_function_exit_cleanup_hook()
						term = M.Return(value=None)
						if ret_span != Span():
							term.span = ret_span
						self.b.set_terminator(term)
						return
					raise AssertionError("Void function must not have a return value (checker bug)")
				self._emit_function_exit_cleanup_hook()
				term = M.Return(value=None)
				if ret_span != Span():
					term.span = ret_span
				self.b.set_terminator(term)
				return
			if stmt.value is None:
				raise AssertionError("non-void bare return reached MIR lowering (checker bug)")
			val = self._lower_return_value(stmt.value)
			if self._ret_type is not None:
				val = self._copy_if_ref_alias(val, self._ret_type)
				val_ty = self._local_types.get(val)
				if val_ty is not None:
					val_td = self._type_table.get(val_ty)
					ret_td = self._type_table.get(self._ret_type)
					if val_td.kind is TypeKind.REF and ret_td.kind is not TypeKind.REF:
						from lang.driftc.stage2.mir_lowering_error import MirLoweringError
						raise MirLoweringError(
							f"cannot return reference as owned '{ret_td.name}'; "
							f"returning an owned value from a reference requires "
							f"explicit 'copy <expr>'"
						)
			self._emit_function_exit_cleanup_hook()
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
					self.lower_stmt(H.HExprStmt(expr=stmt.value))
					self._emit_function_exit_cleanup_hook()
					res_val = self.b.new_temp()
					self.b.emit(M.ConstructResultOk(dest=res_val, value=None))
					term = M.Return(value=res_val)
					if ret_span != Span():
						term.span = ret_span
					self.b.set_terminator(term)
					return
				raise AssertionError("Void function must not have a return value (checker bug)")
			self._emit_function_exit_cleanup_hook()
			res_val = self.b.new_temp()
			self.b.emit(M.ConstructResultOk(dest=res_val, value=None))
			term = M.Return(value=res_val)
			if ret_span != Span():
				term.span = ret_span
			self.b.set_terminator(term)
			return
		if stmt.value is None:
			raise AssertionError("non-void bare return reached MIR lowering (checker bug)")
		val = self._lower_return_value(stmt.value)
		if self._ret_type is not None:
			val = self._copy_if_ref_alias(val, self._ret_type)
			# Reject returning &T where owned T is expected.
			# This catches ref-scrutinee variant binders (e.g.
			# Optional<&String>::Some(v) → v is &String) returned
			# as owned String.  User should write copy(v).
			val_ty = self._local_types.get(val)
			if val_ty is not None:
				val_td = self._type_table.get(val_ty)
				ret_td = self._type_table.get(self._ret_type)
				if val_td.kind is TypeKind.REF and ret_td.kind is not TypeKind.REF:
					from lang.driftc.stage2.mir_lowering_error import MirLoweringError
					raise MirLoweringError(
						f"cannot return reference as owned '{ret_td.name}'; "
						f"returning an owned value from a reference requires "
						f"explicit 'copy <expr>'"
					)
		self._emit_function_exit_cleanup_hook()
		res_val = self.b.new_temp()
		self.b.emit(M.ConstructResultOk(dest=res_val, value=val))
		term = M.Return(value=res_val)
		if ret_span != Span():
			term.span = ret_span
		self.b.set_terminator(term)

	def _visit_stmt_HBreak(self, stmt: H.HBreak) -> None:
		# Break jumps to the innermost loop's break target.
		if not self._loop_stack:
			raise NotImplementedError("break outside of loop not supported yet")
		continue_target, break_target, loop_scope_index, _break_seen = self._loop_stack[-1]
		self._loop_stack[-1] = (continue_target, break_target, loop_scope_index, True)
		# Phase 4 site-1 patch 4b.2: break-point scope drops (body
		# scopes from the break point up to the loop scope) migrated
		# to `M.CleanupHook` + `cleanup_authoring`.  The ledger
		# lattice handles the path-dependent state from conditional
		# breaks via its worklist; the post-authoring ledger rebuild
		# ensures the loop-exit block's downstream consumers see the
		# authored MoveOuts.
		self._emit_scope_cleanup_hook(scope_index=loop_scope_index)
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=break_target))

	def _visit_stmt_HContinue(self, stmt: H.HContinue) -> None:
		# Continue jumps to the innermost loop's continue target (loop header).
		if not self._loop_stack:
			raise NotImplementedError("continue outside of loop not supported yet")
		continue_target, _, loop_scope_index, _break_seen = self._loop_stack[-1]
		# Phase 4 site-1 patch 4b.2: continue-point scope drops
		# migrated to `M.CleanupHook` + `cleanup_authoring`.  Body
		# locals re-initialize on the next iteration via StoreLocal;
		# the lattice transitions each re-init through the per-
		# instruction snapshot in the rebuilt ledger.
		self._emit_scope_cleanup_hook(scope_index=loop_scope_index)
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=continue_target))

	def _visit_stmt_HIf(self, stmt: H.HIf) -> None:
		# If the current block already ended, do nothing.
		if self.b.block.terminator is not None:
			return
		# Constant condition: lower only the reachable branch.
		if isinstance(stmt.cond, H.HLiteralBool):
			if bool(stmt.cond.value):
				self.lower_block(stmt.then_block)
			elif stmt.else_block is not None:
				self.lower_block(stmt.else_block)
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

		# KNOWN LANGUAGE_BUG (Phase 3A Task #5 bucket 6): `_moved_locals`
		# is a function-wide set populated by every `HMove` lowering.
		# Source `move` semantics are path-local, but this set is not
		# — once `move s` lowers anywhere, every subsequent
		# `_emit_scope_drops` skips dropping `s`, including on CFG arms
		# that never executed the move.  Result: latent leaks on the
		# no-move path (e.g. `std.json::_parse_object_throwing.fields`
		# on malformed inputs).  See
		# `work/ownership-ledger/bucket6-known-bug.md` for the full
		# repro + analysis + carrier regressions.
		#
		# Mainline is intentionally NOT patched here.  Two attempted
		# fixes (intersection-with-implicit-else; and a strict
		# fail-stop on disagreeing reaching arms) were each unsound or
		# blocked stdlib compilation respectively — the bug class
		# requires explicit per-program-point representation that only
		# Phase 3C drop-elaboration can provide.  The Phase 3A
		# observational ledger already detects the disagreement; 3C
		# replaces this scope-drop authority entirely.

		# 4) Lower then block.
		self.b.set_block(then_block)
		self.lower_block(stmt.then_block)
		then_terminated = self.b.block.terminator is not None
		if not then_terminated:
			self.b.set_terminator(M.Goto(target=join_block.name))

		# 5) Lower else block if present.
		else_terminated = False
		if else_block is not None:
			self.b.set_block(else_block)
			self.lower_block(stmt.else_block)
			else_terminated = self.b.block.terminator is not None
			if not else_terminated:
				self.b.set_terminator(M.Goto(target=join_block.name))

		# 6) Continue in join block.  If both branches already terminated
		# (return/throw), the join block is unreachable dead code — mark it
		# so that end-of-function analysis does not expect a trailing return.
		self.b.set_block(join_block)
		if then_terminated and else_block is not None and else_terminated:
			self.b.set_terminator(M.Unreachable())

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
		self._loop_stack.append((header.name, exit_block.name, loop_scope_index, False))

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
		_continue_target, _break_target, _scope_idx, break_seen = self._loop_stack.pop()
		self.b.set_block(exit_block)
		if not break_seen and self.b.block.terminator is None:
			self.b.set_terminator(M.Unreachable())

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

		# Phase 4 site-1 patch 6b-B: when throwing a new error from
		# inside a catch arm, the currently-caught error needs release
		# unless the catch arm body has already consumed it.  Pre-6b
		# this was an inline `if not reuses_caught and caught_error
		# not in self._moved_locals: emit MoveOut + DropValue` —
		# `_moved_locals` was the skip authority and `reuses_caught`
		# is unreachable from valid Drift syntax (verified during
		# audit).  Post-6b: emit a focused `M.CleanupHook` carrying
		# `caught_error` as the sole candidate.  `cleanup_authoring`
		# queries `verdict_at(caught_error)` against the post-build
		# ledger:
		#   - LIVE → emit canonical MoveOut + DropValue.
		#   - MOVED_OUT (catch body consumed it) → skip — no
		#     redundant release on already-zeroed storage.
		# Required for the outer-try `Goto(dispatch)` path: this
		# CleanupHook is the ONLY release point for `caught_error`
		# on that path (no function-exit hook fires when the throw
		# routes through `_try_stack[-1].dispatch_block`).
		if self._current_catch_error is not None:
			error_ty = self._type_table.ensure_error()
			scope_id = self._cleanup_hook_counter
			self._cleanup_hook_counter += 1
			self.b.emit(M.CleanupHook(
				scope_id=scope_id,
				candidates=[(self._current_catch_error, error_ty)],
			))
		# If we are inside a try, route to the catch block instead of
		# returning.  Emit captured-frame BEFORE the goto so the frame
		# is observable in the catch arm; the unwind doesn't propagate
		# beyond the current function so _propagate_error won't fire.
		if self._try_stack and self.b.block.terminator is None:
			self._emit_captured_locals(err_val)
			ctx = self._try_stack[-1]
			self.b.ensure_local(ctx.error_local)
			self.b.emit(M.StoreLocal(local=ctx.error_local, value=err_val))
			# LANGUAGE_BUG #102: emit cleanup for every in-flight local
			# in scopes pushed since this try was entered, so the
			# throw-unwind edge drops Destructible locals (e.g. a pool
			# `lease`) even when the dispatch's typed catch arm doesn't
			# match and the throw eventually propagates out of the
			# function.  Without this hook, the body's scopes are popped
			# by `lower_block(body)`'s normal return before the dispatch
			# block is lowered, so the dispatch-else's
			# `_emit_function_exit_cleanup_hook()` cannot see them.  See
			# `lang/tests/memcheck/test_throw_unwind_destructible_drop.py`.
			self._emit_scope_cleanup_hook(scope_index=ctx.scope_index_at_entry)
			self.b.set_terminator(M.Goto(target=ctx.dispatch_block_name))
			return

		# Otherwise, propagate to an outer try if present, or return Err.
		# _propagate_error emits the captured-frame for the current
		# function on EVERY propagation hop (throw site + every outer
		# function's auto-try-then-rethrow), producing innermost-first
		# unwind ordering in `e.context`.
		self._propagate_error(err_val)

	def _propagate_error(self, err_val: M.ValueId) -> None:
		"""
		Propagate an Error value according to current try context:

		  - If there is an outer try on the stack, store into its error_local and
		    jump to its dispatch block (unwind to nearest outer try).
		  - If there is no outer try, the error escapes the current function:
		    wrap into FnResult.Err and return (can-throw ABI).

		Slice 2 DV→JSON: emits the current function's captured-frame
		(if any `^`-locals are active) before the propagation hop,
		producing innermost-first ordering in `e.context`.  Each
		function on the unwind path contributes one frame per call
		invocation (recursive frames are NOT merged).  No-op when no
		captures are active in the current scope.
		"""
		# Emit captured-frame for the current function BEFORE the
		# propagation hop.  The propagation either routes to an outer
		# try (frame is observable when the outer catch reads
		# e.context) or escapes the function via FnResult.Err (frame
		# rides the err_val to wherever it eventually lands).
		self._emit_captured_locals(err_val)

		if self._try_stack:
			ctx = self._try_stack[-1]
			self.b.ensure_local(ctx.error_local)
			self.b.emit(M.StoreLocal(local=ctx.error_local, value=err_val))
			# LANGUAGE_BUG #102 (continued): when propagating to an
			# outer try, drop any in-flight locals declared since the
			# outer try was entered before the Goto.  This covers two
			# distinct call shapes that both land here:
			#   - a dispatch's "no event-arm matches" else propagating
			#     from an inner try's dispatch block to an outer try
			#     (inner body scopes are already popped, outer body
			#     scopes are still live);
			#   - a `rethrow` inside an inner catch arm whose enclosing
			#     try sits inside an outer try.
			# Function-exit propagation (else branch below) is already
			# covered by `_emit_function_exit_cleanup_hook` (scope_index=0).
			self._emit_scope_cleanup_hook(scope_index=ctx.scope_index_at_entry)
			self.b.set_terminator(M.Goto(target=ctx.dispatch_block_name))
		else:
			if self._fn_can_throw() is not True:
				# Nothrow function with no try context: the error cannot propagate
				# via FnResult. Call drift_error_raise (which aborts) so the
				# process terminates cleanly instead of hitting UB.
				self.b.emit(M.ErrorRaise(error=err_val))
				self.b.set_terminator(M.Unreachable())
				return
			self._emit_function_exit_cleanup_hook()
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
		error_ty = self._type_table.ensure_error()
		err_val = self.b.new_temp()
		self.b.emit(M.MoveOut(dest=err_val, local=self._current_catch_error, ty=error_ty))
		self._local_types[err_val] = error_ty
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
				scope_index_at_entry=len(self._scope_stack),
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
				# Consume `error_local` via MoveOut before propagating.
				# `err_tmp` above was a LoadLocal SNAPSHOT used only for
				# the event-code comparison; ownership is still nominally
				# in `error_local`.  The propagation path ahead either
				# stores the value into an outer try's error_local OR
				# wraps it into FnResult.Err via ConstructResultErr — in
				# BOTH cases ownership transfers out, so the function-
				# exit cleanup hook (or any inner-try drop verdict) must
				# see `error_local` as consumed.  Without this MoveOut,
				# the cleanup hook drops the same pointer the caller is
				# about to release, causing UAF / double-free.
				# Reproducer:
				#   pub error A {} pub error B {}
				#   fn inner() -> Int { try { throw A() } catch B(e) { return 999 } }
				#   fn main() nothrow -> Int { return try inner() catch { 0 } }
				# Fixed 2026-05-18.
				consumed_tmp = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=consumed_tmp, local=error_local, ty=error_ty))
				self._local_types[consumed_tmp] = error_ty
				self._propagate_error(consumed_tmp)
		else:
			# No event-specific arms: either jump to catch-all or propagate.
			self.b.set_block(dispatch_block)
			if catch_all_block is not None:
				self.b.set_terminator(M.Goto(target=catch_all_block.name))
			else:
				# Same ownership transfer as the event-arms branch above.
				consumed_tmp = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=consumed_tmp, local=error_local, ty=error_ty))
				self._local_types[consumed_tmp] = error_ty
				self._propagate_error(consumed_tmp)

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
				binder_id = self._find_binder_binding_id(arm.binder, arm.block)
				binder_local = self._canonical_local(binder_id, arm.binder)
				# materialize-audit: allow consumed
				# Explicit MoveOut + DropValue at lines ~8608-8612
				# below handles release at catch-arm exit.  Same
				# pattern as the HTryExpr binder above.
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
				self.b.emit(M.DropValue(value=err_done, ty=error_ty))
			if self.b.block.terminator is None and binder_local is not None:
				binder_done = self.b.new_temp()
				self.b.emit(M.MoveOut(dest=binder_done, local=binder_local, ty=error_ty))
				self._local_types[binder_done] = error_ty
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
			if ty_def.kind is TypeKind.REF and ty_def.param_types:
				subj_ty = ty_def.param_types[0]
				ty_def = self._type_table.get(subj_ty)
			if ty_def.kind is TypeKind.ARRAY and ty_def.param_types:
				return ty_def.param_types[0]

		subj_ty = self._infer_expr_type(subject)
		if subj_ty is None:
			return self._unknown_type
		ty_def = self._type_table.get(subj_ty)
		if ty_def.kind is TypeKind.REF and ty_def.param_types:
			subj_ty = ty_def.param_types[0]
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
			info = self._call_info_by_callsite_id.get(csid)
			if info is None:
				return None
			repaired = self._repair_named_hcall_callinfo(expr, info)
			if repaired is not info:
				self._call_info_by_callsite_id[csid] = repaired
			return repaired
		return None

	def _repair_named_hcall_callinfo(self, expr: H.HExpr, info: CallInfo) -> CallInfo:
		return repair_named_hcall_callinfo(
			expr,
			info,
			self._signatures_by_id,
			verify_target_sig_match=True,
			allow_arity_fallback=True,
			preserve_instantiated_target=True,
			rewrite_sig_on_param_count_mismatch=True,
		)

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
		for issue in call_contract_issues(expr, info):
			if issue.code == "E_CALLINFO_METHOD_CONSTRUCTOR_TARGET":
				raise AssertionError(
					f"method call has constructor CallTarget for callsite_id={getattr(expr, 'callsite_id', None)} (typecheck/call-info bug)"
				)
		return info

	def _call_info_for_invoke(self, expr: H.HInvoke) -> CallInfo:
		info = self._call_info_for_expr_optional(expr)
		if info is None:
			raise AssertionError(
				f"missing call info for HInvoke callsite_id={getattr(expr, 'callsite_id', None)} (typecheck/call-info bug)"
			)
		for issue in call_contract_issues(expr, info):
			if issue.code == "E_CALLINFO_INVOKE_TARGET_KIND":
				raise AssertionError(
					f"invoke callsite_id={getattr(expr, 'callsite_id', None)} requires INDIRECT CallTarget (typecheck/call-info bug)"
				)
			if issue.code == "E_CALLINFO_INVOKE_INCLUDES_CALLEE":
				raise AssertionError(
					f"invoke callsite_id={getattr(expr, 'callsite_id', None)} must not set includes_callee in CallSig (typecheck/call-info bug)"
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
		Lower a call argument.

		`&T` / `&mut T` params auto-borrow (delegated to `lower_expr`).

		For owned by-value non-`Copy` params, Drift's
		explicit-ownership-transfer contract (spec §1.3) requires the
		SOURCE to write `move x` — a bare named non-`Copy` owner at a
		by-value call arg is a compile error.  That rule is enforced
		at TYPE-CHECK time by
		`type_checker.py::_check_explicit_move_required_at_call_arg`,
		wired into `_check_call_expr_boundaries` for EVERY call shape
		whose args reach this method at lowering:
		  - direct free-function HCall (positional + keyword);
		  - HMethodCall declared-method non-receiver args
		    (positional + keyword);
		  - HMethodCall intrinsic-fallback non-receiver args
		    (positional only — method keyword args are non-v1 and
		    rejected earlier, so the intrinsic fallback has no
		    keyword path to gate);
		  - HMethodCall INDIRECT-target non-receiver args —
		    interface-method dispatch (`t.take(x)` where `t` is an
		    interface value, routed via `_lower_iface_call`) and
		    indirect method dispatch (positional);
		  - INDIRECT-target HCall — function-VALUE calls `f(x)`
		    where `f` is a function value (positional + keyword,
		    routed here via `_lower_indirect_call`);
		  - CONSTRUCTOR-target HCall — struct + variant constructor
		    field args (positional + keyword), routed here via
		    `_lower_constructor_call`.  Spec §1.3 names constructors
		    explicitly: a bare named non-`Copy` owner is never
		    silently transferred into a field;
		  - HInvoke (callable-value invocation node; positional +
		    keyword).
		The gate runs BEFORE this lowering, so a user-source bare
		HVar of a non-`Copy` local never reaches this method through
		any of those shapes — it is rejected with the friendly
		`cannot copy 'x' ... use move x` diagnostic and the binary
		never builds.  (The method receiver slot is intentionally
		NOT gated; value-receiver implicit consume is spec §3.8.1.)

		The `MoveOut(local=...)` branch below is therefore an INTERNAL
		BACKSTOP, not the user-facing semantic.  It still fires for
		compiler-synthesized / normalized HVar args that legitimately
		represent an owned transfer at this slot (e.g. desugarings
		that produce a bare-HVar arg the checker accepted), keeping
		the MIR well-formed (non-`Copy` by-value args must originate
		from a `MoveOut`, per `validate_mir_call_byvalue_moves`).  It
		is NOT the place that decides a bare user HVar is a move — the
		type checker owns that decision.
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
					if not self._should_copy_value(param_ty):
						arg_ty = param_ty
				if arg_ty is not None:
					if not self._should_copy_value(arg_ty):
						# Internal backstop (see docstring): the
						# type checker has already rejected any
						# user-source bare non-Copy owner here.
						subj_name = self._canonical_local(getattr(base, "binding_id", None), base.name)
						self.b.ensure_local(subj_name)
						moved_val = self.b.new_temp()
						self.b.emit(M.MoveOut(dest=moved_val, local=subj_name, ty=arg_ty))
						self._local_types[moved_val] = arg_ty
						return moved_val
		val = self.lower_expr(arg, expected_type=param_ty)
		# If the arg was an aliased ref-field temp, deep-copy before passing
		# ownership to the callee (struct/variant ctor, function call).
		arg_ty = param_ty or self._infer_expr_type(arg)
		if arg_ty is not None:
			val = self._copy_if_ref_alias(val, arg_ty)
		return val

	def _lower_constructor_call(self, expr: H.HCall, info: CallInfo) -> M.ValueId:
		variant_ty = info.target.variant_type_id
		struct_ty = info.target.struct_type_id
		ctor_name = info.target.ctor_name
		ctor_arg_field_indices = info.target.ctor_arg_field_indices
		pos_args = list(expr.args)
		kw_pairs = list(getattr(expr, "kwargs", []) or [])
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
		_ctor_issues = ctor_call_issues(len(pos_args), tuple(kw.name for kw in kw_pairs), CtorFieldSpec(field_names=tuple(field_names)), ctor_label="constructor", span=getattr(expr, "loc", None))
		if _ctor_issues:
			raise AssertionError(f"{_ctor_issues[0].message} reached MIR lowering")
		if kw_pairs:
			for kw in kw_pairs:
				field_idx = field_names.index(kw.name)
				field_ty = field_types[field_idx]
				arg_val = self._lower_call_arg(kw.value, field_ty)
				if arg_val is None:
					if self._type_table.is_void(field_ty):
						ordered[field_idx] = None
						continue
					raise AssertionError("Void-returning call used in expression context (checker bug)")
				ordered[field_idx] = arg_val
		if ctor_arg_field_indices is not None:
			if len(pos_args) != len(ctor_arg_field_indices):
				raise AssertionError("constructor arg mapping arity mismatch reached MIR lowering (checker bug)")
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
		arg_vals: list[M.ValueId] = []
		for idx, arg in enumerate(args):
			param_ty = info.sig.param_types[idx] if idx < len(info.sig.param_types) else None
			arg_vals.append(self._lower_call_arg(arg, param_ty))
		param_types = list(info.sig.param_types)
		iface_ty = self._infer_expr_type(iface_expr)
		if iface_ty is None:
			raise AssertionError("interface call missing receiver type (checker bug)")
		# Borrowed interface receivers (&Interface, &mut Interface): unwrap
		# to the inner INTERFACE for vtable / interface-instance lookup.
		# The lowered iface_val is a pointer to the fat pointer; LLVM
		# `_lower_call_iface` loads through it on the way to dispatch.
		iface_ty_def = self._type_table.get(iface_ty)
		if iface_ty_def.kind is TypeKind.REF and iface_ty_def.param_types:
			iface_ty = iface_ty_def.param_types[0]
			iface_ty_def = self._type_table.get(iface_ty)
		if iface_ty_def.kind is not TypeKind.INTERFACE:
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
		if info.sig.can_throw:
			dest = self.b.new_temp()
			self.b.emit(
				M.CallIface(
					dest=dest,
					iface=iface_val,
					args=arg_vals,
					param_types=param_types,
					user_ret_type=info.sig.user_ret_type,
					can_throw=True,
					slot_index=slot_index,
				)
			)
			self._local_types[dest] = call_abi_ret_type(info.sig, self._type_table)
			return dest
		if self._type_table.is_void(info.sig.user_ret_type):
			self.b.emit(
				M.CallIface(
					dest=None,
					iface=iface_val,
					args=arg_vals,
					param_types=param_types,
					user_ret_type=info.sig.user_ret_type,
					can_throw=False,
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
				can_throw=False,
				slot_index=slot_index,
			)
		)
		self._local_types[dest] = info.sig.user_ret_type
		return dest

	def _find_free_fn_id(self, module_name: str, fn_name: str) -> "FunctionId | None":
		"""Locate a free function's FunctionId by module + name.

		Used by the Arc runtime boundary in
		`_lower_method_call_with_info` to resolve the `_arc_*_impl<T>`
		helpers the Stage 2 intrinsic redirect points at.  Linear scan
		over the signatures table; the helpers are stable named
		symbols so this runs at most once per intrinsic-method call
		site and the result is not cached — if that becomes a
		bottleneck, promote to a lazy-built name index.
		"""
		for fn_id, sig in self._signatures_by_id.items():
			if fn_id.module != module_name:
				continue
			if fn_id.name != fn_name:
				continue
			if bool(getattr(sig, "is_method", False)):
				continue
			return fn_id
		return None

	def _lower_arc_intrinsic_call(
		self,
		expr: H.HMethodCall,
		info: CallInfo,
		kind: IntrinsicKind,
	) -> M.ValueId | None:
		"""Lower `INTRINSIC(ARC_CLONE|ARC_GET|ARC_DESTROY)` method call.

		The Arc intrinsic method has no body.  Stage 2 ships the
		concrete-T implementation in `_arc_*_impl<T>` free functions.
		`compile_stubbed_funcs` queues a helper instantiation for each
		concrete Arc<T> call site and publishes
		`type_table.arc_helper_inst_fn_by_callsite[(caller_fn_id, csid)]`
		→ the monomorphized helper's FunctionId.  We look that up here
		and emit a plain `M.Call` with the receiver as arg[0], bypassing
		the method-call self_mode flow (the helper is a free function,
		not a method).

		Receiver mode comes from the helper's instantiated first-param
		type — a `Ref<Arc<T>>` triggers auto-borrow; an owned `Arc<T>`
		triggers by-value lowering (ARC_DESTROY).  This keeps the
		bridge honest: the runtime call shape matches the helper's
		declared signature exactly.
		"""
		# Stage 3 slice 2 — detect fat Arc<I> receiver and dispatch
		# to fat lowering.  Detection uses the **layout** predicate
		# `is_arc_fat_layout_instance` (NOT the semantic
		# `is_arc_interface_view_instance`): the layout predicate
		# reflects the live struct-instance shape, not the semantic
		# "Arc<I>" identity.  This distinction matters for
		# package-consumer builds replaying pre-ABI-10 artifacts that
		# still serialize the thin `{buf}` shape — the fat lowering
		# extracts `ctrl`/`data`/`vtable` fields that only exist on
		# the live ABI-10 layout; running it against a thin instance
		# would probe fields that do not exist.
		_fat_dispatch_ty: TypeId | None = None
		_recv_ty_probe = self._infer_expr_type(expr.receiver)
		if _recv_ty_probe is not None:
			_recv_def_probe = self._type_table.get(_recv_ty_probe)
			_inner_recv_ty = (
				_recv_def_probe.param_types[0]
				if _recv_def_probe.kind is TypeKind.REF and _recv_def_probe.param_types
				else _recv_ty_probe
			)
			if self._type_table.is_arc_fat_layout_instance(_inner_recv_ty):
				_fat_dispatch_ty = _inner_recv_ty
		if _fat_dispatch_ty is not None:
			return self._lower_arc_fat_intrinsic_call(expr, info, kind, _fat_dispatch_ty)
		csid = getattr(expr, "callsite_id", None)
		helper_map = getattr(self._type_table, "arc_helper_inst_fn_by_callsite", None) or {}
		helper_fn_id = helper_map.get((self._current_fn_id, csid)) if csid is not None else None
		if helper_fn_id is None:
			raise AssertionError(
				f"Arc intrinsic {kind} at callsite {csid} in "
				f"{function_symbol(self._current_fn_id) if self._current_fn_id else '?'} "
				f"has no helper instantiation recorded (Stage 2 bridge bug: "
				f"`_queue_instantiations` did not see this call site's "
				f"instantiation record, or the Arc method call resolution "
				f"path did not invoke `record_instantiation`)"
			)
		helper_sig = self._signatures_by_id.get(helper_fn_id)
		if helper_sig is None or helper_sig.param_type_ids is None or helper_sig.return_type_id is None:
			raise AssertionError(
				f"Arc intrinsic helper {function_symbol(helper_fn_id)} has no signature "
				f"(instantiation drain bug)"
			)
		helper_param_types = list(helper_sig.param_type_ids)
		if not helper_param_types:
			raise AssertionError(
				f"Arc intrinsic helper {function_symbol(helper_fn_id)} missing receiver parameter"
			)
		# Determine receiver mode from the helper's first param type.
		recv_param_ty = helper_param_types[0]
		recv_param_def = self._type_table.get(recv_param_ty)
		wants_borrow = recv_param_def.kind is TypeKind.REF
		is_mut_borrow = wants_borrow and recv_param_def.param_types and (
			# ARC_CLONE/GET both take `&Arc<T>` (shared borrow).  If a
			# future Arc intrinsic needs `&mut`, detect it via the Ref's
			# mutability flag, which we do not track here in v1; keep
			# borrows shared for now.
			False
		)
		recv_val: M.ValueId
		if not wants_borrow:
			# By-value receiver (ARC_DESTROY).  Use the normal call-arg
			# lowering so move semantics apply (the helper is declared
			# `var self: Arc<T>` and takes ownership for drop).
			recv_val = self._lower_call_arg(expr.receiver, recv_param_ty)
		else:
			# Borrowed receiver (ARC_CLONE/ARC_GET: `&Arc<T>`).
			# If the HIR receiver is already a REF, pass directly;
			# otherwise auto-borrow the receiver place.
			recv_ty = self._infer_expr_type(expr.receiver)
			recv_def = self._type_table.get(recv_ty) if recv_ty is not None else None
			if recv_def is not None and recv_def.kind is TypeKind.REF:
				recv_val = self.lower_expr(expr.receiver)
			else:
				place_expr = None
				if hasattr(H, "HPlaceExpr") and isinstance(expr.receiver, getattr(H, "HPlaceExpr")):
					place_expr = expr.receiver
				elif isinstance(expr.receiver, H.HVar):
					place_expr = H.HPlaceExpr(base=expr.receiver, projections=[], loc=Span())
				if place_expr is None:
					raise NotImplementedError(
						"Arc intrinsic auto-borrow requires an lvalue receiver in v1"
					)
				recv_val, _inner = self._lower_addr_of_place(place_expr, is_mut=is_mut_borrow)
		arg_vals: list[M.ValueId] = [recv_val]
		for idx, arg in enumerate(expr.args):
			param_ty = helper_param_types[idx + 1] if idx + 1 < len(helper_param_types) else None
			arg_vals.append(self._lower_call_arg(arg, param_ty))
		ret_ty = helper_sig.return_type_id
		# All Arc intrinsic helpers are `nothrow`.  The helper's own
		# `declared_can_throw` is False; asserting here would mask an
		# unrelated bug, so trust the sig.
		if self._type_table.is_void(ret_ty):
			self.b.emit(M.Call(dest=None, fn_id=helper_fn_id, args=arg_vals, can_throw=False))
			return None
		dest = self.b.new_temp()
		self.b.emit(M.Call(dest=dest, fn_id=helper_fn_id, args=arg_vals, can_throw=False))
		self._local_types[dest] = ret_ty
		return dest

	def _lower_arc_fat_intrinsic_call(
		self,
		expr: H.HMethodCall,
		info: CallInfo,
		kind: IntrinsicKind,
		arc_iface_ty: TypeId,
	) -> M.ValueId | None:
		"""Lower `INTRINSIC(ARC_CLONE|ARC_GET|ARC_DESTROY)` for a fat
		`Arc<I>` receiver.

		Fat `Arc<I>` layout is `{ctrl, data, vtable}` (all
		`mem.Ptr<Byte>`).  Refcount and drop are I-erased — a single
		pair of Slice 1 runtime helpers
		(`_arc_fat_bump_strong_via_ctrl` and `_arc_fat_drop_via_ctrl`)
		operating only on `ctrl` serves every `Arc<I>` instance.

		**Dormancy contract (Slice 2):** this method is reached ONLY
		when `is_arc_fat_layout_instance(recv_ty)` returns True for
		the receiver — the **layout** predicate, which inspects the
		live struct-instance shape (field names must be exactly
		`("ctrl", "data", "vtable")`).  This is distinct from the
		semantic `is_arc_interface_view_instance` predicate, which
		only reports the "Arc<I>" identity and returns True
		regardless of whether layout specialization has fired.
		Slice 2 keeps the layout specialization branch in
		`ensure_struct_instantiated` disabled, so every live
		`Arc<I>` still has the thin `{buf}` shape, the layout
		predicate returns False, and this method is unreachable at
		runtime.  Slice 3 flips the layout branch and this path
		becomes live alongside `ARC_AS_INTERFACE` lowering.

		**No Arc-specific vtable namespace.**  `.get()` constructs
		the borrowed interface shape using the same runtime vtable
		symbols the existing `IfaceUpcast` / `CallIface` machinery
		already emits for `&Interface` references; no parallel
		Arc-only vtable path is introduced.
		"""
		# Receiver borrow mode is determined by the intrinsic kind,
		# not by any helper sig — the fat path does not dispatch
		# through a per-T helper template.  ARC_CLONE and ARC_GET
		# want `&Arc<I>`; ARC_DESTROY takes `Arc<I>` by value.
		wants_borrow = kind in (IntrinsicKind.ARC_CLONE, IntrinsicKind.ARC_GET)
		recv_val: M.ValueId
		if wants_borrow:
			recv_ty = self._infer_expr_type(expr.receiver)
			if recv_ty is None:
				raise AssertionError(
					"fat Arc intrinsic receiver type unknown in MIR lowering "
					"(typecheck/inference bug)"
				)
			recv_def = self._type_table.get(recv_ty)
			if recv_def.kind is TypeKind.REF:
				recv_val = self.lower_expr(expr.receiver)
			else:
				place_expr = None
				if hasattr(H, "HPlaceExpr") and isinstance(expr.receiver, getattr(H, "HPlaceExpr")):
					place_expr = expr.receiver
				elif isinstance(expr.receiver, H.HVar):
					place_expr = H.HPlaceExpr(base=expr.receiver, projections=[], loc=Span())
				if place_expr is not None:
					recv_val, _inner = self._lower_addr_of_place(place_expr, is_mut=False)
				else:
					# Rvalue receiver (chained shape like
					# `app.as_interface<I>().clone()` or
					# `app.as_interface<I>().get().method()`) —
					# materialize a `__borrow_tmp<N>` local of type
					# `Arc<I>`, store the rvalue into it, take its
					# address.  Mirrors `_visit_expr_HBorrow`'s
					# shared-borrow rvalue materialization (the thin
					# Arc chained-rvalue tests rely on that same
					# pattern via `&` on the implicit receiver).  The
					# This branch handles fat `Arc<I>` receivers of
					# `.clone()` / `.get()` (ARC_CLONE, ARC_GET) —
					# e.g. `app.as_interface<type I>().clone()` or
					# `app.as_interface<type I>().get().m()`, where
					# the `.as_interface<I>()` result is a rvalue
					# fat Arc<I>.  Call `_register_drop_local` as
					# well as `_local_types[...] = ty`: the latter
					# records typing only, the former adds the
					# local to the scope's drop set that
					# `_emit_scope_drops` iterates at scope exit.
					# Today upstream normalization appears to
					# materialize rvalue method receivers into
					# proper locals before this branch is hit (the
					# chained-rvalue tests stay green when this
					# registration is removed), so this hardening
					# is defensive.  It remains because relying on
					# upstream normalization to cover every future
					# shape is fragile — any normalization change
					# that routes a fat-Arc rvalue through this
					# branch would silently leak without the
					# scope-drop registration.
					# Materialize the rvalue fat-Arc<I> receiver via the
					# canonical helper -- preserves the always-together
					# alloc + type + register-drop + store invariant.
					# Lazy `value` callable keeps lower_expr's temp
					# allocations sequenced after the slot id (matches
					# the original counter ordering, alpha-equivalent IR).
					recv_val = self._materialize_owned_temp_for_borrow(
						ty=recv_ty,
						value=lambda: self.lower_expr(expr.receiver),
						is_mut=False,
					)
					self._local_types[recv_val] = self._type_table.new_ref(recv_ty, is_mut=False)
		else:
			recv_val = self._lower_call_arg(expr.receiver, arc_iface_ty)

		# Field indices into the fat {ctrl, data, vtable} layout.
		# Must stay synchronized with
		# `TypeTable._arc_interface_view_layout`.
		_FAT_CTRL_IDX = 0
		_FAT_DATA_IDX = 1
		_FAT_VTABLE_IDX = 2
		byte_ty = self._type_table.ensure_byte()
		ptr_byte_ty = self._type_table.new_ptr(byte_ty)

		def _extract_fat_field(owner_val: M.ValueId, field_idx: int) -> M.ValueId:
			"""Load a fat Arc<I> field from either a by-value struct or a
			borrowed struct pointer.  Emits `LoadRef(AddrOfField)` when
			the receiver is a pointer; `StructGetField` when by-value."""
			dest = self.b.new_temp()
			if wants_borrow:
				# owner_val is a pointer to the Arc<I> struct.
				addr = self.b.new_temp()
				self.b.emit(
					M.AddrOfField(
						dest=addr,
						base_ptr=owner_val,
						struct_ty=arc_iface_ty,
						field_index=field_idx,
						field_ty=ptr_byte_ty,
					)
				)
				self._local_types[addr] = self._type_table.new_ptr(ptr_byte_ty)
				self.b.emit(M.LoadRef(dest=dest, ptr=addr, inner_ty=ptr_byte_ty))
			else:
				self.b.emit(
					M.StructGetField(
						dest=dest,
						subject=owner_val,
						struct_ty=arc_iface_ty,
						field_index=field_idx,
						field_ty=ptr_byte_ty,
					)
				)
			self._local_types[dest] = ptr_byte_ty
			return dest

		if kind is IntrinsicKind.ARC_CLONE:
			# Extract all three fat fields so we can reconstruct the
			# new Arc<I> with the identical triple.  Bumping the
			# strong count via the Slice 1 helper is the only side
			# effect.
			ctrl_v = _extract_fat_field(recv_val, _FAT_CTRL_IDX)
			data_v = _extract_fat_field(recv_val, _FAT_DATA_IDX)
			vtbl_v = _extract_fat_field(recv_val, _FAT_VTABLE_IDX)
			bump_fn = self._find_free_fn_id(
				"std.core.arc", "_arc_fat_bump_strong_via_ctrl"
			)
			if bump_fn is None:
				raise AssertionError(
					"Slice 1 helper `_arc_fat_bump_strong_via_ctrl` not "
					"found in signatures — the fat ARC_CLONE path requires "
					"the non-generic refcount-bump helper declared in "
					"stdlib/std/core/arc.drift"
				)
			self.b.emit(M.Call(dest=None, fn_id=bump_fn, args=[ctrl_v], can_throw=False))
			dest = self.b.new_temp()
			self.b.emit(
				M.ConstructStruct(
					dest=dest,
					struct_ty=arc_iface_ty,
					args=[ctrl_v, data_v, vtbl_v],
				)
			)
			self._local_types[dest] = arc_iface_ty
			return dest

		if kind is IntrinsicKind.ARC_DESTROY:
			# By-value receiver; ctrl is the first field.  Drop via
			# the Slice 1 helper — null-guarded atomic fetch-sub
			# plus drop_thunk on last drop.
			ctrl_v = _extract_fat_field(recv_val, _FAT_CTRL_IDX)
			drop_fn = self._find_free_fn_id(
				"std.core.arc", "_arc_fat_drop_via_ctrl"
			)
			if drop_fn is None:
				raise AssertionError(
					"Slice 1 helper `_arc_fat_drop_via_ctrl` not found "
					"in signatures — the fat ARC_DESTROY path requires "
					"the non-generic atomic-drop helper declared in "
					"stdlib/std/core/arc.drift"
				)
			self.b.emit(M.Call(dest=None, fn_id=drop_fn, args=[ctrl_v], can_throw=False))
			return None

		if kind is IntrinsicKind.ARC_GET:
			# `.get()` returns `&I` — the borrowed interface fat
			# reference.  Recv is `&Arc<I>` (already a ptr to the
			# fat struct).  Emit `M.ArcFatGet`; LLVM codegen extracts
			# the {data, vtable} pair and writes it into a fresh
			# alloca'd DRIFT_IFACE_TYPE slot, returning the alloca
			# ptr as the borrowed `&I`.  No refcount touch; no new
			# vtable lookup (vtable was resolved at
			# ARC_AS_INTERFACE time and carried in the fat handle).
			iface_ty = arc_iface_ty  # Arc<I>'s struct id is the
			# receiver type; iface_ty for codegen is the single
			# type arg of that Arc<I>.  Read it from the instance.
			arc_inst = self._type_table.get_struct_instance(arc_iface_ty)
			if arc_inst is None or not arc_inst.type_args:
				raise AssertionError(
					"fat ARC_GET: receiver is not an instantiated "
					f"Arc<I> (got TypeId={arc_iface_ty})"
				)
			iface_ty = arc_inst.type_args[0]
			# Result `&I` — REF to I.
			result_ref_ty = self._type_table.new_ref(iface_ty, is_mut=False)
			dest = self.b.new_temp()
			self.b.emit(
				M.ArcFatGet(
					dest=dest,
					src_arc_ref=recv_val,
					src_arc_ty=arc_iface_ty,
					iface_ty=iface_ty,
					result_ref_ty=result_ref_ty,
				)
			)
			self._local_types[dest] = result_ref_ty
			return dest

		raise AssertionError(
			f"unexpected fat Arc intrinsic kind {kind} reached "
			f"`_lower_arc_fat_intrinsic_call` — only ARC_CLONE, ARC_GET, "
			f"and ARC_DESTROY route here; ARC_AS_INTERFACE has its own "
			f"(thin-receiver) lowering slot"
		)

	def _lower_arc_as_interface_op(
		self, expr: H.HMethodCall, info: CallInfo
	) -> M.ValueId:
		"""Lower `INTRINSIC(ARC_AS_INTERFACE)` to the dedicated
		`M.ArcAsInterface` MIR op.

		The receiver is a thin `&Arc<T=concrete>` (auto-borrowed from
		`Arc<T>` if needed).  Result is a fat `Arc<I>`.  LLVM codegen
		in `_lower_arc_as_interface` does the ownership/view
		conversion (one allocation, one atomic strong bump, data
		pointer into concrete ArcBox<T>, vtable via
		`_ensure_interface_vtable(I, T)`).

		This lowering is the only site in Stage 3 where we need BOTH
		the concrete T (to compute `ArcBox<T>` layout) and the
		target I (to resolve the vtable).  Both come from the
		receiver/result types inferred by the type checker: T is the
		sole type arg of the receiver's `Arc<T>`, I is the sole type
		arg of the result's `Arc<I>`.
		"""
		# Receiver: must be `&Arc<T>` (ptr to thin Arc<T>).  Borrow
		# if the source is owned.
		recv_ty = self._infer_expr_type(expr.receiver)
		if recv_ty is None:
			raise AssertionError(
				"ARC_AS_INTERFACE: receiver type unknown in MIR lowering "
				"(typecheck/inference bug)"
			)
		recv_def = self._type_table.get(recv_ty)
		if recv_def.kind is TypeKind.REF and recv_def.param_types:
			recv_val = self.lower_expr(expr.receiver)
			src_arc_ty = recv_def.param_types[0]
		else:
			place_expr = None
			if hasattr(H, "HPlaceExpr") and isinstance(expr.receiver, getattr(H, "HPlaceExpr")):
				place_expr = expr.receiver
			elif isinstance(expr.receiver, H.HVar):
				place_expr = H.HPlaceExpr(base=expr.receiver, projections=[], loc=Span())
			if place_expr is None:
				# Rvalue receiver (e.g. a chained shape like
				# `arc(concrete).as_interface<I>()` when the
				# `conc.arc(...)` call hasn't been normalized into
				# a local upstream) — materialize a borrowed temp.
				# Call both `_local_types[...] = ty` and
				# `_register_drop_local`: typing alone does not add
				# the local to the scope's drop set that
				# `_emit_scope_drops` iterates.  Today upstream
				# normalization appears to lift rvalue
				# `conc.arc(...)` calls into proper locals before
				# this branch fires (confirmed by removing this
				# registration and seeing valgrind stay clean on
				# the minimal std.log shape), so this is defensive
				# hardening rather than a currently-load-bearing
				# fix.  Kept to guard against future normalization
				# changes that may stop covering every shape.
				# Materialize the rvalue Arc<T> receiver via the
				# canonical helper -- same shape as the fat-Arc
				# (`_lower_arc_fat_intrinsic_call`) and HBorrow
				# rvalue fallback sites.
				recv_val = self._materialize_owned_temp_for_borrow(
					ty=recv_ty,
					value=lambda: self.lower_expr(expr.receiver),
					is_mut=False,
				)
				self._local_types[recv_val] = self._type_table.new_ref(recv_ty, is_mut=False)
				src_arc_ty = recv_ty
			else:
				recv_val, _inner = self._lower_addr_of_place(place_expr, is_mut=False)
				src_arc_ty = recv_ty

		# T = src Arc<T>'s single type argument.
		src_arc_inst = self._type_table.get_struct_instance(src_arc_ty)
		if src_arc_inst is None or not src_arc_inst.type_args:
			raise AssertionError(
				"ARC_AS_INTERFACE: receiver type is not an instantiated "
				f"Arc<T> (got TypeId={src_arc_ty})"
			)
		concrete_ty = src_arc_inst.type_args[0]

		# Result type: Arc<I> fat struct, read from the type checker's
		# expression-type map (method call node carries the
		# instantiated result type).
		result_ty = self._expr_types.get(expr.node_id) if self._expr_types else None
		if result_ty is None:
			raise AssertionError(
				"ARC_AS_INTERFACE: result type unknown — type checker did "
				"not record an instantiated Arc<I> for this method call"
			)
		result_inst = self._type_table.get_struct_instance(result_ty)
		if result_inst is None or not result_inst.type_args:
			raise AssertionError(
				"ARC_AS_INTERFACE: result type is not an instantiated "
				f"Arc<I> (got TypeId={result_ty})"
			)
		iface_ty = result_inst.type_args[0]

		dest = self.b.new_temp()
		self.b.emit(
			M.ArcAsInterface(
				dest=dest,
				src_arc_ref=recv_val,
				src_arc_ty=src_arc_ty,
				concrete_ty=concrete_ty,
				iface_ty=iface_ty,
				result_ty=result_ty,
			)
		)
		self._local_types[dest] = result_ty
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
		_kw_issues = call_kwargs_issues("method calls", getattr(expr, "kwargs", None))
		if _kw_issues:
			raise AssertionError(f"{_kw_issues[0].message} reached MIR lowering")

		if info.target.kind is CallTargetKind.INDIRECT:
			recv_ty = self._infer_expr_type(expr.receiver)
			if recv_ty is None and self._expr_types and getattr(expr.receiver, "node_id", None) is not None:
				recv_ty = self._expr_types.get(expr.receiver.node_id)
			if recv_ty is not None:
				recv_def = self._type_table.get(recv_ty)
				if recv_def.kind is TypeKind.INTERFACE:
					return self._lower_iface_call(expr.receiver, expr.args, expr.method_name, info), info
				# Borrowed interface receiver (&Interface, &mut Interface):
				# dispatch through the vtable of the inner interface; the
				# CallIface lowering at lang/codegen/llvm/llvm_codegen.py
				# (_lower_call_iface) detects the pointer-to-fat-pointer
				# argument and loads the fat pointer through it before the
				# vtable lookup.  No retain/release needed.
				if recv_def.kind is TypeKind.REF and recv_def.param_types:
					inner_def = self._type_table.get(recv_def.param_types[0])
					if inner_def.kind is TypeKind.INTERFACE:
						return self._lower_iface_call(expr.receiver, expr.args, expr.method_name, info), info
			return self._lower_indirect_call(expr.receiver, expr.args, info), info
		# Arc runtime boundary — concrete-T method-call redirect.
		#
		# `Arc<T>.clone` / `.get` / `::Destructible::destroy` are
		# `@intrinsic` methods in stdlib; the checker rewrites the
		# call target from DIRECT(intrinsic-method-fn_id) to
		# INTRINSIC(ARC_CLONE|ARC_GET|ARC_DESTROY).  Here we route
		# each kind to its private `_arc_*_impl<T>` helper
		# (stdlib/std/concurrent/concurrent.drift).  The helper
		# carries the concrete-T implementation (see `doc/history.md`
		# 2026-04-18, fat `Arc<Interface>` 0.28.0/ABI 10).
		#
		# ARC_AS_INTERFACE is intentionally NOT redirected here —
		# its runtime lowering ships in Stage 3.  Stage 2 only gates
		# it at compile time via `require T is I`; positive calls
		# would still hit the assert below (acceptable since no
		# stdlib or test exercises a positive as_interface call).
		if info.target.kind is CallTargetKind.INTRINSIC:
			_arc_intrinsic = info.target.intrinsic
			_ARC_HELPER_KINDS = (
				IntrinsicKind.ARC_CLONE,
				IntrinsicKind.ARC_GET,
				IntrinsicKind.ARC_DESTROY,
			)
			if _arc_intrinsic in _ARC_HELPER_KINDS:
				# Lower as a free-function call to the per-callsite
				# monomorphized helper.  We bypass the method-call
				# self_mode path because the helper is a free
				# function; its first parameter's type (Ref<Arc<T>>
				# for clone/get, Arc<T> for destroy) is the source
				# of truth for how we pass the receiver.
				return self._lower_arc_intrinsic_call(expr, info, _arc_intrinsic), info
			if _arc_intrinsic is IntrinsicKind.ARC_AS_INTERFACE:
				# Stage 3 activation gate: emit the real
				# `M.ArcAsInterface` lowering only once the fat
				# `Arc<I>` layout has actually been flipped on in
				# `types_core.STAGE3_FAT_ARC_ACTIVE`.  Without
				# that flip, `Arc<I>` instances are still thin
				# `{buf}` and constructing the `{ctrl, data,
				# vtable}` triple would produce a value whose
				# layout disagrees with the sink type — exactly
				# the half-live hazard this gate forbids.  Until
				# the Slice 3 commit lands the coupled flag flip
				# + stdlib migration + rejection together, keep
				# the Stage-2 placeholder assertion.
				from lang.driftc.core.types_core import STAGE3_FAT_ARC_ACTIVE
				if STAGE3_FAT_ARC_ACTIVE:
					return self._lower_arc_as_interface_op(expr, info), info
				raise AssertionError(
					f"Arc.as_interface<I>() runtime lowering is not yet "
					f"activated (Stage 3 WIP; flag STAGE3_FAT_ARC_ACTIVE "
					f"is False); callsite="
					f"{getattr(expr, 'callsite_id', None)}"
				)
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
			elif self._is_synthesized_error_params_place(expr.receiver):
				# Slice 1 DV→JSON: `<error>.params.encode_compact()` — the
				# receiver `<error>.params` is a synthesized ErrorParamsView
				# value (no real `params` field on Error).  Build the value
				# inline (ExcGetParamsJson + ConstructStruct), materialize
				# to a named local, then take its address for the
				# `&ErrorParamsView` self argument.
				#
				# LANGUAGE_BUG follow-up (Cluster 1, 2026-05-06): the
				# synthesized View struct holds a refcount-retained
				# `String` (the params JSON pulled from the runtime via
				# `ExcGetParamsJson`).  Without registering the
				# materialized local on the current scope's drop list,
				# the cleanup-authoring pass never sees it and the
				# scope-exit drop for the View's String field is
				# missing — leaks one `params_json` allocation per
				# catch arm that reads `e.params`.  Pinned by
				# `lang/tests/memcheck/test_pub_error_params_view_drop.py::
				# test_pub_error_params_encode_compact_no_leak` and the
				# 7 e2e fixtures listed in the bug report.
				# View value is computed eagerly (already a ValueId
				# by the time we get here), so the eager value form
				# of the helper applies.  Helper sequence:
				# alloc + type + register-drop + store; AddrOfLocal
				# emitted separately by this site.  Register-drop now
				# fires BEFORE StoreLocal (the helper's invariant)
				# rather than AFTER (the pre-conversion order); this
				# is a scope-stack append order shift only, and
				# `_register_drop_local`'s semantics are
				# StoreLocal-independent so it's safe.  Pinned by
				# `test_pub_error_params_view_drop` family.
				view_val = self._lower_synthesized_error_params_view(expr.receiver, recv_ty)
				view_local = self._materialize_owned_temp(
					name_prefix="__exc_params_view_",
					ty=recv_ty,
					value=view_val,
				)
				addr_dest = self.b.new_temp()
				self.b.emit(M.AddrOfLocal(dest=addr_dest, local=view_local, is_mut=False))
				receiver_arg = addr_dest
			else:
				# Auto-borrow from an lvalue receiver. We support `HVar` and canonical
				# `HPlaceExpr` receivers; other receiver expressions are not addressable
				# in v1.
				place_expr = None
				if hasattr(H, "HPlaceExpr") and isinstance(expr.receiver, getattr(H, "HPlaceExpr")):
					place_expr = expr.receiver
				elif isinstance(expr.receiver, H.HVar):
					place_expr = H.HPlaceExpr(base=expr.receiver, projections=[], loc=Span())
				if place_expr is None:
					raise NotImplementedError("method auto-borrow requires an lvalue receiver in v1")
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
			# Slice 2 DV→JSON: emit the current function's captured-frame
			# (if any) BEFORE the local-catch goto.  Symmetric with the
			# direct-throw lowering at the throw-statement site; without
			# this, can-throw call errors that get caught by a local try
			# silently drop the outer function's `^` captures (asymmetric
			# with direct-throw inside the same try).
			self._emit_captured_locals(err_val)
			ctx = self._try_stack[-1]
			self.b.emit(M.StoreLocal(local=ctx.error_local, value=err_val))
			# LANGUAGE_BUG #102 (auto-unwind edge): a can-throw call whose
			# FnResult.Err branches into this `call_err` block is exactly
			# the same kind of throw-edge as an explicit `throw` — the
			# only difference is the throw originates in the callee.
			# Without this hook, Destructible locals declared in the try
			# body between try-entry and this call site leak when the
			# callee returns Err.  Singular's `_call_operation_sp` → err
			# → `__bb_call_err3` was the production manifestation.
			self._emit_scope_cleanup_hook(scope_index=ctx.scope_index_at_entry)
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
		if self._should_copy_value(ok_ty):
			self.b.emit(M.LoadLocal(dest=dest, local=ok_local))
		else:
			self.b.emit(M.MoveOut(dest=dest, local=ok_local, ty=ok_ty))
		self._local_types[dest] = ok_ty
		return dest

	def _is_call_terminal_throws(self, info: "CallInfo") -> bool:
		"""True if the callee is a terminal-throws function (never returns)."""
		from lang.driftc.stage1.call_info import CallTargetKind
		# TRAIT calls carry the flag on CallSig (Phase 3.5).
		if bool(getattr(info.sig, "declared_terminal_throws", False)):
			return True
		# DIRECT calls: look up the callee's FnSignature.
		if info.target.kind is CallTargetKind.DIRECT and info.target.symbol is not None:
			sig = self._signatures_by_id.get(info.target.symbol)
			if sig is not None and bool(getattr(sig, "declared_terminal_throws", False)):
				return True
		return False

	def _lower_can_throw_call_stmt(
		self,
		*,
		emit_call: callable,
		ok_ty: TypeId,
		is_terminal_throws: bool = False,
	) -> None:
		"""
		Lower a can-throw call in a try context as a statement (ignores ok value).

		We still must check for Err and route it to the current try dispatch.

		When `is_terminal_throws` is True, the callee never returns normally —
		every invocation exits via exception. The ok path is unreachable, so we
		skip the ok block and join block entirely. Control after the call is
		dead code; subsequent statements in the enclosing block are not lowered
		(the caller must handle this, or the checker must have already validated
		that no live code follows the terminal call).
		"""
		fnres_val = emit_call()
		is_err = self.b.new_temp()
		self.b.emit(M.ResultIsErr(dest=is_err, result=fnres_val))

		if is_terminal_throws:
			# Terminal-throws: the callee always throws, so the ok path is
			# unreachable. Route err to try/propagate; emit an unreachable
			# ok block so the MIR graph is well-formed.
			ok_block = self.b.new_block("call_ok_unreachable")
			err_block = self.b.new_block("call_err")

			self.b.set_terminator(
				M.IfTerminator(cond=is_err, then_target=err_block.name, else_target=ok_block.name)
			)

			self.b.set_block(err_block)
			err_val = self.b.new_temp()
			self.b.emit(M.ResultErr(dest=err_val, result=fnres_val))
			if self._try_stack:
				# Slice 2 DV→JSON: emit captured-frame before the local-
				# catch goto.  See comment at the parallel site above.
				self._emit_captured_locals(err_val)
				ctx = self._try_stack[-1]
				self.b.emit(M.StoreLocal(local=ctx.error_local, value=err_val))
				# LANGUAGE_BUG #102 (auto-unwind edge — terminal-throws).
				self._emit_scope_cleanup_hook(scope_index=ctx.scope_index_at_entry)
				self.b.set_terminator(M.Goto(target=ctx.dispatch_block_name))
			else:
				self._propagate_error(err_val)

			# Unreachable ok block: the callee always throws, so this
			# block is dead. Mark it unreachable.
			self.b.set_block(ok_block)
			self.b.set_terminator(M.Unreachable())
			# Do NOT set a join block — control is dead after the terminal call.
			return

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
			# Slice 2 DV→JSON: emit captured-frame before the local-
			# catch goto.  See comment at the parallel site above.
			self._emit_captured_locals(err_val)
			ctx = self._try_stack[-1]
			self.b.emit(M.StoreLocal(local=ctx.error_local, value=err_val))
			# LANGUAGE_BUG #102 (auto-unwind edge — statement-form call).
			self._emit_scope_cleanup_hook(scope_index=ctx.scope_index_at_entry)
			self.b.set_terminator(M.Goto(target=ctx.dispatch_block_name))
		else:
			self._propagate_error(err_val)

		# Ok path: ignore ok payload and continue.
		self.b.set_block(ok_block)
		if not self._type_table.is_void(ok_ty):
			ok_val = self.b.new_temp()
			self.b.emit(M.ResultOk(dest=ok_val, result=fnres_val))
			if not self._should_copy_value(ok_ty):
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
		def _canonical_forward_nominal(ty: TypeId | None, *, _seen: set[tuple[str | None, str]] | None = None) -> TypeId | None:
			if ty is None:
				return None
			seen = _seen if _seen is not None else set()
			td = self._type_table.get(ty)
			if td.kind is TypeKind.REF and td.param_types:
				inner = _canonical_forward_nominal(td.param_types[0], _seen=seen)
				if inner is not None and inner != td.param_types[0]:
					return self._type_table.ensure_ref_mut(inner) if td.ref_mut else self._type_table.ensure_ref(inner)
				return ty
			if td.kind is TypeKind.ARRAY and td.param_types:
				elem = _canonical_forward_nominal(td.param_types[0], _seen=seen)
				if elem is not None and elem != td.param_types[0]:
					return self._type_table.new_array(elem)
				return ty
			if td.kind is not TypeKind.FORWARD_NOMINAL:
				return ty
			alias_key = (td.module_id, td.name)
			if alias_key in seen:
				return ty
			alias_def = self._type_table.lookup_type_alias(module_id=td.module_id, name=td.name)
			if alias_def is not None:
				alias_params, alias_target, _loc = alias_def
				if not alias_params:
					resolved = resolve_opaque_type(alias_target, self._type_table, module_id=td.module_id, type_params=None, allow_generic_base=True)
					if resolved != ty:
						return _canonical_forward_nominal(resolved, _seen=seen | {alias_key})
			resolved_nom = (
				self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=td.module_id, name=td.name)
				or self._type_table.get_nominal(kind=TypeKind.VARIANT, module_id=td.module_id, name=td.name)
				or self._type_table.get_nominal(kind=TypeKind.INTERFACE, module_id=td.module_id, name=td.name)
			)
			if resolved_nom is not None:
				return resolved_nom
			return ty

		if hasattr(H, "HLiteralUint") and isinstance(expr, getattr(H, "HLiteralUint")):
			return self._uint_type
		if hasattr(H, "HLiteralUint64") and isinstance(expr, getattr(H, "HLiteralUint64")):
			return self._uint64_type
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
		if hasattr(H, "HLiteralUint") and isinstance(expr, H.HLiteralUint):
			return self._uint_type
		if hasattr(H, "HLiteralUint64") and isinstance(expr, H.HLiteralUint64):
			return self._uint64_type
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
			# Block-scope constant: return its declared type directly.
			_lc_bid = getattr(expr, "binding_id", None)
			if _lc_bid is not None and int(_lc_bid) in self._local_consts:
				return self._local_consts[int(_lc_bid)][0]
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
							return _canonical_forward_nominal(bid_ty)
						if local_def.kind is TypeKind.SCALAR and bid_def.kind is TypeKind.SCALAR and local_ty != bid_ty:
							return _canonical_forward_nominal(bid_ty)
				return _canonical_forward_nominal(local_ty)
			if self._expr_types and self._typed_mode != "none":
				known = self._expr_types.get(expr.node_id)
				if known is not None:
					known_def = self._type_table.get(known)
					if known_def.kind is not TypeKind.UNKNOWN:
						return _canonical_forward_nominal(known)
			if getattr(expr, "binding_id", None) is not None:
				bid_ty = self._binding_types.get(int(expr.binding_id))
				if bid_ty is not None:
					return _canonical_forward_nominal(bid_ty)
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
					return _canonical_forward_nominal(known)
		if isinstance(expr, H.HLiteralFloat):
			return self._float_type
		if isinstance(expr, H.HLiteralBool):
			return self._bool_type
		if isinstance(expr, H.HLiteralString):
			return self._string_type
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
				return _canonical_forward_nominal(info.sig.user_ret_type)
			return self._infer_qualified_ctor_variant_type(expr.fn, expr.args)
		if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
			info = self._call_info_for_expr_optional(expr)
			if info is not None:
				return _canonical_forward_nominal(info.sig.user_ret_type)
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
				return _canonical_forward_nominal(info.sig.user_ret_type)
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
			# Slice 7c-3 (ABI 14): `e.attrs` field on Error is gone
			# along with `TypeKind.DIAGNOSTICVALUE` (rejected at the
			# checker since Slice 7a).
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
				return _canonical_forward_nominal(fty)
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
				return inner if inner in (self._uint_type, self._uint64_type) else None
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
				if left == right and left in (self._int_type, self._float_type, self._uint_type, self._uint64_type):
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
					if proj.name == "params":
						# Slice 1 DV→JSON: `<error>.params` projects to
						# `core.ErrorParamsView`.  Mirrors the
						# type_checker.py special-case for HField/HPlaceField.
						td = self._type_table.get(cur)
						if td.kind is TypeKind.REF and td.param_types:
							td = self._type_table.get(td.param_types[0])
						if td.kind is TypeKind.ERROR:
							epv = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id="std.core", name="ErrorParamsView")
							if epv is not None:
								cur = epv
								continue
					if proj.name == "context":
						# Slice 2: same shape for `<error>.context` →
						# `core.ErrorContextView`.
						td = self._type_table.get(cur)
						if td.kind is TypeKind.REF and td.param_types:
							td = self._type_table.get(td.param_types[0])
						if td.kind is TypeKind.ERROR:
							ecv = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id="std.core", name="ErrorContextView")
							if ecv is not None:
								cur = ecv
								continue
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
			# Slice 7c-3 (ABI 14): the DV-receiver method-name
			# inference shortcut is gone with TypeKind.DIAGNOSTICVALUE.
		if hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
			return self._infer_expr_type(expr.attempt)
		if hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
			return self._infer_expr_type(expr.result)
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
			if hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
				walk_block(expr.body)
				walk_expr(expr.result)
				return
			if isinstance(expr, H.HMatchExpr):
				walk_expr(expr.scrutinee)
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
			if isinstance(stmt, H.HLocalConst):
				return  # literal value, no expressions to walk
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
		if isinstance(expr.base, H.HVar) and expr.base.binding_id is None:
			const_mod = getattr(expr.base, "module_id", None) or self._current_module_name()
			const_val = self._type_table.lookup_const(f"{const_mod}::{expr.base.name}")
			if const_val is not None:
				if is_mut:
					raise AssertionError("mutable address-of module const reached MIR lowering (checker bug)")
				if expr.projections:
					raise AssertionError("address-of projected module const reached MIR lowering (checker bug)")
				const_ty, _ = const_val
				init_value = self.lower_expr(expr.base, expected_type=const_ty)
				# Use the canonical owned-temp helper -- const can be
				# owning (e.g. String const).
				local = self._materialize_owned_temp(
					name_prefix=f"__const_{expr.base.name}_",
					ty=const_ty,
					value=init_value,
				)
				addr = self.b.new_temp()
				self.b.emit(M.AddrOfLocal(dest=addr, local=local, is_mut=False))
				return addr, const_ty
		# Block-scope local const: materialize into a temporary and return its address.
		if isinstance(expr.base, H.HVar) and expr.base.binding_id is not None:
			bid = int(expr.base.binding_id)
			if bid in self._local_consts:
				const_ty, _ = self._local_consts[bid]
				init_value = self._emit_local_const(bid)
				# Same shape as the module-const branch above.
				local = self._materialize_owned_temp(
					name_prefix=f"__lconst_{expr.base.name}_",
					ty=const_ty,
					value=init_value,
				)
				addr = self.b.new_temp()
				self.b.emit(M.AddrOfLocal(dest=addr, local=local, is_mut=False))
				return addr, const_ty
		# Canonical place expression (stage1→stage2 boundary).
		if self._typed_mode == "strict" and isinstance(expr.base, H.HVar) and expr.base.binding_id is None and expr.base.module_id is None:
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
				# Slice 1 DV→JSON: synthesized `<error>.params` projection
				# has no real address.  Build the ErrorParamsView VALUE,
				# materialize it to a named local, and take that local's
				# address.  Must be the LAST projection in the chain
				# (no further field/index after `params`).
				if proj.name == "params" and base_def.kind is TypeKind.ERROR:
					if is_mut:
						raise AssertionError(
							"mutable borrow of `<error>.params` is not "
							"supported (the view is a synthesized read-only value)"
						)
					epv_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id="std.core", name="ErrorParamsView")
					if epv_ty is None:
						raise AssertionError("std.core:ErrorParamsView not found in type table (compiler invariant)")
					# Load the Error value at `addr`.
					err_val = self.b.new_temp()
					self.b.emit(M.LoadRef(dest=err_val, ptr=addr, inner_ty=cur_ty))
					json_dest = self.b.new_temp()
					self.b.emit(M.ExcGetParamsJson(dest=json_dest, error=err_val))
					self._local_types[json_dest] = self._string_type
					view_val = self.b.new_temp()
					self.b.emit(M.ConstructStruct(dest=view_val, struct_ty=epv_ty, args=[json_dest]))
					self._local_types[view_val] = epv_ty
					view_local = f"__exc_params_view_{self.b.new_temp()}"
					self.b.ensure_local(view_local)
					self._local_types[view_local] = epv_ty
					self.b.emit(M.StoreLocal(local=view_local, value=view_val))
					# LANGUAGE_BUG follow-up (Cluster 1, 2026-05-06):
					# register the synthesized view local on the
					# current scope's drop list so the cleanup pass
					# emits a structural drop for its `String` field.
					# See companion comment in
					# `_lower_method_call_with_info`'s synthesis site.
					self._register_drop_local(view_local, epv_ty)
					view_addr = self.b.new_temp()
					self.b.emit(M.AddrOfLocal(dest=view_addr, local=view_local, is_mut=False))
					return view_addr, epv_ty
				# Slice 2: same shape for `<error>.context`.
				if proj.name == "context" and base_def.kind is TypeKind.ERROR:
					if is_mut:
						raise AssertionError(
							"mutable borrow of `<error>.context` is not "
							"supported (the view is a synthesized read-only value)"
						)
					ecv_ty = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id="std.core", name="ErrorContextView")
					if ecv_ty is None:
						raise AssertionError("std.core:ErrorContextView not found in type table (compiler invariant)")
					err_val = self.b.new_temp()
					self.b.emit(M.LoadRef(dest=err_val, ptr=addr, inner_ty=cur_ty))
					json_dest = self.b.new_temp()
					self.b.emit(M.ExcGetContextJson(dest=json_dest, error=err_val))
					self._local_types[json_dest] = self._string_type
					view_val = self.b.new_temp()
					self.b.emit(M.ConstructStruct(dest=view_val, struct_ty=ecv_ty, args=[json_dest]))
					self._local_types[view_val] = ecv_ty
					view_local = f"__exc_context_view_{self.b.new_temp()}"
					self.b.ensure_local(view_local)
					self._local_types[view_local] = ecv_ty
					self.b.emit(M.StoreLocal(local=view_local, value=view_val))
					# LANGUAGE_BUG follow-up (Cluster 1, 2026-05-06):
					# parallel scope-drop registration for the
					# `ErrorContextView` synthesis path — same shape as
					# the params-view fix.
					self._register_drop_local(view_local, ecv_ty)
					view_addr = self.b.new_temp()
					self.b.emit(M.AddrOfLocal(dest=view_addr, local=view_local, is_mut=False))
					return view_addr, ecv_ty
				# Typed catch binder field borrow (`&e.field`).  When
				# the type-checker recognized the place as a typed
				# catch binder projection (lines ~9180+ in
				# type_checker.py annotate `expr.typed_proj_event_fqn`
				# + `expr.typed_proj_field_name`), the base type is
				# Error but the field is a declared schema field on
				# the parallel Path-A struct.  Materialize a struct
				# storage local for the binder (lazy, once per
				# binding_id) and emit `AddrOfField` on the storage
				# local instead of asserting "not a struct".
				#
				# This is the lowering side of the option-(b) fix for
				# the sgw 2026-05-17 #2 carrier: typed catch binders
				# now have an addressable native struct view inside
				# the catch arm, supporting `&e.field` borrow with
				# the same storage model as `e.field` by-value reads
				# would use post-unification.  The HField direct-read
				# path still uses the legacy per-access JSON decode
				# helper (unchanged); the two paths converge in a
				# follow-up slice.
				if (
					base_def.kind is TypeKind.ERROR
					and getattr(expr, "typed_proj_event_fqn", None) is not None
					and getattr(expr, "typed_proj_field_name", None) == proj.name
					and isinstance(expr.base, H.HVar)
					and expr.base.binding_id is not None
				):
					storage_local, storage_struct_id = self._get_or_materialize_typed_catch_storage(
						binding_id=int(expr.base.binding_id),
						binder_local=base_name,
						event_fqn=expr.typed_proj_event_fqn,
					)
					storage_info = self._type_table.struct_field(storage_struct_id, proj.name)
					if storage_info is None:
						raise AssertionError(
							f"typed catch binder storage struct lacks declared "
							f"field {proj.name!r} (Path-A invariant violation)"
						)
					storage_field_idx, storage_field_ty = storage_info
					storage_addr = self.b.new_temp()
					self.b.emit(M.AddrOfLocal(dest=storage_addr, local=storage_local, is_mut=False))
					dest = self.b.new_temp()
					self.b.emit(
						M.AddrOfField(
							dest=dest,
							base_ptr=storage_addr,
							struct_ty=storage_struct_id,
							field_index=storage_field_idx,
							field_ty=storage_field_ty,
							is_mut=is_mut,
						)
					)
					return dest, storage_field_ty
				if base_def.kind is not TypeKind.STRUCT:
					fn_name = getattr(self.b.func, "name", None)
					raise AssertionError(
						"field place base is not a struct (checker bug): "
						f"{base_def.kind} for {cur_ty} base={base_name} field={proj.name} fn={fn_name}"
					)
				info = self._type_table.struct_field(cur_ty, proj.name)
				if info is None:
					_fn_name_pp = getattr(self.b.func, "name", None)
					_sd_pp = self._type_table.get(cur_ty)
					_field_list_pp = [getattr(f, "name", None) for f in getattr(_sd_pp, "fields", []) or []]
					raise AssertionError(
						"unknown struct field reached MIR lowering (checker bug): "
						f"field={proj.name!r} subj_ty={cur_ty} struct={getattr(_sd_pp, 'name', None)!r} "
						f"module={getattr(_sd_pp, 'module_id', None)!r} known_fields={_field_list_pp} "
						f"base={base_name!r} fn={_fn_name_pp!r}"
					)
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

		§B fix (2026-05-17): if the supplied `event_fqn` is a
		`pub type Alias = inner.PubError` re-export and the direct
		lookup misses, follow the alias chain in `type_table.type_aliases`
		and look up the underlying pub-error's FQN.  Without this,
		`catch api:Alias(e) { ... }` would emit a comparison against
		event code 0 (the fallback), which never matches a real throw
		-- the catch arm would silently never run and the error
		would escape the try/catch, aborting at runtime.  The
		checker-side gate (`_canonical_event_fqn`) handles the
		throws-coverage analysis; this is the MIR-side mirror so
		catch arm dispatch ALSO matches the underlying event code.
		"""
		if self._exc_env is None:
			return 0
		code = self._exc_env.get(event_fqn, None)
		if code is not None:
			return code
		canonical = self._canonical_event_fqn_for_alias(event_fqn)
		if canonical is not None and canonical != event_fqn:
			return self._exc_env.get(canonical, 0)
		return 0

	def _canonical_event_fqn_for_alias(self, event_fqn: str) -> str | None:
		"""Resolve a catch-arm event FQN through any `pub type` alias
		chain in `type_table.type_aliases` to the underlying name.
		Returns the input on no-alias / cycle / generic-alias.  Mirrors
		the same-named helper in `checker/__init__.py` and
		`type_checker.py:_canonical_pub_error_fqn` -- kept inline
		here so MIR doesn't need to import from the checker layers."""
		if not event_fqn or ":" not in event_fqn:
			return event_fqn
		type_aliases = getattr(self._type_table, "type_aliases", None)
		if not isinstance(type_aliases, dict):
			return event_fqn
		mod, name = event_fqn.split(":", 1)
		seen: set[tuple[str, str]] = set()
		cur_mod, cur_name = mod, name
		while True:
			if (cur_mod, cur_name) in seen:
				return event_fqn
			seen.add((cur_mod, cur_name))
			payload = type_aliases.get((cur_mod, cur_name))
			if payload is None:
				return event_fqn
			type_params, target_te, _loc = payload
			if type_params:
				return event_fqn
			next_name = getattr(target_te, "name", None)
			if not isinstance(next_name, str) or not next_name:
				return event_fqn
			next_mod = getattr(target_te, "module_id", None)
			if not isinstance(next_mod, str) or not next_mod:
				next_mod = cur_mod
			next_fqn = f"{next_mod}:{next_name}"
			# Termination: if next_fqn names a known exception in `_exc_env`,
			# return it.  Otherwise keep walking the chain.
			if self._exc_env is not None and next_fqn in self._exc_env:
				return next_fqn
			cur_mod, cur_name = next_mod, next_name


__all__ = ["MirBuilder", "HIRToMIR"]


@dataclass
class _TryCtx:
	"""
	Internal try/catch context to route throws to the correct catch block.

	error_local: hidden local where the thrown Error is stored.
	dispatch_block_name: block that projects the event code and dispatches to arms.
	cont_block_name: continuation block after the try/catch completes.
	scope_index_at_entry: `len(_scope_stack)` at the moment this try was
	    entered (BEFORE `lower_block(body)` pushed the body scope).  Used
	    by `_visit_stmt_HThrow` / `_propagate_error` to emit a
	    `CleanupHook` covering exactly the in-flight locals between the
	    throw site (or propagation hop) and this try's entry, so a throw
	    that routes through the dispatch's "no event-arm matches" else
	    still drops every Destructible local declared inside the body.
	    Pinned by `lang/tests/memcheck/test_throw_unwind_destructible_drop.py`
	    (LANGUAGE_BUG #102; surfaced 2026-05-22 via singular gateway
	    LeasedConn leak through non-matching typed catch).
	"""

	error_local: str
	dispatch_block_name: str
	cont_block_name: str
	scope_index_at_entry: int = 0


@dataclass
class _CapturedLocal:
	binding_id: int
	local_name: str
	source_name: str
	capture_name: str
