# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: log output must contain vtid and ptid, not legacy tid."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_SOURCE = """\
module main;
import std.core as core;
import std.log as log;

pub fn main() nothrow -> Int {
\tvar cfgb = log.config_builder();
\tcfgb.sink(log.stderr_sink());
\tcfgb.min_level(log.Level::Info());
\tcfgb.formatter(log.FormatterKind::JsonIso8601());
\tval logger = log.create_logger("test", cfgb.build());
\tlogger.info("ev");
\treturn 0;
}
"""


def _compile_and_run(tmp_path: Path) -> dict:
	src = tmp_path / "main.drift"
	src.write_text(_SOURCE)
	out = tmp_path / "test_bin"
	rc = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert rc.returncode == 0, f"compile failed: {rc.stderr[:300]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 0
	lines = [json.loads(l) for l in run.stderr.strip().splitlines()]
	assert len(lines) >= 1
	return lines[0]


def test_vtid_present(tmp_path: Path) -> None:
	record = _compile_and_run(tmp_path)
	assert "vtid" in record, "log record must contain vtid"
	assert isinstance(record["vtid"], int)
	assert record["vtid"] > 0, "vtid on a VT must be > 0"


def test_ptid_present(tmp_path: Path) -> None:
	record = _compile_and_run(tmp_path)
	assert "ptid" in record, "log record must contain ptid"
	assert isinstance(record["ptid"], int)
	assert record["ptid"] != 0, "ptid must be a valid thread id"


def test_no_legacy_tid(tmp_path: Path) -> None:
	record = _compile_and_run(tmp_path)
	assert "tid" not in record, "log record must not contain legacy tid"