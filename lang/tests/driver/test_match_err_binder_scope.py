# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_match_err_binder_scope_ok(tmp_path: Path, capsys) -> None:
	source = """
module m_main;

import std.core as core;

fn main() nothrow -> Int {
	val r: core.Result<Int, Int> = core.Result::Err(7);
	match r {
		Ok(v) => { return v; },
		Err(err) => { return err; },
		default => { return 9; }
	}
}
"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	rc = driftc_main(["-M", str(root), str(main_path), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	assert rc == 0, payload
