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


def test_assign_while_borrowed_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn read(r: &Int) nothrow -> Int {
	return r;
}

fn main() nothrow -> Int {
	var x = 0;
	val r = &x;
	x = 1;
	return read(r);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	write_errors = [d for d in errors if "cannot write to 'x' while it is borrowed" in d.message]
	assert write_errors, errors
	assert all(d.phase == "borrowcheck" for d in write_errors), write_errors
	assert all(d.span.line is not None and d.span.column is not None for d in write_errors), write_errors


def test_augassign_while_borrowed_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn read(r: &Int) nothrow -> Int {
	return r;
}

fn main() nothrow -> Int {
	var x = 0;
	val r = &x;
	x += 1;
	return read(r);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	write_errors = [d for d in errors if "cannot write to 'x' while it is borrowed" in d.message]
	assert write_errors, errors
	assert all(d.phase == "borrowcheck" for d in write_errors), write_errors
	assert all(d.span.line is not None and d.span.column is not None for d in write_errors), write_errors
