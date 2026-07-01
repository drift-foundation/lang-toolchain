# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

import pytest

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile(tmp_path: Path, content: str, *, use_stdlib: bool = True, run_borrow_check: bool = False):
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(src, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root() if use_stdlib else None,
	)
	assert diagnostics == []
	func_hirs, sigs, _fn_ids = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
		run_borrow_check=run_borrow_check,
	)
	return checked.diagnostics


def test_for_on_non_iterable_reports_error(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

pub fn main() nothrow -> Int {
	for x in 1 { x; }
	return 0;
}
""",
	)
	assert any("type is not iterable" in d.message for d in diagnostics)


def test_next_on_non_iterator_reports_error(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

import std.iter as iter;

pub fn main() nothrow -> Int {
	var x = 1;
	iter.SinglePassIterator::next(&mut x);
	return 0;
}
""",
	)
	assert any("no matching method 'next'" in d.message for d in diagnostics)


def test_iterable_ufcs_reports_generic_error(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

import std.iter as iter;

pub fn main() nothrow -> Int {
	iter.Iterable::iter(1);
	return 0;
}
""",
	)
	assert any("no matching method 'iter'" in d.message for d in diagnostics)
	assert not any("type is not iterable" in d.message for d in diagnostics)


def test_for_without_stdlib_root_is_not_iterable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

pub fn main() nothrow -> Int {
	val xs = [1, 2, 3];
	for x in move xs { x; }
	return 0;
}
""",
		use_stdlib=False,
	)
	assert any(d.code == "E-NOT-ITERABLE" for d in diagnostics)


def test_for_iter_result_without_iterator_impl_reports_error(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

import std.iter as iter;

struct Dummy(x: Int);
struct NotIter(x: Int);

implement iter.Iterable<Dummy, Int, NotIter> for Dummy {
	pub fn iter(self: Dummy) -> NotIter { return NotIter(x = 0); }
}

pub fn main() nothrow -> Int {
	val d = Dummy(x = 1);
	for x in d { x; }
	return 0;
}
""",
	)
	assert any(d.code == "E-ITER-RESULT-NOT-ITERATOR" for d in diagnostics)


def test_for_ignores_local_iterable_names(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

trait Iterable<T> { fn iter(self: &Self) -> Int; }
trait SinglePassIterator<T> { fn next(self: &mut Self) -> Int; }

pub fn main() nothrow -> Int {
	val xs = [1, 2, 3];
	for x in xs { x; }
	return 0;
}
""",
	)
	assert diagnostics == []


@pytest.mark.heavy
def test_for_over_function_returned_array_compiles(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

fn make() nothrow -> Array<Int> { return [1, 2, 3]; }

pub fn main() nothrow -> Int {
	for x in make() { x; }
	return 0;
}
""",
	)
	assert diagnostics == []


def test_for_over_borrowed_function_return_does_not_consume(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

fn make() nothrow -> Array<Int> { return [1, 2, 3]; }

pub fn main() nothrow -> Int {
	for x in &make() { x; }
	return 0;
}
""",
		run_borrow_check=True,
	)
	assert diagnostics == []


def test_for_over_owned_array_compiles(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

pub fn main() nothrow -> Int {
	val xs = [1, 2, 3];
	for x in xs { x; }
	return 0;
}
""",
	)
	assert diagnostics == []


def test_for_over_borrowed_array_compiles(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

pub fn main() nothrow -> Int {
	val xs = [1, 2, 3];
	for x in &xs { x; }
	return 0;
}
""",
	)
	assert diagnostics == []


def test_for_over_owned_array_consumes_non_copy(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

pub fn main() nothrow -> Int {
	var a = [1, 2, 3];
	for x in move a { x; }
	a.push(4);
	return 0;
}
""",
		run_borrow_check=True,
	)
	assert any(d.code == "E_USE_AFTER_MOVE" for d in diagnostics)


def test_for_over_borrowed_array_does_not_consume(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

pub fn main() nothrow -> Int {
	var arr = [1, 2, 3];
	for x in &arr { x; }
	arr.push(2);
	return 0;
}
""",
		run_borrow_check=True,
	)
	assert diagnostics == []


