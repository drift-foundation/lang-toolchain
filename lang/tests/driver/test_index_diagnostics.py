from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
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
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	return checked


def test_array_index_requires_int(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

pub fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	xs.push(1);
	val n = xs["0"];
	return n;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("array index must be an Int" in d.message for d in errors), errors


def test_indexing_requires_array_value(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

pub fn main() nothrow -> Int {
	val x = 1;
	val y = x[0];
	return y;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("indexing requires an Array value" in d.message for d in errors), errors
