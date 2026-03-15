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
	return modules, type_table, exc_catalog, module_exports, module_deps, diagnostics


def test_type_alias_basic(tmp_path: Path) -> None:
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = _compile(
		tmp_path,
		"""
module m_main;

type MyInt = Int;

fn main() nothrow -> Int {
	val x: MyInt = 1;
	return x;
}
""",
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
	assert checked.diagnostics == []


def test_type_alias_nested_and_generic(tmp_path: Path) -> None:
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = _compile(
		tmp_path,
		"""
module m_main;

type MyInt = Int;
type Pair<T> = Array<T>;
type Alias = MyInt;

fn main() nothrow -> Int {
	val xs: Pair<Int> = [1, 2, 3];
	val y: Alias = 4;
	return xs[0] + y;
}
""",
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
	assert checked.diagnostics == []


def test_type_alias_cycle_diagnostic(tmp_path: Path) -> None:
	_mods, _table, _exc, _exports, _deps, diagnostics = _compile(
		tmp_path,
		"""
module m_main;

type A = B;
type B = A;

fn main() nothrow -> Int { return 0; }
""",
	)
	assert any("type alias cycle detected" in d.message for d in diagnostics)
