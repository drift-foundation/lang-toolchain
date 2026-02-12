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


def _module_src_inline() -> str:
	return f"""
module main

import lang.atomic as atomic;

struct Wrap {{
	inner: atomic.AtomicUint
}}

struct Holder<T> {{
	inner: atomic.AtomicUint
}}

struct Handle<T> {{
	raw: Uint
}}

fn make(v: Uint) nothrow -> Wrap {{
	return Wrap(inner = atomic.atomic_uint(v));
}}

fn build<T>(h: Handle<T>) nothrow -> Holder<T> {{
	return Holder<type T>(inner = make(h.raw));
}}

fn main() nothrow -> Int {{
	val h = Handle<type Int>(raw = cast<Uint>(1));
	val _ = build<type Int>(h);
	return 0;
}}
""".lstrip()


def _module_src_temp() -> str:
	return """
module main

import lang.atomic as atomic;

struct Wrap {
	inner: atomic.AtomicUint
}

struct Holder<T> {
	inner: atomic.AtomicUint
}

struct Handle<T> {
	raw: Uint
}

fn make(v: Uint) nothrow -> Wrap {
	return Wrap(inner = atomic.atomic_uint(v));
}

fn build<T>(h: Handle<T>) nothrow -> Holder<T> {
	val x = make(h.raw);
	return Holder<type T>(inner = x);
}

fn main() nothrow -> Int {
	val h = Handle<type Int>(raw = cast<Uint>(1));
	val _ = build<type Int>(h);
	return 0;
}
""".lstrip()


def test_generic_ctor_field_incompatible_inline_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(mod_root / "main" / "main.drift", _module_src_inline())
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0, payload
	diags = payload.get("diagnostics", [])
	assert any(d.get("phase") == "typecheck" for d in diags), payload


def test_generic_ctor_field_incompatible_temp_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(mod_root / "main" / "main.drift", _module_src_temp())
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0, payload
	diags = payload.get("diagnostics", [])
	assert any(d.get("phase") == "typecheck" for d in diags), payload
