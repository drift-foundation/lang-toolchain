# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Valgrind proof for the inline variant-drop optimization: Err paths,
early returns with live droppable Results, loop-carried Results, and
scope-exit controls all drop EXACTLY once — no leak, no double free.
Twin of lang/tests/driver/test_variant_drop_inline.py (same fixture)."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

# Reuse the driver fixture verbatim.
from lang.tests.driver.test_variant_drop_inline import SOURCE  # noqa: E402


def test_variant_drop_inline_valgrind_clean(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
	out_bin = tmp_path / "vd_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:1500]}"
	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	assert "variant drop OK" in vg.stdout, f"program failed under valgrind: {vg.stdout!r} {vg.stderr[:400]}"
	assert vg.returncode == 0, f"valgrind found errors:\n{vg_output[-2500:]}"
	assert len(re.findall(r"Invalid (read|write|free)", vg_output)) == 0, vg_output[-2500:]
	assert re.search(r"definitely lost: 0 bytes", vg_output) or "no leaks are possible" in vg_output, (
		vg_output[-2500:]
	)
