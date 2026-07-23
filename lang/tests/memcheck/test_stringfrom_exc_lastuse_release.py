# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Heap-backed carriers for the TLR-5 families (TLR-5-DESIGN.md:
StringFrom{Int,Bool,Uint,Float} and ExcGetParamsJson/ExcGetContextJson
join `is_materialized_release_family_producer`; the string_releases
pass owns their last-use releases).

Runtime-built strings throughout, so both failure directions are
valgrind-visible:
- MISSING RELEASE → definitely-lost blocks;
- DOUBLE RELEASE (recognition failed to suppress the historical in-pass
  bookkeeping) → Invalid read/free.

Rows:
1. f-string row — interpolation holes emit StringFromInt/Bool/Uint/
   Float whose results drain into the interpolation concat chain (the
   TLR-5 family shape), plus interpolation results compared
   non-consumingly.
2. error-inspection row — a throws callee; the catch arm reads
   `e.params` / `e.context` (ExcGetParamsJson / ExcGetContextJson on
   the LIVE error path, exercised every iteration) and uses the dumps
   non-consumingly.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

TLR5_SOURCE = """\
module m;

pub error ProbeError {
	code: Int,
	what: String,
}

// Row 1: every hole kind — StringFromInt/Bool/Uint/Float results drain
// into the interpolation concat chain; the interpolation results are
// then compared (non-consuming last uses).
fn fstring_row(i: Int, b: Bool, u: Uint, x: Float) nothrow -> Int {
	val left = f"row-{i} {b} {u} {x}";
	val right = f"row-{i} {b} {u} {x}";
	if left == right { return 1; }
	return 0;
}

fn risky(i: Int) throws -> Int {
	if i % 2 == 0 {
		throw ProbeError(code = i, what = f"boom-{i}");
	}
	return i;
}

// Row 2: the catch arm inspects the error — `e.params` /
// `e.context` lower through ExcGetParamsJson / ExcGetContextJson on
// the live error path; the dumps are used non-consumingly.
fn inspect_row(i: Int) nothrow -> Int {
	try {
		return risky(i);
	} catch e {
		val params = e.params.encode_compact();
		val ctx = e.context.encode_compact();
		if params == ctx { return 0; }
		if params.byte_length() > 0 { return 1; }
		return 0;
	}
	return 0;
}

pub fn main() nothrow -> Int {
	var acc = 0;
	var i = 0;
	while i < 20 {
		acc = acc + fstring_row(i, i % 2 == 0, 7u, 2.5);
		acc = acc + inspect_row(i);
		i = i + 1;
	}
	if acc > 0 { return 0; }
	return 1;
}
"""


def test_stringfrom_exc_lastuse_release_balanced(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(TLR5_SOURCE)
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
		f"StringFrom*/Exc* temp reads as Invalid read/free.\n{vg_output[-1500:]}"
	)
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert lost == 0, (
		f"{lost} bytes definitely lost — MISSING RELEASE in the "
		f"StringFrom*/Exc* family migration.\n{vg_output[-1500:]}"
	)
	for bad in ("Invalid read", "Invalid write", "Invalid free"):
		assert bad not in vg_output, f"{bad} under valgrind:\n{vg_output[-1500:]}"
