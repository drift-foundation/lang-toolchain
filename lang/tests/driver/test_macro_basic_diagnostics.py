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


def _compile_single_module(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, dict]:
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	argv = ["-M", str(root), str(main_path)]
	return _run_driftc_json(argv, capsys)


def _diag_messages(payload: dict) -> list[str]:
	return [d.get("message", "") for d in payload.get("diagnostics", [])]


def test_macro_unknown_path_reports_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	source = """
module m_main;

import std.log as log;

fn main() nothrow -> Int {
	var b = log.config_builder();
	val logger = log.create_logger("main", b.build());
	val _ = log.warn!(logger, "ev", {:});
	return 0;
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	assert any("unknown macro 'warn!'" in m for m in _diag_messages(payload))


def test_macro_wrong_arity_reports_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	source = """
module m_main;

import std.log as log;

fn main() nothrow -> Int {
	var b = log.config_builder();
	val logger = log.create_logger("main", b.build());
	log.info!(logger);
	return 0;
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	assert any("expects 2-4 positional args" in m for m in _diag_messages(payload))


def test_macro_kwargs_rejected_reports_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	source = """
module m_main;

import std.log as log;

fn main() nothrow -> Int {
	var b = log.config_builder();
	val logger = log.create_logger("main", b.build());
	log.info!(logger, "ev", attrs = {:});
	return 0;
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	assert any("do not support keyword arguments" in m for m in _diag_messages(payload))
