# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from pathlib import Path

from lang.driftc.parser import parse_drift_to_hir


def test_exception_default_module_fqn_is_main(tmp_path: Path) -> None:
	path = tmp_path / "main.drift"
	path.write_text(
		"""
error Boom {}
fn main() -> Int {
    try {
        throw Boom();
    } catch Boom(e) {
        return 0;
    }
    return 1;
}
"""
	)
	module, type_table, exc_catalog, diagnostics = parse_drift_to_hir(path)
	assert diagnostics == []
	assert "main:Boom" in exc_catalog
	assert "main:Boom" in (type_table.exception_schemas or {})
	# Ensure the default-module exception is not stored as a bare name.
	assert "Boom" not in exc_catalog