def test_method_receiver_borrowcheck_ref_and_mut(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

struct Box { value: Int }

implement Box {
	pub fn get(self: &Box) nothrow -> Int { return self.value; }
	pub fn set(self: &mut Box, v: Int) nothrow -> Void { self.value = v; }
}

pub fn main() nothrow -> Int {
	var b = Box(value = 1);
	val a = b.get();
	b.set(2);
	return a + b.value;
}
""",
		run_borrow_check=True,
	)
	assert diagnostics == []


def test_array_borrow_then_write_same_index_rejected(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

pub fn main() nothrow -> Int {
	var arr = [1, 2, 3];
	val r = &arr[0];
	arr[0] = 4;
	return *r;
}
""",
		run_borrow_check=True,
	)
	assert any("cannot write" in d.message and "borrow" in d.message for d in diagnostics)


def test_array_borrow_then_write_disjoint_index_ok(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

pub fn main() nothrow -> Int {
	var arr = [1, 2, 3];
	val r = &arr[0];
	arr[1] = 4;
	return *r;
}
""",
		run_borrow_check=True,
	)
	assert diagnostics == []


def test_by_value_call_consumes_non_copy(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

fn consume(xs: Array<Int>) nothrow -> Int { return 0; }

pub fn main() nothrow -> Int {
	var arr = [1, 2, 3];
	consume(move arr);
	arr.push(2);
	return 0;
}
""",
		run_borrow_check=True,
	)
	assert any(d.code == "E_USE_AFTER_MOVE" for d in diagnostics)


def test_for_owned_array_borrows_non_copy(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

fn consume(xs: Array<Array<Int>>) nothrow -> Int {
	for x in xs { x; }
	return 0;
}

pub fn main() nothrow -> Int {
	return 0;
}
""",
	)
	assert diagnostics == []


def test_for_mut_array_is_iterable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

pub fn main() nothrow -> Int {
	var xs = [1, 2, 3];
	for x in &mut xs { x; }
	xs.push(4);
	return 0;
}
""",
		run_borrow_check=True,
	)
	assert diagnostics == []


def test_mut_iter_next_while_borrowed_is_error(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

import std.iter as iter;
import std.containers as containers;

pub fn main() -> Int {
	var xs = [1, 2];
	var it: containers.ArrayBorrowMutIter<Int> = iter.Iterable::iter(&mut xs);
	val a: Optional<&mut Int> = iter.SinglePassIterator::next(&mut it);
	val b = iter.SinglePassIterator::next(&mut it);
	return 0;
}
""",
		run_borrow_check=True,
	)
	assert any("cannot take mutable borrow" in d.message for d in diagnostics)


def test_mut_iter_next_after_drop_is_ok(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

import std.iter as iter;
import std.containers as containers;

fn take_one(it: &mut containers.ArrayBorrowMutIter<Int>) -> Int {
	val a: Optional<&mut Int> = iter.SinglePassIterator::next(it);
	match a {
		Some(x) => { *x = 3; },
		None => { },
	}
	return 0;
}

pub fn main() -> Int {
	var xs = [1, 2];
	var it: containers.ArrayBorrowMutIter<Int> = iter.Iterable::iter(&mut xs);
	take_one(&mut it);
	val b = iter.SinglePassIterator::next(&mut it);
	return 0;
}
""",
		run_borrow_check=True,
	)
	assert diagnostics == []

def test_borrowed_array_elem_same_index_write_rejected(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

pub fn main() -> Int {
	var xs = [1, 2];
	val r = &xs[0];
	xs[0] = 4;
	return 0;
}
""",
		run_borrow_check=True,
	)
	assert any("cannot write to 'xs'" in d.message for d in diagnostics)
