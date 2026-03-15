# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_orphan_impl_rejected_in_sources(tmp_path: Path) -> None:
	files = {
		Path("m_main/main.drift"): """
module m_main;

import m_a;
import m_b;

implement m_a.TA for m_b.SB {
	pub fn f(self: m_b.SB) -> Int { return 0; }
}

fn main() nothrow -> Int { return 0; }
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	_modules, _table, _exc, _exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		package_id="pkg_main",
		external_module_packages={"m_a": "pkg_a", "m_b": "pkg_b"},
		external_module_exports={
			"m_a": {"traits": ["TA"]},
			"m_b": {"types": {"structs": ["SB"]}},
		},
		stdlib_root=stdlib_root(),
	)
	assert diagnostics
	assert any(d.code == "E-IMPL-ORPHAN" for d in diagnostics)
	assert any(d.phase == "parser" for d in diagnostics if d.code == "E-IMPL-ORPHAN")
	assert not any(d.phase == "typecheck" for d in diagnostics)
