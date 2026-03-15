# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile_source(src: str, tmp_path: Path):
	mod_root = tmp_path / "mods"
	_write_file(mod_root / "main" / "main.drift", src)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diagnostics
	funcs, sigs, _ = flatten_modules(modules)
	return compile_stubbed_funcs(
		func_hirs=funcs,
		signatures=sigs,
		type_table=type_table,
		exc_env=exc_catalog,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)


def test_ufcs_callinfo_preserved(tmp_path: Path) -> None:
	src = """
module main;

import std.core as core;
use trait core.Try;
use trait core.Diagnostic;

fn main() -> Int {
	val r: core.Result<Int, Int> = core.Result::Ok(1);
	val v = core.Try::into_try(r);
	return v;
}
""".lstrip()
	_mir, checked = _compile_source(src, tmp_path)
	assert not checked.diagnostics
