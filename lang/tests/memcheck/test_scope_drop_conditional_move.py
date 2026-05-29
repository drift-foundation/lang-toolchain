# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: variant local conditionally moved across match arms must be
destroyed at scope exit on the non-move path.

Without the fix in string_arc.py, the scope-exit destroy for a variant
local assigned in a match arm and moved on only one sub-arm is omitted.
The variant's internal heap allocations (e.g. HashMap backing arrays
from clone_deep) leak on every call.

This test compiles a minimal repro, runs it under Valgrind, and asserts
zero definitely-lost bytes.  It is the primary regression for the fix;
the e2e case scope_drop_conditional_move is a weaker functional check.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

SOURCE = """\
module main;

import std.core as core;
import std.json as json;

fn build() nothrow -> String {
\tval text = "{\\"k\\":\\"v\\"}";
\tmatch json.parse(&text) {
\t\tcore.Result::Ok(node) => {
\t\t\tval deep = node.clone_deep();
\t\t\tmatch node.into_object() {
\t\t\t\tSome(_obj) => {},
\t\t\t\tNone => { val _ = move deep; }
\t\t\t}
\t\t},
\t\tcore.Result::Err(_) => {}
\t}
\treturn "done";
}

pub fn main() nothrow -> Int {
\tval r1 = build();
\tval r2 = build();
\tval r3 = build();
\treturn r1.byte_length() + r2.byte_length() + r3.byte_length();
}
"""


def test_conditional_move_variant_no_leak(tmp_path: Path) -> None:
	"""Variant local moved on one match arm must be destroyed on the other."""
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
	out_bin = tmp_path / "test_bin"

	# Compile
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:300]}"
	assert out_bin.exists()

	# Run under valgrind
	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=120,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0

	assert vg.returncode != 97, (
		f"Valgrind detected leaks — variant local not destroyed on "
		f"non-move match arm.\n"
		f"definitely lost: {definitely_lost} bytes\n"
		f"valgrind log:\n{vg_output[-500:]}"
	)
	assert definitely_lost == 0, f"definitely lost: {definitely_lost} bytes"
