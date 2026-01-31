# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	cmd = ["./.venv/bin/python3", "-m", "lang2.driftc.driftc", *argv, "--json"]
	res = subprocess.run(cmd, cwd=Path(__file__).parents[3], capture_output=True, text=True)
	out = res.stdout.strip() or "{}"
	payload = json.loads(out)
	_ = capsys.readouterr()
	return res.returncode, payload


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_emit_ir_while_capture_move_in_loop_no_ssa_crash(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.concurrent as conc;

exception Oops()

struct Stream { v: Int }

fn accept() nothrow -> Stream { return Stream(v = 1); }

fn handle(var s: Stream) -> Int {
	if s.v == 0 {
		throw Oops();
	}
	return s.v;
}

fn main() nothrow -> Int {
	while true {
		val s = accept();
		val _ = conc.spawn_cb(| | captures(move s) => {
			return try handle(move s) catch { 1 };
		});
		return 0;
	}
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(
		["-M", str(mod_root), *map(str, paths), "--emit-ir", str(tmp_path / "out.ll")], capsys
	)
	assert rc == 0, payload
