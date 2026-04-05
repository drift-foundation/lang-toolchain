# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: DiagnosticValue::String(local_var) in exception throw
must retain the string so the error payload survives scope exit.

The borrowed path (exception fields referencing live locals) must use
drift_dv_string with retain.  Without retain, the local's scope-exit
release frees the string while the error still holds it → UAF.

This test must PASS both before and after the MIR ownership split.
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
import std.io as io;

exception Info(msg: String, detail: String)

fn _heap_string(bytes: &Array<Byte>) nothrow -> String {
\tval n = bytes.len;
\tvar buf = io.buffer(n);
\tvar i = 0;
\twhile i < n { io.buffer_write(&mut buf, i, bytes[i]); i = i + 1; }
\treturn core.string_from_utf8_bytes(io.buffer_ptr(&buf), n);
}

fn throw_heap(label: String) -> Int {
\tvar b: Array<Byte> = [cast<Byte>(104), cast<Byte>(101), cast<Byte>(108), cast<Byte>(108), cast<Byte>(111)];
\tval s = _heap_string(&b);
\tthrow Info(s, label);
}

pub fn main() nothrow -> Int {
\tval r1 = try throw_heap("detail-1") catch { 1 };
\tif r1 != 1 { return 1; }
\tval r2 = try throw_heap("detail-2") catch { 2 };
\tif r2 != 2 { return 2; }
\tval r3 = try throw_heap("detail-3") catch { 3 };
\tif r3 != 3 { return 3; }
\treturn 0;
}
"""


def test_dv_string_borrowed_exception_no_crash(tmp_path: Path) -> None:
	"""Exception with borrowed DV string must not crash (UAF/double-free)."""
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
		f"borrowed DV string in exception released prematurely.\n"
		f"valgrind log:\n{vg_output[-500:]}"
	)

	# Exit code 0 = all catch blocks fired correctly.
	assert vg.returncode == 0, (
		f"Unexpected exit code {vg.returncode}.\n"
		f"valgrind log:\n{vg_output[-500:]}"
	)
