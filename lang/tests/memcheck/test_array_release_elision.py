# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Heap-backed carriers for the Array release-elision emission slice
(SLICE3-ARRAY-MEASUREMENT.md → GO; return-boundary Array drops elided at
MUST_NOT_DROP ledger verdicts, PATH_DEPENDENT kept unconditional).

Every row uses `Array<String>` with RUNTIME-BUILT heap strings so both
failure directions are observable at the valgrind level:
- OVER-ELISION (a drop that was load-bearing got skipped) → the array
  buffer and its element strings become definitely-lost blocks;
- the double-free direction (a drop kept where ownership already
  transferred) → Invalid read/free.

Rows:
1. live-at-scope-exit — arrays never moved; their drops (authored
   cleanup and/or a MUST_DROP sweep) must still run.
2. moved-to-caller — `return move arr;` transfers ownership; the
   producer's boundary verdict is MOVED_OUT and its sweep drop is
   ELIDED; the caller consumes and drops.  No leak, no double-free.
3. conditionally-moved (PATH_DEPENDENT) — moved on one branch only; the
   unconditional null-safe sweep drop is KEPT; both branch shapes run
   clean.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

ARRAY_ELISION_SOURCE = """\
module m;

import std.format as fmt;

fn build(n: Int) nothrow -> Array<String> {
	var xs: Array<String> = [];
	var i = 0;
	while i < n {
		xs.push("row-" + fmt.format_int(i + 100));
		i = i + 1;
	}
	return move xs;
}

// Row 2: producer returns by move — its boundary verdict for `xs` is
// MOVED_OUT (Return-as-move) and the sweep drop is elided.
fn consume_moved(n: Int) nothrow -> Int {
	val xs = build(n);
	var total = 0;
	var i = 0;
	while i < xs.len {
		total = total + xs[i].byte_length();
		i = i + 1;
	}
	return total;
}

// Row 1: live at scope exit — never moved; drops must run.
fn live_at_exit(n: Int) nothrow -> Int {
	var keep = build(n);
	keep.push("extra-" + fmt.format_int(n));
	return keep.len();
}

// Row 3: conditionally moved — PATH_DEPENDENT at the exit; the
// unconditional null-safe sweep drop is KEPT and must be clean on both
// the moved and unmoved paths.
fn conditional_move(n: Int, take: Bool) nothrow -> Int {
	var xs = build(n);
	var sink: Array<String> = [];
	if take {
		sink = move xs;
	}
	return sink.len();
}

pub fn main() nothrow -> Int {
	var acc = 0;
	var i = 1;
	while i < 5 {
		acc = acc + consume_moved(i);
		acc = acc + live_at_exit(i);
		acc = acc + conditional_move(i, true);
		acc = acc + conditional_move(i, false);
		i = i + 1;
	}
	if acc > 0 { return 0; }
	return 1;
}
"""


def test_array_release_elision_rows_balanced(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(ARRAY_ELISION_SOURCE)
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
		f"exit {run.returncode} under valgrind — a kept-drop-after-move "
		f"regression reads as Invalid read/free.\n{vg_output[-1500:]}"
	)
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert lost == 0, (
		f"{lost} bytes definitely lost — OVER-ELISION: a return-boundary "
		f"Array drop that was load-bearing was skipped (elision must only "
		f"fire at MUST_NOT_DROP verdicts).\n{vg_output[-1500:]}"
	)
	for bad in ("Invalid read", "Invalid write", "Invalid free"):
		assert bad not in vg_output, f"{bad} under valgrind:\n{vg_output[-1500:]}"
