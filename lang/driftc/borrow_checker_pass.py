#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Borrow-check pass (Phase 1/2): track moves per Place and loans.

Scope:
- Operates as a forward dataflow over a CFG derived from HIR.
- Tracks place states (UNINIT/VALID/MOVED) and flags use-after-move.
- Applies implicit moves in consuming positions for non-Copy places (explicit
  `move` is always allowed and always moves).
- Adds explicit borrow handling (& / &mut) with shared-vs-mut conflicts.
- Loan lifetimes: function-wide for most forms, block-liveness regions for
  explicit HLet+HBorrow (NLL-lite: until last use within scope), and
  temporary-borrow dropping for expr/cond/call scopes. Full general region
  analysis for all borrow forms is still TODO.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Mapping, Optional, Set, Tuple

from lang.driftc import stage1 as H
from lang.driftc.stage1 import closures as C
from lang.driftc.stage1.capture_discovery import discover_captures
from lang.driftc.borrow_checker import (
	EscapeLevel,
	Place,
	PlaceBase,
	PlaceKind,
	PlaceState,
	FieldProj,
	IndexProj,
	IndexKind,
	DerefProj,
	merge_place_state,
	place_from_expr,
	places_overlap,
)
from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeKind, TypeTable, TypeId
from lang.driftc.checker import FnSignature, user_facing_binding_name
from lang.driftc.method_registry import CallableDecl
from lang.driftc.method_resolver import MethodResolution, SelfMode
from lang.driftc.stage1.call_info import CallInfo, CallTargetKind, IntrinsicKind
from lang.driftc.call_contract import explicit_arg_param_types
from collections import deque


# std.core callback-wrapper functions emitted by the type checker when a call
# site passes a lambda to a Callback*/Fn* typed parameter.  These are
# transparent wrappers: for escape-level purposes the inner lambda's escape
# level is what matters, not the wrapper itself.
_CALLBACK_WRAPPER_MODULE = "std.core"
_CALLBACK_WRAPPER_NAMES: frozenset = frozenset({
	"callback0", "callback1", "callback2",
	"callback_throw0", "callback_throw1", "callback_throw2",
})


def _is_callback_wrapper_call(expr: object) -> bool:
	"""Return True iff expr is a std.core::callback0/1/2/throw0/1/2 call."""
	if not isinstance(expr, H.HCall):
		return False
	fn = expr.fn
	return (isinstance(fn, H.HVar)
		and getattr(fn, "module_id", None) == _CALLBACK_WRAPPER_MODULE
		and getattr(fn, "name", None) in _CALLBACK_WRAPPER_NAMES)


def _unwrap_callback_lambda(arg: object) -> "Optional[H.HLambda]":
	"""If arg is a callback wrapper call with a single HLambda arg, return it."""
	if not _is_callback_wrapper_call(arg):
		return None
	inner_args = getattr(arg, "args", ())
	if len(inner_args) != 1:
		return None
	inner = inner_args[0]
	return inner if isinstance(inner, H.HLambda) else None


@dataclass
class Terminator:
	"""CFG terminator describing control-flow edges out of a basic block."""

	kind: str  # "jump", "branch", "return", "throw"
	targets: List[int]
	cond: Optional[H.HExpr] = None
	value: Optional[H.HExpr] = None


@dataclass
class BasicBlock:
	"""Basic block of HIR statements with a single terminator."""

	id: int
	statements: List[H.HStmt] = field(default_factory=list)
	terminator: Optional[Terminator] = None


class LoanKind(Enum):
	"""Kinds of borrows supported in Phase 2."""

	SHARED = auto()
	MUT = auto()


@dataclass
class _FlowState:
	"""Dataflow state at a CFG point: place validity + active loans."""

	place_states: Dict[Place, PlaceState] = field(default_factory=dict)
	loans: Set["Loan"] = field(default_factory=set)


@dataclass(frozen=True)
class Loan:
	"""A loan of a place for the lifetime of its reference (coarse-grained for now)."""

	place: Place
	kind: LoanKind
	temporary: bool = False
	live_blocks: Optional[frozenset[int]] = None  # None = function-wide; set filled by RegionBuilder once implemented.
	origin_span: Span = field(default_factory=Span)
	ref_binding_id: Optional[int] = None
	max_escape: EscapeLevel = EscapeLevel.LOCAL


