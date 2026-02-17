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


def test_noncopy_array_index_read_reports_user_diag_with_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct Cell {
	xs: Array<Int>
}

fn main() nothrow -> Int {
	var xs: Array<Cell> = [];
	var inner: Array<Int> = [];
	inner.push(1);
	xs.push(Cell(xs = move inner));
	val c = xs[0];
	return c.xs.len;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	copy_errors = [d for d in errors if "cannot copy value of type" in d.message]
	assert copy_errors, errors
	assert all(d.phase == "typecheck" for d in copy_errors), copy_errors
	assert all(d.span.line is not None and d.span.column is not None for d in copy_errors), copy_errors
	assert not any(d.message.startswith("internal:") for d in errors), errors


def test_noncopy_array_index_read_nested_array_reports_user_diag_with_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

fn main() nothrow -> Int {
	var xs: Array<Array<Int>> = [];
	var inner: Array<Int> = [];
	inner.push(1);
	xs.push(move inner);
	val b = xs[0];
	return b.len;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	copy_errors = [d for d in errors if "cannot copy value of type" in d.message]
	assert copy_errors, errors
	assert all(d.phase == "typecheck" for d in copy_errors), copy_errors
	assert all(d.span.line is not None and d.span.column is not None for d in copy_errors), copy_errors
	assert not any(d.message.startswith("internal:") for d in errors), errors
