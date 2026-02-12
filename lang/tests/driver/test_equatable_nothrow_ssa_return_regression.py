# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	cmd = ["./.venv/bin/python3", "-m", "lang.driftc.driftc", *argv, "--json"]
	res = subprocess.run(cmd, cwd=Path(__file__).parents[3], capture_output=True, text=True)
	out = res.stdout.strip() or "{}"
	payload = json.loads(out)
	_ = capsys.readouterr()
	return res.returncode, payload


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_hash_map_compile_has_no_internal_ssa_return_mismatch_for_equatable_nothrow(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.containers as containers;
import std.core as core;
use trait core.Try;

fn main() nothrow -> Int {
	return try run_main() catch { 1 };
}

fn run_main() throws -> Int {
	var m = containers.hash_map<type Int, Int>();
	m.insert(1, 2);
	val k: Int = 1;
	val got = m.get(&k);
	return match got {
		None => { 0 },
		Some(v) => { *v }
	};
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths), "--emit-ir", str(tmp_path / "out.ll")], capsys)
	assert rc == 0, payload
	for d in payload.get("diagnostics", []):
		msg = str(d.get("message", ""))
		assert "SSA return type does not match declared signature" not in msg, payload
