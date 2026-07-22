# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Driver-boundary containment for the pre-string_arc destructible PLAN
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


def test_plan_set_mismatch_is_boundary_contained(tmp_path, monkeypatch) -> None:
	"""item 1: a plan-set / function-set MISMATCH (a plan present for no
	function, or vice-versa) is boundary-contained as phase `destructible_plan`
	with a clean `internal:` diagnostic — NOT a bare `AssertionError` traceback
	from `compile_stubbed_funcs`. The mismatch is otherwise structurally
	unreachable (the driver populates `_dplans[fn_id]` for every function), so
	we inject a spurious `_dplans` key during plan build to force the guard.
	"""
	import sys
	from lang.driftc.core.function_id import FunctionId

	def _make_boom(real):
		def _boom(func, **kw):
			r = real(func, **kw)
			if getattr(func, "name", "") == "main":
				# Reach the driver's local `_dplans` and inject a ghost plan
				# key with no corresponding MIR function → set mismatch.
				for depth in range(1, 8):
					try:
						fr = sys._getframe(depth)
					except ValueError:
						break
					dp = fr.f_locals.get("_dplans")
					if isinstance(dp, dict):
						dp[FunctionId(module="ghost", name="ghost", ordinal=0)] = r[0]
						break
			return r
		return _boom

	ir, checked = _compile_with_injected_plan_error(tmp_path, monkeypatch, _make_boom)
	# The prefix differs ("completeness failure"); assert containment directly.
	errors = [d for d in getattr(checked, "diagnostics", [])
	          if getattr(d, "severity", None) == "error"]
	assert errors
	assert any(
		"internal: destructible plan completeness failure" in d.message
		and getattr(d, "phase", None) == "destructible_plan"
		for d in errors
	), [(d.message, getattr(d, "phase", None)) for d in errors]
	assert ir == "", "compile must not produce IR after a plan-set mismatch"
