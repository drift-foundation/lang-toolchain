# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root
from lang.tests.driver.driver_cli_helpers import with_target_word_bits


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	if "--dev" not in argv:
		argv = [*argv, "--dev"]
	rc = driftc_main(with_target_word_bits(argv + ["--json"]))
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_driftc_guard_accepts_workspace_module_packages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main;",
				"fn main() nothrow -> Int {",
				"	return 1;",
				"}",
				"",
			]
		)
	)
	argv = ["-M", str(tmp_path), str(src), "--stdlib-root", str(stdlib_root()), "--json"]
	rc, payload = _run_driftc_json(argv, capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
