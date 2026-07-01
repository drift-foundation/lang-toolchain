# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	cmd = ["./.venv/bin/python3", "-m", "lang.driftc.driftc", *argv, "--json"]
	res = subprocess.run(cmd, cwd=Path(__file__).parents[3], capture_output=True, text=True)
	out = res.stdout.strip()
	if not out:
		out = "{}"
	payload = json.loads(out)
	_ = capsys.readouterr()
	return res.returncode, payload


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


@pytest.mark.heavy
def test_instantiation_preserves_callinfo_for_qualified_variant_ctor(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.concurrent as conc;
import std.core as core;

fn g<T>() nothrow -> core.Result<Int, conc.SaturationPolicy> {
	return core.Result::Err(conc.SaturationPolicy::Block());
}

pub fn main() nothrow -> Int {
	val _ = g<type Int>();
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(
		["-M", str(mod_root), *map(str, paths), "--emit-ir", str(tmp_path / "out.ll")], capsys
	)
	assert rc == 0
	diags = payload.get("diagnostics", [])
	assert not any("missing CallInfo" in d.get("message", "") for d in diags)