@dataclass
class BorrowChecker:
	"""
	Phase-1/2 borrow checker: move tracking + coarse loans on typed HIR (CFG/dataflow).

	Inputs:
	- type_table: to answer Copy vs move-only.
	- fn_types: mapping var identities to TypeId (params/locals as available).
	"""

	type_table: TypeTable
	fn_types: Mapping[PlaceBase, TypeId]
	binding_types: Optional[Dict[int, TypeId]] = None
	binding_mutable: Optional[Dict[int, bool]] = None
	binding_names: Optional[Dict[int, str]] = None
	module_id: Optional[str] = None
	signatures_by_id: Optional[Mapping[FunctionId, FnSignature]] = None
	semantic_world: Optional[Any] = None
	call_resolutions: Optional[Mapping[int, object]] = None
	call_info_by_callsite_id: Optional[Mapping[int, CallInfo]] = None
	base_lookup: Callable[[object], Optional[PlaceBase]] = lambda hv: PlaceBase(
		PlaceKind.LOCAL,
		getattr(hv, "binding_id", -1) if getattr(hv, "binding_id", None) is not None else -1,
		hv.name if hasattr(hv, "name") else str(hv),
	)
	diagnostics: List[Diagnostic] = field(default_factory=list)
	enable_auto_borrow: bool = True
	_current_block_id: Optional[int] = field(init=False, default=None, repr=False)
	_current_stmt_span: Optional[Span] = field(init=False, default=None, repr=False)
	_current_stmt_index: Optional[int] = field(init=False, default=None, repr=False)
	_current_block_stmts: Optional[list] = field(init=False, default=None, repr=False)
	_ref_witness_in: Optional[Dict[int, Dict[int, Span]]] = field(init=False, default=None, repr=False)
	_ref_live_after_stmt: Optional[Dict[int, List[Set[int]]]] = field(init=False, default=None, repr=False)
	_ref_no_use_ids: Optional[Set[int]] = field(init=False, default=None, repr=False)
	_synthetic_ref_binding_ids: Set[int] = field(init=False, default_factory=set, repr=False)
	_block_facts_in: Optional[Dict[int, Set[Tuple[int, int]]]] = field(init=False, default=None, repr=False)
	_catch_binders_by_block: Dict[int, str] = field(init=False, default_factory=dict, repr=False)
	local_const_binding_ids: Set[int] = field(default_factory=set)

	def __post_init__(self) -> None:
		# Ensure we always have a binding_id -> TypeId mapping to avoid repeated scans.
		if self.binding_types is None:
			self.binding_types = {pb.local_id: ty for pb, ty in self.fn_types.items()}
		if self.binding_names is None:
			self.binding_names = {pb.local_id: pb.name for pb in self.fn_types.keys()}
		self._bases_by_binding: Dict[int, PlaceBase] = {pb.local_id: pb for pb in self.fn_types.keys()}
		# Keep the earliest binding id for a name to avoid shadowing collisions
		# when fallback name-based lookups are needed (e.g. match binders).
		self._binding_id_by_name: Dict[str, int] = {}
		for bid, name in sorted((self.binding_names or {}).items(), key=lambda it: int(it[0])):
			if name not in self._binding_id_by_name:
				self._binding_id_by_name[name] = int(bid)
		self._method_sig_by_key: Dict[Tuple[int, str], FnSignature] = {}
		# Keyed by (module, fn_name) for free-function lookup by identity when
		# call_resolutions has no entry (e.g. @intrinsic callback0/1/2).
		# Populated when a signature has escape annotations — either on the
		# sig field (legacy) or in the semantic_world overlay (world-backed).
		self._free_fn_escape_sig: Dict[Tuple[Optional[str], str], FnSignature] = {}
		if self.signatures_by_id:
			for fn_id, sig in self.signatures_by_id.items():
				if sig.is_method and sig.impl_target_type_id is not None:
					key = (sig.impl_target_type_id, sig.method_name or sig.name)
					self._method_sig_by_key[key] = sig
				if self.semantic_world is not None:
					_has_escape = self.semantic_world.get_signature_annotation(fn_id, "param_escape_level") is not None
				else:
					_has_escape = bool(sig.param_escape_level)
				if not sig.is_method and _has_escape:
					free_key: Tuple[Optional[str], str] = (fn_id.module, fn_id.name)
					if free_key not in self._free_fn_escape_sig:
						self._free_fn_escape_sig[free_key] = sig

	def _effective_escape_level(self, fn_id: Optional[FunctionId], sig: Optional[FnSignature], param_index: int) -> "EscapeLevel":
		"""Get escape level for a param.

		Production (world-backed): reads from SemanticWorld overlay.
		Test-only (no world): reads from sig.param_escape_level.
		"""
		if self.semantic_world is not None and fn_id is not None:
			return self.semantic_world.effective_param_escape_level(fn_id, param_index)
		# Test-only fallback: no SemanticWorld available.
		if sig is not None:
			return sig.effective_param_escape_level(param_index)
		return EscapeLevel.THREAD

	def _has_escape_annotations(self, fn_id: Optional[FunctionId], sig: Optional[FnSignature]) -> bool:
		"""Check if a function has any escape-level annotations.

		Production (world-backed): reads from SemanticWorld overlay.
		Test-only (no world): reads from sig.param_escape_level.
		"""
		if self.semantic_world is not None and fn_id is not None:
			return self.semantic_world.get_signature_annotation(fn_id, "param_escape_level") is not None
		# Test-only fallback.
		return sig is not None and bool(sig.param_escape_level)

	def _is_unannotated_param(self, fn_id: Optional[FunctionId], sig: Optional[FnSignature], param_index: int) -> bool:
		"""Check if a param's escape level is default/unannotated.

		Production (world-backed): reads from SemanticWorld overlay.
		Test-only (no world): reads from sig.param_escape_level.
		"""
		if self.semantic_world is not None and fn_id is not None:
			pel = self.semantic_world.get_signature_annotation(fn_id, "param_escape_level")
			if pel is not None:
				return param_index >= len(pel) or pel[param_index] is None
			return True
		# Test-only fallback.
		if sig is not None:
			_pel = sig.param_escape_level or []
			return sig.param_escape_level is None or param_index >= len(_pel) or _pel[param_index] is None
		return True

	def _base_for_binding(self, binding_id: int) -> Optional[PlaceBase]:
		return self._bases_by_binding.get(binding_id)

	def _place_from_capture_key(self, key: C.HCaptureKey) -> Optional[Place]:
		base = self._base_for_binding(int(key.root_local))
		if base is None:
			return None
		place = Place(base)
		for proj in key.proj:
			place = place.with_projection(FieldProj(proj.field))
		return place

	def _resolve_fn_id_for_call(self, expr: H.HExpr) -> Optional[FunctionId]:
		"""Resolve the FunctionId for a call expression."""
		if isinstance(expr, H.HCall):
			resolution = self.call_resolutions.get(expr.node_id) if self.call_resolutions is not None else None
			if isinstance(resolution, CallableDecl) and resolution.fn_id is not None:
				return resolution.fn_id
		if isinstance(expr, H.HMethodCall):
			call_info = self.call_info_by_callsite_id.get(getattr(expr, "callsite_id", -1)) if self.call_info_by_callsite_id else None
			if call_info is not None and hasattr(call_info.target, "fn_id"):
				return call_info.target.fn_id
		return None

	def _resolve_sig_for_call(self, expr: H.HExpr) -> Optional[FnSignature]:
		if not self.signatures_by_id:
			return None
		if isinstance(expr, H.HCall):
			resolution = self.call_resolutions.get(expr.node_id) if self.call_resolutions is not None else None
			if isinstance(resolution, CallableDecl):
				if resolution.fn_id is None:
					return None
				return self.signatures_by_id.get(resolution.fn_id)
			# Fallback for @intrinsic free-function calls (e.g. callback0/1/2) that
			# do not get a call_resolutions entry.  Use the (module, name) escape-sig
			# cache which is populated for any free function with escape annotations.
			if isinstance(expr.fn, H.HVar):
				fn_name = getattr(expr.fn, "name", None)
				fn_module = getattr(expr.fn, "module_id", None)
				if fn_name is not None:
					free_sig = self._free_fn_escape_sig.get((fn_module, fn_name))
					if free_sig is not None:
						return free_sig
			return None
		if isinstance(expr, H.HMethodCall):
			resolution = self.call_resolutions.get(expr.node_id) if self.call_resolutions is not None else None
			if isinstance(resolution, MethodResolution):
				if resolution.decl.fn_id is not None:
					return self.signatures_by_id.get(resolution.decl.fn_id)
				impl_target = resolution.decl.impl_target_type_id
				if impl_target is None:
					return None
				return self._method_sig_by_key.get((impl_target, resolution.decl.name))
		if isinstance(expr, H.HInvoke):
			call_info = self._call_info_for_expr(expr)
			if call_info is None:
				return None
			param_types = tuple(call_info.sig.param_types)
			return FnSignature(
				name="__invoke__",
				param_type_ids=param_types,
				return_type_id=call_info.sig.user_ret_type,
				declared_can_throw=call_info.sig.can_throw,
				declared_throws=call_info.sig.can_throw,
			)
		return None

	def _intrinsic_name_for_call(self, expr: H.HCall) -> Optional[IntrinsicKind]:
		call_info = self.call_info_by_callsite_id
		if call_info is None:
			return None
		key = getattr(expr, "callsite_id", None)
		if not isinstance(call_info, dict) or not isinstance(key, int):
			return None
		info = call_info.get(key)
		if info is None or info.target.kind is not CallTargetKind.INTRINSIC:
			return None
		if info.target.intrinsic is None:
			raise AssertionError("intrinsic call missing kind (typecheck/call-info bug)")
		return info.target.intrinsic

	def _call_info_for_expr(self, expr: H.HExpr) -> Optional[CallInfo]:
		call_info = self.call_info_by_callsite_id
		if call_info is None:
			return None
		key = getattr(expr, "callsite_id", None)
		if not isinstance(call_info, dict) or not isinstance(key, int):
			return None
		return call_info.get(key)

	def _param_index_for_call(
		self,
		sig: FnSignature,
		*,
		arg_index: int | None = None,
		kw_name: str | None = None,
	) -> Optional[int]:
		if kw_name is not None:
			if not sig.param_names:
				return None
			try:
				idx = sig.param_names.index(kw_name)
			except ValueError:
				return None
			if sig.is_method and idx == 0:
				return None
			return idx
		if arg_index is None:
			return None
		if sig.is_method:
			return arg_index + 1
		return arg_index

	def _visit_call_arg_with_param(
		self,
		state: _FlowState,
		*,
		arg_expr: H.HExpr,
		param_ty: Optional[TypeId],
		call_span: Span,
	) -> None:
		if param_ty is not None:
			td = self.type_table.get(param_ty)
			if td.kind is TypeKind.REF:
				kind_for_arg: Optional[LoanKind] = None
				if td.ref_mut is True:
					kind_for_arg = LoanKind.MUT
				elif td.ref_mut is False:
					kind_for_arg = LoanKind.SHARED
				if kind_for_arg is not None:
					place_expr = arg_expr.subject if isinstance(arg_expr, H.HBorrow) else arg_expr
					place = place_from_expr(place_expr, base_lookup=self.base_lookup)
					if place is not None:
						self._borrow_place(
							state,
							place,
							kind_for_arg,
							temporary=True,
							span=getattr(arg_expr, "loc", call_span),
						)
						return
			else:
				if self._reject_noncopy_projected_byvalue_arg(arg_expr, fallback_span=call_span):
					return
				self._consume_expr(state, arg_expr, escapes=False)
				return
		self._visit_expr(state, arg_expr, consume=False, escapes=False)

	def _method_call_param_layout(
		self,
		expr: H.HMethodCall,
		*,
		resolution: Optional[MethodResolution],
		call_info: Optional[CallInfo],
	) -> tuple[Optional[list[TypeId]], int, bool, Optional[SelfMode]]:
		param_types: Optional[list[TypeId]] = None
		param_offset = 1
		params_include_receiver = True
		receiver_autoborrow: Optional[SelfMode] = None
		if isinstance(resolution, MethodResolution):
			param_types = list(resolution.decl.signature.param_types)
			receiver_autoborrow = resolution.receiver_autoborrow
			return param_types, param_offset, params_include_receiver, receiver_autoborrow
		if call_info is not None and call_info.target.kind is CallTargetKind.INDIRECT:
			recv_expr = expr.receiver.subject if isinstance(expr.receiver, H.HBorrow) else expr.receiver
			recv_place = place_from_expr(recv_expr, base_lookup=self.base_lookup)
			recv_ty = self._type_of_place(recv_place) if recv_place is not None else None
			if recv_ty is not None and self.type_table.get(recv_ty).kind is TypeKind.INTERFACE:
				param_types = explicit_arg_param_types(expr, call_info)
				param_offset = 0
				params_include_receiver = False
		return param_types, param_offset, params_include_receiver, receiver_autoborrow

	def _add_lambda_capture_loans(self, state: _FlowState, lam: H.HLambda) -> None:
		self._check_lambda_captures(lam)
		for cap in lam.captures:
			if cap.kind not in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT):
				continue
			place = self._place_from_capture_key(cap.key)
			if place is None:
				continue
			kind = LoanKind.MUT if cap.kind is C.HCaptureKind.REF_MUT else LoanKind.SHARED
			self._borrow_place(
				state,
				place,
				kind,
				temporary=True,
				span=cap.span,
			)

	def _lambda_has_borrow_capture(self, lam: H.HLambda) -> bool:
		self._check_lambda_captures(lam)
		for cap in lam.captures:
			if cap.kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT):
				return True
		return False

	def _captured_loan_binding_ids(self, lam: H.HLambda) -> set:
		"""Return the set of ref_binding_ids for REF/REF_MUT captures in lam."""
		self._check_lambda_captures(lam)
		ids: set = set()
		for cap in lam.captures:
			if cap.kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT):
				ids.add(int(cap.key.root_local))
		return ids

	def _lambda_escape_level(self, lam: H.HLambda, state: _FlowState) -> EscapeLevel:
		"""Compute the effective escape level of a lambda based on its captured loans."""
		self._check_lambda_captures(lam)
		capture_ids = self._captured_loan_binding_ids(lam)
		if not capture_ids:
			return EscapeLevel.STATIC
		matching = [ln for ln in state.loans if ln.ref_binding_id in capture_ids]
		if not matching:
			# Captures exist but no active loans tracked — conservative.
			return EscapeLevel.LOCAL
		return min(ln.max_escape for ln in matching)

	def _report_escape_violation(self, lam: H.HLambda, state: _FlowState, required: EscapeLevel, lambda_level: EscapeLevel, span: Span, from_unannotated: bool = False) -> None:
		"""Emit an escape-level diagnostic for a lambda that cannot meet the required escape level."""
		if required >= EscapeLevel.STATIC:
			code = "E_ESCAPE_STATIC"
			msg = "closure captures borrowed value which cannot be used in a 'static callback"
		elif required >= EscapeLevel.THREAD:
			code = "E_ESCAPE_THREAD"
			msg = "closure captures borrowed value which cannot be sent to a detached virtual thread"
		elif required >= EscapeLevel.SCOPED:
			code = "E_ESCAPE_SCOPE"
			msg = "closure captures borrowed value which may not outlive the structured scope"
		else:
			code = "E_ESCAPE_STORE"
			msg = "closure captures borrowed value which cannot escape its original scope"
		notes = []
		capture_ids = self._captured_loan_binding_ids(lam)
		for loan in state.loans:
			if loan.ref_binding_id in capture_ids and loan.max_escape < required:
				name = self.binding_names.get(loan.ref_binding_id, "?") if self.binding_names else "?"
				notes.append(f"captured borrow of `{name}` restricts escape level to {loan.max_escape.name}")
				break
		if from_unannotated:
			notes.append("parameter has no escape-level annotation; treated as THREAD in v1")
		self.diagnostics.append(Diagnostic(
			severity="error",
			code=code,
			message=msg,
			phase="borrow_check",
			span=span,
			notes=notes,
		))

	def _place_is_defined_before_stmt(self, place: Place, stmt_index: int, block_stmts: list) -> bool:
		"""Conservative syntactic check: is place provably alive across a call at stmt_index?

		Returns True if:
		- place is a function parameter (always live for the function's duration), OR
		- place was let-bound or assigned to before stmt_index in block_stmts.

		HAssign to a local (no projections) counts as a definition: if the local is assigned
		in the direct block before the scope call, it was declared somewhere accessible and is
		live for the block's duration.

		Only inspects the DIRECT enclosing BasicBlock — not nested blocks or predecessor blocks
		in the CFG. This is deliberately conservative (MVP rule, §3.6). Patterns rejected by
		this check but provably safe at a deeper analysis level are documented as known false
		positives; do not relax without a design change.
		"""
		if place.base.kind is PlaceKind.PARAM:
			return True
		local_id = place.base.local_id
		for i, stmt in enumerate(block_stmts):
			if i >= stmt_index:
				break
			if isinstance(stmt, H.HLet) and getattr(stmt, "binding_id", None) == local_id:
				return True
			if (isinstance(stmt, H.HAssign)
					and isinstance(stmt.target, H.HPlaceExpr)
					and not stmt.target.projections
					and getattr(stmt.target.base, "binding_id", None) == local_id):
				return True
		return False

	def _check_lambda_scope_escape(self, lam: H.HLambda, state: _FlowState, stmt_index: int, block_stmts: list, span: Span) -> bool:
		"""Check whether a LOCAL lambda can safely be passed to a SCOPED param (§3.6).

		For each captured loan:
		1. The underlying place must be VALID in the current flow state.
		2. The place must be defined before stmt_index in the direct enclosing BasicBlock
		   (conservative proxy for 'will still exist after the scope returns').

		Returns True if all captured loans pass both checks. Emits E_ESCAPE_SCOPE and
		returns False on the first failure.
		"""
		capture_ids = self._captured_loan_binding_ids(lam)
		for loan in state.loans:
			if loan.ref_binding_id not in capture_ids:
				continue
			curr = self._state_for(state, loan.place)
			if curr is not PlaceState.VALID:
				self._report_escape_violation(lam, state, EscapeLevel.SCOPED, EscapeLevel.LOCAL, span)
				return False
			if not self._place_is_defined_before_stmt(loan.place, stmt_index, block_stmts):
				self._report_escape_violation(lam, state, EscapeLevel.SCOPED, EscapeLevel.LOCAL, span)
				return False
		return True

	def _check_lambda_escape_level(self, lam: H.HLambda, state: _FlowState, required: EscapeLevel, span: Span, from_unannotated: bool = False) -> None:
		"""Validate that lam can satisfy required escape level; emit diagnostic if not."""
		lambda_level = self._lambda_escape_level(lam, state)
		if lambda_level < required:
			if (required == EscapeLevel.SCOPED and lambda_level == EscapeLevel.LOCAL
					and self._current_stmt_index is not None
					and self._current_block_stmts is not None):
				# SCOPED level: run scope-escape check before emitting an error.
				# If the captured places are provably alive across the scope call, accept.
				if self._check_lambda_scope_escape(lam, state, self._current_stmt_index, self._current_block_stmts, span):
					self._add_lambda_capture_loans(state, lam)
					return
				# _check_lambda_scope_escape already emitted E_ESCAPE_SCOPE
				return
			self._report_escape_violation(lam, state, required, lambda_level, span, from_unannotated=from_unannotated)
		elif required <= EscapeLevel.LOCAL:
			self._add_lambda_capture_loans(state, lam)

	def _report_lambda_escape_if_borrowed(self, lam: H.HLambda, *, span: Span) -> None:
		if not self._lambda_has_borrow_capture(lam):
			return
		self._diagnostic(
			"closures with borrowed captures are non-escaping in v0; only immediate invocation or proven non-retaining params are supported",
			span,
		)

	def _apply_lambda_capture_moves(self, state: _FlowState, lam: H.HLambda) -> None:
		self._check_lambda_captures(lam)
		for cap in lam.captures:
			if cap.kind is not C.HCaptureKind.MOVE:
				continue
			place = self._place_from_capture_key(cap.key)
			if place is None:
				continue
			self._force_move_place_use(state, place, cap.span)

	def _is_copy_projected_field(self, key: C.HCaptureKey) -> bool:
		"""Type-aware check used to let `discover_captures` downgrade a
		MOVE-kind capture of a Copy-typed projected field (e.g. `p.count`)
		to a plain COPY read instead of rejecting it outright. Only called
		with real field types available (post type-check).

		Narrowed to BITCOPY types only (0.33.70 review finding): a
		Copy-but-non-bitcopy field (a `String`, or a Copy struct/variant
		containing one) captured this way produced a CONFIRMED
		heap-use-after-free under ASAN for the struct/variant case — the
		boxed-callback COPY-kind env-construction branch's retain/copy of
		the field does not survive intact once the field's own value later
		flows out of the callback and both the source struct and the
		callback env are dropped. The plain-`String` case happens to not
		reproduce today only because a separate, independent pass
		(`string_arc.py`) provides incidental coverage — not something this
		lowering path can rely on. Restricting to bitcopy types sidesteps
		the whole retain/alias question: a bitcopy value has no refcount to
		double-own in the first place.

		`type_table.is_bitcopy()` is NOT just scalars — it is TRANSITIVE
		for structs: a Copy struct is bitcopy iff every one of its fields
		is (recursively) bitcopy too (`types_core.py::TypeTable.is_bitcopy`).
		So this accepts `p.count: Int` AND e.g. `p.point: Point` where
		`Point { x: Int, y: Int }` is marked `implement core.Copy for Point
		{}` — both are equally safe (no refcount anywhere in the value's
		closure), and this is intentional, not an oversight to be narrowed
		further. Variants are NEVER bitcopy regardless of field types (see
		`is_bitcopy`'s VARIANT case), so a Copy variant field is always
		rejected here. Extending this past bitcopy — accepting a
		Copy-but-non-bitcopy field like `String` — is a real follow-up, not
		a checker-side one-line change — see
		`work/callback-env-uaf-ref-args/REPORT-0.33.70-projected-capture-lowering.md`
		§10/§15."""
		place = self._place_from_capture_key(key)
		if place is None:
			return False
		field_ty = self._type_of_place(place)
		if field_ty is None:
			return False
		return self._is_copy(field_ty) and self.type_table.is_bitcopy(field_ty)

	def _check_lambda_captures(self, lam: H.HLambda) -> None:
		if not lam.captures:
			resolver = self._is_copy_projected_field if self.binding_types is not None else None
			result = discover_captures(lam, is_copy_projected_field=resolver)
			lam.captures = result.captures
			for d in result.diagnostics:
				self.diagnostics.append(d)
		if self.binding_types is None:
			return
		for cap in lam.captures:
			if cap.kind is not C.HCaptureKind.COPY:
				continue
			# A projected COPY capture (e.g. `p.count`) must be validated
			# against the PROJECTED FIELD's type, not the root's — the root
			# (`Prepared`) may itself be non-Copy while the field is.
			ty = self._type_of_place(self._place_from_capture_key(cap.key)) if cap.key.proj else self.binding_types.get(cap.key.root_local)
			if ty is None or not self._is_copy(ty):
				base = self._base_for_binding(int(cap.key.root_local))
				name = base.name if base is not None else str(cap.key.root_local)
				self._diagnostic(
					f"cannot copy '{user_facing_binding_name(name)}': type is not Copy",
					cap.span,
				)

	@classmethod
	def from_typed_fn(
		cls,
		typed_fn,
		type_table: TypeTable,
		*,
		signatures_by_id: Optional[Mapping[FunctionId, FnSignature]] = None,
		enable_auto_borrow: bool = True,
		semantic_world: Optional[Any] = None,
	) -> "BorrowChecker":
		"""
		Build a BorrowChecker from a TypedFn (binding-aware).

		TypedFn is expected to expose:
		  - binding_types: mapping binding_id -> TypeId
		  - binding_names: mapping binding_id -> name
		"""
		# Preserve binding identity kind (param vs local). This matters for:
		# - future ABI/calling convention decisions,
		# - diagnostics/readability (param vs local),
		# - avoiding accidental overlaps if/when we introduce nested binding scopes.
		param_ids = set(getattr(typed_fn, "param_bindings", []) or [])
		if hasattr(typed_fn, "binding_names"):
			for bid, name in typed_fn.binding_names.items():
				if name == "self" and bid not in param_ids:
					param_ids.add(bid)
		binding_place_kind = getattr(typed_fn, "binding_place_kind", None)
		fn_types = {}
		for bid, ty in typed_fn.binding_types.items():
			if binding_place_kind is not None and bid in binding_place_kind:
				kind = binding_place_kind[bid]
			else:
				kind = PlaceKind.PARAM if bid in param_ids else PlaceKind.LOCAL
			fn_types[PlaceBase(kind, bid, typed_fn.binding_names.get(bid, "_b"))] = ty

		def base_lookup(hv: object) -> Optional[PlaceBase]:
			name = hv.name if hasattr(hv, "name") else str(hv)
			bid = getattr(hv, "binding_id", None)
			if bid is None and hasattr(typed_fn, "binding_for_var"):
				bid = typed_fn.binding_for_var.get(hv.node_id)
			local_id = bid if isinstance(bid, int) else -1
			if bid is None:
				kind = PlaceKind.GLOBAL
			elif binding_place_kind is not None and local_id in binding_place_kind:
				kind = binding_place_kind[local_id]
			else:
				kind = PlaceKind.PARAM if local_id in param_ids else PlaceKind.LOCAL
			return PlaceBase(kind, local_id, name)

		# Collect local-const binding_ids from the HIR block.
		local_const_bids: set[int] = set()
		def _scan_for_local_consts(block: H.HBlock) -> None:
			for stmt in block.statements:
				if isinstance(stmt, H.HLocalConst) and stmt.binding_id is not None:
					local_const_bids.add(int(stmt.binding_id))
				elif isinstance(stmt, H.HIf):
					_scan_for_local_consts(stmt.then_block)
					if stmt.else_block:
						_scan_for_local_consts(stmt.else_block)
				elif isinstance(stmt, H.HLoop):
					_scan_for_local_consts(stmt.body)
				elif isinstance(stmt, H.HTry):
					_scan_for_local_consts(stmt.body)
					for arm in stmt.catches:
						_scan_for_local_consts(arm.block)
				elif isinstance(stmt, H.HBlock):
					_scan_for_local_consts(stmt)
		if isinstance(typed_fn.body, H.HBlock):
			_scan_for_local_consts(typed_fn.body)

		return cls(
			type_table=type_table,
			fn_types=fn_types,
			binding_types=dict(typed_fn.binding_types),
			binding_names=dict(getattr(typed_fn, "binding_names", {}) or {}),
			binding_mutable=dict(getattr(typed_fn, "binding_mutable", {}) or {}),
			signatures_by_id=signatures_by_id,
			semantic_world=semantic_world,
			call_resolutions=getattr(typed_fn, "call_resolutions", None),
			call_info_by_callsite_id=getattr(typed_fn, "call_info_by_callsite_id", None),
			base_lookup=base_lookup,
			module_id=getattr(getattr(typed_fn, "fn_id", None), "module", None),
			enable_auto_borrow=enable_auto_borrow,
			local_const_binding_ids=local_const_bids,
		)

	def _is_copy(self, ty: Optional[TypeId]) -> bool:
		"""Return True if the type is Copy per the core type table."""
		if ty is None:
			return False
		copy_status = self.type_table.copy_status(ty)
		if copy_status is None:
			return False
		return copy_status

	def _is_optional_ref_type(self, ty: Optional[TypeId], *, is_mut: bool) -> bool:
		"""Return True if `ty` is Optional<&T> or Optional<&mut T>."""
		if ty is None:
			return False
		inst = self.type_table.get_variant_instance(ty)
		if inst is None:
			return False
		optional_base = self.type_table.get_variant_base(module_id="lang.core", name="Optional")
		if optional_base is None or inst.base_id != optional_base:
			return False
		if len(inst.type_args) != 1:
			return False
		inner = inst.type_args[0]
		td = self.type_table.get(inner)
		if td.kind is not TypeKind.REF:
			return False
		return bool(td.ref_mut) is is_mut

	def _is_ref_binding_id(self, binding_id: Optional[int]) -> bool:
		if binding_id is None or self.binding_types is None:
			return binding_id is not None and int(binding_id) in self._synthetic_ref_binding_ids
		if int(binding_id) in self._synthetic_ref_binding_ids:
			return True
		ty = self.binding_types.get(binding_id)
		if ty is None:
			return False
		td = self.type_table.get(ty)
		if td.kind is TypeKind.REF:
			return True
		return self._is_optional_ref_type(ty, is_mut=True) or self._is_optional_ref_type(ty, is_mut=False)

	def _state_for(self, state: _FlowState, place: Place) -> PlaceState:
		"""
		Lookup helper with UNINIT default for missing places.

		MVP precision: if a projected place (e.g. `x.field` or `arr[0]`) has no
		explicit state entry, fall back to the closest prefix state (`x`).
		This keeps move/borrow checks usable before we implement full per-subplace
		state propagation.
		"""
		if place.base.kind is PlaceKind.GLOBAL:
			return PlaceState.VALID
		if place.base.local_id in self.local_const_binding_ids:
			return PlaceState.VALID
		if self.module_id is not None:
			const_sym = f"{self.module_id}::{place.base.name}"
			if self.type_table.lookup_const(const_sym) is not None:
				return PlaceState.VALID
		if place.base.name == "self":
			return PlaceState.VALID
		if place in state.place_states:
			return state.place_states[place]
		# Prefix fallback: treat unknown subplace state as the base place state.
		if place.projections:
			for n in range(len(place.projections) - 1, -1, -1):
				prefix = Place(place.base, place.projections[:n])
				if prefix in state.place_states:
					return state.place_states[prefix]
		return PlaceState.UNINIT

	def _set_state(self, state: _FlowState, place: Place, value: PlaceState) -> None:
		"""Mutate the local state map for a given place."""
		state.place_states[place] = value

	def _diagnostic(self, message: str, span: Span | None = None, *, code: str | None = None) -> None:
		"""
		Append an error-level diagnostic anchored at `span`.

		Borrow checking frequently reports *uses* that occur after the original
		borrow/move site. Anchoring errors at a best-effort span (even when it is
		just the explicit sentinel `Span()`) makes diagnostics significantly more
		actionable than emitting spanless errors.
		"""
		self.diagnostics.append(
			Diagnostic(message=message, severity="error", phase="borrowcheck", span=span or Span(), code=code)
		)

	def _note(self, message: str, span: Span | None = None) -> None:
		"""Append a note-level diagnostic anchored at `span`."""
		self.diagnostics.append(
			Diagnostic(message=message, severity="note", phase="borrowcheck", span=span or Span())
		)

	def _emit_loan_notes(self, loan: Loan, block_id: Optional[int]) -> None:
		"""Attach notes explaining why a conflicting loan is still live."""
		self._note("borrow created here", loan.origin_span)
		if loan.ref_binding_id is None or block_id is None or self._ref_witness_in is None:
			return
		witness = self._ref_witness_in.get(block_id, {}).get(loan.ref_binding_id)
		if witness is None:
			return
		self._note("borrow considered live here because of use at (on some path)", witness)

	def _consume_place_use(self, state: _FlowState, place: Place, span: Span | None = None) -> None:
		"""Consume a place in non-consuming position (use-after-move / borrow checks)."""
		if (span is None or span.line is None) and self._current_stmt_span is not None and self._current_stmt_span.line is not None:
			span = self._current_stmt_span
		curr = self._state_for(state, place)
		if curr is PlaceState.MOVED:
			self._diagnostic(f"use after move of '{place.base.name}'", span, code="E_USE_AFTER_MOVE")
			return
		if curr is PlaceState.UNINIT:
			# Match-arm binders are compiler-synthesized names and may not always
			# carry a stable binding id through every lowering path. Treat them as
			# initialized when first observed at use-site.
			if str(place.base.name).startswith("__match_binder_"):
				self._set_state(state, place, PlaceState.VALID)
				curr = PlaceState.VALID
			else:
				self._diagnostic(f"use of uninitialized '{place.base.name}'", span)
				return
		overlap_loan = None
		mut_loan = None
		for loan in state.loans:
			if not self._places_overlap(place, loan.place):
				continue
			overlap_loan = loan
			if loan.kind is LoanKind.MUT:
				mut_loan = loan
				break
		if mut_loan is not None:
			self._diagnostic(f"cannot read '{place.base.name}' while it is mutably borrowed", span)
			self._emit_loan_notes(mut_loan, self._current_block_id)
			return
		ty = self._type_of_place(place)
		if self._is_copy(ty):
			return
		# Non-Copy value uses do not implicitly move in non-consuming positions.
		return

	def _type_of_place(self, place: Place) -> Optional[TypeId]:
		ty = self.fn_types.get(place.base)
		if ty is None:
			return None
		for proj in place.projections:
			td = self.type_table.get(ty)
			if isinstance(proj, DerefProj):
				if td.kind is not TypeKind.REF or not td.param_types:
					return None
				ty = td.param_types[0]
				continue
			if isinstance(proj, FieldProj):
				if td.kind is not TypeKind.STRUCT:
					return None
				info = self.type_table.struct_field_info(ty, proj.name)
				if info is None:
					return None
				_, field_ty, _is_pub = info
				ty = field_ty
				continue
			if isinstance(proj, IndexProj):
				if td.kind is not TypeKind.ARRAY or not td.param_types:
					return None
				ty = td.param_types[0]
				continue
			return None
		return ty

	def _is_projected_byvalue_arg(self, arg_expr: H.HExpr) -> bool:
		if hasattr(H, "HMove") and isinstance(arg_expr, getattr(H, "HMove")):
			return False
		place = place_from_expr(arg_expr, base_lookup=self.base_lookup)
		return place is not None and bool(place.projections)

	def _is_noncopy_projected_byvalue_arg(self, arg_expr: H.HExpr) -> bool:
		place = place_from_expr(arg_expr, base_lookup=self.base_lookup)
		if place is None or not place.projections:
			return False
		ty = self._type_of_place(place)
		return ty is not None and not self._is_copy(ty)

	def _best_effort_expr_span(self, expr: H.HExpr | None) -> Span:
		if expr is None:
			return Span()
		loc = getattr(expr, "loc", None)
		span = loc if isinstance(loc, Span) else Span.from_loc(loc)
		if span.line is not None:
			return span
		candidates: list[H.HExpr] = []
		if isinstance(expr, H.HField):
			candidates.append(expr.subject)
		elif isinstance(expr, H.HIndex):
			candidates.extend([expr.subject, expr.index])
		elif hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
			base_expr = getattr(expr, "base", None)
			if isinstance(base_expr, H.HExpr):
				candidates.append(base_expr)
			place_index_ty = getattr(H, "HPlaceIndex", None)
			for pr in getattr(expr, "projections", []) or []:
				if place_index_ty is not None and isinstance(pr, place_index_ty):
					idx_expr = getattr(pr, "index", None)
					if isinstance(idx_expr, H.HExpr):
						candidates.append(idx_expr)
		for cand in candidates:
			cand_loc = getattr(cand, "loc", None)
			cand_span = cand_loc if isinstance(cand_loc, Span) else Span.from_loc(cand_loc)
			if cand_span.line is not None:
				return cand_span
		return span

	def _reject_noncopy_projected_byvalue_arg(self, arg_expr: H.HExpr, *, fallback_span: Span | None = None) -> bool:
		if not self._is_noncopy_projected_byvalue_arg(arg_expr):
			return False
		span = self._best_effort_expr_span(arg_expr)
		if span.line is None and self._current_stmt_span is not None and self._current_stmt_span.line is not None:
			span = self._current_stmt_span
		if span.line is None and fallback_span is not None and fallback_span.line is not None:
			span = fallback_span
		self._diagnostic(
			"move of a projected place is not supported in v1; move a local/param or use swap/replace",
			span,
			code="E_USE_AFTER_MOVE",
		)
		return True

	def _consume_place_value(
		self,
		state: _FlowState,
		place: Place,
		*,
		consuming: bool,
		span: Span | None = None,
	) -> None:
		if consuming:
			ty = self._type_of_place(place)
			if ty is not None and not self._is_copy(ty):
				self._force_move_place_use_implicit(state, place, span)
				return
		self._consume_place_use(state, place, span)

	def _consume_expr(self, state: _FlowState, expr: H.HExpr, *, escapes: bool = False) -> None:
		self._visit_expr(state, expr, consume=True, escapes=escapes)

	def _force_move_place_use(self, state: _FlowState, place: Place, span: Span | None = None) -> None:
		"""
		Consume a place via an explicit `move` expression.

		Unlike implicit move semantics (which only apply to non-Copy types),
		`move <place>` is an explicit ownership-transfer marker and always
		invalidates the source place, even for Copy types.

		The borrow checker still enforces:
		- no moving while borrowed (overlap with any live loan), and
		- use-after-move diagnostics until the place is reinitialized.
		"""
		if (span is None or span.line is None) and self._current_stmt_span is not None and self._current_stmt_span.line is not None:
			span = self._current_stmt_span
		curr = self._state_for(state, place)
		if curr is PlaceState.MOVED:
			self._diagnostic(f"use after move of '{place.base.name}'", span, code="E_USE_AFTER_MOVE")
			return
		for loan in state.loans:
			if self._places_overlap(place, loan.place):
				self._diagnostic(f"cannot move '{place.base.name}' while borrowed", span)
				self._emit_loan_notes(loan, self._current_block_id)
				return
		self._set_state(state, place, PlaceState.MOVED)

	def _force_move_place_use_implicit(self, state: _FlowState, place: Place, span: Span | None = None) -> None:
		if (span is None or span.line is None) and self._current_stmt_span is not None and self._current_stmt_span.line is not None:
			span = self._current_stmt_span
		curr = self._state_for(state, place)
		if curr is PlaceState.MOVED:
			self._diagnostic(f"use after move of '{place.base.name}'", span, code="E_USE_AFTER_MOVE")
			return
		for loan in state.loans:
			if self._places_overlap(place, loan.place):
				self._diagnostic(f"cannot move '{place.base.name}' while borrowed", span)
				self._emit_loan_notes(loan, self._current_block_id)
				return
		self._set_state(state, place, PlaceState.MOVED)

	def _places_overlap(self, a: Place, b: Place) -> bool:
		"""
		Delegation hook for the "place overlap" predicate.

		We keep this as a method so the pass can be instrumented in tests, but the
		actual overlap semantics live in `lang.driftc.borrow_checker.places_overlap`
		as a single source of truth.
		"""
		facts = None
		if self._block_facts_in is not None and self._current_block_id is not None:
			facts = self._block_facts_in.get(self._current_block_id)
		if facts and self._places_disjoint_by_fact(a, b, facts):
			return False
		return places_overlap(a, b)

	def _places_disjoint_by_fact(self, a: Place, b: Place, facts: Set[Tuple[int, int]]) -> bool:
		"""Return True when known facts prove index projections are disjoint."""
		if a.base != b.base:
			return False
		ap = a.projections
		bp = b.projections
		n = min(len(ap), len(bp))
		for idx in range(n):
			pa = ap[idx]
			pb = bp[idx]
			if pa == pb:
				continue
			if isinstance(pa, IndexProj) and isinstance(pb, IndexProj):
				if (
					pa.kind is IndexKind.VAR
					and pb.kind is IndexKind.VAR
					and pa.value is not None
					and pb.value is not None
					and pa.value != pb.value
				):
					pair = self._fact_pair(int(pa.value), int(pb.value))
					return pair in facts
			break
		return False

	@staticmethod
	def _fact_pair(a: int, b: int) -> Tuple[int, int]:
		return (a, b) if a <= b else (b, a)

	def _branch_neq_fact(self, cond: H.HExpr) -> Optional[Tuple[int, int, bool]]:
		"""
		Return (a, b, then_is_neq) when a condition proves a != b on one branch.
		"""
		invert = False
		if isinstance(cond, H.HUnary) and cond.op is H.UnaryOp.NOT:
			cond = cond.expr
			invert = True
		if not isinstance(cond, H.HBinary) or cond.op not in (H.BinaryOp.NE, H.BinaryOp.EQ):
			return None
		if not isinstance(cond.left, H.HVar) or not isinstance(cond.right, H.HVar):
			return None
		left_id = getattr(cond.left, "binding_id", None)
		right_id = getattr(cond.right, "binding_id", None)
		if left_id is None or right_id is None or left_id == right_id:
			return None
		op = cond.op
		if invert:
			op = H.BinaryOp.NE if op is H.BinaryOp.EQ else H.BinaryOp.EQ
		then_is_neq = op is H.BinaryOp.NE
		return (int(left_id), int(right_id), then_is_neq)

	def _build_block_facts(self, blocks: List[BasicBlock]) -> Dict[int, Set[Tuple[int, int]]]:
		"""Compute must-hold index inequality facts at block entry."""
		succs: Dict[int, List[int]] = {}
		preds: Dict[int, List[int]] = {blk.id: [] for blk in blocks}
		for blk in blocks:
			succs[blk.id] = blk.terminator.targets if blk.terminator else []
			for succ in succs[blk.id]:
				preds.setdefault(succ, []).append(blk.id)

		in_facts: Dict[int, Set[Tuple[int, int]]] = {blk.id: set() for blk in blocks}
		out_edge: Dict[Tuple[int, int], Set[Tuple[int, int]]] = {}
		changed = True
		while changed:
			changed = False
			new_out: Dict[Tuple[int, int], Set[Tuple[int, int]]] = {}
			for blk in blocks:
				base = in_facts.get(blk.id, set())
				term = blk.terminator
				if term and term.kind == "branch" and term.cond is not None and len(term.targets) == 2:
					fact = self._branch_neq_fact(term.cond)
					if fact is not None:
						a, b, then_is_neq = fact
						pair = self._fact_pair(a, b)
						then_extra = {pair} if then_is_neq else set()
						else_extra = {pair} if not then_is_neq else set()
					else:
						then_extra = set()
						else_extra = set()
					new_out[(blk.id, term.targets[0])] = set(base) | then_extra
					new_out[(blk.id, term.targets[1])] = set(base) | else_extra
				else:
					for succ in succs.get(blk.id, []):
						new_out[(blk.id, succ)] = set(base)

			for blk in blocks:
				pred_list = preds.get(blk.id, [])
				if not pred_list:
					new_in = set()
				else:
					sets = [new_out.get((p, blk.id), set()) for p in pred_list]
					new_in = set(sets[0])
					for s in sets[1:]:
						new_in &= s
				if new_in != in_facts.get(blk.id, set()):
					in_facts[blk.id] = new_in
					changed = True
			out_edge = new_out

		return in_facts

	def _eval_temporary(self, state: _FlowState, expr: H.HExpr) -> None:
		"""
		Evaluate an expression in non-consuming position (expr stmt / cond).

		New loans created during evaluation are dropped immediately to model
		temporary borrow lifetimes (coarse NLL approximation).
		"""
		before = set(state.loans)
		self._visit_expr(state, expr, consume=False, escapes=False)
		new_loans = state.loans - before
		state.loans -= new_loans

	def _borrow_place(
		self,
		state: _FlowState,
		place: Place,
		kind: LoanKind,
		*,
		temporary: bool = False,
		span: Span | None = None,
		ref_binding_id: Optional[int] = None,
	) -> None:
		"""
		Process a borrow of `place` with the given kind, enforcing lvalue validity
		and active-loan conflict rules.
		"""
		if (span is None or span.line is None) and self._current_stmt_span is not None and self._current_stmt_span.line is not None:
			span = self._current_stmt_span
		curr = self._state_for(state, place)
		if curr is PlaceState.MOVED or curr is PlaceState.UNINIT:
			self._diagnostic(f"cannot borrow from moved or uninitialized '{place.base.name}'", span)
			return
		if kind is LoanKind.MUT and self.binding_mutable is not None:
			has_deref = any(isinstance(p, DerefProj) for p in place.projections)
			if not has_deref:
				mut = self.binding_mutable.get(place.base.local_id, False)
				if not mut:
					base_ty = None
					if self.binding_types is not None:
						base_ty = self.binding_types.get(place.base.local_id)
					if base_ty is not None:
						base_def = self.type_table.get(base_ty)
						if base_def.kind is TypeKind.REF and base_def.ref_mut:
							# Allow reborrowing a mutable reference held in an immutable binding.
							pass
						else:
							self._diagnostic(f"cannot take mutable borrow of immutable binding '{place.base.name}'", span)
							return
					else:
						self._diagnostic(f"cannot take mutable borrow of immutable binding '{place.base.name}'", span)
						return
			else:
				base_ty = None
				if self.binding_types is not None:
					base_ty = self.binding_types.get(place.base.local_id)
				if base_ty is not None:
					base_def = self.type_table.get(base_ty)
					if base_def.kind is TypeKind.REF and not base_def.ref_mut:
						self._diagnostic(
							f"cannot take mutable borrow through shared reference '{place.base.name}'",
							span,
						)
						return
		for loan in state.loans:
			if not self._places_overlap(place, loan.place):
				continue
			if kind is LoanKind.SHARED and loan.kind is LoanKind.MUT:
				self._diagnostic(
					f"cannot take shared borrow while mutable borrow active on '{place.base.name}'",
					span,
				)
				self._emit_loan_notes(loan, self._current_block_id)
				return
			if kind is LoanKind.MUT:
				self._diagnostic(f"cannot take mutable borrow while borrow active on '{place.base.name}'", span)
				self._emit_loan_notes(loan, self._current_block_id)
				return
		live_blocks = None
		if not temporary and hasattr(self, "_ref_live_blocks") and self._ref_live_blocks is not None:
			lbs = self._ref_live_blocks.get(ref_binding_id) if ref_binding_id is not None else None
			if lbs is not None:
				live_blocks = frozenset(lbs)
		state.loans.add(
			Loan(
				place=place,
				kind=kind,
				temporary=temporary,
				live_blocks=live_blocks,
				origin_span=span or Span(),
				ref_binding_id=ref_binding_id,
			)
		)

	def _reject_write_while_borrowed(self, state: _FlowState, place: Place, span: Span | None = None) -> bool:
		"""
		Enforce the MVP "freeze while borrowed" rule.

		If there is any live loan overlapping the write target, the write is
		rejected. This prevents both:
		- aliasing violations (`&x` then `x = ...`), and
		- storage-invalidating mutations when borrows exist (e.g. later for arrays).

		Returns True when the write is permitted, False when rejected.
		"""
		for loan in state.loans:
			if self._places_overlap(place, loan.place):
				self._diagnostic(f"cannot write to '{place.base.name}' while it is borrowed", span)
				self._emit_loan_notes(loan, self._current_block_id)
				return False
		return True

	def _loan_live_here(self, loan: Loan, block_id: Optional[int]) -> bool:
		"""
		Check if a loan is live at a given block. When live_blocks is None, treat as live everywhere.
		"""
		if loan.live_blocks is None:
			return True
		if block_id is None:
			return False
		return block_id in loan.live_blocks

	def _filter_live_loans(self, loans: Set[Loan], block_id: int) -> Set[Loan]:
		"""Filter a loan set to those live at the given block."""
		return {ln for ln in loans if self._loan_live_here(ln, block_id)}

	def _drop_dead_ref_bound_loans_after_stmt(self, state: _FlowState, block_id: int, stmt_index: int) -> None:
		if not self._ref_live_after_stmt:
			return
		live_after = self._ref_live_after_stmt.get(block_id)
		if live_after is None or stmt_index >= len(live_after):
			return
		live_refs = live_after[stmt_index]
		no_use = self._ref_no_use_ids or set()
		state.loans = {
			ln
			for ln in state.loans
			if ln.ref_binding_id is None
			or ln.temporary
			or ln.ref_binding_id in no_use
			or ln.ref_binding_id in live_refs
		}

	def _clone_loans_from_ref(
		self,
		state: _FlowState,
		src_rid: int,
		dst_rid: int,
		*,
		drop_dst: bool,
	) -> None:
		if src_rid == dst_rid:
			return
		if drop_dst:
			state.loans = {ln for ln in state.loans if ln.ref_binding_id != dst_rid}
		live_blocks = None
		if self._ref_live_blocks is not None:
			dst_blocks = self._ref_live_blocks.get(dst_rid)
			if dst_blocks is not None:
				live_blocks = frozenset(dst_blocks)
		clones: Set[Loan] = set()
		for loan in state.loans:
			if loan.ref_binding_id != src_rid:
				continue
			clones.add(
				Loan(
					place=loan.place,
					kind=loan.kind,
					temporary=loan.temporary,
					live_blocks=live_blocks,
					origin_span=loan.origin_span,
					ref_binding_id=dst_rid,
					max_escape=loan.max_escape,
				)
			)
		if clones:
			state.loans |= clones

	def _param_types_for_call(self, expr: H.HCall) -> Optional[List[TypeId]]:
		"""Return param TypeIds for a call if a signature is available; otherwise None."""
		if not self.signatures_by_id:
			return None
		resolution = self.call_resolutions.get(expr.node_id) if self.call_resolutions is not None else None
		if isinstance(resolution, CallableDecl) and resolution.fn_id is not None:
			sig = self.signatures_by_id.get(resolution.fn_id)
			if sig and sig.param_type_ids:
				return sig.param_type_ids
		return None

	def _build_regions(self, blocks: List[BasicBlock], scopes: List[Set[int]]) -> Optional[Dict[int, Set[int]]]:
		"""
		Compute per-ref live block sets for explicit borrows.

		NLL-lite policy: explicit borrow bindings are live until their last use,
		approximated via a fixed-point liveness analysis over the CFG.

		Returns mapping ref_binding_id -> set(block_ids) or None if no ref info.
		"""
		ref_defs: Dict[int, int] = {}  # ref_binding_id -> def block
		ref_uses: Dict[int, Set[int]] = {}
		ref_use_spans: Dict[int, Dict[int, Span]] = {}
		use_by_block: Dict[int, Set[int]] = {blk.id: set() for blk in blocks}
		def_by_block: Dict[int, Set[int]] = {blk.id: set() for blk in blocks}
		uses_stmt: Dict[int, List[Set[int]]] = {blk.id: [set() for _ in blk.statements] for blk in blocks}
		defs_stmt: Dict[int, List[Set[int]]] = {blk.id: [set() for _ in blk.statements] for blk in blocks}
		uses_term: Dict[int, Set[int]] = {blk.id: set() for blk in blocks}
		succs: Dict[int, List[int]] = {}

		for blk in blocks:
			succs[blk.id] = blk.terminator.targets if blk.terminator else []
			for stmt_i, stmt in enumerate(blk.statements):
				if isinstance(stmt, H.HLet):
					self._collect_ref_uses_in_expr(stmt.value, blk.id, ref_uses, ref_use_spans)
					uses_stmt[blk.id][stmt_i] |= self._ref_binding_ids_in_expr(stmt.value)
					bid = getattr(stmt, "binding_id", None)
					if bid is not None and self._call_returns_optional_ref(stmt.value):
						self._synthetic_ref_binding_ids.add(int(bid))
					if self._is_ref_binding_id(bid):
						def_by_block[blk.id].add(int(bid))
						defs_stmt[blk.id][stmt_i].add(int(bid))
						ref_defs.setdefault(int(bid), blk.id)
				elif isinstance(stmt, H.HAssign):
					tgt = stmt.target
					if isinstance(tgt, H.HPlaceExpr) and tgt.projections:
						self._collect_ref_uses_in_expr(tgt, blk.id, ref_uses, ref_use_spans)
						uses_stmt[blk.id][stmt_i] |= self._ref_binding_ids_in_expr(tgt)
					self._collect_ref_uses_in_expr(stmt.value, blk.id, ref_uses, ref_use_spans)
					uses_stmt[blk.id][stmt_i] |= self._ref_binding_ids_in_expr(stmt.value)
					if isinstance(tgt, H.HPlaceExpr) and not tgt.projections and isinstance(tgt.base, H.HVar):
						tgt_bid = getattr(tgt.base, "binding_id", None)
						if tgt_bid is not None and self._call_returns_optional_ref(stmt.value):
							self._synthetic_ref_binding_ids.add(int(tgt_bid))
						if self._is_ref_binding_id(getattr(tgt.base, "binding_id", None)):
							def_by_block[blk.id].add(int(tgt.base.binding_id))
							defs_stmt[blk.id][stmt_i].add(int(tgt.base.binding_id))
							ref_defs.setdefault(int(tgt.base.binding_id), blk.id)
				elif hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
					self._collect_ref_uses_in_expr(stmt.target, blk.id, ref_uses, ref_use_spans)
					self._collect_ref_uses_in_expr(stmt.value, blk.id, ref_uses, ref_use_spans)
					uses_stmt[blk.id][stmt_i] |= self._ref_binding_ids_in_expr(stmt.target)
					uses_stmt[blk.id][stmt_i] |= self._ref_binding_ids_in_expr(stmt.value)
					if isinstance(stmt.target, H.HPlaceExpr) and isinstance(stmt.target.base, H.HVar):
						if self._is_ref_binding_id(getattr(stmt.target.base, "binding_id", None)):
							def_by_block[blk.id].add(int(stmt.target.base.binding_id))
							defs_stmt[blk.id][stmt_i].add(int(stmt.target.base.binding_id))
							ref_defs.setdefault(int(stmt.target.base.binding_id), blk.id)
				elif isinstance(stmt, H.HReturn) and stmt.value is not None:
					self._collect_ref_uses_in_expr(stmt.value, blk.id, ref_uses, ref_use_spans)
					uses_stmt[blk.id][stmt_i] |= self._ref_binding_ids_in_expr(stmt.value)
				elif isinstance(stmt, H.HThrow):
					self._collect_ref_uses_in_expr(stmt.value, blk.id, ref_uses, ref_use_spans)
					uses_stmt[blk.id][stmt_i] |= self._ref_binding_ids_in_expr(stmt.value)
				elif isinstance(stmt, H.HExprStmt):
					self._collect_ref_uses_in_expr(stmt.expr, blk.id, ref_uses, ref_use_spans)
					uses_stmt[blk.id][stmt_i] |= self._ref_binding_ids_in_expr(stmt.expr)
			if blk.terminator:
				if blk.terminator.cond is not None:
					self._collect_ref_uses_in_expr(blk.terminator.cond, blk.id, ref_uses, ref_use_spans)
				if blk.terminator.value is not None:
					self._collect_ref_uses_in_expr(blk.terminator.value, blk.id, ref_uses, ref_use_spans)
				uses_term[blk.id] |= self._ref_binding_ids_in_expr(blk.terminator.cond)
				uses_term[blk.id] |= self._ref_binding_ids_in_expr(blk.terminator.value)

		for rid, blocks_using in ref_uses.items():
			for bid in blocks_using:
				use_by_block[bid].add(rid)

		if not ref_defs:
			self._ref_live_after_stmt = None
			self._ref_no_use_ids = None
			return None

		def _smallest_scope_containing(block_id: int) -> Optional[Set[int]]:
			best: Optional[Set[int]] = None
			best_size = 0
			for s in scopes:
				if block_id not in s:
					continue
				if best is None or len(s) < best_size:
					best = s
					best_size = len(s)
			return best

		live_in: Dict[int, Set[int]] = {blk.id: set() for blk in blocks}
		live_out: Dict[int, Set[int]] = {blk.id: set() for blk in blocks}
		changed = True
		while changed:
			changed = False
			for blk in reversed(blocks):
				out_set: Set[int] = set()
				for succ in succs.get(blk.id, []):
					out_set |= live_in.get(succ, set())
				in_set = set(use_by_block.get(blk.id, set()))
				in_set |= out_set - def_by_block.get(blk.id, set())
				if out_set != live_out.get(blk.id, set()) or in_set != live_in.get(blk.id, set()):
					live_out[blk.id] = out_set
					live_in[blk.id] = in_set
					changed = True

		ref_live_after_stmt: Dict[int, List[Set[int]]] = {}
		for blk in blocks:
			n = len(blk.statements)
			if n == 0:
				ref_live_after_stmt[blk.id] = []
				continue
			live: Set[int] = set(live_out.get(blk.id, set()))
			live |= set(uses_term.get(blk.id, set()))
			live_after: List[Set[int]] = [set() for _ in range(n)]
			for i in range(n - 1, -1, -1):
				live_after[i] = set(live)
				live = (live - defs_stmt[blk.id][i]) | uses_stmt[blk.id][i]
			ref_live_after_stmt[blk.id] = live_after
		self._ref_live_after_stmt = ref_live_after_stmt

		ref_regions: Dict[int, Set[int]] = {}
		no_use_ids: Set[int] = set()
		for rid, def_block in ref_defs.items():
			scope = _smallest_scope_containing(def_block)
			if scope is None:
				continue
			use_blocks = ref_uses.get(rid, set())
			if not use_blocks:
				# No tracked uses:
				# - `_`/underscore-prefixed bindings are treated as intentionally
				#   discarded and should not keep borrows alive past the statement.
				# - other bindings stay conservative (lexical scope).
				name = None
				if self.binding_names is not None:
					name = self.binding_names.get(rid)
				if isinstance(name, str) and name.startswith("_"):
					ref_regions[rid] = {def_block}
				else:
					ref_regions[rid] = set(scope)
					no_use_ids.add(rid)
				continue
			region = {bid for bid, live in live_in.items() if rid in live}
			region.add(def_block)
			region &= scope
			if not region:
				region = set(scope)
			ref_regions[rid] = set(region)

		witness_in: Dict[int, Dict[int, Span]] = {blk.id: {} for blk in blocks}
		changed = True
		while changed:
			changed = False
			for blk in reversed(blocks):
				new_map: Dict[int, Span] = {}
				for rid in live_in.get(blk.id, set()):
					span = ref_use_spans.get(rid, {}).get(blk.id)
					if span is not None:
						new_map[rid] = span
						continue
					for succ in succs.get(blk.id, []):
						succ_map = witness_in.get(succ, {})
						if rid in succ_map:
							new_map[rid] = succ_map[rid]
							break
				if new_map != witness_in.get(blk.id, {}):
					witness_in[blk.id] = new_map
					changed = True

		self._ref_witness_in = witness_in
		self._ref_no_use_ids = no_use_ids

		return ref_regions

	def _ref_binding_ids_in_expr(self, expr: Optional[H.HExpr]) -> Set[int]:
		out: Set[int] = set()
		if expr is None:
			return out

		def _walk(node: H.HExpr) -> None:
			if hasattr(H, "HPlaceExpr") and isinstance(node, getattr(H, "HPlaceExpr")):
				_walk(node.base)
				for proj in node.projections:
					if isinstance(proj, H.HPlaceIndex):
						_walk(proj.index)
				return
			if isinstance(node, H.HVar):
				bid_id = getattr(node, "binding_id", None)
				if self._is_ref_binding_id(bid_id):
					out.add(int(bid_id))
				return
			if isinstance(node, H.HField):
				_walk(node.subject)
				return
			if isinstance(node, H.HIndex):
				_walk(node.subject)
				_walk(node.index)
				return
			if isinstance(node, H.HBorrow):
				_walk(node.subject)
				return
			if isinstance(node, H.HCall):
				_walk(node.fn)
				for a in node.args:
					_walk(a)
				for kw in node.kwargs:
					_walk(kw.value)
				return
			if isinstance(node, H.HMethodCall):
				_walk(node.receiver)
				for a in node.args:
					_walk(a)
				for kw in node.kwargs:
					_walk(kw.value)
				return
			if isinstance(node, H.HInvoke):
				_walk(node.callee)
				for a in node.args:
					_walk(a)
				for kw in node.kwargs:
					_walk(kw.value)
				return
			if isinstance(node, H.HBinary):
				_walk(node.left)
				_walk(node.right)
				return
			if isinstance(node, H.HUnary):
				_walk(node.expr)
				return
			if isinstance(node, H.HTernary):
				_walk(node.cond)
				_walk(node.then_expr)
				_walk(node.else_expr)
				return
			if isinstance(node, H.HResultOk):
				_walk(node.value)
				return
			if isinstance(node, H.HArrayLiteral):
				for el in node.elements:
					_walk(el)
				return
			if hasattr(H, "HMatchExpr") and isinstance(node, getattr(H, "HMatchExpr")):
				_walk(node.scrutinee)
				for arm in node.arms:
					for stmt in arm.block.statements:
						if isinstance(stmt, H.HLet):
							_walk(stmt.value)
						elif isinstance(stmt, H.HAssign):
							_walk(stmt.target)
							_walk(stmt.value)
						elif hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
							_walk(stmt.target)
							_walk(stmt.value)
						elif hasattr(H, "HExprStmt") and isinstance(stmt, getattr(H, "HExprStmt")):
							_walk(stmt.expr)
						elif hasattr(H, "HReturn") and isinstance(stmt, getattr(H, "HReturn")):
							_walk(stmt.value)
						elif hasattr(H, "HBreak") and isinstance(stmt, getattr(H, "HBreak")):
							val = getattr(stmt, "value", None)
							if val is not None:
								_walk(val)
						elif hasattr(H, "HContinue") and isinstance(stmt, getattr(H, "HContinue")):
							pass
					if arm.result is not None:
						_walk(arm.result)
				return

		_walk(expr)
		return out

	def _ref_binding_id_from_expr(self, expr: H.HExpr) -> Optional[int]:
		bid_id: Optional[int] = None
		if isinstance(expr, H.HVar):
			bid_id = getattr(expr, "binding_id", None)
		elif isinstance(expr, H.HPlaceExpr) and not expr.projections and isinstance(expr.base, H.HVar):
			bid_id = getattr(expr.base, "binding_id", None)
		if self._is_ref_binding_id(bid_id):
			return int(bid_id)
		return None

	def _expr_references_any_binder(self, expr: Optional[H.HExpr], binder_ids: Set[int]) -> bool:
		"""Return True iff `expr` (recursively) contains an `HVar`
		whose `binding_id` is in `binder_ids`.

		Used by the match-arm-binder escape detector: when an HAssign
		/ HLet RHS contains an arm-binder reference and the target
		binding is outside the arm scope, we treat that as an escape.
		"""
		if expr is None or not binder_ids:
			return False
		found = [False]

		def _walk(node: object) -> None:
			if found[0] or node is None:
				return
			if isinstance(node, H.HVar):
				bid = getattr(node, "binding_id", None)
				if bid is not None and int(bid) in binder_ids:
					found[0] = True
				return
			if hasattr(H, "HPlaceExpr") and isinstance(node, getattr(H, "HPlaceExpr")):
				_walk(node.base)
				for proj in node.projections:
					if isinstance(proj, H.HPlaceIndex):
						_walk(proj.index)
				return
			if isinstance(node, H.HField):
				_walk(node.subject); return
			if isinstance(node, H.HIndex):
				_walk(node.subject); _walk(node.index); return
			if isinstance(node, H.HBorrow):
				_walk(node.subject); return
			if isinstance(node, H.HCall):
				_walk(node.fn)
				for a in node.args:
					_walk(a)
				for kw in node.kwargs:
					_walk(kw.value)
				return
			if isinstance(node, H.HMethodCall):
				_walk(node.receiver)
				for a in node.args:
					_walk(a)
				for kw in node.kwargs:
					_walk(kw.value)
				return
			if isinstance(node, H.HInvoke):
				_walk(node.callee)
				for a in node.args:
					_walk(a)
				for kw in node.kwargs:
					_walk(kw.value)
				return
			if isinstance(node, H.HBinary):
				_walk(node.left); _walk(node.right); return
			if isinstance(node, H.HUnary):
				_walk(node.expr); return
			if isinstance(node, H.HTernary):
				_walk(node.cond); _walk(node.then_expr); _walk(node.else_expr); return
			if isinstance(node, H.HResultOk):
				_walk(node.value); return
			if isinstance(node, H.HArrayLiteral):
				for el in node.elements:
					_walk(el)
				return
			# Conservatively no-op for nodes we don't recognize —
			# escape detection is one-way (false-negative is the
			# safe direction for the user-facing diagnostic surface;
			# false-positive would block valid programs).

		_walk(expr)
		return found[0]

	def _arm_binder_target_bid(self, expr: object) -> Optional[int]:
		"""Best-effort: extract the root binding id of an assignment
		target.  Returns None for non-binding-rooted targets (e.g.
		anonymous places)."""
		if isinstance(expr, H.HVar):
			return getattr(expr, "binding_id", None)
		if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
			return self._arm_binder_target_bid(expr.base)
		if isinstance(expr, H.HField):
			return self._arm_binder_target_bid(expr.subject)
		if isinstance(expr, H.HIndex):
			return self._arm_binder_target_bid(expr.subject)
		return None

	def _expr_passes_binder_to_call(self, expr: Optional[object], binder_ids: Set[int]) -> bool:
		"""Return True iff `expr` (recursively) contains an HCall /
		HMethodCall / HInvoke whose arguments or receiver reference
		an arm binder in `binder_ids`.

		Conservative call-escape detection.  We can't see what a
		callee does with a borrowed argument; treat any call that
		takes an arm binder as potentially storing it.  The match
		handler then keeps the scrutinee loan live (rather than
		dropping it as the no-escape case does), so subsequent
		owner mutation / move / reassign is rejected by the
		standard loan-conflict check.

		This closes the UAF that direct-escape-only detection
		missed:

		    fn store(slot: &mut Optional<&mut T>, p: &mut T) { *slot = Some(p); }
		    val _ = match &mut r {
		        Ok(x) => { store(&mut leaked, x); 0 }, ...
		    };
		    r = make_err();   // ← without call detection, this was
		                       //   accepted; with it, rejected
		"""
		if expr is None or not binder_ids:
			return False
		found = [False]

		def _arg_has_binder(arg: object) -> bool:
			# An HBorrow of an arm-binder place is itself a
			# call-shaped escape — `f(&x)` or `f(&mut x)`.
			if isinstance(arg, H.HBorrow):
				return self._expr_references_any_binder(arg.subject, binder_ids)
			return self._expr_references_any_binder(arg, binder_ids)

		def _walk(node: object) -> None:
			if found[0] or node is None:
				return
			if isinstance(node, H.HCall):
				for a in node.args:
					if _arg_has_binder(a):
						found[0] = True
						return
				for kw in node.kwargs:
					if _arg_has_binder(kw.value):
						found[0] = True
						return
				_walk(node.fn)
				for a in node.args:
					_walk(a)
				for kw in node.kwargs:
					_walk(kw.value)
				return
			if isinstance(node, H.HMethodCall):
				if _arg_has_binder(node.receiver):
					found[0] = True
					return
				for a in node.args:
					if _arg_has_binder(a):
						found[0] = True
						return
				for kw in node.kwargs:
					if _arg_has_binder(kw.value):
						found[0] = True
						return
				_walk(node.receiver)
				for a in node.args:
					_walk(a)
				for kw in node.kwargs:
					_walk(kw.value)
				return
			if isinstance(node, H.HInvoke):
				for a in node.args:
					if _arg_has_binder(a):
						found[0] = True
						return
				for kw in node.kwargs:
					if _arg_has_binder(kw.value):
						found[0] = True
						return
				_walk(node.callee)
				for a in node.args:
					_walk(a)
				for kw in node.kwargs:
					_walk(kw.value)
				return
			# Recurse through other expression shapes.
			if hasattr(H, "HPlaceExpr") and isinstance(node, getattr(H, "HPlaceExpr")):
				_walk(node.base)
				for proj in node.projections:
					if isinstance(proj, H.HPlaceIndex):
						_walk(proj.index)
				return
			if isinstance(node, H.HField):
				_walk(node.subject); return
			if isinstance(node, H.HIndex):
				_walk(node.subject); _walk(node.index); return
			if isinstance(node, H.HBorrow):
				_walk(node.subject); return
			if isinstance(node, H.HBinary):
				_walk(node.left); _walk(node.right); return
			if isinstance(node, H.HUnary):
				_walk(node.expr); return
			if isinstance(node, H.HTernary):
				_walk(node.cond); _walk(node.then_expr); _walk(node.else_expr); return
			if isinstance(node, H.HResultOk):
				_walk(node.value); return
			if isinstance(node, H.HArrayLiteral):
				for el in node.elements:
					_walk(el)
				return

		_walk(expr)
		return found[0]

	def _arm_binder_escapes(self, arm_block: object, arm_result: Optional[object], arm_binder_ids: Set[int]) -> tuple[bool, bool]:
		"""Detect arm-binder escape.  Returns
		`(any_escape, store_to_outer)`:

		- `any_escape`: True if ANY escape shape was detected —
		  store-to-outer OR pass-to-call.  When True, the caller
		  must keep the scrutinee loan live so the standard
		  loan-conflict check rejects subsequent owner mutation /
		  move / reassign.

		- `store_to_outer`: True only for direct
		  HAssign / HLet whose target binding is outside the arm
		  scope and whose RHS references an arm binder.  This is
		  the shape the `&mut` rejection diagnostic targets — a
		  user explicitly stashing the binder past the arm.

		Indirect escape via intermediate locals (`val temp =
		arm_binder; outer = temp`) is a known v1 false-negative
		— the existing non-Copy borrow rules already reject the
		`val temp = arm_binder` step for `&mut` binders, but a
		future shape that slips through that check is not caught
		here.  Adding general taint propagation is future work.
		"""
		if not arm_binder_ids:
			return (False, False)
		any_escape = False
		store_to_outer = False
		statements = list(getattr(arm_block, "statements", []) or [])
		for stmt in statements:
			if isinstance(stmt, H.HLet):
				tgt_bid = getattr(stmt, "binding_id", None)
				outside = tgt_bid is None or int(tgt_bid) not in arm_binder_ids
				if outside and self._expr_references_any_binder(stmt.value, arm_binder_ids):
					any_escape = True
					store_to_outer = True
				if self._expr_passes_binder_to_call(stmt.value, arm_binder_ids):
					any_escape = True
			elif isinstance(stmt, H.HAssign):
				tgt_bid = self._arm_binder_target_bid(stmt.target)
				outside = tgt_bid is None or int(tgt_bid) not in arm_binder_ids
				if outside and self._expr_references_any_binder(stmt.value, arm_binder_ids):
					any_escape = True
					store_to_outer = True
				# An assignment whose value is itself a call passing
				# the binder is call-escape regardless of LHS.  Note
				# we do NOT flag `arm_binder.field = ...` here — that
				# is a legitimate write *through* the binder, not an
				# escape; the binder is used as a place root, not
				# stored elsewhere.  The target's root binding is
				# the arm binder itself in that case, which the
				# `outside` check above correctly skips.
				if self._expr_passes_binder_to_call(stmt.value, arm_binder_ids):
					any_escape = True
			elif hasattr(H, "HExprStmt") and isinstance(stmt, getattr(H, "HExprStmt")):
				# A bare expression-statement that calls a function
				# passing the binder (e.g. `store(&mut leaked, x);`).
				if self._expr_passes_binder_to_call(stmt.expr, arm_binder_ids):
					any_escape = True
			elif hasattr(H, "HReturn") and isinstance(stmt, getattr(H, "HReturn")):
				val = getattr(stmt, "value", None)
				if val is not None and self._expr_passes_binder_to_call(val, arm_binder_ids):
					any_escape = True
		# Arm result expression: detect call-shape only.  A bare
		# arm-result HVar of an arm binder isn't an escape — its
		# lifetime is bounded by the match expression — but a call
		# in the arm result that passes the binder counts.
		if arm_result is not None and self._expr_passes_binder_to_call(arm_result, arm_binder_ids):
			any_escape = True
		return (any_escape, store_to_outer)

	def _collect_binding_ids_for_name_in_expr(self, expr: H.HExpr, name: str, out: Set[int]) -> None:
		if isinstance(expr, H.HVar):
			if expr.name == name and getattr(expr, "binding_id", None) is not None:
				out.add(int(expr.binding_id))
			return
		if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
			self._collect_binding_ids_for_name_in_expr(expr.base, name, out)
			for proj in expr.projections:
				if isinstance(proj, H.HPlaceIndex):
					self._collect_binding_ids_for_name_in_expr(proj.index, name, out)
			return
		if isinstance(expr, H.HField):
			self._collect_binding_ids_for_name_in_expr(expr.subject, name, out)
			return
		if isinstance(expr, H.HIndex):
			self._collect_binding_ids_for_name_in_expr(expr.subject, name, out)
			self._collect_binding_ids_for_name_in_expr(expr.index, name, out)
			return
		if isinstance(expr, H.HBorrow):
			self._collect_binding_ids_for_name_in_expr(expr.subject, name, out)
			return
		if hasattr(H, "HCopy") and isinstance(expr, getattr(H, "HCopy")):
			self._collect_binding_ids_for_name_in_expr(expr.subject, name, out)
			return
		if hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
			self._collect_binding_ids_for_name_in_expr(expr.subject, name, out)
			return
		if isinstance(expr, H.HCall):
			if not (isinstance(expr.fn, H.HVar) and getattr(expr.fn, "binding_id", None) is None):
				self._collect_binding_ids_for_name_in_expr(expr.fn, name, out)
			for a in expr.args:
				self._collect_binding_ids_for_name_in_expr(a, name, out)
			for kw in expr.kwargs:
				self._collect_binding_ids_for_name_in_expr(kw.value, name, out)
			return
		if isinstance(expr, H.HMethodCall):
			self._collect_binding_ids_for_name_in_expr(expr.receiver, name, out)
			for a in expr.args:
				self._collect_binding_ids_for_name_in_expr(a, name, out)
			for kw in expr.kwargs:
				self._collect_binding_ids_for_name_in_expr(kw.value, name, out)
			return
		if isinstance(expr, H.HInvoke):
			self._collect_binding_ids_for_name_in_expr(expr.callee, name, out)
			for a in expr.args:
				self._collect_binding_ids_for_name_in_expr(a, name, out)
			for kw in expr.kwargs:
				self._collect_binding_ids_for_name_in_expr(kw.value, name, out)
			return
		if isinstance(expr, H.HBinary):
			self._collect_binding_ids_for_name_in_expr(expr.left, name, out)
			self._collect_binding_ids_for_name_in_expr(expr.right, name, out)
			return
		if isinstance(expr, H.HUnary):
			self._collect_binding_ids_for_name_in_expr(expr.expr, name, out)
			return
		if isinstance(expr, H.HTernary):
			self._collect_binding_ids_for_name_in_expr(expr.cond, name, out)
			self._collect_binding_ids_for_name_in_expr(expr.then_expr, name, out)
			self._collect_binding_ids_for_name_in_expr(expr.else_expr, name, out)
			return
		if hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
			self._collect_binding_ids_for_name_in_expr(expr.scrutinee, name, out)
			for arm in expr.arms:
				self._collect_binding_ids_for_name_in_block(arm.block, name, out)
				if arm.result is not None:
					self._collect_binding_ids_for_name_in_expr(arm.result, name, out)
			return
		if isinstance(expr, H.HArrayLiteral):
			for el in expr.elements:
				self._collect_binding_ids_for_name_in_expr(el, name, out)
			return
		if isinstance(expr, H.HResultOk):
			self._collect_binding_ids_for_name_in_expr(expr.value, name, out)
			return

	def _collect_binding_ids_for_name_in_block(self, block: H.HBlock, name: str, out: Set[int]) -> None:
		for stmt in block.statements:
			if isinstance(stmt, H.HLocalConst):
				if stmt.name == name and getattr(stmt, "binding_id", None) is not None:
					out.add(int(stmt.binding_id))
			elif isinstance(stmt, H.HLet):
				if stmt.name == name and getattr(stmt, "binding_id", None) is not None:
					out.add(int(stmt.binding_id))
				self._collect_binding_ids_for_name_in_expr(stmt.value, name, out)
			elif isinstance(stmt, H.HAssign):
				self._collect_binding_ids_for_name_in_expr(stmt.target, name, out)
				self._collect_binding_ids_for_name_in_expr(stmt.value, name, out)
			elif hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
				self._collect_binding_ids_for_name_in_expr(stmt.target, name, out)
				self._collect_binding_ids_for_name_in_expr(stmt.value, name, out)
			elif isinstance(stmt, H.HExprStmt):
				self._collect_binding_ids_for_name_in_expr(stmt.expr, name, out)
			elif isinstance(stmt, H.HReturn) and stmt.value is not None:
				self._collect_binding_ids_for_name_in_expr(stmt.value, name, out)
			elif isinstance(stmt, H.HIf):
				self._collect_binding_ids_for_name_in_expr(stmt.cond, name, out)
				self._collect_binding_ids_for_name_in_block(stmt.then_block, name, out)
				if stmt.else_block is not None:
					self._collect_binding_ids_for_name_in_block(stmt.else_block, name, out)
			elif isinstance(stmt, H.HLoop):
				self._collect_binding_ids_for_name_in_block(stmt.body, name, out)
			elif isinstance(stmt, H.HTry):
				self._collect_binding_ids_for_name_in_block(stmt.body, name, out)
				for arm in stmt.catches:
					self._collect_binding_ids_for_name_in_block(arm.block, name, out)
			elif isinstance(stmt, H.HBlock):
				self._collect_binding_ids_for_name_in_block(stmt, name, out)

	def _binding_ids_for_name_in_block(self, block: H.HBlock, name: str) -> Set[int]:
		out: Set[int] = set()
		if name in self._binding_id_by_name:
			out.add(int(self._binding_id_by_name[name]))
		self._collect_binding_ids_for_name_in_block(block, name, out)
		return out

	def _call_returns_optional_ref(self, expr: H.HExpr) -> bool:
		if self.call_info_by_callsite_id is None:
			return False
		callsite_id = getattr(expr, "callsite_id", None)
		info = self.call_info_by_callsite_id.get(callsite_id) if callsite_id is not None else None
		if info is None:
			return False
		ty = info.sig.user_ret_type
		return self._is_optional_ref_type(ty, is_mut=True)

	def _borrow_from_optional_ref_call(
		self,
		state: _FlowState,
		expr: H.HExpr,
		dst_rid: Optional[int],
	) -> None:
		"""
		If `dst_rid` is an Optional<&T>/Optional<&mut T> binding and `expr` is a call
		that returns such a value, borrow the receiver/arg0 place and tie the loan
		to `dst_rid`. This keeps &mut iterators honest: you can't call `next()` again
		while the prior element borrow is live.
		"""
		if dst_rid is None:
			return
		ty = self.binding_types.get(dst_rid) if self.binding_types is not None else None
		if ty is None and self.call_info_by_callsite_id is not None:
			callsite_id = getattr(expr, "callsite_id", None)
			info = self.call_info_by_callsite_id.get(callsite_id) if callsite_id is not None else None
			if info is not None:
				ty = info.sig.user_ret_type
		if ty is None:
			return
		kind: Optional[LoanKind] = None
		if self._is_optional_ref_type(ty, is_mut=True):
			kind = LoanKind.MUT
		if kind is None:
			return
		place_expr: Optional[H.HExpr] = None
		if isinstance(expr, H.HCall):
			if expr.args:
				place_expr = expr.args[0]
		elif isinstance(expr, H.HMethodCall):
			place_expr = expr.receiver
		elif isinstance(expr, H.HInvoke):
			if expr.args:
				place_expr = expr.args[0]
		elif isinstance(expr, H.HField):
			self._borrow_from_optional_ref_call(state, expr.subject, dst_rid)
			return
		elif isinstance(expr, H.HIndex):
			self._borrow_from_optional_ref_call(state, expr.subject, dst_rid)
			return
		elif hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
			self._borrow_from_optional_ref_call(state, expr.base, dst_rid)
			return
		if isinstance(place_expr, H.HBorrow):
			place_expr = place_expr.subject
		if place_expr is None:
			return
		place = place_from_expr(place_expr, base_lookup=self.base_lookup)
		if place is None:
			return
		self._borrow_place(
			state,
			place,
			kind,
			temporary=False,
			span=getattr(expr, "loc", Span()),
			ref_binding_id=int(dst_rid),
		)

	def _collect_ref_uses_in_expr(
		self,
		expr: H.HExpr,
		bid: int,
		ref_uses: Dict[int, Set[int]],
		ref_use_spans: Optional[Dict[int, Dict[int, Span]]] = None,
	) -> None:
		if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
			self._collect_ref_uses_in_expr(expr.base, bid, ref_uses, ref_use_spans)
			for proj in expr.projections:
				if isinstance(proj, H.HPlaceIndex):
					self._collect_ref_uses_in_expr(proj.index, bid, ref_uses, ref_use_spans)
			return
		if hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
			self._collect_ref_uses_in_expr(expr.scrutinee, bid, ref_uses, ref_use_spans)
			for arm in expr.arms:
				for stmt in arm.block.statements:
					if isinstance(stmt, H.HLet):
						self._collect_ref_uses_in_expr(stmt.value, bid, ref_uses, ref_use_spans)
					elif isinstance(stmt, H.HAssign):
						self._collect_ref_uses_in_expr(stmt.target, bid, ref_uses, ref_use_spans)
						self._collect_ref_uses_in_expr(stmt.value, bid, ref_uses, ref_use_spans)
					elif hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
						self._collect_ref_uses_in_expr(stmt.target, bid, ref_uses, ref_use_spans)
						self._collect_ref_uses_in_expr(stmt.value, bid, ref_uses, ref_use_spans)
					elif hasattr(H, "HExprStmt") and isinstance(stmt, getattr(H, "HExprStmt")):
						self._collect_ref_uses_in_expr(stmt.expr, bid, ref_uses, ref_use_spans)
					elif hasattr(H, "HReturn") and isinstance(stmt, getattr(H, "HReturn")):
						self._collect_ref_uses_in_expr(stmt.value, bid, ref_uses, ref_use_spans)
					elif hasattr(H, "HBreak") and isinstance(stmt, getattr(H, "HBreak")):
						val = getattr(stmt, "value", None)
						if val is not None:
							self._collect_ref_uses_in_expr(val, bid, ref_uses, ref_use_spans)
					elif hasattr(H, "HContinue") and isinstance(stmt, getattr(H, "HContinue")):
						pass
				if arm.result is not None:
					self._collect_ref_uses_in_expr(arm.result, bid, ref_uses, ref_use_spans)
			return
		if isinstance(expr, H.HVar):
			bid_id = getattr(expr, "binding_id", None)
			if bid_id is not None:
				if self._is_ref_binding_id(int(bid_id)):
					ref_uses.setdefault(bid_id, set()).add(bid)
					if ref_use_spans is not None:
						ref_use_spans.setdefault(bid_id, {}).setdefault(bid, getattr(expr, "loc", Span()))
			return
		if isinstance(expr, H.HField):
			self._collect_ref_uses_in_expr(expr.subject, bid, ref_uses, ref_use_spans)
			return
		if isinstance(expr, H.HIndex):
			self._collect_ref_uses_in_expr(expr.subject, bid, ref_uses, ref_use_spans)
			self._collect_ref_uses_in_expr(expr.index, bid, ref_uses, ref_use_spans)
			return
		if isinstance(expr, H.HBorrow):
			self._collect_ref_uses_in_expr(expr.subject, bid, ref_uses, ref_use_spans)
			return
		if isinstance(expr, H.HCall):
			self._collect_ref_uses_in_expr(expr.fn, bid, ref_uses, ref_use_spans)
			for a in expr.args:
				self._collect_ref_uses_in_expr(a, bid, ref_uses, ref_use_spans)
			for kw in expr.kwargs:
				self._collect_ref_uses_in_expr(kw.value, bid, ref_uses, ref_use_spans)
			return
		if isinstance(expr, H.HMethodCall):
			self._collect_ref_uses_in_expr(expr.receiver, bid, ref_uses, ref_use_spans)
			for a in expr.args:
				self._collect_ref_uses_in_expr(a, bid, ref_uses, ref_use_spans)
			for kw in expr.kwargs:
				self._collect_ref_uses_in_expr(kw.value, bid, ref_uses, ref_use_spans)
			return
		if isinstance(expr, H.HInvoke):
			self._collect_ref_uses_in_expr(expr.callee, bid, ref_uses, ref_use_spans)
			for a in expr.args:
				self._collect_ref_uses_in_expr(a, bid, ref_uses, ref_use_spans)
			for kw in expr.kwargs:
				self._collect_ref_uses_in_expr(kw.value, bid, ref_uses, ref_use_spans)
			return
		if isinstance(expr, H.HBinary):
			self._collect_ref_uses_in_expr(expr.left, bid, ref_uses, ref_use_spans)
			self._collect_ref_uses_in_expr(expr.right, bid, ref_uses, ref_use_spans)
			return
		if isinstance(expr, H.HUnary):
			self._collect_ref_uses_in_expr(expr.expr, bid, ref_uses, ref_use_spans)
			return
		if isinstance(expr, H.HTernary):
			self._collect_ref_uses_in_expr(expr.cond, bid, ref_uses, ref_use_spans)
			self._collect_ref_uses_in_expr(expr.then_expr, bid, ref_uses, ref_use_spans)
			self._collect_ref_uses_in_expr(expr.else_expr, bid, ref_uses, ref_use_spans)
			return
		if isinstance(expr, H.HArrayLiteral):
			for e in expr.elements:
				self._collect_ref_uses_in_expr(e, bid, ref_uses, ref_use_spans)
			return
		if isinstance(expr, H.HResultOk):
			self._collect_ref_uses_in_expr(expr.value, bid, ref_uses, ref_use_spans)
			return

	def _reachable_forward(self, start: int, succs: Dict[int, List[int]]) -> Set[int]:
		seen: Set[int] = set()
		q: deque[int] = deque([start])
		while q:
			bid = q.popleft()
			if bid in seen:
				continue
			seen.add(bid)
			for s in succs.get(bid, []):
				if s not in seen:
					q.append(s)
		return seen

	def _reachable_backward(self, starts: Set[int], preds: Dict[int, List[int]]) -> Set[int]:
		seen: Set[int] = set()
		q: deque[int] = deque(starts)
		while q:
			bid = q.popleft()
			if bid in seen:
				continue
			seen.add(bid)
			for p in preds.get(bid, []):
				if p not in seen:
					q.append(p)
		return seen

	def _visit_expr(
		self,
		state: _FlowState,
		expr: H.HExpr,
		*,
		consume: bool = False,
		escapes: bool = True,
	) -> None:
		"""
		Traverse expressions and enforce implicit moves in consuming positions.

		This is the single place to extend when new HIR forms appear (e.g.,
		dereference or pattern matching). The walker must visit all
		subexpressions so that moves through calls, arithmetic, literals, etc.
		are properly tracked.
		"""
		if isinstance(expr, (H.HVar, H.HField, H.HIndex, H.HPlaceExpr)):
			place = place_from_expr(expr, base_lookup=self.base_lookup)
			if place is not None:
				self._consume_place_value(state, place, consuming=consume, span=getattr(expr, "loc", Span()))
			return
		if isinstance(expr, H.HLambda):
			self._apply_lambda_capture_moves(state, expr)
			return
		if isinstance(expr, H.HBorrow):
			place = place_from_expr(expr.subject, base_lookup=self.base_lookup)
			if place is None:
				if expr.is_mut:
					self._diagnostic("cannot borrow from a non-lvalue expression", getattr(expr, "loc", Span()))
					return
				if not bool(getattr(expr, "allow_rvalue", False)):
					self._diagnostic("cannot borrow from a non-lvalue expression", getattr(expr, "loc", Span()))
					return
				# Phase 1: compiler-synthesized shared borrow of rvalue receivers is
				# lowered via temporary materialization in MIR; no place-loan to track.
				self._visit_expr(state, expr.subject, consume=False, escapes=False)
				return
			self._borrow_place(
				state,
				place,
				LoanKind.MUT if expr.is_mut else LoanKind.SHARED,
				temporary=not escapes,
				span=getattr(expr, "loc", Span()),
			)
			return
		if hasattr(H, "HCopy") and isinstance(expr, getattr(H, "HCopy")):
			place = place_from_expr(expr.subject, base_lookup=self.base_lookup)
			if place is None:
				self._diagnostic("copy operand must be an addressable place in v1 (local/param/field/index)", getattr(expr, "loc", Span()))
				return
			ty = self._type_of_place(place)
			if not self._is_copy(ty):
				self._diagnostic(f"cannot copy '{user_facing_binding_name(place.base.name)}': type is not Copy", getattr(expr, "loc", Span()))
				return
			self._consume_place_use(state, place, getattr(expr, "loc", Span()))
			return
		if hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
			place = place_from_expr(expr.subject, base_lookup=self.base_lookup)
			if place is None:
				self._diagnostic("move operand must be an addressable place", getattr(expr, "loc", Span()))
				return
			self._force_move_place_use(state, place, getattr(expr, "loc", Span()))
			return
		if isinstance(expr, H.HCall):
			if isinstance(expr.fn, H.HLambda):
				self._apply_lambda_capture_moves(state, expr.fn)
				pre_loans = set(state.loans)
				self._add_lambda_capture_loans(state, expr.fn)
				for arg in expr.args:
					self._visit_expr(state, arg, consume=False, escapes=False)
				for kw in expr.kwargs:
					self._visit_expr(state, kw.value, consume=False, escapes=False)
				# Call executes here; keep capture loans live for the duration of the call.
				new_loans = state.loans - pre_loans
				state.loans -= {ln for ln in new_loans if ln.temporary}
				return
			# swap/replace are builtin place-manipulation operations.
			#
			# They mutate their first argument (and `swap` mutates both). For borrow
			# checking we must treat them as writes to their place operands, not as a
			# regular call that only evaluates values.
			intrinsic_kind = self._intrinsic_name_for_call(expr)
			if intrinsic_kind is IntrinsicKind.SWAP:
				if len(expr.args) != 2:
					raise AssertionError("swap expects exactly 2 arguments (checker bug)")
				a_expr, b_expr = expr.args
				a_place_expr = a_expr.subject if isinstance(a_expr, H.HBorrow) and a_expr.is_mut else a_expr
				b_place_expr = b_expr.subject if isinstance(b_expr, H.HBorrow) and b_expr.is_mut else b_expr
				a_place = place_from_expr(a_place_expr, base_lookup=self.base_lookup)
				b_place = place_from_expr(b_place_expr, base_lookup=self.base_lookup)
				if a_place is None:
					raise AssertionError("swap argument 0 must be an addressable place (checker bug)")
				if b_place is None:
					raise AssertionError("swap argument 1 must be an addressable place (checker bug)")
				# swap reads both places (use-after-move checks) and then writes both.
				self._consume_place_use(state, a_place, getattr(a_place_expr, "loc", Span()))
				self._consume_place_use(state, b_place, getattr(b_place_expr, "loc", Span()))
				if not self._reject_write_while_borrowed(state, a_place, getattr(a_place_expr, "loc", Span())):
					return
				if not self._reject_write_while_borrowed(state, b_place, getattr(b_place_expr, "loc", Span())):
					return
				# swap preserves initialized state when it succeeds.
				self._set_state(state, a_place, PlaceState.VALID)
				self._set_state(state, b_place, PlaceState.VALID)
				return
			if intrinsic_kind is IntrinsicKind.REPLACE:
				if len(expr.args) != 2:
					raise AssertionError("replace expects exactly 2 arguments (checker bug)")
				place_expr, new_expr = expr.args
				place_base = place_expr.subject if isinstance(place_expr, H.HBorrow) and place_expr.is_mut else place_expr
				place = place_from_expr(place_base, base_lookup=self.base_lookup)
				if place is not None:
					# Inline-borrow / direct place form — we can track
					# state of the underlying place precisely.  Read the
					# old value (use-after-move), lower the replacement,
					# reject write-while-borrowed conflicts, and mark
					# the place VALID after the swap.
					self._consume_place_use(state, place, getattr(place_base, "loc", Span()))
					self._visit_expr(state, new_expr, consume=True, escapes=False)
					if not self._reject_write_while_borrowed(state, place, getattr(place_base, "loc", Span())):
						return
					self._set_state(state, place, PlaceState.VALID)
					return
				# Named &mut T value (HVar binding, parameter, method-call
				# return, etc.).  The underlying place's borrow rights were
				# already validated when the &mut T was formed at its
				# binding site; the borrow's liveness covers this write.
				# We just need to visit the ref-valued arg expression as a
				# read (use-after-move / move-checks on the local holding
				# the ref) and consume the replacement.  Place-level state
				# tracking is skipped — we don't have direct access to the
				# underlying place from a ref-valued expression.
				self._visit_expr(state, place_expr, consume=False, escapes=False)
				self._visit_expr(state, new_expr, consume=True, escapes=False)
				return

			pre_loans = set(state.loans)
			resolution = self.call_resolutions.get(expr.node_id) if self.call_resolutions is not None else None
			call_info = self._call_info_for_expr(expr)
			callee_is_value = True
			if isinstance(expr.fn, H.HVar) and getattr(expr.fn, "binding_id", None) is None:
				callee_is_value = False
			if hasattr(H, "HQualifiedMember") and isinstance(expr.fn, getattr(H, "HQualifiedMember")):
				callee_is_value = False
			if callee_is_value:
				self._visit_expr(state, expr.fn, consume=False, escapes=False)
			sig = self._resolve_sig_for_call(expr)
			_call_fn_id = self._resolve_fn_id_for_call(expr)
			if self._has_escape_annotations(_call_fn_id, sig):
				for idx, arg in enumerate(expr.args):
					param_index = self._param_index_for_call(sig, arg_index=idx)
					if param_index is None:
						continue
					if self._effective_escape_level(_call_fn_id, sig, param_index) not in (EscapeLevel.LOCAL, EscapeLevel.SCOPED):
						continue
					if isinstance(arg, H.HLambda):
						self._add_lambda_capture_loans(state, arg)
				for kw in expr.kwargs:
					param_index = self._param_index_for_call(sig, kw_name=kw.name)
					if param_index is None:
						continue
					if self._effective_escape_level(_call_fn_id, sig, param_index) not in (EscapeLevel.LOCAL, EscapeLevel.SCOPED):
						continue
					if isinstance(kw.value, H.HLambda):
						self._add_lambda_capture_loans(state, kw.value)
			param_types = None
			if call_info is not None:
				param_types = list(call_info.sig.param_types)
			elif isinstance(resolution, CallableDecl):
				param_types = list(resolution.signature.param_types)
			elif self.enable_auto_borrow:
				param_types = self._param_types_for_call(expr)
			for idx, arg in enumerate(expr.args):
				pty = param_types[idx] if param_types and idx < len(param_types) else None
				if isinstance(arg, H.HLambda) and not _is_callback_wrapper_call(expr):
					# Skip all escape checks when the current call is a callback
					# wrapper (callback0/1/2 etc.).  Escape is enforced at the
					# outer THREAD/STATIC-annotated call site via the transparent-
					# wrapper propagation branch below.  The type checker owns the
					# coercion-check path for direct wrapper calls.
					if sig is not None:
						pi = self._param_index_for_call(sig, arg_index=idx)
						required = self._effective_escape_level(_call_fn_id, sig, pi) if pi is not None else EscapeLevel.THREAD
						from_unannotated = self._is_unannotated_param(_call_fn_id, sig, pi) if pi is not None else True
						self._check_lambda_escape_level(arg, state, required, getattr(arg, "loc", getattr(expr, "loc", Span())), from_unannotated=from_unannotated)
					else:
						self._report_lambda_escape_if_borrowed(arg, span=getattr(arg, "loc", getattr(expr, "loc", Span())))
				elif sig is not None and _is_callback_wrapper_call(arg):
					# Transparent-wrapper propagation: a THREAD/STATIC/SCOPED-
					# annotated outer call receives callback0/1/2(lambda).
					# Propagate the outer escape level to the inner lambda.
					inner_lam = _unwrap_callback_lambda(arg)
					if inner_lam is not None:
						pi = self._param_index_for_call(sig, arg_index=idx)
						required = self._effective_escape_level(_call_fn_id, sig, pi) if pi is not None else EscapeLevel.THREAD
						from_unannotated = self._is_unannotated_param(_call_fn_id, sig, pi) if pi is not None else True
						self._check_lambda_escape_level(inner_lam, state, required, getattr(inner_lam, "loc", getattr(expr, "loc", Span())), from_unannotated=from_unannotated)
				self._visit_call_arg_with_param(
					state,
					arg_expr=arg,
					param_ty=pty,
					call_span=getattr(expr, "loc", Span()),
				)
			for kw in expr.kwargs:
				kw_index = self._param_index_for_call(sig, kw_name=kw.name) if sig is not None else None
				pty = param_types[kw_index] if (param_types and kw_index is not None and kw_index < len(param_types)) else None
				if isinstance(kw.value, H.HLambda) and not _is_callback_wrapper_call(expr):
					if sig is not None:
						pi = kw_index
						required = self._effective_escape_level(_call_fn_id, sig, pi) if pi is not None else EscapeLevel.THREAD
						from_unannotated = self._is_unannotated_param(_call_fn_id, sig, pi) if pi is not None else True
						self._check_lambda_escape_level(kw.value, state, required, getattr(kw.value, "loc", getattr(expr, "loc", Span())), from_unannotated=from_unannotated)
					else:
						self._report_lambda_escape_if_borrowed(kw.value, span=getattr(kw.value, "loc", getattr(expr, "loc", Span())))
				elif sig is not None and _is_callback_wrapper_call(kw.value):
					inner_lam = _unwrap_callback_lambda(kw.value)
					if inner_lam is not None:
						pi = kw_index
						required = self._effective_escape_level(_call_fn_id, sig, pi) if pi is not None else EscapeLevel.THREAD
						from_unannotated = self._is_unannotated_param(_call_fn_id, sig, pi) if pi is not None else True
						self._check_lambda_escape_level(inner_lam, state, required, getattr(inner_lam, "loc", getattr(expr, "loc", Span())), from_unannotated=from_unannotated)
				self._visit_call_arg_with_param(
					state,
					arg_expr=kw.value,
					param_ty=pty,
					call_span=getattr(expr, "loc", Span()),
				)
			new_loans = state.loans - pre_loans
			state.loans -= {ln for ln in new_loans if ln.temporary}
			return
		if isinstance(expr, H.HMethodCall):
			pre_loans = set(state.loans)
			sig = self._resolve_sig_for_call(expr)
			_call_fn_id = self._resolve_fn_id_for_call(expr)
			if self._has_escape_annotations(_call_fn_id, sig):
				for idx, arg in enumerate(expr.args):
					param_index = self._param_index_for_call(sig, arg_index=idx)
					if param_index is None:
						continue
					if self._effective_escape_level(_call_fn_id, sig, param_index) not in (EscapeLevel.LOCAL, EscapeLevel.SCOPED):
						continue
					if isinstance(arg, H.HLambda):
						self._add_lambda_capture_loans(state, arg)
				for kw in expr.kwargs:
					param_index = self._param_index_for_call(sig, kw_name=kw.name)
					if param_index is None:
						continue
					if self._effective_escape_level(_call_fn_id, sig, param_index) not in (EscapeLevel.LOCAL, EscapeLevel.SCOPED):
						continue
					if isinstance(kw.value, H.HLambda):
						self._add_lambda_capture_loans(state, kw.value)
			resolution = self.call_resolutions.get(expr.node_id) if self.call_resolutions is not None else None
			call_info = self._call_info_for_expr(expr)
			param_types, param_offset, params_include_receiver, receiver_autoborrow = self._method_call_param_layout(
				expr,
				resolution=resolution,
				call_info=call_info,
			)
			# No legacy fallback; method resolution metadata is expected when auto-borrowing.
			recv_kind: Optional[LoanKind] = None
			if param_types and params_include_receiver:
				pty = param_types[0]
				if pty is not None:
					td = self.type_table.get(pty)
					if td.kind is TypeKind.REF:
						if td.ref_mut is True:
							recv_kind = LoanKind.MUT
						elif td.ref_mut is False:
							recv_kind = LoanKind.SHARED
			deferred_recv: Optional[tuple[Place, LoanKind, Span]] = None
			if recv_kind is not None or receiver_autoborrow is not None:
				recv_expr = expr.receiver.subject if isinstance(expr.receiver, H.HBorrow) else expr.receiver
				recv_place = place_from_expr(recv_expr, base_lookup=self.base_lookup)
				if recv_place is not None and (recv_kind is not None or receiver_autoborrow is not None):
					kind_to_use = recv_kind
					if kind_to_use is None and receiver_autoborrow is not None:
						kind_to_use = LoanKind.MUT if receiver_autoborrow is SelfMode.SELF_BY_REF_MUT else LoanKind.SHARED
					if kind_to_use is not None:
						deferred_recv = (recv_place, kind_to_use, getattr(expr.receiver, "loc", Span()))
				else:
					self._visit_expr(state, expr.receiver, consume=False, escapes=False)
			elif param_types and params_include_receiver:
				pty = param_types[0]
				if pty is not None and self.type_table.get(pty).kind is not TypeKind.REF:
					if self._reject_noncopy_projected_byvalue_arg(expr.receiver, fallback_span=getattr(expr, "loc", Span())):
						return
					self._consume_expr(state, expr.receiver, escapes=False)
				else:
					self._visit_expr(state, expr.receiver, consume=False, escapes=False)
			else:
				self._visit_expr(state, expr.receiver, consume=False, escapes=False)

			for idx, arg in enumerate(expr.args):
				if isinstance(arg, H.HBorrow):
					self._visit_expr(state, arg, consume=False, escapes=False)
					continue
				param_idx = idx + param_offset
				pty = param_types[param_idx] if (param_types and param_idx < len(param_types)) else None
				if isinstance(arg, H.HLambda):
					if sig is not None:
						pi = self._param_index_for_call(sig, arg_index=idx)
						required = self._effective_escape_level(_call_fn_id, sig, pi) if pi is not None else EscapeLevel.THREAD
						from_unannotated = self._is_unannotated_param(_call_fn_id, sig, pi) if pi is not None else True
						self._check_lambda_escape_level(arg, state, required, getattr(arg, "loc", getattr(expr, "loc", Span())), from_unannotated=from_unannotated)
					else:
						self._report_lambda_escape_if_borrowed(arg, span=getattr(arg, "loc", getattr(expr, "loc", Span())))
				self._visit_call_arg_with_param(
					state,
					arg_expr=arg,
					param_ty=pty,
					call_span=getattr(expr, "loc", Span()),
				)
			for kw in expr.kwargs:
				kw_index = self._param_index_for_call(sig, kw_name=kw.name) if sig is not None else None
				if param_types and kw_index is not None and kw_index < len(param_types):
					pty = param_types[kw_index]
				else:
					pty = None
				if isinstance(kw.value, H.HLambda):
					if sig is not None:
						pi = kw_index
						required = self._effective_escape_level(_call_fn_id, sig, pi) if pi is not None else EscapeLevel.THREAD
						from_unannotated = self._is_unannotated_param(_call_fn_id, sig, pi) if pi is not None else True
						self._check_lambda_escape_level(kw.value, state, required, getattr(kw.value, "loc", getattr(expr, "loc", Span())), from_unannotated=from_unannotated)
					else:
						self._report_lambda_escape_if_borrowed(kw.value, span=getattr(kw.value, "loc", getattr(expr, "loc", Span())))
				self._visit_call_arg_with_param(
					state,
					arg_expr=kw.value,
					param_ty=pty,
					call_span=getattr(expr, "loc", Span()),
				)
			if deferred_recv is not None:
				recv_place, kind_to_use, span = deferred_recv
				self._borrow_place(state, recv_place, kind_to_use, temporary=True, span=span)
			new_loans = state.loans - pre_loans
			state.loans -= {ln for ln in new_loans if ln.temporary}
			return
		if isinstance(expr, H.HInvoke):
			pre_loans = set(state.loans)
			sig = self._resolve_sig_for_call(expr)
			self._visit_expr(state, expr.callee, consume=False, escapes=False)
			call_info = self._call_info_for_expr(expr)
			param_types = list(call_info.sig.param_types) if call_info is not None else None
			for idx, arg in enumerate(expr.args):
				pty = param_types[idx] if (param_types and idx < len(param_types)) else None
				if isinstance(arg, H.HLambda):
					if sig is not None:
						pi = self._param_index_for_call(sig, arg_index=idx)
						required = self._effective_escape_level(_call_fn_id, sig, pi) if pi is not None else EscapeLevel.THREAD
						from_unannotated = self._is_unannotated_param(_call_fn_id, sig, pi) if pi is not None else True
						self._check_lambda_escape_level(arg, state, required, getattr(arg, "loc", getattr(expr, "loc", Span())), from_unannotated=from_unannotated)
					else:
						self._report_lambda_escape_if_borrowed(arg, span=getattr(arg, "loc", getattr(expr, "loc", Span())))
				self._visit_call_arg_with_param(
					state,
					arg_expr=arg,
					param_ty=pty,
					call_span=getattr(expr, "loc", Span()),
				)
			for kw in expr.kwargs:
				kw_index = self._param_index_for_call(sig, kw_name=kw.name) if sig is not None else None
				pty = param_types[kw_index] if (param_types and kw_index is not None and kw_index < len(param_types)) else None
				if isinstance(kw.value, H.HLambda):
					if sig is not None:
						pi = kw_index
						required = self._effective_escape_level(_call_fn_id, sig, pi) if pi is not None else EscapeLevel.THREAD
						from_unannotated = self._is_unannotated_param(_call_fn_id, sig, pi) if pi is not None else True
						self._check_lambda_escape_level(kw.value, state, required, getattr(kw.value, "loc", getattr(expr, "loc", Span())), from_unannotated=from_unannotated)
					else:
						self._report_lambda_escape_if_borrowed(kw.value, span=getattr(kw.value, "loc", getattr(expr, "loc", Span())))
				self._visit_call_arg_with_param(
					state,
					arg_expr=kw.value,
					param_ty=pty,
					call_span=getattr(expr, "loc", Span()),
				)
			new_loans = state.loans - pre_loans
			state.loans -= {ln for ln in new_loans if ln.temporary}
			return
		if isinstance(expr, H.HBinary):
			self._visit_expr(state, expr.left, consume=False, escapes=False)
			self._visit_expr(state, expr.right, consume=False, escapes=False)
			return
		if isinstance(expr, H.HUnary):
			self._visit_expr(state, expr.expr, consume=False, escapes=False)
			return
		if isinstance(expr, H.HTernary):
			self._visit_expr(state, expr.cond, consume=False, escapes=False)
			self._visit_expr(state, expr.then_expr, consume=False, escapes=False)
			self._visit_expr(state, expr.else_expr, consume=False, escapes=False)
			return
		if hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
			# G1+G2 (narrower-C model with conservative call-escape
			# detection, 2026-04-29):
			#
			# Snapshot loans before visiting the scrutinee.  After
			# all arms are processed, decide what to do with the
			# loans the scrutinee introduced:
			#
			#   - No escape detected (no store-to-outer AND no call
			#     passes the binder): drop new scrutinee loans.
			#     The scrutinee borrow's lifetime ends at the match
			#     expression for both `&` and `&mut` forms.
			#   - Any escape detected (store-to-outer OR call
			#     passing binder): KEEP the new loans live.  This
			#     extends the F2 owner-extends-lifetime contract
			#     conservatively to all escape shapes — subsequent
			#     owner mutation / move / reassign is rejected by
			#     the standard loan-conflict check, closing the UAF
			#     a call-stashed binder would otherwise enable.
			#   - Direct store-to-outer escape on `&mut` ALSO emits
			#     a clear diagnostic so the user sees the issue
			#     pointed at the escape site, not just at the
			#     downstream owner-mutation rejection.
			#
			# Calls passing arm binders (e.g. stdlib `it.next()` in
			# `match self { Ctor(it) => it.next() }`) are NOT
			# rejected — they keep the loan live but the function
			# body usually has no further operation on the scrutinee
			# after the match, so the conservative loan-keeping is
			# invisible to those patterns.  When a user does follow
			# up with a scrutinee borrow / move, they get a clear
			# loan-conflict diagnostic that points back to the
			# match's escape.
			pre_scrut_loans = set(state.loans)
			self._visit_expr(state, expr.scrutinee, consume=False, escapes=False)
			new_loans = set(state.loans) - pre_scrut_loans
			any_mut_loan = any(ln.kind is LoanKind.MUT for ln in new_loans)
			any_escape = False
			for arm in expr.arms:
				arm_state = _FlowState(place_states=dict(state.place_states), loans=set(state.loans))
				arm_binder_ids: Set[int] = set()
				for bname in getattr(arm, "binders", []) or []:
					bids = self._binding_ids_for_name_in_block(arm.block, bname)
					if arm.result is not None:
						self._collect_binding_ids_for_name_in_expr(arm.result, bname, bids)
					self._set_state(arm_state, Place(PlaceBase(PlaceKind.LOCAL, -1, bname)), PlaceState.VALID)
					for bid in sorted(bids):
						base = self._base_for_binding(bid)
						if base is None:
							base = PlaceBase(PlaceKind.LOCAL, bid, bname)
						self._set_state(arm_state, Place(base), PlaceState.VALID)
						arm_binder_ids.add(int(bid))
				arm_block = BasicBlock(id=self._current_block_id or 0, statements=list(arm.block.statements), terminator=None)
				arm_state = self._transfer_block(arm_block, arm_state)
				if arm.result is not None:
					self._visit_expr(arm_state, arm.result, consume=consume, escapes=escapes)
				# G2: detect any arm-binder escape (store + call shapes).
				escape, store_to_outer = self._arm_binder_escapes(
					arm.block, arm.result, arm_binder_ids,
				)
				if escape:
					any_escape = True
					if store_to_outer and any_mut_loan:
						# Direct &mut store-style escape: emit a
						# clear diagnostic at the escape site.
						# Call-style escape on &mut is left to the
						# loan-conflict check downstream — it's
						# conservative-but-sound, and rejecting at
						# the call would break load-bearing
						# stdlib patterns like `match self {
						# Ctor(it) => it.next() }`.
						self._diagnostic(
							"&mut match arm binder must not escape the match arm; "
							"this would extend exclusive access to the scrutinee",
							getattr(arm, "loc", Span()),
						)
			# G1 / G2: drop loans only when no escape was detected.
			# Otherwise keep them live — F2 owner-extends contract
			# for shared, conservative-but-sound for mut.
			if not any_escape:
				state.loans -= new_loans
			return
		if isinstance(expr, H.HResultOk):
			self._visit_expr(state, expr.value, consume=False, escapes=False)
			return
		if isinstance(expr, H.HArrayLiteral):
			for el in expr.elements:
				self._visit_expr(state, el, consume=False, escapes=False)
			return
		# Literals and other rvalues need no action.

	def _transfer_block(self, block: BasicBlock, in_state: _FlowState) -> _FlowState:
		"""
		Transfer function for a single basic block: walk statements and mutate
		state to produce the outgoing place-state map.
		"""
		prev_block = self._current_block_id
		prev_stmt_span = self._current_stmt_span
		prev_stmt_index = self._current_stmt_index
		prev_block_stmts = self._current_block_stmts
		self._current_block_id = block.id
		self._current_block_stmts = block.statements
		try:
			state = _FlowState(
				place_states=dict(in_state.place_states),
				loans=self._filter_live_loans(in_state.loans, block.id),
			)
			catch_binder = self._catch_binders_by_block.get(block.id)
			if catch_binder is not None:
				shadow_block = H.HBlock(statements=list(block.statements))
				bids = self._binding_ids_for_name_in_block(shadow_block, catch_binder)
				self._set_state(state, Place(PlaceBase(PlaceKind.LOCAL, -1, catch_binder)), PlaceState.VALID)
				for bid in sorted(bids):
					base = self._base_for_binding(bid)
					if base is None:
						base = PlaceBase(PlaceKind.LOCAL, bid, catch_binder)
					self._set_state(state, Place(base), PlaceState.VALID)
			for stmt_i, stmt in enumerate(block.statements):
				stmt_loc = getattr(stmt, "loc", None)
				self._current_stmt_span = stmt_loc if isinstance(stmt_loc, Span) else Span.from_loc(stmt_loc)
				self._current_stmt_index = stmt_i
				if isinstance(stmt, H.HLet):
					if isinstance(stmt.value, H.HBorrow):
						place = place_from_expr(stmt.value.subject, base_lookup=self.base_lookup)
						if place is None:
							if stmt.value.is_mut:
								self._diagnostic("cannot borrow from a non-lvalue expression", getattr(stmt.value, "loc", Span()))
							elif bool(getattr(stmt.value, "allow_rvalue", False)):
								self._visit_expr(state, stmt.value.subject, consume=False, escapes=False)
							else:
								self._diagnostic("cannot borrow from a non-lvalue expression", getattr(stmt.value, "loc", Span()))
						else:
							self._borrow_place(
								state,
								place,
								LoanKind.MUT if stmt.value.is_mut else LoanKind.SHARED,
								span=getattr(stmt.value, "loc", Span()),
								ref_binding_id=getattr(stmt, "binding_id", None),
							)
					else:
						if isinstance(stmt.value, H.HLambda):
							self._report_lambda_escape_if_borrowed(stmt.value, span=getattr(stmt.value, "loc", Span()))
						self._consume_expr(state, stmt.value, escapes=False)
					if getattr(stmt, "binding_id", None) is not None:
						base = PlaceBase(PlaceKind.LOCAL, stmt.binding_id, stmt.name)
					else:
						base = self.base_lookup(H.HVar(stmt.name))
					if base is not None:
						self._set_state(state, Place(base), PlaceState.VALID)
					dst_rid = getattr(stmt, "binding_id", None)
					if self._is_ref_binding_id(dst_rid) or self._call_returns_optional_ref(stmt.value):
						src_rid = self._ref_binding_id_from_expr(stmt.value)
						if src_rid is not None:
							self._clone_loans_from_ref(state, src_rid, int(dst_rid), drop_dst=False)
						else:
							self._borrow_from_optional_ref_call(state, stmt.value, dst_rid)
				elif isinstance(stmt, H.HAssign):
					tgt_place = place_from_expr(stmt.target, base_lookup=self.base_lookup)
					if (
						isinstance(stmt.value, H.HBorrow)
						and isinstance(stmt.target, H.HPlaceExpr)
						and not stmt.target.projections
						and isinstance(stmt.target.base, H.HVar)
					):
						bid = getattr(stmt.target.base, "binding_id", None)
						if bid is not None and self.binding_types is not None:
							ty = self.binding_types.get(bid)
							if ty is not None and self.type_table.get(ty).kind is TypeKind.REF:
								# Rebind: drop any prior loan tied to this ref binding id.
								state.loans = {ln for ln in state.loans if ln.ref_binding_id != bid}
								place = place_from_expr(stmt.value.subject, base_lookup=self.base_lookup)
								if place is None:
									if stmt.value.is_mut:
										self._diagnostic("cannot borrow from a non-lvalue expression", getattr(stmt.value, "loc", Span()))
									elif bool(getattr(stmt.value, "allow_rvalue", False)):
										self._visit_expr(state, stmt.value.subject, consume=False, escapes=False)
									else:
										self._diagnostic("cannot borrow from a non-lvalue expression", getattr(stmt.value, "loc", Span()))
								else:
									self._borrow_place(
										state,
										place,
										LoanKind.MUT if stmt.value.is_mut else LoanKind.SHARED,
										span=getattr(stmt.value, "loc", Span()),
										ref_binding_id=bid,
									)
								if tgt_place is not None:
									self._set_state(state, tgt_place, PlaceState.VALID)
								self._drop_dead_ref_bound_loans_after_stmt(state, block.id, stmt_i)
								continue
					self._consume_expr(state, stmt.value, escapes=False)
					if tgt_place is not None:
						# MVP rule: do not silently "drop" active borrows on assignment.
						# Instead, reject the write while any *live* loan overlaps the target.
						tgt_span = getattr(stmt.target, "loc", None)
						if not isinstance(tgt_span, Span):
							tgt_span = Span.from_loc(tgt_span)
						if tgt_span.line is None and self._current_stmt_span is not None and self._current_stmt_span.line is not None:
							tgt_span = self._current_stmt_span
						if self._reject_write_while_borrowed(state, tgt_place, tgt_span):
							self._set_state(state, tgt_place, PlaceState.VALID)
					else:
						tgt_span = getattr(stmt.target, "loc", None)
						if not isinstance(tgt_span, Span):
							tgt_span = Span.from_loc(tgt_span)
						if tgt_span.line is None and self._current_stmt_span is not None and self._current_stmt_span.line is not None:
							tgt_span = self._current_stmt_span
						self._diagnostic("assignment target is not an lvalue", tgt_span)
					if (
						isinstance(stmt.target, H.HPlaceExpr)
						and not stmt.target.projections
						and isinstance(stmt.target.base, H.HVar)
						and not isinstance(stmt.value, H.HBorrow)
					):
						dst_rid = getattr(stmt.target.base, "binding_id", None)
						if self._is_ref_binding_id(dst_rid) or self._call_returns_optional_ref(stmt.value):
							src_rid = self._ref_binding_id_from_expr(stmt.value)
							if src_rid is not None:
								self._clone_loans_from_ref(state, src_rid, int(dst_rid), drop_dst=True)
							else:
								self._borrow_from_optional_ref_call(state, stmt.value, dst_rid)
				elif hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
					# Augmented assignment reads and writes the target place.
					#
					# Read: use-after-move checks apply because `x += y` must read the old `x`.
					# Write: freeze-while-borrowed applies because it mutates the place.
					self._consume_expr(state, stmt.value, escapes=False)
					tgt = place_from_expr(stmt.target, base_lookup=self.base_lookup)
					tgt_span = getattr(stmt, "loc", getattr(stmt.target, "loc", Span()))
					if tgt is None:
						self._diagnostic("assignment target is not an lvalue", tgt_span)
						self._drop_dead_ref_bound_loans_after_stmt(state, block.id, stmt_i)
						continue
					# Read the old value (may mark moved for move-only types if used as a value).
					self._consume_place_use(state, tgt, tgt_span)
					# Write the new value.
					if self._reject_write_while_borrowed(state, tgt, tgt_span):
						self._set_state(state, tgt, PlaceState.VALID)
				elif isinstance(stmt, H.HReturn):
					if stmt.value is not None:
						if isinstance(stmt.value, H.HLambda):
							self._report_lambda_escape_if_borrowed(stmt.value, span=getattr(stmt.value, "loc", Span()))
						self._consume_expr(state, stmt.value, escapes=False)
				elif isinstance(stmt, H.HExprStmt):
					self._eval_temporary(state, stmt.expr)
				elif isinstance(stmt, H.HThrow):
					self._consume_expr(state, stmt.value, escapes=False)
				# other stmts: continue
				self._drop_dead_ref_bound_loans_after_stmt(state, block.id, stmt_i)

			# Terminator expressions
			term = block.terminator
			if term and term.kind == "branch" and term.cond is not None:
				self._eval_temporary(state, term.cond)
			# return/throw values were evaluated (and temp-borrow dropped) in the stmt loop

			return state
		finally:
			self._current_block_id = prev_block
			self._current_stmt_span = prev_stmt_span
			self._current_stmt_index = prev_stmt_index
			self._current_block_stmts = prev_block_stmts

	def check_block(self, block: H.HBlock) -> List[Diagnostic]:
		"""Run move tracking on a HIR block by building a CFG and flowing states."""
		self.diagnostics.clear()
		self._catch_binders_by_block = {}
		self._synthetic_ref_binding_ids.clear()
		blocks, entry_id, scopes = self._build_cfg(block)
		# Build region info for explicit borrows.
		#
		# NLL-lite: borrow bindings live until last use within their lexical scope.
		# We approximate scope boundaries using the structured-CFG construction:
		# each nested HIR block corresponds to a set of CFG blocks, and the borrow
		# lifetime is capped to that smallest enclosing scope.
		self._ref_live_blocks = self._build_regions(blocks, scopes)
		if self._ref_live_blocks is None:
			self._ref_witness_in = None
			self._ref_live_after_stmt = None
			self._ref_no_use_ids = None
		self._block_facts_in = self._build_block_facts(blocks)
		in_states: Dict[int, _FlowState] = {b.id: _FlowState() for b in blocks}
		# Parameters are initialized at function entry.
		entry_state = in_states.get(entry_id)
		if entry_state is not None and self.fn_types:
			for base in self.fn_types.keys():
				if base.kind is PlaceKind.PARAM or base.name == "self":
					entry_state.place_states[Place(base)] = PlaceState.VALID
		worklist = [entry_id]
		while worklist:
			bid = worklist.pop()
			blk = blocks[bid]
			in_state = in_states[bid]
			out_state = self._transfer_block(blk, in_state)
			succs = blk.terminator.targets if blk.terminator else []
			for succ in succs:
				prev = in_states.get(succ, _FlowState())
				merged = self._merge_states(prev, out_state, succ)
				if merged != prev:
					in_states[succ] = merged
					worklist.append(succ)
		return self.diagnostics

	def _merge_states(self, a: _FlowState, b: _FlowState, block_id: int) -> _FlowState:
		"""Join two place-state maps using merge_place_state as the meet operator."""
		result = _FlowState(place_states=dict(a.place_states), loans=self._filter_live_loans(a.loans, block_id))
		for place, state_b in b.place_states.items():
			state_a = result.place_states.get(place, PlaceState.UNINIT)
			if state_a is state_b:
				continue
			result.place_states[place] = merge_place_state(state_a, state_b)
		# Region-aware merge: keep only loans live at this join.
		result.loans |= self._filter_live_loans(b.loans, block_id)
		return result

	def _build_cfg(self, block: H.HBlock) -> Tuple[List[BasicBlock], int, List[Set[int]]]:
		"""
		Lower a structured HIR block into a rudimentary CFG.

		Each HIf/HLoop/HTry introduces new blocks with branch/jump terminators.
		Tail statements after a control construct are placed in a continuation
		block so successors join correctly. Return/throw terminate a block with
		no successors.
		"""
		blocks: List[BasicBlock] = []
		# Each `build(...)` invocation corresponds to one lexical scope (HIR block)
		# and returns the set of CFG blocks created for that scope. We keep those
		# sets so borrow regions can approximate lexical lifetimes without NLL.
		scope_sets: List[Set[int]] = []

		def new_block() -> BasicBlock:
			bb = BasicBlock(id=len(blocks))
			blocks.append(bb)
			return bb

		exit_block = new_block()
		exit_block.terminator = Terminator(kind="jump", targets=[])
		exit_id = exit_block.id

		def add_backedge(body_ids: List[int], body_entry: int, exit_id: int) -> None:
			for bid in body_ids:
				bb = blocks[bid]
				if bb.terminator and exit_id in bb.terminator.targets and bb.terminator.kind == "jump":
					if body_entry not in bb.terminator.targets:
						bb.terminator.targets.append(body_entry)

		def build(stmts: List[H.HStmt], cont: int) -> Tuple[int, List[int]]:
			bb = new_block()
			ids = [bb.id]
			idx = 0
			while idx < len(stmts):
				stmt = stmts[idx]
				if isinstance(stmt, H.HBlock):
					# Inline nested blocks so move/borrow tracking sees desugared constructs.
					stmts = stmts[:idx] + stmt.statements + stmts[idx + 1 :]
					continue
				if isinstance(stmt, H.HIf):
					tail = stmts[idx + 1 :]
					cont_entry, cont_ids = (cont, [])
					if tail:
						cont_entry, cont_ids = build(tail, cont)
					then_entry, then_ids = build(stmt.then_block.statements, cont_entry)
					else_entry, else_ids = build(stmt.else_block.statements if stmt.else_block else [], cont_entry)
					bb.terminator = Terminator(kind="branch", targets=[then_entry, else_entry], cond=stmt.cond)
					ids.extend(then_ids + else_ids + cont_ids)
					scope_sets.append(set(ids))
					return bb.id, ids
				if isinstance(stmt, H.HLoop):
					tail = stmts[idx + 1 :]
					cont_entry, cont_ids = (cont, [])
					if tail:
						cont_entry, cont_ids = build(tail, cont)
					body_entry, body_ids = build(stmt.body.statements, cont_entry)
					bb.terminator = Terminator(kind="branch", targets=[body_entry, cont_entry], cond=None)
					add_backedge(body_ids, body_entry, cont_entry)
					ids.extend(body_ids + cont_ids)
					scope_sets.append(set(ids))
					return bb.id, ids
				if isinstance(stmt, H.HTry):
					tail = stmts[idx + 1 :]
					cont_entry, cont_ids = (cont, [])
					if tail:
						cont_entry, cont_ids = build(tail, cont)
					body_entry, body_ids = build(stmt.body.statements, cont_entry)
					catch_entries = []
					catch_ids: List[int] = []
					for arm in stmt.catches:
						entry, ids_arm = build(arm.block.statements, cont_entry)
						catch_entries.append(entry)
						catch_ids.extend(ids_arm)
						if arm.binder is not None:
							self._catch_binders_by_block[entry] = arm.binder
					targets = [body_entry] + catch_entries
					bb.terminator = Terminator(kind="branch", targets=targets, cond=None)
					ids.extend(body_ids + catch_ids + cont_ids)
					scope_sets.append(set(ids))
					return bb.id, ids
				if isinstance(stmt, (H.HReturn, H.HThrow)):
					bb.statements.append(stmt)
					bb.terminator = Terminator(kind="return" if isinstance(stmt, H.HReturn) else "throw", targets=[], value=stmt.value)
					scope_sets.append(set(ids))
					return bb.id, ids
				bb.statements.append(stmt)
				idx += 1
			if bb.terminator is None:
				bb.terminator = Terminator(kind="jump", targets=[cont])
			scope_sets.append(set(ids))
			return bb.id, ids

		entry, ids = build(block.statements, exit_id)
		# Ensure the top-level scope is recorded.
		scope_sets.append(set(ids))
		return blocks, entry, scope_sets
