# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Heap-backed carriers for TLR-7 cross-block last-use releases
(TLR-7-DESIGN.md: fn-wide producer resolution; the release lands in the
DRAIN block, which may differ from the producer block).

The measured production shape (7,392 of 7,398): a StringConcat temp
produced in one block of a LOOP and drained in a later block of the
SAME iteration — multi-block loop bodies created by bounds-checked
array reads splitting the concat chain.  This row drives exactly that:
`"row-" + fmt.format_int(i) + ": " + names[i] + ";"` — the early concat
temps are produced before the `names[i]` bounds-check blocks and
drained by the next concat AFTER the join, cross-block, inside the
loop, fresh each iteration.

Runtime-built strings throughout: MISSING RELEASE (a cross-block drain
the pass failed to place) → definitely-lost; DOUBLE RELEASE (drain-block
recognition failed to suppress) → Invalid read/free.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

TLR7_SOURCE = """\
module m;

import std.format as fmt;

fn build_names(n: Int) nothrow -> Array<String> {
	var xs: Array<String> = [];
	var i = 0;
	while i < n {
		xs.push("name-" + fmt.format_int(i + 100));
		i = i + 1;
	}
	return move xs;
}

// The measured TLR-7 shape: concat temps produced BEFORE the bounds-
// checked names[i] read (which splits the block) and drained by the
// next concat AFTER the join — cross-block, intra-loop, per-iteration.
fn render(names: &Array<String>, n: Int) nothrow -> Int {
	var total = 0;
	var i = 0;
	while i < n {
		val row = "row-" + fmt.format_int(i) + ": " + names[i] + ";";
		total = total + row.byte_length();
		// A second chain with a conditional inside the body — more
		// intra-loop block boundaries for the temps to cross.
		if i % 2 == 0 {
			val alt = "even-" + names[i] + "!";
			total = total + alt.byte_length();
		}
		i = i + 1;
	}
	return total;
}

pub fn main() nothrow -> Int {
	val names = build_names(9);
	var acc = 0;
	var round = 0;
	while round < 3 {
		acc = acc + render(names, 9);
		round = round + 1;
	}
	if acc > 0 { return 0; }
	return 1;
}
"""


def test_crossblock_lastuse_release_balanced(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(TLR7_SOURCE)
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
		f"cross-block concat temp (drain-block recognition failure) "
		f"reads as Invalid read/free.\n{vg_output[-1500:]}"
	)
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert lost == 0, (
		f"{lost} bytes definitely lost — MISSING RELEASE: a cross-block "
		f"drain the pass failed to place (per-iteration concat temps in "
		f"multi-block loop bodies).\n{vg_output[-1500:]}"
	)
	for bad in ("Invalid read", "Invalid write", "Invalid free"):
		assert bad not in vg_output, f"{bad} under valgrind:\n{vg_output[-1500:]}"
