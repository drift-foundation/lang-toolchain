# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _write_std_mem_modules(tmp_path: Path) -> list[Path]:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "std" / "mem" / "mem.drift",
		"""
module std.mem;

export { swap, replace };

@intrinsic pub fn swap<T>(a: &mut T, b: &mut T) -> Void;

@intrinsic pub fn replace<T>(ptr: &mut T, v: T) -> T;
""".lstrip(),
	)
	return sorted(mod_root.rglob("*.drift"))


def test_swap_requires_import(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

fn main() nothrow -> Int {
	var x: Int = 1;
	var y: Int = 2;
	swap(x, y);
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("swap" in d.get("message", "") for d in diags)


def test_std_mem_swap_is_intrinsic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	paths = _write_std_mem_modules(tmp_path)
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module std.mem.test;

import std.mem as mem;

	fn main() nothrow -> Int {
		var x: Int = 1;
		var y: Int = 2;
		val r = &x;
		mem.swap(&mut x, &mut y);
		return 0;
	}
""".lstrip(),
	)
	paths = sorted(set(paths + list(mod_root.rglob("*.drift"))))
	rc, payload = _run_driftc_json(["--dev", "-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("cannot write to 'x' while it is borrowed" in d.get("message", "") for d in diags)
