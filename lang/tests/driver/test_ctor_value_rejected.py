# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_ctor_value_reference_rejected(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	main = mod_root / "main" / "main.drift"
	main.parent.mkdir(parents=True, exist_ok=True)
	main.write_text(
		"""
module main;

import std.concurrent as conc;

pub fn main() nothrow -> Int {
	val f = conc.SaturationPolicy::Block;
	return 0;
}
""".lstrip()
	)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diagnostics
	funcs, sigs, _ = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=funcs,
		signatures=sigs,
		type_table=type_table,
		exc_env=exc_catalog,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	msgs = [d.message for d in checked.diagnostics]
	assert any("qualified member reference" in m for m in msgs)
