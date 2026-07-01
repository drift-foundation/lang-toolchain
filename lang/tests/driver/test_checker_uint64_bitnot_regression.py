from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_uint64_bitnot_compiles_and_emits_i64(tmp_path: Path) -> None:
	"""Uint64 bitwise NOT compiles in ordinary user code and emits i64 ops."""
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

pub fn main() nothrow -> Int {
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
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], f"expected no errors, got {errors}"
	assert ir is not None, "expected IR output"
	# Verify the BIT_NOT actually operates on i64, not silently falling back to i-word-width.
	assert "xor i64" in ir, f"expected 'xor i64' in IR for Uint64 bitnot, IR snippet: {ir[:2000]}"
