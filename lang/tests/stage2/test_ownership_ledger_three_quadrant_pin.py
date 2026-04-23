# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3A gate pin: end-to-end verdict comparison for the three-quadrant
match-scrut shapes from Phase 2a (dual-owner, POD variant, transient
rvalue).

This test exists because `test_match_scrut_copy_store_emits_copyvalue.py`
exercises only the MIR emission shape — it does not lower through the
driver-side ledger build / drain / compare path.  Per
`work/ownership-ledger/design.md` the 3A→3B gate requires zero verdict
disagreements (`ledger_stricter` / `site_stricter`) on the three-
quadrant pin.  Each quadrant here:

  1. lowers the same HIR fixture as the existing pin
  2. forces the ledger event log on (independent of the
     `DRIFT_COMPILER_DEBUG` env var, so the test does not leak global
     state)
  3. builds a `LiveStateMap` on the resulting MIR
  4. drains the recorded site 1/2 events
  5. runs `compare_events` with a `needs_drop` callable that reads the
     real `DropPolicy.needs_drop` axis (NOT the driver-side
     `TypeTable.has_drop` approximation — that approximation is a known
     3A telemetry tradeoff and must not be the basis for the pin)
  6. asserts the disagreement bucket has no leak-shape (`ledger_stricter`)
     and no ledger-bug-shape (`site_stricter`) records

Semantic-equivalent and path-dependent records are tolerated — they
indicate either provenance differences that collapse to the same drop
verdict (Tombstoned drop) or 3C queue material respectively, neither
of which gates 3A.
"""

from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import TypeId, TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage1 import assign_callsite_ids, assign_node_ids
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.stage2.ownership_ledger import build_ledger
from lang.driftc.stage2.ownership_ledger_events import DropDecisionLog
from lang.driftc.stage2.ownership_ledger_reporter import (
	CLASS_LEDGER_STRICTER,
	CLASS_SITE_STRICTER,
	compare_events,
)


def _make_v_variant(type_table: TypeTable) -> int:
	"""V<T> := Some(T) | None — Optional<T>-shaped variant.  Shared by
	all three quadrants."""
	return type_table.declare_variant(
		module_id="main",
		name="V",
		type_params=["T"],
		arms=[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)


def _collect_ctor_callinfo(hir: H.HBlock, type_table: TypeTable, var_tid: TypeId) -> dict[int, CallInfo]:
	"""Synthesize CallInfo entries for ctor calls in top-level HLet
	statements — same shape as the helper in
	`test_match_scrut_copy_store_emits_copyvalue.py`."""
	out: dict[int, CallInfo] = {}
	for stmt in hir.statements:
		if not (isinstance(stmt, H.HLet) and isinstance(stmt.value, H.HCall) and isinstance(stmt.value.fn, H.HQualifiedMember)):
			continue
		inst = type_table.get_variant_instance(var_tid)
		if inst is None:
			continue
		arm_def = inst.arms_by_name.get(stmt.value.fn.member)
		if arm_def is None:
			continue
		csid = getattr(stmt.value, "callsite_id", None)
		if isinstance(csid, int):
			out[csid] = CallInfo(
				target=CallTarget.constructor(var_tid, stmt.value.fn.member),
				sig=CallSig(param_types=tuple(arm_def.field_types), user_ret_type=var_tid, can_throw=False),
			)
	return out


def _build_match_arms() -> list[H.HMatchArm]:
	"""Some(s) => 1, None => 2 — shared arm shape across all three
	quadrants."""
	return [
		H.HMatchArm(
			ctor="Some",
			binders=["s"],
			block=H.HBlock(statements=[]),
			result=H.HLiteralInt(value=1),
			pattern_arg_form="positional",
			binder_field_indices=[0],
		),
		H.HMatchArm(
			ctor="None",
			binders=[],
			block=H.HBlock(statements=[]),
			result=H.HLiteralInt(value=2),
			pattern_arg_form="bare",
			binder_field_indices=[],
		),
	]


def _lower_with_ledger(hir: H.HBlock, type_table: TypeTable, var_tid: TypeId) -> tuple:
	"""Lower `hir` through HIRToMIR with the ledger log attached
	directly (bypassing env-var gating so the test does not depend on
	process state).  Returns `(mir_func, log, lower)` so callers can
	build the ledger and supply a real DropPolicy.needs_drop callable."""
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	call_info = _collect_ctor_callinfo(hir, type_table, var_tid)
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	lower = HIRToMIR(builder, type_table=type_table, call_info_by_callsite_id=call_info)
	# Override the env-gated initialisation: attach a fresh log so the
	# recording sites inside HIRToMIR fire regardless of process env.
	lower._drop_decision_log = DropDecisionLog(fn_name=builder.func.name)
	setattr(builder.func, "_drop_decision_log", lower._drop_decision_log)
	lower.lower_block(hir)
	return builder.func, lower._drop_decision_log, lower


def _make_needs_drop_from_policy(lower: HIRToMIR, func) -> callable:
	"""Return a `needs_drop(local) -> bool` callable that consults the
	real `DropPolicy.needs_drop` axis (NOT the driver-side
	`TypeTable.has_drop` approximation).  The pin must hold against the
	canonical policy, not against the telemetry-grade approximation."""
	def fn(local: str) -> bool:
		ty = func.local_types.get(local)
		if ty is None:
			return False
		return bool(lower._drop_policy(ty).needs_drop)
	return fn


def _assert_no_verdict_disagreements(records: list, *, quadrant: str) -> None:
	"""Assert no `ledger_stricter` / `site_stricter` records.  Print the
	full disagreeing record on failure so the triage path is the actual
	site + program point + local, not a generic test name."""
	leak_shape = [r for r in records if r.classification == CLASS_LEDGER_STRICTER]
	bug_shape = [r for r in records if r.classification == CLASS_SITE_STRICTER]
	if leak_shape or bug_shape:
		details = []
		for r in leak_shape + bug_shape:
			details.append(
				f"  {r.classification:18s} site={r.site:18s} local={r.local:30s} "
				f"site_verdict={r.site_verdict:14s} ledger_verdict={r.ledger_verdict:14s} "
				f"raw_state={r.raw_state:14s} reason={r.site_reason}"
			)
		raise AssertionError(
			f"Phase 3A gate violation on quadrant {quadrant!r}: "
			f"{len(leak_shape)} ledger_stricter (potential-leak shape) "
			f"and {len(bug_shape)} site_stricter (ledger-bug shape) "
			f"verdict disagreements recorded.  Per design.md the gate "
			f"requires zero of either class on the three-quadrant pin "
			f"before Phase 3B may begin.\n" + "\n".join(details)
		)


# -- Quadrant 1: dual-owner (named source local + structural drop) ---------


def test_three_quadrant_pin_dual_owner_named_source() -> None:
	"""V<String> with `val m = ctor(); match m { ... }` — named source
	local triggers the CopyValue path; both `m` and the arm scrut tmp
	are owners that scope-drop independently.  Ledger must classify
	their drops the same way the sites do."""
	type_table = TypeTable()
	var_base = _make_v_variant(type_table)
	string_ty = type_table.ensure_string()
	v_string_ty = type_table.ensure_instantiated(var_base, [string_ty])
	type_table.set_copy_query(lambda tid: tid == v_string_ty, allow_fallback=True)
	v_string_te = TypeExpr(name="V", args=[TypeExpr(name="String")], module_id="main")
	hir = H.HBlock(statements=[
		H.HLet(
			name="m",
			value=H.HCall(
				fn=H.HQualifiedMember(base_type_expr=v_string_te, member="Some"),
				args=[H.HLiteralString("x")],
				kwargs=[],
			),
			declared_type_expr=v_string_te,
			is_mutable=False,
			binding_id=None,
		),
		H.HLet(
			name="result",
			value=H.HMatchExpr(scrutinee=H.HVar(name="m", binding_id=None), arms=_build_match_arms()),
			declared_type_expr=TypeExpr(name="Int"),
			is_mutable=False,
			binding_id=None,
		),
	])
	func, log, lower = _lower_with_ledger(hir, type_table, v_string_ty)
	ledger = build_ledger(func, drop_policy=lower._drop_policy)
	records = compare_events(log.drain(), ledger, needs_drop=_make_needs_drop_from_policy(lower, func))
	_assert_no_verdict_disagreements(records, quadrant="dual_owner")


# -- Quadrant 2: POD variant (no structural drop) --------------------------


def test_three_quadrant_pin_pod_variant() -> None:
	"""V<Int> with named source — fast-path bare StoreLocal.  No
	refcount traffic; ledger must agree the scrutinee and binders need
	no drop."""
	type_table = TypeTable()
	var_base = _make_v_variant(type_table)
	int_ty = type_table.ensure_int()
	v_int_ty = type_table.ensure_instantiated(var_base, [int_ty])
	type_table.set_copy_query(lambda tid: tid == v_int_ty, allow_fallback=True)
	v_int_te = TypeExpr(name="V", args=[TypeExpr(name="Int")], module_id="main")
	hir = H.HBlock(statements=[
		H.HLet(
			name="m",
			value=H.HCall(
				fn=H.HQualifiedMember(base_type_expr=v_int_te, member="Some"),
				args=[H.HLiteralInt(value=42)],
				kwargs=[],
			),
			declared_type_expr=v_int_te,
			is_mutable=False,
			binding_id=None,
		),
		H.HLet(
			name="result",
			value=H.HMatchExpr(scrutinee=H.HVar(name="m", binding_id=None), arms=_build_match_arms()),
			declared_type_expr=TypeExpr(name="Int"),
			is_mutable=False,
			binding_id=None,
		),
	])
	func, log, lower = _lower_with_ledger(hir, type_table, v_int_ty)
	ledger = build_ledger(func, drop_policy=lower._drop_policy)
	records = compare_events(log.drain(), ledger, needs_drop=_make_needs_drop_from_policy(lower, func))
	_assert_no_verdict_disagreements(records, quadrant="pod_variant")


# -- Quadrant 3: transient rvalue (no source local) ------------------------


def test_three_quadrant_pin_transient_rvalue() -> None:
	"""V<String> with inline ctor as scrutinee — no named source local,
	so the bare StoreLocal fast-path applies even though
	`has_structural_drop=True`.  The Phase 2a review finding (refcount
	leak when CopyValue ran here) is the critical case the ledger must
	classify correctly."""
	type_table = TypeTable()
	var_base = _make_v_variant(type_table)
	string_ty = type_table.ensure_string()
	v_string_ty = type_table.ensure_instantiated(var_base, [string_ty])
	type_table.set_copy_query(lambda tid: tid == v_string_ty, allow_fallback=True)
	v_string_te = TypeExpr(name="V", args=[TypeExpr(name="String")], module_id="main")
	inline_scrut = H.HCall(
		fn=H.HQualifiedMember(base_type_expr=v_string_te, member="Some"),
		args=[H.HLiteralString("x")],
		kwargs=[],
	)
	hir = H.HBlock(statements=[
		H.HLet(
			name="result",
			value=H.HMatchExpr(scrutinee=inline_scrut, arms=_build_match_arms()),
			declared_type_expr=TypeExpr(name="Int"),
			is_mutable=False,
			binding_id=None,
		),
	])
	# Inline ctor is not a top-level HLet, so synthesise CallInfo
	# manually — _collect_ctor_callinfo only inspects HLet rhs.
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	inst = type_table.get_variant_instance(v_string_ty)
	assert inst is not None
	arm_def = inst.arms_by_name["Some"]
	csid = getattr(inline_scrut, "callsite_id", None)
	call_info: dict[int, CallInfo] = {}
	if isinstance(csid, int):
		call_info[csid] = CallInfo(
			target=CallTarget.constructor(v_string_ty, "Some"),
			sig=CallSig(param_types=tuple(arm_def.field_types), user_ret_type=v_string_ty, can_throw=False),
		)
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	lower = HIRToMIR(builder, type_table=type_table, call_info_by_callsite_id=call_info)
	lower._drop_decision_log = DropDecisionLog(fn_name=builder.func.name)
	setattr(builder.func, "_drop_decision_log", lower._drop_decision_log)
	lower.lower_block(hir)
	ledger = build_ledger(builder.func, drop_policy=lower._drop_policy)
	records = compare_events(
		lower._drop_decision_log.drain(),
		ledger,
		needs_drop=_make_needs_drop_from_policy(lower, builder.func),
	)
	_assert_no_verdict_disagreements(records, quadrant="transient_rvalue")
