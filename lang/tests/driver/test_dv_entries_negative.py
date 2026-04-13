# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Negative tests for DiagnosticValue.len() / .entries() checker validation
and DiagnosticEntry shadowing regression.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "m::main",
		 "-o", str(tmp_path / "out"),
		 "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=60,
	)


def test_dv_len_rejects_positional_arg(tmp_path: Path) -> None:
	res = _compile(tmp_path, """\
module m;
pub fn main() nothrow -> Int {
	val dv = DiagnosticValue::Int(1);
	return dv.len(42);
}
""")
	assert res.returncode != 0
	assert "DiagnosticValue.len takes no arguments" in res.stderr


def test_dv_entries_rejects_positional_arg(tmp_path: Path) -> None:
	res = _compile(tmp_path, """\
module m;
pub fn main() nothrow -> Int {
	val dv = DiagnosticValue::Int(1);
	val _ = dv.entries(42);
	return 0;
}
""")
	assert res.returncode != 0
	assert "DiagnosticValue.entries takes no arguments" in res.stderr


def test_dv_len_rejects_kwargs(tmp_path: Path) -> None:
	res = _compile(tmp_path, """\
module m;
pub fn main() nothrow -> Int {
	val dv = DiagnosticValue::Int(1);
	return dv.len(x = 1);
}
""")
	assert res.returncode != 0
	assert "DiagnosticValue.len takes no keyword arguments" in res.stderr


def test_dv_entries_rejects_kwargs(tmp_path: Path) -> None:
	res = _compile(tmp_path, """\
module m;
pub fn main() nothrow -> Int {
	val dv = DiagnosticValue::Int(1);
	val _ = dv.entries(x = 1);
	return 0;
}
""")
	assert res.returncode != 0
	assert "DiagnosticValue.entries takes no keyword arguments" in res.stderr


def test_user_defined_diagnostic_entry_does_not_shadow(tmp_path: Path) -> None:
	"""User code defining its own DiagnosticEntry struct must not affect
	the return type of DiagnosticValue.entries(), which always returns
	Array<std.core:DiagnosticEntry>."""
	res = _compile(tmp_path, """\
module m;

import std.core as core;

pub struct DiagnosticEntry {
	pub x: Int,
}

fn run() -> Int {
	val obj = DiagnosticValue::Object([
		core.diagnostic_entry("k", DiagnosticValue::Int(1))
	]);
	val entries = obj.entries();
	val e = entries[0];
	if e.key != "k" {
		return 1;
	}
	return 0;
}

pub fn main() nothrow -> Int {
	return try run() catch { 99 };
}
""")
	assert res.returncode == 0, f"entries() should resolve std.core:DiagnosticEntry even with user shadow:\n{res.stderr[-500:]}"
