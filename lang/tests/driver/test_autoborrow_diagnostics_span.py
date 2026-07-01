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


def test_autoborrow_mut_param_rvalue_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn f(x: &mut Int) nothrow -> Int { return 0; }

pub fn main() nothrow -> Int {
	return f(1 + 2);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	msg = "borrow requires an addressable place; bind to a local first"
	matches = [d for d in errors if msg in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_autoborrow_mut_receiver_rvalue_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

struct S { v: Int }

implement S {
	pub fn bump(self: &mut S) nothrow -> Int { return self.v; }
}

fn mk() nothrow -> S { return S(v = 1); }

pub fn main() nothrow -> Int {
	return mk().bump();
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	msg = "borrow requires an addressable place; bind to a local first"
	matches = [d for d in errors if msg in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches
