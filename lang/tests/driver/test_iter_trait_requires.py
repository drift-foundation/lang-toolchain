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


def test_multipass_requires_copy(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module m_main;

import std.iter as iter;

struct BadIter {
	data: Array<Int>,
	idx: Uint
}

implement iter.SinglePassIterator<Int> for BadIter {
	pub fn next(self: &mut BadIter) -> Optional<Int> {
		return Optional::None();
	}
}

implement iter.MultiPassIterator<Int> for BadIter {
}

struct Box<T> require T is iter.MultiPassIterator<Int> { value: T }

pub fn main() nothrow -> Int {
	val it = BadIter(data = [1], idx = 0);
	val _b = Box<type BadIter>(value = it);
	return 0;
}
""",
	)
	assert any("requirement not satisfied" in d.message and "MultiPassIterator" in d.message for d in diagnostics)
