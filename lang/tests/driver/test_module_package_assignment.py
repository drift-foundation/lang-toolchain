# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_workspace_module_named_std_is_not_stdlib(tmp_path: Path) -> None:
	src = tmp_path / "std.fake.drift"
	src.write_text(
		"\n".join(
			[
				"module std.fake",
				"fn ping() nothrow -> Int {",
				"	return 1;",
				"}",
				"",
			]
		)
	)

	modules, type_table, _exc, _exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		[src],
		stdlib_root=stdlib_root(),
		package_id="app",
	)

	assert diagnostics == []
	assert "std.fake" in modules
	assert type_table.module_packages.get("std.fake") == "app"
