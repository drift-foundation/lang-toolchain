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
	return list(diagnostics) + list(checked.diagnostics)


def test_compare_mismatch_mentions_types(tmp_path: Path) -> None:
	diags = _compile(
		tmp_path,
		"""
module m

import std.core as core;

pub fn main() nothrow -> Int {
	val b = cast<Byte>(65);
	val i: Int = 65;
	if b == i { return 0; }
	return 1;
}
""",
	)
	assert any(
		"comparison requires matching operand types" in (d.message or "")
		and "Byte" in (d.message or "")
		and "Int" in (d.message or "")
		for d in diags
	)
