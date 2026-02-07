# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile(tmp_path: Path, content: str):
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(src, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
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
	)
	return checked.diagnostics


def test_comparable_requires_equatable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.core.cmp as cmp;

struct Foo { x: Int }

implement cmp.Comparable for Foo {
	pub fn cmp(self: &Foo, other: &Foo) -> Int { return 0; }
}

fn needs_cmp<T>(x: T) nothrow -> Int require T is cmp.Comparable { return 0; }

fn main() nothrow -> Int {
	val _x = Foo(x = 1);
	val _y = needs_cmp(_x);
	return 0;
}
""",
	)
	assert any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::std.core.cmp.Comparable" in n for n in (d.notes or [])) for d in diagnostics)


def test_binary_search_requires_binarysearchable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.iter as iter;
import std.core.cmp as cmp;

struct Range { n: Int }
struct Key { x: Int }

implement iter.RandomAccessReadable<Key> for Range {
	pub fn len(self: &Range) -> Int { return self.n; }
	pub fn compare_at(self: &Range, i: Int, j: Int) -> Int { return 0; }
}

trait BinarySearchable<T> require Self is iter.RandomAccessReadable<T> {
	fn compare_key(self: &Self, i: Int, key: &T) -> Int;
}

fn binary_search<R, T>(r: &R, key: &T) nothrow -> Optional<Int>
	require (R is BinarySearchable<T> and T is cmp.Comparable) {
	return Optional::None();
}

fn main() nothrow -> Int {
	val r = Range(n = 0);
	val k = Key(x = 1);
	val _i = binary_search<type Range, Key>(&r, &k);
	return 0;
}
""",
	)
	assert any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::m_main.BinarySearchable" in n for n in (d.notes or [])) for d in diagnostics)


def test_binary_search_callable_with_random_access_readable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.core as core;

struct Range { n: Int }
struct Key { x: Int }

trait Equatable {
	fn eq(self: &Self, other: &Self) -> Bool;
}

trait Comparable require Self is Equatable {
	fn cmp(self: &Self, other: &Self) -> Int;
}

implement Equatable for Key {
	pub fn eq(self: &Key, other: &Key) -> Bool { return true; }
}

implement Comparable for Key {
	pub fn cmp(self: &Key, other: &Key) -> Int { return 0; }
}

implement core.Copy for Range {
}

implement core.Copy for Key {
}

trait RandomAccessReadable<T> {
	fn len(self: &Self) -> Int;
	fn compare_at(self: &Self, i: Int, j: Int) -> Int;
}

implement RandomAccessReadable<Key> for Range {
	pub fn len(self: &Range) -> Int { return self.n; }
	pub fn compare_at(self: &Range, i: Int, j: Int) -> Int { return 0; }
}

trait BinarySearchable<T> {
	fn compare_key(self: &Self, i: Int, key: &T) -> Int;
}

implement BinarySearchable<Key> for Range {
	pub fn compare_key(self: &Range, i: Int, key: &Key) -> Int { return 0; }
}

struct Need<R, T> require (R is BinarySearchable<T> and R is RandomAccessReadable<T> and T is Comparable) {
	r: R,
	t: T
}

fn main() nothrow -> Int {
	val r = Range(n = 0);
	val k = Key(x = 1);
	val _n = Need<type Range, Key>(r = r, t = k);
	return 0;
}
""",
	)
	assert diagnostics == []


def test_binary_search_requires_comparable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.iter as iter;
import std.core.cmp as cmp;

struct Range { n: Int }
struct Key { x: Int }

trait BinarySearchable<T> {
	fn compare_key(self: &Self, i: Int, key: &T) -> Int;
}

implement iter.RandomAccessReadable<Key> for Range {
	pub fn len(self: &Range) -> Int { return self.n; }
	pub fn compare_at(self: &Range, i: Int, j: Int) -> Int { return 0; }
}

implement BinarySearchable<Key> for Range {
	pub fn compare_key(self: &Range, i: Int, key: &Key) -> Int { return 0; }
}

fn binary_search<R, T>(r: &R, key: &T) nothrow -> Optional<Int>
	require (R is BinarySearchable<T> and T is cmp.Comparable) {
	return Optional::None();
}

fn main() nothrow -> Int {
	val r = Range(n = 0);
	val k = Key(x = 1);
	val _i = binary_search<type Range, Key>(&r, &k);
	return 0;
}
""",
	)
	assert any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::std.core.cmp.Comparable" in n for n in (d.notes or [])) for d in diagnostics)


def test_binary_search_missing_both_capability_and_comparable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.core.cmp as cmp;

struct Range { n: Int }
struct Key { x: Int }

trait BinarySearchable<T> {
	fn compare_key(self: &Self, i: Int, key: &T) -> Int;
}

struct NeedCap<R, T> require R is BinarySearchable<T> {
	r: R
}

struct NeedCmp<T> require T is cmp.Comparable {
	t: T
}

	fn main() nothrow -> Int {
		val r = Range(n = 0);
		val k = Key(x = 1);
		val _n1 = NeedCap<type Range, Key>(r = r);
		val _n2 = NeedCmp<type Key>(t = k);
		return 0;
	}
""",
	)
	has_cap = any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::m_main.BinarySearchable" in n for n in (d.notes or [])) for d in diagnostics)
	has_cmp = any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::std.core.cmp.Comparable" in n for n in (d.notes or [])) for d in diagnostics)
	assert has_cap and has_cmp


def test_sort_in_place_requires_comparable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.core.cmp as cmp;

struct Range { n: Int }
struct Key { x: Int }

trait Permutable<T> { fn swap(self: &mut Self, i: Int, j: Int) -> Void; }

implement Permutable<Key> for Range {
	pub fn swap(self: &mut Range, i: Int, j: Int) -> Void { return; }
}

fn sort_in_place<R, T>(r: &mut R) nothrow -> Void
	require (R is Permutable<T> and T is cmp.Comparable) {
	return;
}

fn main() nothrow -> Int {
	var r = Range(n = 0);
	val _ = sort_in_place<type Range, Key>(&mut r);
	return 0;
}
""",
	)
	assert any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::std.core.cmp.Comparable" in n for n in (d.notes or [])) for d in diagnostics)


def test_min_max_require_comparable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.core.cmp as cmp;

struct Key { x: Int }

fn min_val<T>(a: T, b: T) nothrow -> T require T is cmp.Comparable { return a; }
fn max_val<T>(a: T, b: T) nothrow -> T require T is cmp.Comparable { return b; }

fn main() nothrow -> Int {
	val k1 = Key(x = 1);
	val k2 = Key(x = 2);
	val _a = min_val<type Key>(k1, k2);
	val _b = max_val<type Key>(k1, k2);
	return 0;
}
""",
	)
	assert any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::std.core.cmp.Comparable" in n for n in (d.notes or [])) for d in diagnostics)


def test_sort_in_place_compiles_for_non_copy_t(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

struct NonCopy { data: Array<Int> }
struct Range { n: Int }

trait RandomAccessReadable<T> {
	fn len(self: &Self) -> Int;
	fn compare_at(self: &Self, i: Int, j: Int) -> Int;
}

implement RandomAccessReadable<NonCopy> for Range {
	pub fn len(self: &Range) -> Int { return self.n; }
	pub fn compare_at(self: &Range, i: Int, j: Int) -> Int { return 0; }
}

trait Permutable<T> {
	fn swap(self: &mut Self, i: Int, j: Int) -> Void;
}

implement Permutable<NonCopy> for Range {
	pub fn swap(self: &mut Range, i: Int, j: Int) -> Void { return; }
}

struct Need<R, T> require (R is Permutable<T> and R is RandomAccessReadable<T>) {
	r: R
}

	fn main() nothrow -> Int {
		var r = Range(n = 0);
		val _n = Need<type Range, NonCopy>(r = move r);
		return 0;
	}
""",
	)
	assert diagnostics == []


def test_sort_in_place_missing_both_capability_and_comparable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.core.cmp as cmp;

struct Range { n: Int }
struct Key { x: Int }

trait Permutable<T> { fn swap(self: &mut Self, i: Int, j: Int) -> Void; }

struct NeedPerm<R, T> require R is Permutable<T> {
	r: R
}

struct NeedCmp<T> require T is cmp.Comparable {
	t: T
}

	fn main() nothrow -> Int {
		var r = Range(n = 0);
		val _n1 = NeedPerm<type Range, Key>(r = r);
		val _n2 = NeedCmp<type Key>(t = Key(x = 1));
		return 0;
	}
""",
	)
	has_perm = any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::m_main.Permutable" in n for n in (d.notes or [])) for d in diagnostics)
	has_cmp = any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::std.core.cmp.Comparable" in n for n in (d.notes or [])) for d in diagnostics)
	assert has_perm and has_cmp


def test_min_max_require_comparable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.core.cmp as cmp;

struct Foo { x: Int }

struct Need<T> require T is cmp.Comparable { value: T }

fn main() nothrow -> Int {
	val a = Foo(x = 1);
	val b = Foo(x = 2);
	val _m1 = Need<type Foo>(value = a);
	val _m2 = Need<type Foo>(value = b);
	return 0;
}
""",
	)
	assert any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::std.core.cmp.Comparable" in n for n in (d.notes or [])) for d in diagnostics)


def test_array_binary_search_unavailable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.core.cmp as cmp;

trait BinarySearchable<T> { fn compare_key(self: &Self, i: Int, key: &T) -> Int; }

struct Need<R, T> require (R is BinarySearchable<T> and T is cmp.Comparable) {
	r: R,
	t: T
}

fn main() nothrow -> Int {
	val xs = [1, 2, 3];
	val key = 2;
	val _n = Need<type Array<Int>, Int>(r = xs, t = key);
	return 0;
}
""",
	)
	assert any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::m_main.BinarySearchable" in n for n in (d.notes or [])) for d in diagnostics)


def test_array_sort_in_place_unavailable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.core.cmp as cmp;

trait Permutable<T> { fn swap(self: &mut Self, i: Int, j: Int) -> Void; }

struct Need<R, T> require (R is Permutable<T> and T is cmp.Comparable) {
	r: R
}

fn main() nothrow -> Int {
	var xs = [3, 1, 2];
	val _n = Need<type Array<Int>, Int>(r = xs);
	return 0;
}
""",
	)
	assert any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any("requirement_trait=__local__::m_main.Permutable" in n for n in (d.notes or [])) for d in diagnostics)
