# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from pathlib import Path

from lang2.driftc.parser import parse_drift_to_hir


def test_fstring_unbalanced_brace_reports_diagnostic(tmp_path: Path) -> None:
	src = tmp_path / "bad_unbalanced.drift"
	src.write_text(
		"""
module m

fn main() returns Int {
	println(f"{")
	return 0
}
"""
	)
	_func_hirs, _sigs, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics
	assert any("E-FSTR-UNBALANCED-BRACE" in d.message for d in diagnostics)


def test_fstring_empty_hole_reports_diagnostic(tmp_path: Path) -> None:
	src = tmp_path / "bad_empty_hole.drift"
	src.write_text(
		"""
module m

fn main() returns Int {
	println(f"{}")
	return 0
}
"""
	)
	_func_hirs, _sigs, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics
	assert any("E-FSTR-EMPTY-HOLE" in d.message for d in diagnostics)


def test_fstring_nested_braces_in_spec_reports_diagnostic(tmp_path: Path) -> None:
	src = tmp_path / "bad_nested_spec.drift"
	src.write_text(
		"""
module m

fn main() returns Int {
	println(f"{1:{x}}")
	return 0
}
"""
	)
	_func_hirs, _sigs, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics
	assert any("E-FSTR-NESTED" in d.message for d in diagnostics)

