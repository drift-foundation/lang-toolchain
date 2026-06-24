# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: passing `assert(cond, msg)` must not leak `msg`.

drift 0.33.54 lowered the assertion message expression before the
condition branch.  A passing assertion with a heap-built String message
therefore constructed an owned temporary that never reached a drop.
`std.concurrent.await_signal()` hit this through its passing
single-waiter assertion diagnostic.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import asan_active, sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

SOURCE = """\
module main;

import std.core as core;

pub fn main() nothrow -> Int {
\tassert(1 == 1, "await_signal: " + "single-waiter diagnostic");
\treturn 0;
}
"""


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_passing_assert_heap_message_no_leak(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
	out_bin = tmp_path / "test_bin"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:300]}"

	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0

	assert vg.returncode == 0, (
		f"valgrind found leaks/errors for passing assert message\n"
		f"definitely lost: {definitely_lost} bytes\n"
		f"valgrind log:\n{vg_output[-1200:]}"
	)
	assert definitely_lost == 0, f"definitely lost: {definitely_lost} bytes"
