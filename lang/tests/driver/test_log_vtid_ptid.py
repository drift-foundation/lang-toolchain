# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: log output identity fields.

- `vtid` is always emitted (the VT scheduling unit that corresponds
  to a request / fiber / scoped piece of work).
- `ptid` is opt-in via `LoggerConfigBuilder.include_ptid(true)` —
  off by default since the OS thread is shared across many VTs and
  is not meaningful at app granularity.  Opt in for runtime /
  scheduler debugging.
- Legacy `tid` is never emitted.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_DEFAULT_SOURCE = """\
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

_PTID_OPT_IN_SOURCE = """\
module main;
import std.core as core;
import std.log as log;

pub fn main() nothrow -> Int {
\tvar cfgb = log.config_builder();
\tcfgb.sink(log.stderr_sink());
\tcfgb.min_level(log.Level::Info());
\tcfgb.formatter(log.FormatterKind::JsonIso8601());
\tcfgb.include_ptid(true);
\tval logger = log.create_logger("test", cfgb.build());
\tlogger.info("ev");
\treturn 0;
}
"""


def _compile_and_run(tmp_path: Path, source: str) -> dict:
	src = tmp_path / "main.drift"
	src.write_text(source)
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


def test_vtid_present_by_default(tmp_path: Path) -> None:
	record = _compile_and_run(tmp_path, _DEFAULT_SOURCE)
	assert "vtid" in record, "log record must contain vtid"
	assert isinstance(record["vtid"], int)
	assert record["vtid"] > 0, "vtid on a VT must be > 0"


def test_ptid_absent_by_default(tmp_path: Path) -> None:
	"""Under VT scheduling the OS thread is shared across many
	virtual threads, so `ptid` is omitted from the default
	JsonIso8601 output to keep records uncluttered at app
	granularity.  Opt in via `LoggerConfigBuilder.include_ptid(true)`.
	"""
	record = _compile_and_run(tmp_path, _DEFAULT_SOURCE)
	assert "ptid" not in record, (
		"ptid must NOT appear in default output (opt in via include_ptid(true)); "
		f"got: {record}"
	)


def test_ptid_present_when_opted_in(tmp_path: Path) -> None:
	"""Runtime / scheduler debugging contexts where the OS thread
	identity is the quantity of interest can re-enable `ptid` via
	`LoggerConfigBuilder.include_ptid(true)`.
	"""
	record = _compile_and_run(tmp_path, _PTID_OPT_IN_SOURCE)
	assert "ptid" in record, "include_ptid(true) must surface ptid"
	assert isinstance(record["ptid"], int)
	assert record["ptid"] != 0, "ptid must be a valid thread id"


def test_no_legacy_tid(tmp_path: Path) -> None:
	record = _compile_and_run(tmp_path, _DEFAULT_SOURCE)
	assert "tid" not in record, "log record must not contain legacy tid"