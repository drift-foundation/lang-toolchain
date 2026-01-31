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

from lang2.driftc import stage1 as H
from lang2.driftc.stage1 import closures as C
from lang2.driftc.stage1.capture_discovery import discover_captures
from lang2.driftc.borrow_checker import (
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
from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.core.span import Span
from lang2.driftc.core.types_core import TypeKind, TypeTable, TypeId
from lang2.driftc.checker import FnSignature
from lang2.driftc.method_registry import CallableDecl
from lang2.driftc.method_resolver import MethodResolution, SelfMode
from lang2.driftc.stage1.call_info import CallInfo, CallTargetKind, IntrinsicKind
from collections import deque


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
	_ref_witness_in: Optional[Dict[int, Dict[int, Span]]] = field(init=False, default=None, repr=False)
	_ref_live_after_stmt: Optional[Dict[int, List[Set[int]]]] = field(init=False, default=None, repr=False)
	_ref_no_use_ids: Optional[Set[int]] = field(init=False, default=None, repr=False)
	_block_facts_in: Optional[Dict[int, Set[Tuple[int, int]]]] = field(init=False, default=None, repr=False)

	def __post_init__(self) -> None:
		# Ensure we always have a binding_id -> TypeId mapping to avoid repeated scans.
		if self.binding_types is None:
			self.binding_types = {pb.local_id: ty for pb, ty in self.fn_types.items()}
		if self.binding_names is None:
			self.binding_names = {pb.local_id: pb.name for pb in self.fn_types.keys()}
		self._bases_by_binding: Dict[int, PlaceBase] = {pb.local_id: pb for pb in self.fn_types.keys()}
		self._binding_id_by_name: Dict[str, int] = {
			name: bid for bid, name in (self.binding_names or {}).items()
		}
		self._method_sig_by_key: Dict[Tuple[int, str], FnSignature] = {}
		if self.signatures_by_id:
			for sig in self.signatures_by_id.values():
				if sig.is_method and sig.impl_target_type_id is not None:
					key = (sig.impl_target_type_id, sig.method_name or sig.name)
					self._method_sig_by_key[key] = sig

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

	def _resolve_sig_for_call(self, expr: H.HExpr) -> Optional[FnSignature]:
		if not self.signatures_by_id:
			return None
		if isinstance(expr, H.HCall):
			resolution = self.call_resolutions.get(expr.node_id) if self.call_resolutions is not None else None
			if isinstance(resolution, CallableDecl):
				if resolution.fn_id is None:
					return None
				return self.signatures_by_id.get(resolution.fn_id)
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

	def _apply_lambda_capture_moves(self, state: _FlowState, lam: H.HLambda) -> None:
		self._check_lambda_captures(lam)
		for cap in lam.captures:
			if cap.kind is not C.HCaptureKind.MOVE:
				continue
			place = self._place_from_capture_key(cap.key)
			if place is None:
				continue
			self._force_move_place_use(state, place, cap.span)

	def _check_lambda_captures(self, lam: H.HLambda) -> None:
		if not lam.captures:
			result = discover_captures(lam)
			lam.captures = result.captures
			for d in result.diagnostics:
				self.diagnostics.append(d)
		if self.binding_types is None:
			return
		for cap in lam.captures:
			if cap.kind is not C.HCaptureKind.COPY:
				continue
			ty = self.binding_types.get(cap.key.root_local)
			if ty is None or not self._is_copy(ty):
				base = self._base_for_binding(int(cap.key.root_local))
				name = base.name if base is not None else str(cap.key.root_local)
				self._diagnostic(
					f"cannot copy '{name}': type is not Copy",
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

		return cls(
			type_table=type_table,
			fn_types=fn_types,
			binding_types=dict(typed_fn.binding_types),
			binding_names=dict(getattr(typed_fn, "binding_names", {}) or {}),
			binding_mutable=dict(getattr(typed_fn, "binding_mutable", {}) or {}),
			signatures_by_id=signatures_by_id,
			call_resolutions=getattr(typed_fn, "call_resolutions", None),
			call_info_by_callsite_id=getattr(typed_fn, "call_info_by_callsite_id", None),
			base_lookup=base_lookup,
			module_id=getattr(getattr(typed_fn, "fn_id", None), "module", None),
			enable_auto_borrow=enable_auto_borrow,
		)

	def _is_copy(self, ty: Optional[TypeId]) -> bool:
		"""Return True if the type is Copy per the core type table."""
		if ty is None:
			return False
		return bool(self.type_table.is_copy(ty))

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
			return False
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
		curr = self._state_for(state, place)
		if curr is PlaceState.MOVED:
			self._diagnostic(f"use after move of '{place.base.name}'", span, code="E_USE_AFTER_MOVE")
			return
		if curr is PlaceState.UNINIT:
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
		actual overlap semantics live in `lang2.driftc.borrow_checker.places_overlap`
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
				# No tracked uses: keep the lexical scope to stay conservative.
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
			if isinstance(node, H.HDVInit):
				for a in node.args:
					_walk(a)
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

	def _call_returns_optional_ref(self, expr: H.HExpr) -> bool:
		if self.call_info_by_callsite_id is None:
			return False
		callsite_id = getattr(expr, "callsite_id", None)
		info = self.call_info_by_callsite_id.get(callsite_id) if callsite_id is not None else None
		if info is None:
			return False
		ty = info.sig.user_ret_type
		return self._is_optional_ref_type(ty, is_mut=True) or self._is_optional_ref_type(ty, is_mut=False)

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
		elif self._is_optional_ref_type(ty, is_mut=False):
			kind = LoanKind.SHARED
		if kind is None:
			return
		place_expr: Optional[H.HExpr] = None
		if isinstance(expr, H.HCall):
			if expr.args:
				place_expr = expr.args[0]
		elif isinstance(expr, H.HMethodCall):
			place_expr = expr.receiver
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
				ty = None
				if self.binding_types is not None:
					ty = self.binding_types.get(bid_id)
				if ty is not None and self.type_table.get(ty).kind is TypeKind.REF:
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
		if isinstance(expr, H.HDVInit):
			for a in expr.args:
				self._collect_ref_uses_in_expr(a, bid, ref_uses, ref_use_spans)
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
				self._diagnostic("cannot borrow from a non-lvalue expression", getattr(expr, "loc", Span()))
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
				self._diagnostic("copy operand must be an addressable place in MVP (local/param/field/index)", getattr(expr, "loc", Span()))
				return
			ty = self._type_of_place(place)
			if not self._is_copy(ty):
				self._diagnostic(f"cannot copy '{place.base.name}': type is not Copy", getattr(expr, "loc", Span()))
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
				if place is None:
					raise AssertionError("replace argument 0 must be an addressable place (checker bug)")
				# replace reads the old value (use-after-move) and writes the new.
				self._consume_place_use(state, place, getattr(place_base, "loc", Span()))
				self._visit_expr(state, new_expr, consume=True, escapes=False)
				if not self._reject_write_while_borrowed(state, place, getattr(place_base, "loc", Span())):
					return
				self._set_state(state, place, PlaceState.VALID)
				return

			pre_loans = set(state.loans)
			resolution = self.call_resolutions.get(expr.node_id) if self.call_resolutions is not None else None
			call_info = None
			if self.call_info_by_callsite_id is not None:
				csid = getattr(expr, "callsite_id", None)
				if isinstance(csid, int):
					call_info = self.call_info_by_callsite_id.get(csid)
			callee_is_value = True
			if isinstance(expr.fn, H.HVar) and getattr(expr.fn, "binding_id", None) is None:
				callee_is_value = False
			if hasattr(H, "HQualifiedMember") and isinstance(expr.fn, getattr(H, "HQualifiedMember")):
				callee_is_value = False
			if callee_is_value:
				self._visit_expr(state, expr.fn, consume=False, escapes=False)
			sig = self._resolve_sig_for_call(expr)
			if sig and sig.param_nonretaining:
				for idx, arg in enumerate(expr.args):
					param_index = self._param_index_for_call(sig, arg_index=idx)
					if param_index is None or param_index >= len(sig.param_nonretaining):
						continue
					if sig.param_nonretaining[param_index] is not True:
						continue
					if isinstance(arg, H.HLambda):
						self._add_lambda_capture_loans(state, arg)
				for kw in expr.kwargs:
					param_index = self._param_index_for_call(sig, kw_name=kw.name)
					if param_index is None or param_index >= len(sig.param_nonretaining):
						continue
					if sig.param_nonretaining[param_index] is not True:
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
				kind_for_arg: Optional[LoanKind] = None
				if param_types and idx < len(param_types):
					pty = param_types[idx]
					if pty is not None:
						td = self.type_table.get(pty)
						if td.kind is TypeKind.REF:
							if td.ref_mut is True:
								kind_for_arg = LoanKind.MUT
							elif td.ref_mut is False:
								kind_for_arg = LoanKind.SHARED
				if kind_for_arg is not None:
					place_expr = arg.subject if isinstance(arg, H.HBorrow) else arg
					place = place_from_expr(place_expr, base_lookup=self.base_lookup)
					if place is not None:
						self._borrow_place(
							state,
							place,
							kind_for_arg,
							temporary=True,
							span=getattr(arg, "loc", Span()),
						)
						continue
				if param_types and idx < len(param_types):
					pty = param_types[idx]
					if pty is not None and self.type_table.get(pty).kind is not TypeKind.REF:
						self._consume_expr(state, arg, escapes=False)
						continue
				self._visit_expr(state, arg, consume=False, escapes=False)
			for kw in expr.kwargs:
				kind_for_kw: Optional[LoanKind] = None
				kw_index = self._param_index_for_call(sig, kw_name=kw.name) if sig is not None else None
				if param_types and kw_index is not None and kw_index < len(param_types):
					pty = param_types[kw_index]
					if pty is not None:
						td = self.type_table.get(pty)
						if td.kind is TypeKind.REF:
							if td.ref_mut is True:
								kind_for_kw = LoanKind.MUT
							elif td.ref_mut is False:
								kind_for_kw = LoanKind.SHARED
				if kind_for_kw is not None:
					place_expr = kw.value.subject if isinstance(kw.value, H.HBorrow) else kw.value
					place = place_from_expr(place_expr, base_lookup=self.base_lookup)
					if place is not None:
						self._borrow_place(
							state,
							place,
							kind_for_kw,
							temporary=True,
							span=getattr(kw.value, "loc", Span()),
						)
						continue
				if param_types and kw_index is not None and kw_index < len(param_types):
					pty = param_types[kw_index]
					if pty is not None and self.type_table.get(pty).kind is not TypeKind.REF:
						self._consume_expr(state, kw.value, escapes=False)
						continue
				self._visit_expr(state, kw.value, consume=False, escapes=False)
			new_loans = state.loans - pre_loans
			state.loans -= {ln for ln in new_loans if ln.temporary}
			return
		if isinstance(expr, H.HMethodCall):
			pre_loans = set(state.loans)
			sig = self._resolve_sig_for_call(expr)
			if sig and sig.param_nonretaining:
				for idx, arg in enumerate(expr.args):
					param_index = self._param_index_for_call(sig, arg_index=idx)
					if param_index is None or param_index >= len(sig.param_nonretaining):
						continue
					if sig.param_nonretaining[param_index] is not True:
						continue
					if isinstance(arg, H.HLambda):
						self._add_lambda_capture_loans(state, arg)
				for kw in expr.kwargs:
					param_index = self._param_index_for_call(sig, kw_name=kw.name)
					if param_index is None or param_index >= len(sig.param_nonretaining):
						continue
					if sig.param_nonretaining[param_index] is not True:
						continue
					if isinstance(kw.value, H.HLambda):
						self._add_lambda_capture_loans(state, kw.value)
			resolution = self.call_resolutions.get(expr.node_id) if self.call_resolutions is not None else None
			param_types = None
			receiver_autoborrow: Optional[SelfMode] = None
			if isinstance(resolution, MethodResolution):
				param_types = list(resolution.decl.signature.param_types)
				receiver_autoborrow = resolution.receiver_autoborrow
			# No legacy fallback; method resolution metadata is expected when auto-borrowing.
			recv_kind: Optional[LoanKind] = None
			if param_types:
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
			elif param_types:
				pty = param_types[0]
				if pty is not None and self.type_table.get(pty).kind is not TypeKind.REF:
					self._consume_expr(state, expr.receiver, escapes=False)
				else:
					self._visit_expr(state, expr.receiver, consume=False, escapes=False)
			else:
				self._visit_expr(state, expr.receiver, consume=False, escapes=False)

			for idx, arg in enumerate(expr.args):
				if isinstance(arg, H.HBorrow):
					self._visit_expr(state, arg, consume=False, escapes=False)
					continue
				kind_for_arg = None
				param_idx = idx + 1
				if param_types and param_idx < len(param_types):
					pty = param_types[param_idx]
					if pty is not None:
						td = self.type_table.get(pty)
						if td.kind is TypeKind.REF:
							if td.ref_mut is True:
								kind_for_arg = LoanKind.MUT
							elif td.ref_mut is False:
								kind_for_arg = LoanKind.SHARED
				if kind_for_arg is not None:
					place_expr = arg.subject if isinstance(arg, H.HBorrow) else arg
					place = place_from_expr(place_expr, base_lookup=self.base_lookup)
					if place is not None:
						self._borrow_place(
							state,
							place,
							kind_for_arg,
							temporary=True,
							span=getattr(arg, "loc", Span()),
						)
						continue
				if param_types and param_idx < len(param_types):
					pty = param_types[param_idx]
					if pty is not None and self.type_table.get(pty).kind is not TypeKind.REF:
						self._consume_expr(state, arg, escapes=False)
						continue
				self._visit_expr(state, arg, consume=False, escapes=False)
			for kw in expr.kwargs:
				kind_for_kw: Optional[LoanKind] = None
				kw_index = self._param_index_for_call(sig, kw_name=kw.name) if sig is not None else None
				if param_types and kw_index is not None and kw_index < len(param_types):
					pty = param_types[kw_index]
					if pty is not None:
						td = self.type_table.get(pty)
						if td.kind is TypeKind.REF:
							if td.ref_mut is True:
								kind_for_kw = LoanKind.MUT
							elif td.ref_mut is False:
								kind_for_kw = LoanKind.SHARED
				if kind_for_kw is not None:
					place_expr = kw.value.subject if isinstance(kw.value, H.HBorrow) else kw.value
					place = place_from_expr(place_expr, base_lookup=self.base_lookup)
					if place is not None:
						self._borrow_place(
							state,
							place,
							kind_for_kw,
							temporary=True,
							span=getattr(kw.value, "loc", Span()),
						)
						continue
				if param_types and kw_index is not None and kw_index < len(param_types):
					pty = param_types[kw_index]
					if pty is not None and self.type_table.get(pty).kind is not TypeKind.REF:
						self._consume_expr(state, kw.value, escapes=False)
						continue
				self._visit_expr(state, kw.value, consume=False, escapes=False)
			if deferred_recv is not None:
				recv_place, kind_to_use, span = deferred_recv
				self._borrow_place(state, recv_place, kind_to_use, temporary=True, span=span)
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
			self._visit_expr(state, expr.scrutinee, consume=False, escapes=False)
			for arm in expr.arms:
				arm_state = _FlowState(place_states=dict(state.place_states), loans=set(state.loans))
				for bname in getattr(arm, "binders", []) or []:
					bid = self._binding_id_by_name.get(bname)
					if bid is None:
						continue
					base = self._base_for_binding(bid)
					if base is None:
						base = PlaceBase(PlaceKind.LOCAL, bid, bname)
					self._set_state(arm_state, Place(base), PlaceState.VALID)
				arm_block = BasicBlock(id=self._current_block_id or 0, statements=list(arm.block.statements), terminator=None)
				_ = self._transfer_block(arm_block, arm_state)
				if arm.result is not None:
					self._visit_expr(arm_state, arm.result, consume=consume, escapes=escapes)
			return
		if isinstance(expr, H.HResultOk):
			self._visit_expr(state, expr.value, consume=False, escapes=False)
			return
		if isinstance(expr, H.HArrayLiteral):
			for el in expr.elements:
				self._visit_expr(state, el, consume=False, escapes=False)
			return
		if isinstance(expr, H.HDVInit):
			for a in expr.args:
				self._visit_expr(state, a, consume=False, escapes=False)
			return
		# Literals and other rvalues need no action.

	def _transfer_block(self, block: BasicBlock, in_state: _FlowState) -> _FlowState:
		"""
		Transfer function for a single basic block: walk statements and mutate
		state to produce the outgoing place-state map.
		"""
		prev_block = self._current_block_id
		self._current_block_id = block.id
		try:
			state = _FlowState(
				place_states=dict(in_state.place_states),
				loans=self._filter_live_loans(in_state.loans, block.id),
			)
			for stmt_i, stmt in enumerate(block.statements):
				if isinstance(stmt, H.HLet):
					if isinstance(stmt.value, H.HBorrow):
						place = place_from_expr(stmt.value.subject, base_lookup=self.base_lookup)
						if place is None:
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
						tgt_span = getattr(stmt.target, "loc", Span())
						if self._reject_write_while_borrowed(state, tgt_place, tgt_span):
							self._set_state(state, tgt_place, PlaceState.VALID)
					else:
						self._diagnostic("assignment target is not an lvalue", getattr(stmt.target, "loc", Span()))
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

	def check_block(self, block: H.HBlock) -> List[Diagnostic]:
		"""Run move tracking on a HIR block by building a CFG and flowing states."""
		self.diagnostics.clear()
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
