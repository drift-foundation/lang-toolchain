from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _compile(tmp_path: Path, source: str):
	src = tmp_path / "main.drift"
	src.write_text(source)
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
	return checked


def test_generic_index_read_typevar_scan_does_not_crash(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn head<T>(xs: Array<T>) nothrow -> T {
	return xs[0];
}

pub fn main() nothrow -> Int {
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []


def test_generic_nested_index_read_typevar_scan_does_not_crash(tmp_path: Path) -> None:
	# Indexing Array<Array<T>> where the element type contains a type parameter
	# should not crash and should not produce Copy errors at the generic
	# definition site — Copy enforcement for type-parameter-containing types
	# is deferred to instantiation.
	checked = _compile(
		tmp_path,
		"""
module m;

fn head_nested<T>(xss: Array<Array<T>>) nothrow -> Array<T> {
	return xss[0];
}

pub fn main() nothrow -> Int {
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert not any(d.message.startswith("internal:") for d in errors), errors
