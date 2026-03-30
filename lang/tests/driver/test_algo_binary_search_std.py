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


def test_std_binary_search_requires_comparable(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

import std.algo as algo;
import std.iter as iter;
import std.core.cmp as cmp;

struct Range { n: Int }
struct Key { x: Int }

implement iter.RandomAccessReadable<Key> for Range {
	pub fn len(self: &Range) -> Int { return self.n; }
	pub fn compare_at(self: &Range, i: Int, j: Int) -> Int { return 0; }
}

implement algo.BinarySearchable<Key> for Range {
	pub fn compare_key(self: &Range, i: Int, key: &Key) -> Int { return 0; }
}

fn main() nothrow -> Int {
	val r = Range(n = 0);
	val k = Key(x = 1);
	val _i = algo.binary_search<type Range, Key>(&r, &k);
	return 0;
}
""",
	)
	assert any(d.code == "E_REQUIREMENT_NOT_SATISFIED" and any(n == "requirement_trait=std.core.cmp.Comparable" for n in (d.notes or [])) for d in diagnostics)


def test_std_binary_search_key_type_mismatch(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

import std.algo as algo;

fn main() nothrow -> Int {
	var arr = [1, 2, 3];
	var r = arr.range();
	val _i = algo.binary_search(&r, &"nope");
	return 0;
}
""",
	)
	assert diagnostics != []
