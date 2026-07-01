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


def test_deref_requires_reference_value(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

pub fn main() nothrow -> Int {
	val x = 1;
	val y = *x;
	return y;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("deref requires a reference value" in d.message for d in errors), errors


def test_deref_of_noncopy_from_ref_requires_copy(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

struct Box {
	xs: Array<Int>
}

fn take(b: &Box) nothrow -> Box {
	return *b;
}

pub fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	xs.push(1);
	val b = Box(xs = move xs);
	val out = take(&b);
	return out.xs.len;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("cannot copy value of type" in d.message for d in errors), errors
