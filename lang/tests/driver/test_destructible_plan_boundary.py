# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Driver-boundary containment for the pre-normalization destructible PLAN
build (`with _timed("destructible_plan"):` loop in `driftc.py`).

The plan build reuses the shared destructible authority; its failure modes
are BOTH a frozen-plan `PlanContractError` (subclasses `AssertionError`)
AND the site-4 / site-3 authority tripwires + `PlannerStop`, which are
`RuntimeError`.  Item-2 requires EVERY one of these to be contained as a
clean `internal:` diagnostic (phase `destructible_plan`, empty MIR, no
traceback).  These pins inject each error class and assert containment.
"""
from __future__ import annotations


def _compile_with_injected_plan_error(tmp_path, monkeypatch, make_boom):
	from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
	from lang.driftc.module_lowered import flatten_modules
	from lang.driftc import driftc as D
	from lang.driftc.core.function_id import function_symbol

	src = tmp_path / "main.drift"
	src.write_text(
		"module main;\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar s = \"a\";\n"
		"\ts = \"b\";\n"
		"\tif s.byte_length() > 0 { return 0; }\n"
		"\treturn 1;\n"
		"}\n"
	)
	modules, type_table, exc, mexp, mdeps, pdiags = parse_drift_workspace_to_hir(
		[src], stdlib_root=stdlib_root(), test_build_only=True
	)
	assert not pdiags, [d.message for d in pdiags]
	func_hirs, signatures, _ = flatten_modules(modules)
	main_id = [i for i, s in signatures.items() if i.name == "main" and not s.is_method][0]
	origin = {}
	for m in modules.values():
		origin.update(m.origin_by_fn_id)

	_real = D.build_destructible_plan
	monkeypatch.setattr(D, "build_destructible_plan", make_boom(_real))

	ir, checked = D.compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc,
		entry=function_symbol(main_id),
		type_table=type_table,
		module_exports=mexp,
		module_deps=mdeps,
		origin_by_fn_id=origin,
		enforce_entrypoint=True,
		reserved_namespace_policy=D.ReservedNamespacePolicy.ALLOW_DEV,
	)
	return ir, checked


def _assert_contained(ir, checked, needle):
	errors = [
		d for d in getattr(checked, "diagnostics", [])
		if getattr(d, "severity", None) == "error"
	]
	assert errors, "injected destructible-plan failure must surface as a diagnostic"
	msgs = [d.message for d in errors]
	assert any(
		"internal: destructible plan contract failure" in m and needle in m
		for m in msgs
	), msgs
	assert any(getattr(d, "phase", None) == "destructible_plan" for d in errors), [
		(d.message, getattr(d, "phase", None)) for d in errors
	]
	assert ir == "", "compile must not produce IR after a plan-build failure"


def test_plan_build_plancontract_error_is_contained(tmp_path, monkeypatch) -> None:
	"""A `PlanContractError` (AssertionError) in planning surfaces as a clean
	`internal:` diagnostic (phase destructible_plan), not a traceback."""
	from lang.driftc.stage2.cleanup_plan import PlanContractError

	def _make_boom(real):
		def _boom(func, **kw):
			if getattr(func, "name", "") == "main":
				raise PlanContractError(
					"destructible plan contract failure: injected-plancontract"
				)
			return real(func, **kw)
		return _boom

	ir, checked = _compile_with_injected_plan_error(tmp_path, monkeypatch, _make_boom)
	_assert_contained(ir, checked, "injected-plancontract")


def test_plan_build_planner_stop_runtimeerror_is_contained(tmp_path, monkeypatch) -> None:
	"""A `PlannerStop` / PATH_DEPENDENT `RuntimeError` in planning ALSO
	surfaces as a clean `internal:` diagnostic (phase destructible_plan),
	not a traceback — item-2 widened the containment to RuntimeError."""
	from lang.driftc.stage2.destructible_planner import PlannerStop

	def _make_boom(real):
		def _boom(func, **kw):
			if getattr(func, "name", "") == "main":
				raise PlannerStop(
					"destructible plan contract failure: injected-plannerstop"
				)
			return real(func, **kw)
		return _boom

	ir, checked = _compile_with_injected_plan_error(tmp_path, monkeypatch, _make_boom)
	_assert_contained(ir, checked, "injected-plannerstop")


def _make_table_ghost_boom(table_name: str, value_factory):
	"""A `build_destructible_plan` wrapper that, after the real plan build for
	`main`, reaches the driver's local contribution table `table_name` via the
	frame stack and injects a ghost key with no corresponding MIR function —
	forcing the (otherwise structurally-unreachable) table-completeness guard
	that runs BEFORE any consumer.  A missing entry trips the SAME set-equality
	guard (the comparison is symmetric), so the ghost injection covers both
	directions per table."""
	import sys
	from lang.driftc.core.function_id import FunctionId

	def _factory(real):
		def _boom(func, **kw):
			r = real(func, **kw)
			if getattr(func, "name", "") == "main":
				for depth in range(1, 8):
					try:
						fr = sys._getframe(depth)
					except ValueError:
						break
					tbl = fr.f_locals.get(table_name)
					if isinstance(tbl, dict):
						ghost = FunctionId(module="ghost", name="ghost", ordinal=0)
						tbl[ghost] = value_factory(r)
						break
			return r
		return _boom
	return _factory


def _assert_completeness_contained(ir, checked, needle: str) -> None:
	errors = [d for d in getattr(checked, "diagnostics", [])
	          if getattr(d, "severity", None) == "error"]
	assert errors
	assert any(
		"internal: destructible plan completeness failure" in d.message
		and needle in d.message
		and getattr(d, "phase", None) == "destructible_plan"
		for d in errors
	), [(d.message, getattr(d, "phase", None)) for d in errors]
	assert ir == "", "compile must not produce IR after a table mismatch"


def test_plan_set_mismatch_is_boundary_contained(tmp_path, monkeypatch) -> None:
	"""A plan-set / function-set MISMATCH (a plan present for no function, or
	vice-versa) is boundary-contained as phase `destructible_plan` with a clean
	`internal:` diagnostic — NOT a bare `AssertionError` traceback.  The guard
	now runs BEFORE every consumer (S5/S6 closure: no consumer runs before
	completeness is proven)."""
	ir, checked = _compile_with_injected_plan_error(
		tmp_path, monkeypatch, _make_table_ghost_boom("_dplans", lambda r: r[0]))
	_assert_completeness_contained(ir, checked, "destructible plan set")


def test_r8_table_mismatch_is_boundary_contained(tmp_path, monkeypatch) -> None:
	"""S5/S6 closure: an R8-recognition table that does not exactly match the
	MIR function set is contained BEFORE any consumer — a missing entry must
	never silently select the direct-recomputation R8 fallback."""
	ir, checked = _compile_with_injected_plan_error(
		tmp_path, monkeypatch,
		_make_table_ghost_boom("_r8contrib", lambda r: object()))
	_assert_completeness_contained(ir, checked, "R8 recognition set")


def test_audit_collector_table_mismatch_is_boundary_contained(tmp_path, monkeypatch) -> None:
	"""S5 closure (audit enabled): a collector table that does not exactly
	match the MIR function set is contained before any consumer — a missing
	collector must never surface later as a finalize-time failure."""
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT", "1")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
	ir, checked = _compile_with_injected_plan_error(
		tmp_path, monkeypatch,
		_make_table_ghost_boom("_audit_collectors", lambda r: object()))
	_assert_completeness_contained(ir, checked, "audit collector set")


def test_c1_table_mismatch_is_boundary_contained(tmp_path, monkeypatch) -> None:
	"""S5 closure (audit enabled): a frozen-C1 table that does not exactly
	match the MIR function set is contained before any consumer — a missing
	entry must never silently select the monolithic finalize path (the pipeline
	no longer records C1; the class would silently disappear)."""
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT", "1")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
	ir, checked = _compile_with_injected_plan_error(
		tmp_path, monkeypatch,
		_make_table_ghost_boom("_dc1contrib", lambda r: r[2]))
	_assert_completeness_contained(ir, checked, "C1 contribution set")


def test_audit_disabled_maps_must_be_empty(tmp_path, monkeypatch) -> None:
	"""S5 closure (audit DISABLED): any residue in the audit maps is a
	completeness contract failure — the disabled path must allocate nothing."""
	monkeypatch.delenv("DRIFT_STRING_ARC_AUDIT", raising=False)
	ir, checked = _compile_with_injected_plan_error(
		tmp_path, monkeypatch,
		_make_table_ghost_boom("_audit_collectors", lambda r: object()))
	_assert_completeness_contained(ir, checked, "audit DISABLED")


def test_plan_value_type_mismatch_is_boundary_contained(tmp_path, monkeypatch) -> None:
	"""S7+S8 defensive polish: a non-CleanupPlan VALUE in the plan table
	(complete key set, foreign value) is contained before any consumer."""
	def _make_boom(real):
		def _boom(func, **kw):
			r = real(func, **kw)
			if getattr(func, "name", "") == "main":
				return (object(), r[1], r[2])
			return r
		return _boom

	ir, checked = _compile_with_injected_plan_error(tmp_path, monkeypatch, _make_boom)
	_assert_completeness_contained(ir, checked, "non-CleanupPlan value")


def test_r8_value_type_mismatch_is_boundary_contained(tmp_path, monkeypatch) -> None:
	"""S7+S8 defensive polish: a non-R8Recognition VALUE in the R8 table
	(complete key set, foreign value) is contained before any consumer."""
	import lang.driftc.stage2.string_ownership_analysis as SOA

	_real_r8 = SOA.compute_recognized_releases

	def _bad_r8(func, **kw):
		if getattr(func, "name", "") == "main":
			return object()
		return _real_r8(func, **kw)

	monkeypatch.setattr(SOA, "compute_recognized_releases", _bad_r8)
	ir, checked = _compile_with_injected_plan_error(
		tmp_path, monkeypatch, lambda real: real)
	_assert_completeness_contained(ir, checked, "non-R8Recognition value")


def test_finalize_contract_failure_is_boundary_contained(tmp_path, monkeypatch) -> None:
	"""S5 closure end-to-end: an injected `finalize` contract failure is
	boundary-contained as phase `string_arc_audit_finalize` (clean `internal:`
	diagnostic, empty IR, no traceback)."""
	from lang.driftc.stage2 import ownership_ledger_reporter as R

	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT", "1")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_FILE", str(tmp_path / "audit.jsonl"))

	def _boom_finalize(self, **kw):
		raise AssertionError("injected-finalize-boom")

	monkeypatch.setattr(R.StringArcAudit, "finalize", _boom_finalize)
	ir, checked = _compile_with_injected_plan_error(
		tmp_path, monkeypatch, lambda real: real)
	errors = [d for d in getattr(checked, "diagnostics", [])
	          if getattr(d, "severity", None) == "error"]
	assert errors
	assert any(
		"internal: string_arc audit finalize contract failure" in d.message
		and "injected-finalize-boom" in d.message
		and getattr(d, "phase", None) == "string_arc_audit_finalize"
		for d in errors
	), [(d.message, getattr(d, "phase", None)) for d in errors]
	assert ir == "", "compile must not produce IR after a finalize contract failure"
