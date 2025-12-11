# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from pathlib import Path

from lang2.driftc.core.event_codes import event_code
from lang2.driftc.parser import parse_drift_to_hir


def test_exception_decl_yields_event_code(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module foo.bar

exception EvtA(code: Int)

fn main() returns Int { return 0; }
"""
	)
	_, _, _type_table, exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert "EvtA" in exc_catalog
	assert exc_catalog["EvtA"] == event_code("foo.bar:EvtA")


def test_duplicate_exception_reports_diagnostic(tmp_path: Path) -> None:
	src = tmp_path / "dupe.drift"
	src.write_text(
		"""
exception Boom(msg: String)
exception Boom(code: Int)

fn main() returns Int { return 0; }
"""
	)
	_, _, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics
	assert any("duplicate exception" in d.message for d in diagnostics)
