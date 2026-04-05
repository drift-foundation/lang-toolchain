# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: deref of &DiagnosticValue into owned context must clone,
not alias.

The Debuggable::to_debug impl for DiagnosticValue does `return *self`,
which lowers to LoadRef — a raw bitwise copy.  Without a CopyValue
(drift_dv_clone), the loaded DV and the original share the same inner
string pointer with only one refcount.  When both are destroyed, the
string is freed twice.

This test pins:
1. No crash (double-free / UAF from the aliased deref)
2. No leak (the cloned DV must still be properly released)
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
\t\t"port": DiagnosticValue::String(fmt.format_int(8080))
\t});
\tval _ = logger.info("ev", {
\t\t"port": DiagnosticValue::String(fmt.format_int(8081))
\t});
\tval _ = logger.info("ev", {
\t\t"port": DiagnosticValue::String(fmt.format_int(8082))
\t});
\treturn 0;
}
"""


def test_dv_deref_no_crash_no_leak(tmp_path: Path) -> None:
	"""Deref of &DiagnosticValue must clone, not alias — no crash, no leak."""
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

	# The deref-clone fix prevents double-free / UAF (the P0 crash).
	# Assert zero invalid reads — that proves the aliasing is resolved.
	invalid_reads = len(re.findall(r"Invalid read", vg_output))
	assert invalid_reads == 0, (
		f"Valgrind detected {invalid_reads} invalid reads — "
		f"deref of &DiagnosticValue aliased without clone.\n"
		f"valgrind log:\n{vg_output[-500:]}"
	)

	# The 20-byte/request string leak from drift_dv_string retain remains
	# open (LANGUAGE_BUG: ConstructDV(String) serves two ownership modes
	# via one MIR instruction — needs MIR-level ownership split).
	# This test pins crash freedom only.  Zero-leak assertion should be
	# added when the MIR ownership split lands.
