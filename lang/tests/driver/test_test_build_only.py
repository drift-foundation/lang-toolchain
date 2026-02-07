from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir


def _parse(tmp_path: Path, *, test_build_only: bool) -> dict[str, object]:
	source = tmp_path / "main.drift"
	source.write_text(
		"""
module m_main

@test_build_only
fn __test_helper() -> Int { return 1; }

fn main() -> Int { return 0; }
"""
	)
	modules, _type_table, _exc, _exports, _deps, diags = parse_drift_workspace_to_hir(
		[source],
		test_build_only=test_build_only,
	)
	assert diags == []
	return modules


def test_test_build_only_excluded_by_default(tmp_path: Path) -> None:
	modules = _parse(tmp_path, test_build_only=False)
	mod = modules["m_main"]
	names = {fn_id.name for fn_id in mod.signatures_by_id.keys()}
	assert "__test_helper" not in names


def test_test_build_only_included_when_enabled(tmp_path: Path) -> None:
	modules = _parse(tmp_path, test_build_only=True)
	mod = modules["m_main"]
	names = {fn_id.name for fn_id in mod.signatures_by_id.keys()}
	assert "__test_helper" in names
