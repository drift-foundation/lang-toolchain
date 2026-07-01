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


def test_trait_impl_method_inherits_interface_nothrow_when_omitted(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.containers as containers;
import std.core as core;
import std.core.cmp as cmp;
import std.core.hash as hash;

struct BadKey {
	id: Int,
}

implement cmp.Equatable for BadKey {
	pub fn eq(self: &BadKey, other: &BadKey) -> Bool {
		return self.id == other.id;
	}
}

implement hash.Hash<hash.DefaultHasher> for BadKey {
	pub fn hash(self: &BadKey, state: &mut hash.DefaultHasher) nothrow -> Void {
		hash.Hasher::write_i64(state, self.id);
	}
}

fn run() throws -> Int {
	var map = containers.hash_map<type BadKey, Int>();
	map.insert(BadKey(id = 1), 7);
	val probe = BadKey(id = 1);
	val got = map.get(&probe);
	return match got {
		None => { 1 },
		Some(v) => { *v }
	};
}

pub fn main() nothrow -> Int {
	return try run() catch { 2 };
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths), "--emit-ir", str(tmp_path / "out.ll")], capsys)
	assert rc == 0, payload
