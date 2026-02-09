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


def test_std_parse_bool_api_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.parse as parse;

fn main() nothrow -> Int {
	val a = parse.parse_bool("true");
	val b = parse.parse_bool("FALSE");
	if a and (not b) {
		return 0;
	}
	return 1;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_std_parse_numeric_api_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.parse as parse;
import std.core as core;

fn main() nothrow -> Int {
	val ri = parse.parse_int("42");
	val ru = parse.parse_uint("7");
	val rf = parse.parse_float("3.5");
	match ri {
		core.Result::Ok(v) => {
			if v != 42 { return 1; }
		},
		default => { return 2; }
	}
	match ru {
		core.Result::Ok(v) => {
			if v != cast<Uint>(7) { return 3; }
		},
		default => { return 4; }
	}
	match rf {
		core.Result::Ok(v) => {
			if v != 3.5 { return 5; }
		},
		default => { return 6; }
	}
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
