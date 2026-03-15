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


def test_noncopy_field_projection_from_borrow_is_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

struct Payload {
	xs: Array<Int>
}

struct Wrapper {
	p: Payload
}

fn take(w: &Wrapper) nothrow -> Payload {
	return w.p;
}

fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	xs.push(1);
	val w = Wrapper(p = Payload(xs = move xs));
	val y = take(&w);
	return y.xs.len;
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any(d.phase == "typecheck" for d in errors), errors
	assert any("cannot copy value of type" in d.message for d in errors), errors


def test_copy_nested_field_projection_from_borrow_is_allowed(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

struct Inner {
	n: Int
}

struct Wrapper {
	inner: Inner
}

fn take(w: &Wrapper) nothrow -> Int {
	return w.inner.n;
}

fn main() nothrow -> Int {
	val w = Wrapper(inner = Inner(n = 41));
	return take(&w) + 1;
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []


def test_owned_noncopy_field_replace_extract_is_allowed(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

import std.mem as mem;

struct Payload {
	xs: Array<Int>
}

struct Wrapper {
	p: Payload
}

fn take(var w: Wrapper) nothrow -> Payload {
	var empty: Array<Int> = [];
	return mem.replace(&mut w.p, Payload(xs = move empty));
}

fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	xs.push(1);
	val w = Wrapper(p = Payload(xs = move xs));
	val y = take(w);
	return y.xs.len;
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []


def test_nested_noncopy_projection_from_borrow_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

struct Payload {
	xs: Array<Int>
}

struct Wrapper {
	p: Payload
}

fn consume(xs: Array<Int>) nothrow -> Int {
	return xs.len;
}

fn take(w: &Wrapper) nothrow -> Int {
	val ys = w.p.xs;
	return ys.len;
}

fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	xs.push(1);
	val w = Wrapper(p = Payload(xs = move xs));
	return take(&w);
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any(d.phase == "typecheck" for d in errors), errors
	copy_errors = [d for d in errors if "cannot copy value of type" in d.message]
	assert copy_errors, errors
	assert all(d.span.line is not None and d.span.column is not None for d in copy_errors), copy_errors


def test_owned_nested_noncopy_projection_byvalue_call_rejected_with_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

struct Payload {
	xs: Array<Int>
}

struct Wrapper {
	p: Payload
}

fn consume(xs: Array<Int>) nothrow -> Int {
	return xs.len;
}

fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	xs.push(1);
	val w = Wrapper(p = Payload(xs = move xs));
	return consume(w.p.xs);
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	proj_errors = [d for d in errors if "move of a projected place is not supported in v1" in d.message]
	assert proj_errors, errors
	assert all(d.phase == "borrowcheck" for d in proj_errors), proj_errors
	assert all(d.span.line is not None and d.span.column is not None for d in proj_errors), proj_errors


def test_owned_nested_noncopy_projection_replace_extract_is_allowed(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

import std.mem as mem;

struct Payload {
	xs: Array<Int>
}

struct Wrapper {
	p: Payload
}

fn take(var w: Wrapper) nothrow -> Int {
	var empty: Array<Int> = [];
	val ys = mem.replace(&mut w.p.xs, move empty);
	return ys.len;
}

fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	xs.push(1);
	val w = Wrapper(p = Payload(xs = move xs));
	return take(w);
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []


def test_borrowed_index_noncopy_projection_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

struct Payload {
	xs: Array<Int>
}

struct Wrapper {
	ps: Array<Payload>
}

fn take(w: &Wrapper) nothrow -> Int {
	val p = w.ps[0];
	return p.xs.len;
}

fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	xs.push(1);
	var ps: Array<Payload> = [];
	ps.push(Payload(xs = move xs));
	val w = Wrapper(ps = move ps);
	return take(&w);
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any(d.phase == "typecheck" for d in errors), errors
	copy_errors = [d for d in errors if "cannot copy value of type" in d.message]
	assert copy_errors, errors
	assert all(d.span.line is not None and d.span.column is not None for d in copy_errors), copy_errors


def test_move_non_place_operand_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn main() nothrow -> Int {
	val x = move (1 + 2);
	return x;
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	move_errors = [d for d in errors if "move operand must be an addressable place in v1" in d.message]
	assert move_errors, errors
	assert all(d.phase == "typecheck" for d in move_errors), move_errors
	assert all(d.span.line is not None and d.span.column is not None for d in move_errors), move_errors


def test_borrow_conflict_in_same_statement_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn f(a: &mut Array<Int>, b: &Array<Int>) nothrow -> Int {
	return b.len;
}

fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	return f(&mut xs, &xs);
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	borrow_errors = [d for d in errors if "conflicting borrows in the same statement" in d.message]
	assert borrow_errors, errors
	assert all(d.phase == "typecheck" for d in borrow_errors), borrow_errors
	assert all(d.span.line is not None and d.span.column is not None for d in borrow_errors), borrow_errors


def test_move_from_reference_type_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	val r = &xs;
	val y = move r;
	return 0;
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	move_errors = [d for d in errors if "cannot move from a reference type; move requires owned storage" in d.message]
	assert move_errors, errors
	assert all(d.phase == "typecheck" for d in move_errors), move_errors
	assert all(d.span.line is not None and d.span.column is not None for d in move_errors), move_errors


def test_copy_non_place_operand_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn main() nothrow -> Int {
	val x = copy (1 + 2);
	return x;
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	copy_errors = [d for d in errors if "copy operand must be an addressable place in v1" in d.message]
	assert copy_errors, errors
	assert all(d.phase == "typecheck" for d in copy_errors), copy_errors
	assert all(d.span.line is not None and d.span.column is not None for d in copy_errors), copy_errors


def test_borrow_mut_immutable_binding_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn main() nothrow -> Int {
	val xs: Array<Int> = [];
	val r = &mut xs;
	return 0;
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	borrow_errors = [d for d in errors if "cannot take &mut of an immutable binding; declare it with `var`" in d.message]
	assert borrow_errors, errors
	assert all(d.phase == "typecheck" for d in borrow_errors), borrow_errors
	assert all(d.span.line is not None and d.span.column is not None for d in borrow_errors), borrow_errors


def test_borrow_mut_through_shared_ref_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	val p = &xs;
	val r = &mut *p;
	return 0;
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	borrow_errors = [d for d in errors if "cannot take &mut through *p unless p is a mutable reference (&mut T)" in d.message]
	assert borrow_errors, errors
	assert all(d.phase == "typecheck" for d in borrow_errors), borrow_errors
	assert all(d.span.line is not None and d.span.column is not None for d in borrow_errors), borrow_errors
