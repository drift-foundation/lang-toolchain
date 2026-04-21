# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 0 fail-stop for the `match Optional<String>` double-drop UAF
(fix/ownership-drop-ledger track).

Bug shape (reproduced end-to-end against the staged toolchain on
2026-04-20):
  - Source program: `val opt = std.env::get("X"); match opt { Some(s)
    => ..., None => ... }` — anything that matches a refcount-bearing
    variant returned from a callee loaded from a packaged `.dmp`.
  - Compile path: `_should_copy_value(scrut_ty)` returned True for
    `Optional<String>` because `copy_status` resolved to True (only
    fully evaluable on the package-load path, which eagerly walks the
    transitive trait-impl graph; source-build paths often left it
    None and fell through to the correct branch).
  - `_ensure_arm_scrut_ptr` consequently took its else-branch, which
    bitcopies `scrut_val` into `arm_scrut_local` WITHOUT a `MoveOut`
    of the source local.  The source local stays live (never enters
    `_moved_locals`); the arm-end cleanup drops `arm_scrut_local`,
    AND `string_arc._drop_all_destructibles` at the return terminator
    drops the source — the inner refcount is released twice.
  - Runtime: glibc `tcache_thread_shutdown(): unaligned tcache chunk
    detected`, ~80% repro rate native, 100% under valgrind memcheck,
    crash inside `drift_string_release` reading a freed
    `DriftStringHeader`.

The structural fix is the per-program-point ownership ledger (Phase 2
of the same track).  This test pins the Phase 0 fail-stop: the
compiler must hard-error at MIR build time instead of silently
emitting the bug shape, so we don't ship more variants of the same
UAF while the ledger is built.

Specifically, the scrut-store branch of `_ensure_arm_scrut_ptr`
asserts that any reachable scrutinee with a named source local AND
`has_drop=True` AND `is_bitcopy=False` was ALREADY routed through the
MoveOut sibling — reaching the Copy-store branch under those
conditions IS the bug.

This test forces the precondition by installing a mock Copy query
that claims `Optional<String>` is Copy (matching the package-load
miscalculation), then drives `_lower_match` through `compile_stubbed_funcs`
on a `match opt { Some(s) => ..., None => ... }` shape and asserts
the assertion fires with a diagnostic that names the actual failure
mode.  When the Phase 2 ledger lands and the underlying
classification can't take this branch in the first place, this test
becomes obsolete and should be deleted along with the assertion.
"""
from __future__ import annotations

import pytest

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.types_core import TypeId, TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage1 import assign_callsite_ids, assign_node_ids
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage2 import HIRToMIR, make_builder


def _collect_ctor_callinfo(hir: H.HBlock, type_table: TypeTable, var_tid: TypeId) -> dict[int, CallInfo]:
	"""Build CallInfo entries for any ctor calls in `hir`.

	Lifted from `test_hir_to_mir_match_copy_payload_drop_once.py` —
	HIRToMIR's HCall lowering requires a CallInfo for every qualified-
	member call, and at unit-test scope we don't run the checker, so
	we synthesise the entries from the HIR shape directly.
	"""
	call_info_by_callsite_id: dict[int, CallInfo] = {}
	for stmt in hir.statements:
		if isinstance(stmt, H.HLet) and isinstance(stmt.value, H.HCall) and isinstance(stmt.value.fn, H.HQualifiedMember):
			base_te = stmt.value.fn.base_type_expr
			base_tid = resolve_opaque_type(base_te, type_table, module_id=getattr(base_te, "module_id", None))
			inst_tid = base_tid
			if type_table.get_variant_instance(inst_tid) is None:
				inst_tid = type_table.ensure_instantiated(base_tid, [])
			inst = type_table.get_variant_instance(inst_tid)
			if inst is None:
				continue
			arm_def = inst.arms_by_name.get(stmt.value.fn.member)
			if arm_def is None:
				continue
			info = CallInfo(
				target=CallTarget.constructor(var_tid, stmt.value.fn.member),
				sig=CallSig(
					param_types=tuple(arm_def.field_types),
					user_ret_type=var_tid,
					can_throw=False,
				),
			)
			csid = getattr(stmt.value, "callsite_id", None)
			if isinstance(csid, int):
				call_info_by_callsite_id[csid] = info
	return call_info_by_callsite_id


def _build_optional_string(type_table: TypeTable) -> TypeId:
	"""Construct a `V<String>` variant that mirrors `Optional<String>`."""
	var_base = type_table.declare_variant(
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
	string_ty = type_table.ensure_string()
	return type_table.ensure_instantiated(var_base, [string_ty])


def _force_copy_status_true_for(type_table: TypeTable, *types: TypeId) -> None:
	"""Install a Copy query that reports the named types as Copy.

	Mirrors the package-load classification path that resolved
	`copy_status(Optional<String>) == True` while `has_drop` correctly
	stayed True — the exact precondition that triggered the UAF.

	`set_copy_query` is the real public hook; using it here keeps the
	test exercising the same plumbing the package-load path does.
	"""
	wanted = set(types)

	def _query(tid: TypeId) -> bool:
		return tid in wanted

	type_table.set_copy_query(_query, allow_fallback=True)


def test_copy_classified_drop_bearing_scrutinee_hard_errors_at_mir_build() -> None:
	type_table = TypeTable()
	opt_string_ty = _build_optional_string(type_table)
	# `has_drop` of `V<String>` is True because the Some payload is
	# `String` (refcounted).  Sanity-check the precondition before we
	# install the buggy Copy claim, so a future TypeTable change that
	# regresses `has_drop` doesn't make this test pass for the wrong
	# reason.
	assert type_table.has_drop(opt_string_ty), (
		"precondition: V<String> must report has_drop=True; if this "
		"fails, the regression target has shifted and the assertion "
		"under test is no longer pinning the original bug shape"
	)
	# Install the buggy classification.  After this call,
	# `_should_copy_value(V<String>)` returns True via the Copy hook,
	# matching what package-load consumers see.
	_force_copy_status_true_for(type_table, opt_string_ty)
	assert type_table.copy_status(opt_string_ty) is True, (
		"test setup: copy_status hook did not take effect; the "
		"assertion under test will not be reached and the test would "
		"silently pass for the wrong reason"
	)

	# `match opt { Some(s) => 1, None => 2 }` — statement context, named
	# source local `opt`, drop-bearing scrutinee.  Exactly the shape
	# `tiny.drift` (the minimal repro) lowers to.
	let_opt = H.HLet(
		name="opt",
		value=H.HCall(
			fn=H.HQualifiedMember(
				base_type_expr=TypeExpr(name="V", args=[TypeExpr(name="String")], module_id="main"),
				member="Some",
			),
			args=[H.HLiteralString("x")],
			kwargs=[],
		),
		declared_type_expr=TypeExpr(name="V", args=[TypeExpr(name="String")], module_id="main"),
		is_mutable=False,
		binding_id=None,
	)
	match = H.HMatchExpr(
		scrutinee=H.HVar(name="opt", binding_id=None),
		arms=[
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
		],
	)
	hir = H.HBlock(
		statements=[
			let_opt,
			H.HLet(
				name="result",
				value=match,
				declared_type_expr=TypeExpr(name="Int"),
				is_mutable=False,
				binding_id=None,
			),
		],
	)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	call_info_by_callsite_id = _collect_ctor_callinfo(hir, type_table, opt_string_ty)
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))

	with pytest.raises(AssertionError) as exc_info:
		HIRToMIR(
			builder,
			type_table=type_table,
			call_info_by_callsite_id=call_info_by_callsite_id,
		).lower_block(hir)

	msg = str(exc_info.value)
	# The diagnostic must (a) identify the offending construct so the
	# triage path is "search for this string in hir_to_mir.py", and
	# (b) explain WHY the assertion fired so the next engineer doesn't
	# have to re-derive the bug from first principles.  The exact
	# wording can churn — keep the assertions on stable load-bearing
	# fragments only.
	assert "match scrutinee" in msg, msg
	assert "has_drop=True" in msg, msg
	assert "ownership-drop-ledger" in msg, msg
	assert "double-drop" in msg, msg


def test_pod_variant_scrutinee_does_not_trip_assertion() -> None:
	"""Negative: `Optional<Int>`-shaped variant must NOT trip the gate.

	`Int` is bitcopy with `has_drop=False`, so `V<Int>` has
	`has_drop=False` — the assertion's precondition (drop-bearing
	scrutinee) is not satisfied and the lowering must succeed
	regardless of `copy_status`.

	Without this pin, a future tightening of the assertion (e.g. one
	that drops the `has_drop` predicate) would silently break every
	`match Optional<Int> { ... }` use site at compile time.  Failing
	loudly here forces such a change to be reviewed against this test
	rather than landing as a stealth tightening.
	"""
	type_table = TypeTable()
	var_base = type_table.declare_variant(
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
	int_ty = type_table.ensure_int()
	opt_int_ty = type_table.ensure_instantiated(var_base, [int_ty])
	assert not type_table.has_drop(opt_int_ty), (
		"precondition: V<Int> must report has_drop=False; if this "
		"fails the negative pin no longer covers the intended case"
	)
	# Force copy_status=True even for V<Int> so the Copy-store branch
	# of `_ensure_arm_scrut_ptr` is the path actually taken.  The
	# assertion must STILL not trip because the has_drop precondition
	# is absent.
	_force_copy_status_true_for(type_table, opt_int_ty)

	let_opt = H.HLet(
		name="opt",
		value=H.HCall(
			fn=H.HQualifiedMember(
				base_type_expr=TypeExpr(name="V", args=[TypeExpr(name="Int")], module_id="main"),
				member="Some",
			),
			args=[H.HLiteralInt(value=42)],
			kwargs=[],
		),
		declared_type_expr=TypeExpr(name="V", args=[TypeExpr(name="Int")], module_id="main"),
		is_mutable=False,
		binding_id=None,
	)
	match = H.HMatchExpr(
		scrutinee=H.HVar(name="opt", binding_id=None),
		arms=[
			H.HMatchArm(
				ctor="Some",
				binders=["v"],
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
		],
	)
	hir = H.HBlock(
		statements=[
			let_opt,
			H.HLet(
				name="result",
				value=match,
				declared_type_expr=TypeExpr(name="Int"),
				is_mutable=False,
				binding_id=None,
			),
		],
	)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	call_info_by_callsite_id = _collect_ctor_callinfo(hir, type_table, opt_int_ty)
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))

	# Must NOT raise.  If this regresses, the assertion has been
	# tightened past its documented contract.
	HIRToMIR(
		builder,
		type_table=type_table,
		call_info_by_callsite_id=call_info_by_callsite_id,
	).lower_block(hir)
