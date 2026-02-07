from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.driftc import compile_stubbed_funcs


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_deque_callinfo_intrinsics_ok(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(
		src,
		"""
module m_main

import std.containers as containers;

fn run() -> Int {
	var dq = containers.deque<type Int>();
	dq.push_back(1);
	val _ = dq.at(0);
	return 0;
}

fn main() nothrow -> Int {
	return 0;
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, sigs, _fn_ids_by_name = flatten_modules(modules)
	_mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	assert checked.diagnostics == []
