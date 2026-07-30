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


def test_std_sort_in_place_allows_non_comparable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

import std.algo as algo;
import std.iter as iter;

struct Range { n: Int }
struct Key { x: Int }

implement iter.RandomAccessReadable<Key> for Range {
	pub fn len(self: &Range) -> Int { return self.n; }
	pub fn compare_at(self: &Range, i: Int, j: Int) -> Int { return 0; }
}

implement iter.RandomAccessPermutable<Key> for Range {
	pub fn swap(self: &mut Range, i: Int, j: Int) -> Void { return; }
}

pub fn main() -> Int {
	var r = Range(n = 0);
	algo.sort_in_place<type Range, Key>(r);
	return 0;
}
""",
	)
	assert diagnostics == []


def test_std_sort_in_place_requires_permutable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

import std.algo as algo;
import std.iter as iter;

struct Range { n: Int }
struct Key { x: Int }

implement iter.RandomAccessReadable<Key> for Range {
	pub fn len(self: &Range) -> Int { return self.n; }
	pub fn compare_at(self: &Range, i: Int, j: Int) -> Int { return 0; }
}

pub fn main() -> Int {
	var r = Range(n = 0);
	algo.sort_in_place<type Range, Key>(r);
	return 0;
}
""",
	)
	assert any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any(n == "requirement_trait=std.iter.RandomAccessPermutable" for n in (d.notes or [])) for d in diagnostics)
