# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir


def test_single_entry_missing_import_reports_actionable_hint(tmp_path: Path) -> None:
	a = tmp_path / "a.drift"
	b = tmp_path / "b.drift"
	a.write_text(
		"""
module main;

import b as b;

fn main() nothrow -> Int {
	return b.answer();
}
""".lstrip(),
		encoding="utf-8",
	)
	b.write_text(
		"""
module b;

pub fn answer() nothrow -> Int {
	return 42;
}
""".lstrip(),
		encoding="utf-8",
	)
	_modules, _table, _exc, _exports, _deps, diags = parse_drift_workspace_to_hir([a], module_paths=[tmp_path])
	errors = [d for d in diags if d.severity == "error"]
	assert errors, diags
	msgs = [str(d.message) for d in errors]
	assert any("imported module 'b' not found" in m for m in msgs), msgs
	assert any("pass -M <dir> and compile all module files" in m for m in msgs), msgs
