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


def test_generic_throw_exception_string_field_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

error E { tag: String }
fn fail<T>(tag: String) -> Int {
	throw E(tag = tag);
}

fn run() -> Int {
	return try fail<type Int>("x") catch E(_e) { 0 };
}

fn main() nothrow -> Int {
	return try run() catch { 1 };
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0, payload


def test_generic_throw_exception_string_field_after_optional_ref_match_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

error E { tag: String }
fn get_opt<T>() nothrow -> Optional<&T> {
	return Optional<&T>::None();
}

fn expect<T>(tag: String) -> &T {
	match get_opt<type T>() {
		Some(v) => { return v; },
		None => { throw E(tag = tag); }
	}
}

fn run() -> Int {
	try {
		val _ = expect<type Int>("x");
		return 1;
	} catch E(_e) {
		return 0;
	}
}

fn main() nothrow -> Int {
	return try run() catch { 2 };
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0, payload
