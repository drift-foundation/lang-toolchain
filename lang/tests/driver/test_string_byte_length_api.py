# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	root = stdlib_root()
	args = list(argv)
	if root:
		args += ["--stdlib-root", str(root)]
	args += ["--dev"]
	args += ["--json"]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_global_byte_length_is_rejected_outside_std(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

fn main() nothrow -> Int {
	val n = byte_length("abc");
	return n;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("global byte_length(...) is not exposed; use s.byte_length()" in str(d.get("message", "")) for d in diags)
