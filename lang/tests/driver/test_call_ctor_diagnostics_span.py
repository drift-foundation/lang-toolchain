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


def test_struct_ctor_unknown_field_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct S { a: Int }

fn main() nothrow -> Int {
	val s = S(z = 1);
	return s.a;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "unknown field 'z' for struct 'S'" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_struct_ctor_missing_field_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct S { a: Int, b: Int }

fn main() nothrow -> Int {
	val s = S(a = 1);
	return s.a;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "missing field(s) for struct 'S': b" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_non_ctor_keyword_args_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

fn f(a: Int) nothrow -> Int { return a; }

fn main() nothrow -> Int {
	return f(a = 1);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "keyword arguments are only supported for constructors in MVP" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_struct_ctor_duplicate_field_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct S { a: Int }

fn main() nothrow -> Int {
	val s = S(a = 1, a = 2);
	return s.a;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "duplicate field 'a' for struct 'S'" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_struct_ctor_mixed_positional_named_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct S { a: Int }

fn main() nothrow -> Int {
	val s = S(1, a = 2);
	return s.a;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "cannot mix positional and named arguments for struct 'S'" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_variant_ctor_unknown_field_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

variant V {
	A(x: Int),
}

fn main() nothrow -> Int {
	val v: V = A(y = 1);
	match v {
		A(x) => { return x; }
	}
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "unknown field 'y' for constructor 'A'" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_variant_ctor_missing_field_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

variant V {
	A(x: Int, y: Int),
}

fn main() nothrow -> Int {
	val v: V = A(x = 1);
	match v {
		A(x, y) => { return x + y; }
	}
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "missing field 'y' for constructor 'A'" in d.message]
	assert matches, errors
	assert all(d.phase == "typecheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches
