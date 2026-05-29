# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Memcheck carrier for symmetric `&T → T` argument coercion.

After the type-checker rewrites the argument to an explicit
deref (`*s_ref`), HIR→MIR lowering for String must retain via
the existing string_arc handler.  The callee then drops its
parameter at scope end, and the caller still holds the borrow.
Net effect: one extra retain + one extra release per call.

If valgrind reports a leak or invalid free, the regression is
in HIR→MIR lowering of the synthesized `HUnary(DEREF, &String)`
node, or in the implicit `.const_share()` wrap for non-Copy
ConstShare types.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

import pytest

ROOT = Path(__file__).resolve().parents[3]


CARRIER_REF_STRING_TO_STRING = """\
module main;

import std.format as fmt;

pub fn take_s(s: String) nothrow -> Int {
\treturn s.byte_length();
}

pub fn caller(s_ref: &String) nothrow -> Int {
\tval a = take_s(s_ref);
\tval b = take_s(s_ref);
\treturn a + b;
}

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tval total = caller(&s);
\treturn total - 6;
\t// Two `take_s(s_ref)` calls each dup the String via `*s_ref`.
\t// String is Copy + ConstShare; the retain happens in
\t// string_arc on deref-load.  Callee drops the param at scope
\t// end (refcount -1).  Caller still owns `s` until function
\t// return.  Net: original 1 → +2 retains → -2 callee drops →
\t// -1 caller drop → 0.
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str, int]:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:1500]}"
	assert out_bin.exists(), f"[{label}] binary not produced"
	vg_log = tmp_path / f"valgrind_{label}.log"
	subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=180,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	err_match = re.search(r"ERROR SUMMARY: (\d+) errors", vg_output)
	error_count = int(err_match.group(1)) if err_match else 0
	return definitely_lost, vg_output, error_count


def test_ref_string_to_string_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CARRIER_REF_STRING_TO_STRING, label="ref_string_dup",
	)
	assert lost == 0, (
		f"definitely lost: {lost} bytes — implicit deref+copy at "
		f"call boundary leaked.\nValgrind log tail:\n{vg[-1500:]}"
	)
	if "Invalid read" in vg or "Invalid write" in vg or "Invalid free" in vg:
		raise AssertionError(
			f"valgrind invalid memory access on `&String → String` "
			f"call boundary; errors={errors}\n\nVG log tail:\n{vg[-2000:]}"
		)
