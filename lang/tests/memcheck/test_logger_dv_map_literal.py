# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: logger.info with DiagnosticValue::String(heap_string) in a
map literal must not crash (use-after-free) or leak.

0.27.146 overcorrected by releasing the caller's string after
drift_dv_string retained it, causing a double-free when HashMap cleanup
also released the string.  0.27.147 uses drift_dv_string_move (ownership
transfer, no retain) so only the DV holds a reference.

This test pins:
1. No crash (the 0.27.146 UAF shape)
2. No leak (the original 0.27.145 residual)
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

SOURCE = """\
module main;

import std.core as core;
import std.format as fmt;
import std.log as log;

pub fn main() nothrow -> Int {
\tvar cfgb = log.config_builder();
\tcfgb.sink(log.stderr_sink());
\tcfgb.min_level(log.Level::Error());
\tval logger = log.create_logger("test", cfgb.build());
\tval _ = logger.info("event", {
\t\t"port": DiagnosticValue::String(fmt.format_int(8080))
\t});
\tval _ = logger.info("event", {
\t\t"port": DiagnosticValue::String(fmt.format_int(8081))
\t});
\tval _ = logger.info("event", {
\t\t"port": DiagnosticValue::String(fmt.format_int(8082))
\t});
\treturn 0;
}
"""


def test_logger_dv_map_literal_no_crash_no_leak(tmp_path: Path) -> None:
	"""Logger with DV::String(heap_string) in map literal must not crash or leak."""
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
	out_bin = tmp_path / "test_bin"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:300]}"

	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		["valgrind", "--tool=memcheck", "--leak-check=full",
		 "--show-leak-kinds=definite,indirect",
		 "--errors-for-leak-kinds=definite,indirect",
		 "--error-exitcode=97",
		 f"--log-file={vg_log}",
		 str(out_bin)],
		capture_output=True, text=True, timeout=120,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""

	# Check for UAF / invalid reads (the 0.27.146 crash shape)
	invalid_reads = len(re.findall(r"Invalid read", vg_output))
	assert invalid_reads == 0, (
		f"Valgrind detected {invalid_reads} invalid reads — "
		f"use-after-free in DV string ownership.\n"
		f"valgrind log:\n{vg_output[-500:]}"
	)

	# Check for leaks (the original 0.27.145 residual)
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert vg.returncode != 97, (
		f"Valgrind detected errors.\n"
		f"definitely lost: {definitely_lost} bytes\n"
		f"valgrind log:\n{vg_output[-500:]}"
	)
	assert definitely_lost == 0, f"definitely lost: {definitely_lost} bytes"
