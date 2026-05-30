# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: log output identity fields.

- `vtid` is always emitted (the VT scheduling unit that corresponds
  to a request / fiber / scoped piece of work).  Same value the liveness
  interrogator reports as `vtid`.
- `tid` (OS kernel thread id, `gettid()`) is opt-in via
  `LoggerConfigBuilder.include_tid(true)` — off by default since the carrier
  thread is shared across many VTs and is not meaningful at app granularity.
  When present it is the same kernel TID liveness reports as `carrier_tid`
  (and that top/ps/proc/perf/strace use), NOT a pthread handle.
- The legacy `ptid` (pthread_self() cast) field is fully gone.
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

_TID_OPT_IN_SOURCE = """\
module main;
import std.core as core;
import std.log as log;

pub fn main() nothrow -> Int {
\tvar cfgb = log.config_builder();
\tcfgb.sink(log.stderr_sink());
\tcfgb.min_level(log.Level::Info());
\tcfgb.formatter(log.FormatterKind::JsonIso8601());
\tcfgb.include_tid(true);
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


def test_tid_absent_by_default(tmp_path: Path) -> None:
	"""Under VT scheduling the carrier thread is shared across many
	virtual threads, so `tid` is omitted from the default JsonIso8601
	output to keep records uncluttered at app granularity.  Opt in via
	`LoggerConfigBuilder.include_tid(true)`.
	"""
	record = _compile_and_run(tmp_path, _DEFAULT_SOURCE)
	assert "tid" not in record, (
		"tid must NOT appear in default output (opt in via include_tid(true)); "
		f"got: {record}"
	)


def test_tid_present_when_opted_in(tmp_path: Path) -> None:
	"""Runtime / scheduler debugging contexts can surface `tid` via
	`LoggerConfigBuilder.include_tid(true)`.  It must be the OS kernel TID
	(gettid()), not a pthread handle — i.e. a small positive integer that
	lines up with top/ps/proc/perf and with liveness `carrier_tid`.
	"""
	record = _compile_and_run(tmp_path, _TID_OPT_IN_SOURCE)
	assert "tid" in record, "include_tid(true) must surface tid"
	assert isinstance(record["tid"], int)
	# Kernel TIDs are bounded by /proc/sys/kernel/pid_max (default 2**22);
	# a pthread_self() handle is a pointer-sized value (~2**47).  This bound
	# pins that we report the kernel TID, not the old pthread value.
	assert 0 < record["tid"] < (1 << 31), (
		f"tid must be a kernel TID (gettid), got {record['tid']!r} "
		"(looks like a pthread handle?)"
	)


def test_no_legacy_ptid(tmp_path: Path) -> None:
	"""The old pthread-based `ptid` field is fully removed; it must never
	appear, even when the kernel `tid` is opted in."""
	record = _compile_and_run(tmp_path, _TID_OPT_IN_SOURCE)
	assert "ptid" not in record, f"legacy ptid must not appear: {record}"
