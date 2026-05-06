# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: owned-temp String through `core.diagnostic_json_string`
must not leak the original string reference.

Slice 7a (0.31.62, 2026-05-05): the original probe used
`DiagnosticValue::String(fmt.format_int(code))` — that DV public
surface is gone.  The equivalent ownership shape is
`core.diagnostic_json_string(&fmt.format_int(code))` returning the
JSON-quoted String.  The borrowed `&` of an owned temporary still
needs the source release to fire after the helper returns; without
the ownership split, string_arc would miss the release for the temp
→ leak per call.

This test asserts zero definitely-lost bytes.
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
import std.format as fmt;

fn do_work(code: Int) nothrow -> Int {
\tval _projected: String = core.diagnostic_json_string(&fmt.format_int(code));
\treturn code;
}

pub fn main() nothrow -> Int {
\tval r1 = do_work(200);
\tval r2 = do_work(200);
\tval r3 = do_work(200);
\treturn r1 + r2 + r3 - 600;
}
"""


def test_dv_string_owned_temp_no_leak(tmp_path: Path) -> None:
	"""`core.diagnostic_json_string(&fmt.format_int(...))` must not leak."""
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
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0

	assert vg.returncode != 97, (
		f"Valgrind detected leaks — owned string temp not released "
		f"(historical: leak after ConstructDV(String); the DV substrate "
		f"is now retired but the owned-temp ownership invariant still "
		f"applies to throw-side params projection).\n"
		f"definitely lost: {definitely_lost} bytes\n"
		f"valgrind log:\n{vg_output[-500:]}"
	)
	assert definitely_lost == 0, f"definitely lost: {definitely_lost} bytes"
