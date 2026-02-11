# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _resolve(tmp_path: Path, content: str) -> None:
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(src, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, _diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	_ = modules, exc_catalog, module_exports, module_deps
	base_id = type_table.get_variant_base(module_id="m_main", name="Maybe")
	assert base_id is not None
	type_table.ensure_instantiated(base_id, [type_table.ensure_string()])


def test_droppable_variant_without_tombstone_is_allowed(tmp_path: Path) -> None:
	_resolve(
		tmp_path,
		"""
module m_main

variant Maybe<T> {
	Some(value: T),
	None
}

fn main() nothrow -> Int {
	var xs: Array<Maybe<String>> = [];
	val _ = xs.pop();
	return 0;
}
""",
	)
