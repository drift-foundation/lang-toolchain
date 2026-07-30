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


def test_index_on_ref_array_subject_is_allowed(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn first(xs: &Array<Int>) nothrow -> Int {
	return xs[0];
}

pub fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	xs.push(7);
	return first(xs);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []
