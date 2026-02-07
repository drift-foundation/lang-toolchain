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


def test_ufcs_trait_call_with_require_type_args_compiles(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.algo as algo;

fn use_compare<R, T>(r: &R, key: &T) -> Int require R is algo.BinarySearchable<T> {
	val _c = algo.BinarySearchable::compare_key(r, 0, key);
	return 0;
}

fn main() nothrow -> Int {
	return 0;
}
""",
	)
	assert diagnostics == []


def test_trait_impl_with_type_args_resolves(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

trait Marker { }

trait Uses<T> require T is Marker {
	fn f(self: &Self) -> Int;
}

struct Foo { }
struct Bar { }

implement Marker for Bar { }

implement Uses<Bar> for Foo {
	fn f(self: &Foo) -> Int { return 7; }
}

fn main() -> Int {
	var foo = Foo();
	return Uses<Bar>::f(&foo);
}
""",
	)
	assert diagnostics == []


def test_trait_impl_with_mismatched_type_args_fails(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

trait Marker { }

trait Uses<T> require T is Marker {
	fn f(self: &Self) -> Int;
}

struct Foo { }
struct Bar { }
struct Baz { }

implement Marker for Bar { }

implement Uses<Bar> for Foo {
	fn f(self: &Foo) -> Int { return 7; }
}

fn main() -> Int {
	var foo = Foo();
	return Uses<Baz>::f(&foo);
}
""",
	)
	assert diagnostics != []
