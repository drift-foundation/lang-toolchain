from __future__ import annotations

from pathlib import Path

import pytest

from lang.driftc import driftc
from lang.driftc import type_checker as tc_mod
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget


def test_mir_validate_boundary_failure_is_diagnostic_not_assert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module m;

fn main() nothrow -> Int {
	return 0;
}
"""
	)
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert parse_diags == []
	func_hirs, signatures, _ = flatten_modules(modules)

	def _boom(_mir_funcs_by_id):
		raise AssertionError("forced mir validator failure")

	monkeypatch.setattr(driftc, "validate_mir_call_invariants", _boom)

	_mir, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry_module="m",
		entry_name="main",
		return_checked=True,
	)
	errs = [d for d in checked.diagnostics if d.severity == "error"]
	assert any(d.phase == "mir_validate" for d in errs), errs
	assert any("forced mir validator failure" in d.message for d in errs), errs
	assert any("validate_mir_call_invariants" in d.message for d in errs), errs
	matches = [d for d in errs if "forced mir validator failure" in d.message]
	assert matches
	assert matches[0].span is not None
	assert matches[0].span.line is not None
	assert matches[0].span.column is not None


def test_mir_lowering_contract_failure_is_diagnostic_not_assert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module m;

fn add1(v: Int) nothrow -> Int {
	return v + 1;
}

fn main() nothrow -> Int {
	return add1(1);
}
"""
	)
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert parse_diags == []
	func_hirs, signatures, _ = flatten_modules(modules)
	orig_check_function = tc_mod.TypeChecker.check_function

	def _patched_check_function(self, *args, **kwargs):
		res = orig_check_function(self, *args, **kwargs)
		typed = res.typed_fn
		if typed.fn_id.name != "main":
			return res
		callsite_ids = sorted(typed.call_info_by_callsite_id.keys())
		if callsite_ids:
			csid = callsite_ids[0]
			info = typed.call_info_by_callsite_id.get(csid)
			if info is not None:
				typed.call_info_by_callsite_id[csid] = CallInfo(
					target=CallTarget.constructor_struct(info.sig.user_ret_type),
					sig=CallSig(
						param_types=info.sig.param_types,
						user_ret_type=info.sig.user_ret_type,
						can_throw=info.sig.can_throw,
						includes_callee=info.sig.includes_callee,
					),
				)
		return res

	monkeypatch.setattr(tc_mod.TypeChecker, "check_function", _patched_check_function)

	_mir, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry_module="m",
		entry_name="main",
		return_checked=True,
	)
	errs = [d for d in checked.diagnostics if d.severity == "error"]
	assert any(d.phase == "mir_validate" for d in errs), errs
	assert any("MIR lowering contract failure" in d.message for d in errs), errs
	assert any("constructor call resolved to non-STRUCT" in d.message for d in errs), errs
	matches = [d for d in errs if "constructor call resolved to non-STRUCT" in d.message]
	assert matches
	assert matches[0].span is not None
	assert matches[0].span.line is not None
	assert matches[0].span.column is not None
