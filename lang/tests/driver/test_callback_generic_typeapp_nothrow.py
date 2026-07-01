# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_callback1_accepts_generic_nothrow_typeapp(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(
		main_path,
		"""
module m_main;

import std.core as core;

fn sink<T>(x: Int) nothrow -> Void {
	return;
}

pub fn main() nothrow -> Int {
	val cb = core.callback1(sink<type Int>);
	cb.call(7);
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(root), str(main_path)], capsys)
	assert rc == 0, payload
