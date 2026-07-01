# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_match_stmt_all_arms_return_ok(tmp_path: Path, capsys) -> None:
	source = """
module m_main;

import std.core as core;

pub fn main() nothrow -> Int {
	val w: core.Result<Int, Int> = core.Result::Ok(1);
	match w {
		Ok(v) => {
			val t = v + 1;
			if t == 0 { return 1; }
		},
		Err(_) => { return 2; },
		default => { return 3; }
	}
	val r: core.Result<Int, Int> = core.Result::Err(7);
	match r {
		Ok(v2) => {
			if v2 == 0 { return 4; }
			return v2;
		},
		Err(e2) => { return e2; },
		default => { return 5; }
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


def test_match_stmt_nonreturning_arm_then_return_ok(tmp_path: Path, capsys) -> None:
	source = """
module m_main;

import std.core as core;

pub fn main() nothrow -> Int {
	val w: core.Result<Int, Int> = core.Result::Ok(1);
	match w {
		Ok(v) => {
			val t = v + 1;
			if t == 0 { return 1; }
		},
		Err(_) => { return 2; },
		default => { return 3; }
	}
	val r: core.Result<Int, Int> = core.Result::Err(7);
	match r {
		Ok(v2) => { return v2; },
		Err(e2) => { return e2; },
		default => { return 5; }
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


@pytest.mark.heavy
def test_match_stmt_fallthrough_after_match_ok(tmp_path: Path, capsys) -> None:
	source = """
module m_main;

import std.core as core;

pub fn main() nothrow -> Int {
	val w: core.Result<Int, Int> = core.Result::Ok(1);
	match w {
		Ok(v) => {
			val t = v + 1;
			if t == 0 { return 1; }
		},
		Err(_) => { return 2; },
		default => { return 3; }
	}
	val r: core.Result<Int, Int> = core.Result::Err(7);
	match r {
		Ok(v2) => { return v2; },
		Err(e2) => { return e2; },
		default => { return 5; }
	}
	return 0;
}
"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	rc = driftc_main(["-M", str(root), str(main_path), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	assert rc == 0, payload


def test_match_stmt_nested_match_in_arm_no_return_ok(tmp_path: Path, capsys) -> None:
	source = """
module m_main;

import std.core as core;

pub fn main() nothrow -> Int {
	val w: core.Result<Int, Int> = core.Result::Ok(1);
	match w {
		Ok(v) => {
			val r: core.Result<Int, Int> = core.Result::Ok(v + 1);
			val k = match r {
				Ok(n) => { n },
				default => { 9 }
			};
			if k != 2 { return 7; }
		},
		Err(_) => { return 2; },
		default => { return 3; }
	}
	val r2: core.Result<Int, Int> = core.Result::Err(7);
	match r2 {
		Ok(v2) => { return v2; },
		Err(e2) => { return e2; },
		default => { return 5; }
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
