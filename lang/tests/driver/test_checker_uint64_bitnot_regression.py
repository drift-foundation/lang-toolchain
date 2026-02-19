from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_checker_uint64_bitnot_reports_user_diagnostic_not_internal(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main

fn main() nothrow -> Int {
	var x: Uint64 = cast<Uint64>(1);
	x = ~x;
	if x == cast<Uint64>(0) {
		return 1;
	}
	return 0;
}
""".lstrip()
	)
	modules, type_table, exception_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert diagnostics == []
	func_hirs, signatures, _ = flatten_modules(modules)
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("bitwise operators require Uint operands" in (d.message or "") for d in errors), errors
	assert not any((d.message or "").startswith("internal:") for d in errors), errors
