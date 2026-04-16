# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_lambda_capture_duration_no_ssa_load_before_store(tmp_path: Path, capsys) -> None:
	source = """
module m_main;

import std.concurrent as conc;
import std.core as core;
import std.err;


fn use_timeout(t: conc.Duration) nothrow -> Int {
	return t.millis;
}

fn run_main() -> Int {
	val io_t = conc.Duration(millis = 5);
	var t = conc.spawn_cb(core.callback0(| | captures(copy io_t) nothrow => {
		return use_timeout(io_t);
	}));
	return t.join().on_error(|_e| => { throw std.err:ResultError(dv = 1); });
}

pub fn main() nothrow -> Int {
	return try run_main() catch { 2 };
}
"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	rc = driftc_main(["-M", str(root), str(main_path), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	assert rc == 0, payload
