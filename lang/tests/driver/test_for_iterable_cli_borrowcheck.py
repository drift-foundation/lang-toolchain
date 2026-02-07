# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_owned_for_consumes_array_in_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	source = """
module m_main

import std.containers;

fn main() nothrow -> Int {
	var xs = [1, 2, 3];
	for x in move xs { x; }
	xs.push(4);
return 0;
}
"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	rc, payload = _run_driftc_json(
		["-M", str(root), "--stdlib-root", str(stdlib_root()), "--dev", str(main_path)],
		capsys,
	)
	assert rc != 0
	codes = [d.get("code") for d in payload.get("diagnostics", [])]
	assert "E_USE_AFTER_MOVE" in codes
