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


def test_rawbuffer_intrinsics_restricted_to_trusted_modules(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "std" / "mem" / "mem.drift",
		"""
module std.mem

export { RawBuffer, alloc_uninit, dealloc, ptr_at_ref, ptr_at_mut, write, read };

pub struct RawBuffer<T> { }

@intrinsic pub unsafe fn alloc_uninit<T>(cap: Int) -> RawBuffer<T>;
@intrinsic pub unsafe fn dealloc<T>(buf: RawBuffer<T>) -> Void;
@intrinsic pub unsafe fn ptr_at_ref<T>(buf: &RawBuffer<T>, i: Int) -> &T;
@intrinsic pub unsafe fn ptr_at_mut<T>(buf: &mut RawBuffer<T>, i: Int) -> &mut T;
@intrinsic pub unsafe fn write<T>(buf: &mut RawBuffer<T>, i: Int, v: T) -> Void;
@intrinsic pub unsafe fn read<T>(buf: &mut RawBuffer<T>, i: Int) -> T;
""".lstrip(),
	)
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.mem as mem;

fn main() nothrow -> Int {
	var buf = mem.alloc_uninit<type Int>(4);
	mem.dealloc<type Int>(buf);
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["--dev", "-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("restricted to toolchain-trusted modules" in d.get("message", "") for d in diags)
