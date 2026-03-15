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


def test_call_arity_mismatch_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn f(a: Int) nothrow -> Int { return a; }

fn main() nothrow -> Int {
	return f();
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "no matching overload for function 'f'" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_call_type_mismatch_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn f(a: Int) nothrow -> Int { return a; }

fn main() nothrow -> Int {
	return f(true);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "no matching overload for function 'f'" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_method_keyword_args_rejected_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

struct S {}

implement S {
	fn f(self: &S, x: Int) nothrow -> Int {
		return x;
	}
}

fn main() nothrow -> Int {
	var s = S();
	return s.f(x = 1);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "keyword arguments are not supported for method calls in v1" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches
