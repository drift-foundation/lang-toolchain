# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Heap-backed carriers for the TLR-4 Call-family last-use releases
(TLR-4-DESIGN.md: non-throw String-returning call results join
`is_materialized_release_family_producer`; the string_releases pass owns
their last-use releases).

Every string is RUNTIME-BUILT (format_int concat) so both failure
directions are valgrind-visible:
- MISSING RELEASE (the pass skipped a temp string_arc used to release)
  → the call result becomes a definitely-lost block;
- DOUBLE RELEASE (recognition failed to suppress the historical in-pass
  bookkeeping) → Invalid read/free.

Rows:
1. compare_names — the family shape: non-throw call results whose last
   use is a comparison operand (generic-fallthrough USE), drained in
   the producing block.
2. probe — the REQUIRED THROWING-CALL TOPOLOGY row: a `throws` callee's
   String result flows through the FnResult envelope + hidden ok-local
   (its temps are NOT family members); the error edge is actually
   exercised (every third i throws) and unwinds through the
   try/catch-expression fallback.  Both edges must be release-balanced,
   with family releases (the `make_name` comparison operand) present in
   the same function.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

CALL_LASTUSE_SOURCE = """\
module m;

import std.format as fmt;

pub error ProbeError {
	what: String,
}

fn make_name(i: Int) nothrow -> String {
	return "name-" + fmt.format_int(i + 1000);
}

fn risky(i: Int) throws -> String {
	if i % 3 == 0 {
		throw ProbeError(what = "boom-" + fmt.format_int(i));
	}
	return "risky-" + fmt.format_int(i);
}

// Row 1: non-throw call results drained as comparison operands — the
// TLR-4 family; the materialization pass owns these releases.
fn compare_names(i: Int) nothrow -> Int {
	if make_name(i) == make_name(i + 1) { return 0; }
	return 1;
}

// Row 2: throwing topology — risky()'s result reaches `got` through
// the FnResult envelope + hidden ok-local on the ok edge; every third
// i takes the error edge into the catch fallback.  The comparison
// operand make_name(i) is a family release on BOTH edges' join.
fn probe(i: Int) nothrow -> Int {
	val got = try risky(i) catch { "fallback-" + fmt.format_int(i) };
	if got == make_name(i) { return 2; }
	if got.byte_length() > 0 { return 1; }
	return 0;
}

pub fn main() nothrow -> Int {
	var acc = 0;
	var i = 0;
	while i < 30 {
		acc = acc + compare_names(i);
		acc = acc + probe(i);
		i = i + 1;
	}
	if acc > 0 { return 0; }
	return 1;
}
"""


def test_call_result_lastuse_release_balanced(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(CALL_LASTUSE_SOURCE)
	out_bin = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "m::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:1500]}"
	vg_log = tmp_path / "valgrind.log"
	run = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	assert run.returncode == 0, (
		f"exit {run.returncode} under valgrind — a double release of a "
		f"call-result temp reads as Invalid read/free.\n{vg_output[-1500:]}"
	)
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert lost == 0, (
		f"{lost} bytes definitely lost — MISSING RELEASE: a call-result "
		f"temp the family migration was supposed to release leaked "
		f"(check the throwing-topology row's both edges).\n{vg_output[-1500:]}"
	)
	for bad in ("Invalid read", "Invalid write", "Invalid free"):
		assert bad not in vg_output, f"{bad} under valgrind:\n{vg_output[-1500:]}"
