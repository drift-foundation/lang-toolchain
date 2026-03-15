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


def test_return_ref_local_rejected_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn bad() nothrow -> &Int {
	val x = 1;
	return &x;
}

fn main() nothrow -> Int {
	val _ = bad();
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "reference return must be derived from a reference parameter" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_return_mut_ref_from_shared_param_rejected_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn bad(x: &Int) nothrow -> &mut Int {
	return x;
}

fn main() nothrow -> Int {
	val x = 1;
	val _ = bad(&x);
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "mutable reference return must derive from an &mut parameter" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches
