# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: Logger.debug/info/error no-attrs overloads must compile,
run, and emit structured JSON with empty attrs.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

_SOURCE = """\
module main;
import std.core as core;
import std.log as log;

pub fn main() nothrow -> Int {
\tvar cfgb = log.config_builder();
\tcfgb.sink(log.stderr_sink());
\tcfgb.min_level(log.Level::Debug());
\tval logger = log.create_logger("test", cfgb.build());
\tval _ = logger.debug("debug-ev");
\tval _ = logger.info("info-ev");
\tval _ = logger.error("error-ev");
\tval _ = logger.info("with-attrs", {"k": "v"});
\treturn 0;
}
"""


def _compile_and_run(tmp_path: Path) -> tuple[int, str, str]:
	src = tmp_path / "main.drift"
	src.write_text(_SOURCE)
	out = tmp_path / "test_bin"
	rc = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=60,
	)
	assert rc.returncode == 0, f"compile failed: {rc.stderr[:300]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	return run.returncode, run.stdout, run.stderr


def test_debug_no_attrs(tmp_path: Path) -> None:
	rc, _, stderr = _compile_and_run(tmp_path)
	assert rc == 0
	lines = [json.loads(l) for l in stderr.strip().splitlines()]
	debug_lines = [l for l in lines if l.get("ev") == "debug-ev"]
	assert len(debug_lines) == 1
	assert debug_lines[0]["level"] == "debug"
	assert debug_lines[0]["attrs"] == {}


def test_info_no_attrs(tmp_path: Path) -> None:
	rc, _, stderr = _compile_and_run(tmp_path)
	assert rc == 0
	lines = [json.loads(l) for l in stderr.strip().splitlines()]
	info_lines = [l for l in lines if l.get("ev") == "info-ev"]
	assert len(info_lines) == 1
	assert info_lines[0]["level"] == "info"
	assert info_lines[0]["attrs"] == {}


def test_error_no_attrs(tmp_path: Path) -> None:
	rc, _, stderr = _compile_and_run(tmp_path)
	assert rc == 0
	lines = [json.loads(l) for l in stderr.strip().splitlines()]
	error_lines = [l for l in lines if l.get("ev") == "error-ev"]
	assert len(error_lines) == 1
	assert error_lines[0]["level"] == "error"
	assert error_lines[0]["attrs"] == {}


def test_attrs_overload_unchanged(tmp_path: Path) -> None:
	rc, _, stderr = _compile_and_run(tmp_path)
	assert rc == 0
	lines = [json.loads(l) for l in stderr.strip().splitlines()]
	attrs_lines = [l for l in lines if l.get("ev") == "with-attrs"]
	assert len(attrs_lines) == 1
	assert attrs_lines[0]["attrs"] == {"k": "v"}
