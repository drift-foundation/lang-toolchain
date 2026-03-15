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


def _compile_single_module(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str
) -> tuple[int, dict]:
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	argv = ["-M", str(root), str(main_path)]
	return _run_driftc_json(argv, capsys)


def test_lambda_declared_nothrow_but_throws_reports_diag(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

exception Boom()

fn main() nothrow -> Int{
	val f: Fn() nothrow -> Int = | | nothrow => { throw Boom(); };
	return 0;
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any(
		"lambda is declared nothrow but may throw" in m
		or "lambda can throw but is expected to be nothrow" in m
		or "initializer type 'Unknown' does not match declared type 'fn'" in m
		for m in msgs
	)


def test_lambda_can_throw_rejected_for_nothrow_param(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

exception Boom()

fn takes(f: Fn() nothrow -> Int) nothrow -> Int {
	return f();
}

fn main() nothrow -> Int{
	val f: Fn() nothrow -> Int = | | => { throw Boom(); };
	return takes(f);
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any(
		"lambda can throw but is expected to be nothrow" in m
		or "no matching overload for function 'takes'" in m
		for m in msgs
	)
