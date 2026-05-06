# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: log-attr value ownership through Debuggable.

Slice 7a (0.31.62, 2026-05-05): the original probe exercised
`Debuggable::to_debug` for `DiagnosticValue` (`return *self` lowering
to LoadRef) — that DV public surface is gone.  The equivalent
ownership contract today is `Debuggable::to_debug_json_text` on
String-valued attrs: the typed `Logger.info(ev, attrs: HashMap<String,
String>)` form consumes the attrs map and projects each value through
`Debuggable`; the projected JSON text and the original input must
each release exactly once across multiple emit calls.

This test pins:
1. No crash (double-free / UAF from aliased ownership)
2. No leak (each formatted value released exactly once)
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
\tval _ = logger.info("ev", {
\t\t"port": fmt.format_int(8080)
\t});
\tval _ = logger.info("ev", {
\t\t"port": fmt.format_int(8081)
\t});
\tval _ = logger.info("ev", {
\t\t"port": fmt.format_int(8082)
\t});
\treturn 0;
}
"""


def test_dv_deref_no_crash_no_leak(tmp_path: Path) -> None:
	"""Log-attr String value through `Debuggable::to_debug_json_text`
	must not double-free or leak across multiple emits."""
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

	invalid_reads = len(re.findall(r"Invalid read", vg_output))
	assert invalid_reads == 0, (
		f"Valgrind detected {invalid_reads} invalid reads — "
		f"deref-of-borrow regression (historical: aliased deref of "
		f"&DiagnosticValue without clone; the DV substrate is now "
		f"retired but the deref-of-borrow ownership invariant still "
		f"applies to other reference types).\n"
		f"valgrind log:\n{vg_output[-500:]}"
	)

	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert vg.returncode != 97, (
		f"Valgrind detected errors.\n"
		f"definitely lost: {definitely_lost} bytes\n"
		f"valgrind log:\n{vg_output[-500:]}"
	)
	assert definitely_lost == 0, f"definitely lost: {definitely_lost} bytes"
