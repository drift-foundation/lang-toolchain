# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.driftc.module_lowered import flatten_modules
from lang2.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


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


def test_match_optional_scrutinee_is_variant(tmp_path: Path) -> None:
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = _compile(
		tmp_path,
		"""
module m_main

fn run() -> Int {
	val opt = Optional::Some(1);
	match opt {
		Some(v) => { return v; },
		None => { return 0; },
		default => { return 0; }
	}
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
