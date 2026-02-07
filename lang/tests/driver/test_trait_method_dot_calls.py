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


def test_trait_dot_call_on_type_param_requires_use_trait(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.iter as iter;

struct Range { n: Int }

implement iter.RandomAccessReadable<Int> for Range {
	pub fn len(self: &Range) -> Int { return self.n; }
	pub fn compare_at(self: &Range, i: Int, j: Int) -> Int { return 0; }
}

fn total<R>(r: &R) -> Int
require R is iter.RandomAccessReadable<Int> {
	return r.len();
}

fn main() -> Int {
	val r = Range(n = 3);
	return total(&r);
}
""",
	)
	assert any("no matching method 'len'" in d.message for d in diagnostics)


def test_trait_dot_call_on_type_param_with_use_trait(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main

import std.iter as iter;

use trait iter.RandomAccessReadable;

struct Range { n: Int }

implement iter.RandomAccessReadable<Int> for Range {
	pub fn len(self: &Range) -> Int { return self.n; }
	pub fn compare_at(self: &Range, i: Int, j: Int) -> Int { return 0; }
}

fn total<R>(r: &R) -> Int
require R is iter.RandomAccessReadable<Int> {
	return r.len();
}

fn main() -> Int {
	val r = Range(n = 3);
	return total(&r);
}
""",
	)
	assert diagnostics == []
