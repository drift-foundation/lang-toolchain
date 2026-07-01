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


def test_eq_uses_std_core_cmp_without_std_algo(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

import std.core.cmp as cmp;
import std.core as core;

struct Foo { x: Int }

implement cmp.Equatable for Foo {
	pub fn eq(self: &Foo, other: &Foo) -> Bool { return self.x == other.x; }
}

implement core.Copy for Foo {
}

pub fn main() nothrow -> Int {
	val a = Foo(x = 1);
	val b = Foo(x = 1);
	if a == b { return 1; }
	return 0;
}
""",
	)
	assert diagnostics == []
