# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_to_hir


def _parse_with_diagnostics(src: str, tmp_path: Path):
	path = tmp_path / "terminators.drift"
	path.write_text(src)
	_module, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(path)
	return diagnostics


def test_missing_semicolon_between_statements_reports_diagnostic(tmp_path: Path) -> None:
	diagnostics = _parse_with_diagnostics(
		"""
fn main() -> Int {
	val x = 1
	val y = 2;
	return y;
}
""",
		tmp_path,
	)
	assert diagnostics


def test_missing_semicolon_after_return_reports_diagnostic(tmp_path: Path) -> None:
	diagnostics = _parse_with_diagnostics(
		"""
fn main() -> Int {
	return 0
}
""",
		tmp_path,
	)
	assert diagnostics
