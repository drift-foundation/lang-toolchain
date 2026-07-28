# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Memcheck twin for the PathDependent drop-before-overwrite (site-4) fix.

The driver test
(`lang/tests/driver/test_pathdependent_overwrite_cleanup.py`) proves
EXACTLY-ONCE destruction by counting user-destructor prints.  This twin
runs the SAME program — reusing its `SRC` verbatim, single source of
truth — under valgrind to prove the zero-storage-UNSAFE (heap `String`)
guarded drops leak nothing and double-free nothing on ANY branch: the
moved branch (flag false → guarded drop skipped), the live branch (flag
true → guarded drop fires), loop backedges with repeated overwrites, the
2c uniformly-moved-at-exit carrier, and the THROWING unwind path (the
landing pad must drop the live slot exactly once).

A missed guarded drop on the live branch → `definitely lost` bytes (the
overwritten `Res.name` String).  A guarded drop firing on the MOVED
branch → `Invalid free` / double-free of the moved-out zeroed storage.
Both fail this test.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import valgrind_cmd
from lang.driftc.parser import stdlib_root
from lang.tests.driver.test_pathdependent_overwrite_cleanup import SRC

ROOT = Path(__file__).resolve().parents[3]


def test_pathdependent_overwrite_guarded_drops_valgrind_clean(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(SRC)
	out_bin = tmp_path / "pd_mc.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=240)
	assert res.returncode == 0, f"compile failed: {res.stderr[-1500:]}"
	assert out_bin.exists(), "binary not produced"

	vg_log = tmp_path / "valgrind_pd.log"
	subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=240)
	vg = vg_log.read_text() if vg_log.exists() else ""

	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg)
	lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert lost == 0, (
		"zero-unsafe guarded drop-before-overwrite leaked "
		f"{lost} bytes — a live-branch overwrite (or the throwing unwind) "
		f"skipped its guarded String release.\n"
		f"Touch points: overwrite_cleanup.py guarded site-4 block split, "
		f"drop_flag_guard.py, drop_flags.py criterion 2c.\n\n"
		f"{vg[-1800:]}")
	for bad in ("Invalid free", "Invalid read", "Invalid write"):
		assert bad not in vg, (
			f"valgrind reported '{bad}' — a guarded drop fired on the MOVED "
			f"branch (double-free of moved-out zeroed storage), or a "
			f"use-after-free in the split.\n\n{vg[-1800:]}")
