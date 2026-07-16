# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Heap-backed carriers for the TLR-6 CopyValue family
(COPYVALUE-RESIDUAL-REPORT.md Path A: String CopyValue dests join
`is_materialized_release_family_producer`; the string_releases pass owns
their last-use releases, and the CopyValue rewrite arm carries the
recognized guard — the review-amendment suppression).

The measured population is exactly the two array-read lowering sites,
so the rows drive both through `Array<String>` with RUNTIME-BUILT
elements:
- `arr[i]` value reads (array_elem_copy, 9,246) — the bounds-checked
  read parks the extraction +1 in a hidden local and CopyValue takes
  the expression's +1;
- `arr[i].field` reads (array_elem_field_copy, 1,849) — CopyValue
  materializes ownership from a borrowed field view.
Both used NON-consumingly (comparisons), so the copies' last-use
releases are family emissions.  MISSING RELEASE → definitely-lost;
DOUBLE RELEASE (the guard-teeth failure mode at runtime) → Invalid
read/free.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

TLR6_SOURCE = """\
module m;

import std.format as fmt;

pub struct Row {
	name: String,
	tag: Int,
}

fn build_names(n: Int) nothrow -> Array<String> {
	var xs: Array<String> = [];
	var i = 0;
	while i < n {
		xs.push("name-" + fmt.format_int(i + 100));
		i = i + 1;
	}
	return move xs;
}

fn build_rows(n: Int) nothrow -> Array<Row> {
	var xs: Array<Row> = [];
	var i = 0;
	while i < n {
		xs.push(Row(name = "row-" + fmt.format_int(i + 500), tag = i));
		i = i + 1;
	}
	return move xs;
}

// arr[i] String value reads, compared non-consumingly — the
// array_elem_copy site's CopyValue last-use releases.
fn compare_elems(names: &Array<String>, i: Int, j: Int) nothrow -> Int {
	if names[i] == names[j] { return 1; }
	return 0;
}

// arr[i].field String reads, compared non-consumingly — the
// array_elem_field_copy site's CopyValue last-use releases.
fn compare_fields(rows: &Array<Row>, i: Int, j: Int) nothrow -> Int {
	if rows[i].name == rows[j].name { return 1; }
	return 0;
}

pub fn main() nothrow -> Int {
	val names = build_names(8);
	val rows = build_rows(8);
	var acc = 0;
	var i = 0;
	while i < 8 {
		acc = acc + compare_elems(&names, i, (i + 1) % 8);
		acc = acc + compare_elems(&names, i, i);
		acc = acc + compare_fields(&rows, i, (i + 1) % 8);
		acc = acc + compare_fields(&rows, i, i);
		i = i + 1;
	}
	if acc > 0 { return 0; }
	return 1;
}
"""


def test_copyvalue_lastuse_release_balanced(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(TLR6_SOURCE)
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
		f"CopyValue temp (the unguarded-arm failure mode) reads as "
		f"Invalid read/free.\n{vg_output[-1500:]}"
	)
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert lost == 0, (
		f"{lost} bytes definitely lost — MISSING RELEASE in the "
		f"CopyValue family migration.\n{vg_output[-1500:]}"
	)
	for bad in ("Invalid read", "Invalid write", "Invalid free"):
		assert bad not in vg_output, f"{bad} under valgrind:\n{vg_output[-1500:]}"
