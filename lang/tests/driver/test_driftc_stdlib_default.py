# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.tests.driver.driver_cli_helpers import with_target_word_bits


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(with_target_word_bits(argv + ["--json"]))
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_driftc_uses_default_stdlib_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main;",
				"import std.core;",
				"pub fn main() nothrow -> Int {",
				"	return 0;",
				"}",
				"",
			]
		)
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_driftc_rejects_reserved_namespace_outside_stdlib_root(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	src = tmp_path / "std_fake.drift"
	src.write_text(
		"\n".join(
			[
				"module std.fake;",
				"import std.core;",
				"pub fn main() nothrow -> Int {",
				"	return 0;",
				"}",
				"",
			]
		)
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc != 0
	assert any(
		"reserved module namespace 'std.fake' requires toolchain trust" in d.get("message", "")
		for d in payload.get("diagnostics", [])
	)
