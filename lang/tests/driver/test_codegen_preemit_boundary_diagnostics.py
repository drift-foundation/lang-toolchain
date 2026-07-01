from __future__ import annotations

from pathlib import Path

import pytest

from lang.driftc import driftc
from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_codegen_preemit_contract_failure_is_diagnostic_not_assert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module m;

pub fn main() nothrow -> Int {
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

	def _boom_validate(*_args, **_kwargs):
		raise AssertionError("forced pre-emit contract failure")

	def _boom_lower(*_args, **_kwargs):
		raise AssertionError("lower_module_to_llvm should not run after pre-emit failure")

	monkeypatch.setattr(driftc, "_validate_codegen_contract", _boom_validate)
	monkeypatch.setattr(driftc, "lower_module_to_llvm", _boom_lower)

	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="m::main",
	)
	assert ir == ""
	errs = [d for d in checked.diagnostics if d.severity == "error"]
	assert any(d.phase == "codegen" for d in errs), errs
	assert any("forced pre-emit contract failure" in d.message for d in errs), errs
	assert all("should not run after pre-emit failure" not in d.message for d in errs), errs
	matches = [d for d in errs if "forced pre-emit contract failure" in d.message]
	assert matches
	assert matches[0].span is not None
	assert matches[0].span.line is not None
	assert matches[0].span.column is not None
