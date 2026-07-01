from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_missing_entrypoint_reports_span(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module m;

fn helper() nothrow -> Int {
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
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="m::main",
	)
	errs = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errs if "missing entry point 'main' for code generation" in d.message]
	assert matches, errs
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_duplicate_entrypoint_reports_span(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module m;

pub fn main() nothrow -> Int {
	return 0;
}

pub fn main(x: Int) nothrow -> Int {
	return x;
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
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="m::main",
	)
	errs = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errs if "duplicate entry point definition for 'm::main'" in d.message]
	assert matches, errs
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches
