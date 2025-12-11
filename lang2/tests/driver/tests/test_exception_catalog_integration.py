# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from pathlib import Path

from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.driftc.parser import parse_drift_to_hir


def _compile_source(src: str, tmp_path: Path):
	path = tmp_path / "main.drift"
	path.write_text(src)
	func_hirs, signatures, type_table, exc_catalog, diagnostics = parse_drift_to_hir(path)
	assert diagnostics == []
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc_catalog,
		type_table=type_table,
		return_checked=True,
	)
	return mir_funcs, checked


def test_catch_unknown_event_reports_diagnostic(tmp_path: Path) -> None:
	_, checked = _compile_source(
		"""
fn main() returns FnResult<Int, Error> {
    try {
        throw "boom";
    } catch Unknown(e) {
    }
    return Ok(1);
}
""",
		tmp_path,
	)
	assert checked.diagnostics
	assert any("unknown catch event" in d.message for d in checked.diagnostics)


def test_declared_exception_enables_catch(tmp_path: Path) -> None:
	mir_funcs, checked = _compile_source(
		"""
exception Boom(msg: String)

fn main() returns FnResult<Int, Error> {
    try {
        throw Boom(msg = "boom");
    } catch Boom(e) {
        return Ok(0);
    }
    return Ok(1);
}
""",
		tmp_path,
	)
	# No checker diagnostics; try lowering should succeed with event codes.
	assert checked.diagnostics == []
	assert "main" in mir_funcs
