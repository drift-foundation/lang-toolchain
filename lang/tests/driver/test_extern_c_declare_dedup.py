# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Two modules in one compilation unit may declare the same extern "C"
symbol.  Codegen previously emitted one LLVM `declare` line per declaring
module, and clang rejects a repeated `declare` for the same symbol even when
the signatures are identical ("invalid redefinition of function").  Found
2026-07-12 while verifying the drift-query Slice 12 spawn-lambda repro pair
(both files declare `usleep`); fixed by deduping exact-duplicate declare
lines in `add_extern_c_declare`.

A repeat with a DIFFERENT signature remains a genuine conflict and still
fails at the LLVM level (unchanged behavior, not pinned here).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_HELPER_SOURCE = """\
module helper;

extern "C" fn getpid() nothrow -> Int32;

pub fn helper_pid() nothrow -> Int {
	unsafe {
		return cast<Int>(getpid());
	}
}
"""

_MAIN_SOURCE = """\
module main;

import helper as h;

extern "C" fn getpid() nothrow -> Int32;

pub fn main() nothrow -> Int {
	unsafe {
		val mine = cast<Int>(getpid());
		if mine == h.helper_pid() {
			return 0;
		}
	}
	return 1;
}
"""


def test_same_extern_in_two_modules_compiles_and_runs(tmp_path: Path) -> None:
	helper = tmp_path / "helper.drift"
	helper.write_text(_HELPER_SOURCE)
	main = tmp_path / "main.drift"
	main.write_text(_MAIN_SOURCE)
	out = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(main), str(helper), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240), env=os.environ.copy(),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1800:]}"
	run = subprocess.run([str(out)], capture_output=True, text=True,
	                     timeout=sanitizer_timeout(60))
	assert run.returncode == 0, f"run failed (exit {run.returncode}):\n{run.stderr[-800:]}"
