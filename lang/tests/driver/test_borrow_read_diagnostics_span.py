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


def test_use_after_move_reports_span(tmp_path: Path) -> None:
	# Source uses explicit `move xs` at the consume site (required
	# by spec §1.3 — bare HVar at by-value owned call arg is a
	# compile error since 0.33.7).  Borrow-checker's use-after-move
	# diagnostic still fires on the subsequent `xs.len` read.
	checked = _compile(
		tmp_path,
		"""
module m;

fn consume(xs: Array<Int>) nothrow -> Int {
	return xs.len;
}

pub fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	val _ = consume(move xs);
	return xs.len;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "use after move of 'xs'" in d.message]
	assert matches, errors
	assert all(d.phase == "borrowcheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_use_of_uninitialized_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

pub fn main() nothrow -> Int {
	var x: Int = 0;
	val _ = move x;
	return x;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "use after move of 'x'" in d.message]
	assert matches, errors
	assert all(d.phase == "borrowcheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_cannot_read_while_mutably_borrowed_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn read(r: &Int) nothrow -> Int {
	return r;
}

pub fn main() nothrow -> Int {
	var x = 0;
	val m = &mut x;
	return read(x);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "cannot take shared borrow while mutable borrow active on 'x'" in d.message]
	assert matches, errors
	assert all(d.phase == "borrowcheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_cannot_move_while_borrowed_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn consume(xs: Array<Int>) nothrow -> Int {
	return xs.len;
}

pub fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	val r = &xs;
	val y = move xs;
	return consume(move y) + r.len;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "cannot move 'xs' while borrowed" in d.message]
	assert matches, errors
	assert all(d.phase == "borrowcheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_cannot_borrow_from_moved_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

fn consume(xs: Array<Int>) nothrow -> Int {
	return xs.len;
}

pub fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	val _ = consume(move xs);
	val r = &xs;
	return r.len;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "cannot borrow from moved or uninitialized 'xs'" in d.message]
	assert matches, errors
	assert all(d.phase == "borrowcheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches


def test_cannot_read_while_mutably_borrowed_without_reborrow_reports_span(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

pub fn main() nothrow -> Int {
	var x = 0;
	val m = &mut x;
	return x;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "cannot read 'x' while it is mutably borrowed" in d.message]
	assert matches, errors
	assert all(d.phase == "borrowcheck" for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches
