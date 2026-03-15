# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_match_ref_scrutinee_ok(tmp_path: Path, capsys) -> None:
	source = """
module m_main;

fn main() nothrow -> Int{
	val o = Optional::Some(1);
	val r = match &o {
		Some(v) => { *v },
		None => { 0 },
		default => { 0 }
	};
	return r;
}
"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	rc = driftc_main(["-M", str(root), str(main_path), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	assert rc == 0, payload
