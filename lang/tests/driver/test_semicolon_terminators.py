# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _error_codes(payload: dict) -> list[str]:
	diags = payload.get("diagnostics", []) if isinstance(payload, dict) else []
	return [d.get("code") for d in diags if isinstance(d, dict)]


def test_missing_semicolon_in_block_reports_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "missing_semicolon.drift"
	_write_file(
		src,
		"""
module main;

pub fn main() nothrow -> Int {
	val a: Int = 1
	return a;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc != 0
	assert "E_EXPECTED_SEMICOLON" in _error_codes(payload)


def test_missing_semicolon_after_return_try_catch_reports_error(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	src = tmp_path / "missing_semicolon_try.drift"
	_write_file(
		src,
		"""
module main;

	error Boom {}
	fn foo() -> Int { throw Boom(); }

pub fn main() nothrow -> Int {
	return try foo() catch { 0 }
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc != 0
	assert "E_EXPECTED_SEMICOLON" in _error_codes(payload)


def test_value_block_allows_trailing_expr_without_semicolon(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	src = tmp_path / "value_block_ok.drift"
	_write_file(
		src,
		"""
module main;

error Boom {}
import std.console as console;

fn foo() -> Int { throw Boom(); }

pub fn main() nothrow -> Int {
	val x: Int = try foo() catch { console.println("x"); 0 };
	return x;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc == 0
	diags = payload.get("diagnostics", []) if isinstance(payload, dict) else []
	assert not [d for d in diags if d.get("severity") == "error"]


def test_compound_stmt_does_not_require_semicolon(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	src = tmp_path / "compound_stmt_ok.drift"
	_write_file(
		src,
		"""
module main;

pub fn main() nothrow -> Int {
	while true {
		break;
	}
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc == 0
	diags = payload.get("diagnostics", []) if isinstance(payload, dict) else []
	assert not [d for d in diags if d.get("severity") == "error"]


def test_semicolon_after_compound_stmt_is_error(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	src = tmp_path / "compound_stmt_semicolon.drift"
	_write_file(
		src,
		"""
module main;

pub fn main() nothrow -> Int {
	while true { break; };
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc != 0
	assert "E_UNEXPECTED_SEMICOLON_AFTER_COMPOUND" in _error_codes(payload)


def test_match_expr_and_stmt_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "match_expr_and_stmt_ok.drift"
	_write_file(
		src,
		"""
module main;

variant Flag {
	On,
	Off,
}
import std.console as console;

pub fn main() nothrow -> Int {
	val f: Flag = On();
	val y: Int = match f {
		On => { 10 },
		Off => { 20 },
	};
	match f {
		On => { console.println("on"); },
		Off => { console.println("off"); },
	}
	return y;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc == 0
	diags = payload.get("diagnostics", []) if isinstance(payload, dict) else []
	assert not [d for d in diags if d.get("severity") == "error"]


def test_match_missing_comma_reports_error(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	src = tmp_path / "match_missing_comma.drift"
	_write_file(
		src,
		"""
module main;

variant Flag {
	On,
	Off,
}

pub fn main() nothrow -> Int {
	val f: Flag = On();
	val y: Int = match f {
		On => { 10 }
		Off => { 20 },
	};
	return y;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc != 0
	assert "E_EXPECTED_COMMA_BETWEEN_MATCH_ARMS" in _error_codes(payload)


def test_match_stmt_semicolon_is_error(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	src = tmp_path / "match_stmt_semicolon.drift"
	_write_file(
		src,
		"""
module main;

variant Flag {
	On,
	Off,
}
import std.console as console;

pub fn main() nothrow -> Int {
	val f: Flag = On();
	match f {
		On => { console.println("on"); },
		Off => { console.println("off"); },
	};
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc != 0
	assert "E_UNEXPECTED_SEMICOLON_AFTER_COMPOUND" in _error_codes(payload)


def test_match_stmt_value_arm_is_error(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	src = tmp_path / "match_stmt_value_arm.drift"
	_write_file(
		src,
		"""
module main;

variant Flag {
	On,
	Off,
}

pub fn main() nothrow -> Int {
	val f: Flag = On();
	match f {
		On => { 10 },
		Off => { 20 },
	}
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc != 0
	assert "E_EXPECTED_SEMICOLON" in _error_codes(payload)
