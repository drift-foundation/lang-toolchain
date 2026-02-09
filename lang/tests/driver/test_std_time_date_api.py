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


def test_std_time_date_api_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.time as time;
import std.core as core;

fn main() nothrow -> Int {
	val d = time.Date(year = 2024, month = 2, day = 29);
	val _s = time.format_iso8601_date(&d);
	val _v = time.is_valid_date(2024, 2, 29);
	val _dim = time.days_in_month(2024, 2);
	val _ly = time.is_leap_year(2024);
	match time.parse_iso8601_date("2024-02-29") {
		core.Result::Ok(_) => { },
		core.Result::Err(_) => { }
	}
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
