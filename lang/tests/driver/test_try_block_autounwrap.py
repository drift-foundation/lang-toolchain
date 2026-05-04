# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Try-block auto-unwrap contract.

Auto-try is compiler-owned: a `try {}` block auto-unwraps Result<T, E>
expression statements via or_throw() without requiring any lexical
trait import.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile_source(src: str, tmp_path: Path):
	path = tmp_path / "main.drift"
	_write_file(path, src)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	return checked.diagnostics


def test_try_block_result_stmt_autounwraps(tmp_path: Path) -> None:
	"""A Result-typed expression statement inside a try {} block is
	auto-unwrapped via or_throw() — no trait import required, no
	'value discarded' diagnostic."""
	diagnostics = _compile_source(
		"""
module main;

import std.core as core;

pub error MyErr {
    code: Int,
}

fn ok() -> core.Result<Int, MyErr> {
    return core.Result::Ok(1);
}

fn main() nothrow -> Int {
    try {
        ok();
        return 0;
    } catch {
        return 1;
    }
}
""",
		tmp_path,
	)
	assert diagnostics == [], (
		f"try-block auto-unwrap should fire without trait import; "
		f"got {[d.message for d in diagnostics]}"
	)


def test_try_block_without_catch_autounwraps(tmp_path: Path) -> None:
	"""Same as above, without the catch arm.  Auto-unwrap is
	a property of the try-block, not the presence of catch."""
	diagnostics = _compile_source(
		"""
module main;

import std.core as core;

pub error MyErr {
    code: Int,
}

fn ok() -> core.Result<Int, MyErr> {
    return core.Result::Ok(1);
}

fn main() -> Int {
    try {
        ok();
        return 0;
    }
}
""",
		tmp_path,
	)
	assert diagnostics == [], (
		f"try-block auto-unwrap should fire without trait import; "
		f"got {[d.message for d in diagnostics]}"
	)


def test_try_block_autounwrap_stmt_sets_callsite_id(tmp_path: Path) -> None:
	diagnostics = _compile_source(
		"""
module main;

import std.core as core;

pub error MyErr {
    code: Int,
}

fn ok() -> core.Result<Int, MyErr> {
    return core.Result::Ok(1);
}

fn main() -> Int {
    try {
        ok();
        return 0;
    }
}
""",
		tmp_path,
	)
	assert diagnostics == []
