# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.driftc.module_lowered import flatten_modules
from lang2.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


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


def test_non_generic_module_qualified_ctor_without_expected_type_ok(tmp_path: Path) -> None:
	src = """
module main

import std.concurrent as conc;
import std.err as err;
import std.core as core;

fn main() nothrow -> Int {
	val e = conc.ConcurrencyError::Closed();
	return 0;
}
""".lstrip()
	_mir, checked = _compile_source(src, tmp_path)
	assert not checked.diagnostics


def test_generic_module_qualified_ctor_requires_expected_type(tmp_path: Path) -> None:
	src = """
module main

import std.core as core;
import std.concurrent as conc;

fn main() nothrow -> Int {
	val x = core.Result::Err(conc.ConcurrencyError::Closed());
	return 0;
}
""".lstrip()
	_mir, checked = _compile_source(src, tmp_path)
	msgs = [d.message for d in checked.diagnostics]
	assert any("cannot infer" in m for m in msgs)


def test_generic_module_qualified_ctor_with_explicit_type_args_ok(tmp_path: Path) -> None:
	src = """
module main

import std.core as core;

fn main() nothrow -> Int {
	val r = core.Result::Ok<type Int, Int>(1);
	return 0;
}
""".lstrip()
	_mir, checked = _compile_source(src, tmp_path)
	assert not checked.diagnostics
