# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Heap-string carrier for the `captures(move <String>)` env-slot
ZERO-BACK shape (the C2-singleton fix, 2026-07-13).

The hidden-lambda prologue for a move-captured String materializes the
capture (stake-copy -> StoreLocal -> MoveOut) and then zero-backs the
env slot: `StoreRef(env_field, ZeroValue)` — so cb_drop cannot
double-free the slot.  string_arc's StoreRef path used to `_ensure_owned`
the stored ZeroValue (a dead retain of zeroed bytes — the last
c2_invisible_stake / store_value_retain corpus residual); the fix
classifies fresh String ZeroValue dests as owned/no-stake-needed.

This carrier proves refcount balance at the valgrind level with a HEAP
string (runtime concat — a static literal would mask imbalance), run
across several spawn/join cycles:
- an over-retain regression surfaces as definitely-lost blocks;
- an over-release regression (e.g. eliding the wrong side) surfaces as
  Invalid read/free from cb_drop or the lambda body.

Kept OUT of the e2e fixture corpus deliberately: adding a fixture would
change the corpus-audit universe mid-phase; the shape's counter-level
regression is pinned in
lang/tests/stage2/test_string_arc_audit_reporter.py::test_zerovalue_store_needs_no_stake.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

SPAWN_CB_MOVE_CAPTURE_SOURCE = """\
module main;

import std.concurrent as conc;
import std.format as fmt;

fn run_serve(name: String, port: Int) nothrow -> Int {
\tif name.byte_length() > 0 { return port; }
\treturn 0;
}

fn start_server(tag: Int) nothrow -> Int {
\t// HEAP string: runtime concat, so a retain/release imbalance on the
\t// capture slot is visible to valgrind (static literals are not).
\tvar name = "srv-" + fmt.format_int(tag);
\tvar port = 8000 + tag;
\tvar vt = conc.spawn_cb(| | captures(move name, copy port) => {
\t\treturn run_serve(move name, port);
\t});
\tmatch vt.join() {
\t\tOk(v) => { return v; },
\t\tErr(_) => { return 1; },
\t\tdefault => { return 2; }
\t}
}

pub fn main() nothrow -> Int {
\tvar i = 0;
\tvar acc = 0;
\twhile i < 8 {
\t\tacc = acc + start_server(i);
\t\ti = i + 1;
\t}
\tif acc > 0 { return 0; }
\treturn 1;
}
"""


def test_spawn_cb_move_capture_zero_back_balanced(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(SPAWN_CB_MOVE_CAPTURE_SOURCE)
	out_bin = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
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
			"--fair-sched=yes",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	assert run.returncode == 0, (
		f"binary exit {run.returncode} under valgrind — an over-release "
		f"regression on the zero-back path reads as Invalid read/free "
		f"from cb_drop or the lambda body.\n{vg_output[-1500:]}"
	)
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert lost == 0, (
		f"{lost} bytes definitely lost — an over-retain regression on "
		f"the move-capture env-slot zero-back (the pre-fix dead retain "
		f"was a no-op on zeroed bytes; a NON-zero retain here leaks one "
		f"string per spawn).\n{vg_output[-1500:]}"
	)
	for bad in ("Invalid read", "Invalid write", "Invalid free"):
		assert bad not in vg_output, f"{bad} under valgrind:\n{vg_output[-1500:]}"
