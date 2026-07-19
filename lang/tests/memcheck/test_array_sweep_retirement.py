# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""string-arc-endgame-array-sweep — heap-backed valgrind carriers for
the two measured residual classes (checkpoint §2.1/§6 pin 8).

Both rows use RUNTIME-BUILT heap strings so each failure direction is
valgrind-visible: a lost load-bearing drop → definitely-lost; a drop
over transferred ownership → Invalid read/free.

Row 1 — the `std.fs::read_to_bytes` shape (B-M class): an accumulator
array moved into the result on the success arm of a value-producing
match, left LIVE on the error arm.  Pre-B-M the error-arm free came
from the Return-boundary sweep; post-B-M it comes from the authored
hook drop.  Both arms exercised every iteration pair.

Row 2 — the `std.json::_parse_array` shape (B-U class): a loop-built
accumulator conditionally consumed under flag-guarded cleanup; the
post-cleanup residual sweep drop is the proven no-op B-U deletes.
Both exits exercised.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

SOURCE = """\
module m;

import std.format as fmt;

fn build(n: Int) nothrow -> Array<String> {
	var xs: Array<String> = [];
	var i = 0;
	while i < n {
		xs.push("it-" + fmt.format_int(i + 1000));
		i = i + 1;
	}
	return move xs;
}

// Row 1: read_to_bytes shape — moved into the result on the ok arm,
// LIVE on the error arm (the arm the sweep used to free; post-B-M the
// authored hook drop frees it).
fn read_like(n: Int, ok: Bool) nothrow -> Int {
	var bytes = build(n);
	val r = match ok {
		true  => { consume(move bytes) },
		false => { 0 - n }
	};
	return r;
}

fn consume(a: Array<String>) nothrow -> Int {
	return a.len;
}

// Row 2: json-parser shape — loop-built accumulator conditionally
// consumed; the guarded cleanup covers the consumed path and the
// residual boundary drop must stay balanced on BOTH exits.
fn parse_like(n: Int, take: Bool) nothrow -> Int {
	var items = build(n);
	var out: Array<String> = [];
	var i = 0;
	while i < n {
		out.push("o-" + fmt.format_int(i + 2000));
		i = i + 1;
	}
	if take {
		out = move items;
	}
	return out.len;
}

pub fn main() nothrow -> Int {
	var acc = 0;
	var i = 1;
	while i < 5 {
		acc = acc + read_like(i, true);
		acc = acc + read_like(i, false);
		acc = acc + parse_like(i, true);
		acc = acc + parse_like(i, false);
		i = i + 1;
	}
	if acc != 0 { return 0; }
	return 1;
}
"""


def test_array_sweep_retirement_rows_balanced(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
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
		f"exit {run.returncode} under valgrind — a drop over transferred "
		f"ownership reads as Invalid read/free.\n{vg_output[-1500:]}"
	)
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert lost == 0, (
		f"{lost} bytes definitely lost — the error-arm/live-path array "
		f"free was dropped (B-M authoring or the guarded cleanup "
		f"regressed).\n{vg_output[-1500:]}"
	)
	for bad in ("Invalid read", "Invalid write", "Invalid free"):
		assert bad not in vg_output, f"{bad} under valgrind:\n{vg_output[-1500:]}"
