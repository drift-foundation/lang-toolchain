# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	args = list(argv)
	root = stdlib_root()
	if root:
		args += ["--stdlib-root", str(root)]
	args += ["--json"]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_app_entrypoint_main_must_be_pub(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
		encoding="utf-8",
	)
	ir = tmp_path / "out.ll"
	rc, payload = _run_driftc_json([str(src), "--emit-ir", str(ir)], capsys)
	assert rc == 1, payload
	diags = payload.get("diagnostics", [])
	assert any(
		d.get("phase") == "typecheck" and "entrypoint main must be declared pub" in d.get("message", "")
		for d in diags
	), diags


def test_pub_app_entrypoint_main_still_builds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

pub fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
		encoding="utf-8",
	)
	ir = tmp_path / "out.ll"
	rc, payload = _run_driftc_json([str(src), "--emit-ir", str(ir)], capsys)
	assert rc == 0, payload
	assert payload.get("diagnostics", []) == []
	assert ir.exists()
