# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Heap-backed carriers for the TLR-8 MoveOut-family last-use releases
(`move`d String operands draining at non-consuming concats — the
drift-workflows release-arm tripwire firings,
issues/string-arc-release-arm-tripwire/: the MoveOut dest inherits the
storage local's +1 stake, the concat only borrows it, and the release
is owned by the string_releases pass since TLR-8).

Every string is RUNTIME-BUILT (format_int concat) so both failure
directions are valgrind-visible:
- MISSING RELEASE (the pass skipped a qualified move temp) → the moved
  payload becomes a definitely-lost block;
- DOUBLE RELEASE (recognition failed to suppress the historical in-pass
  bookkeeping, or the expansion arm re-owned a recognized temp) →
  Invalid read/free.

Rows:
1. tag — the pinned minimal repro shape: `"lit" + move s` on a by-value
   param, drained in the producing block.
2. describe — production firing 1's shape: the moved operand is a MATCH
   BINDER, concatenated inside a value-producing match arm.
3. join3 — production firing 2's shape: a chained concat
   (`a + sep + move p`) — the move drains at the outer concat alongside
   the inner concat's own family temp.
4. reject — production firing 3's shape: the moved concat drains into a
   THROW's error-constructor field (`throw E(what = ... + move m)`);
   the error edge is actually exercised and unwinds through the
   try/catch-expression fallback, so both edges must be
   release-balanced.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

MOVE_CONCAT_SOURCE = """\
module m;

import std.core as core;
import std.format as fmt;

// Row 1: the pinned repro — a moved String operand at a non-consuming
// concat (TLR-8 family; pre-fix this ICE'd with the release-arm
// tripwire, family=False, producer=MoveOut).
fn tag(s: String) nothrow -> String {
	return "x: " + move s;
}

// Row 2: match-arm shape (production firing 1): the moved operand is a
// match binder and the concat is the arm's value.
fn describe(o: Optional<String>, i: Int) nothrow -> String {
	return match o {
		Optional::Some(v) => { "dup: " + move v },
		Optional::None    => { "none-" + fmt.format_int(i) }
	};
}

// Row 3: chained concat (production firing 2): the move drains at the
// OUTER concat; the inner concat's result temp drains there too.
fn join3(a: String, sep: String, p: String) nothrow -> String {
	return a + sep + move p;
}

pub error BadCall {
	what: String,
}

// Row 4: throw path (production firing 3): the moved concat feeds an
// error-constructor field inside a throw; every third i takes the
// error edge and unwinds into the catch fallback.
fn reject(i: Int) throws -> Int {
	val m = "call-" + fmt.format_int(i);
	if i % 3 == 0 {
		throw BadCall(what = "unknown call '" + move m + "'");
	}
	return m.byte_length();
}

pub fn main() nothrow -> Int {
	var acc = 0;
	var i = 0;
	while i < 30 {
		val s = "s-" + fmt.format_int(i);
		acc = acc + tag(move s).byte_length();
		val nm = "nm-" + fmt.format_int(i);
		val o: Optional<String> = match i % 3 == 0 {
			true  => { Optional::None() },
			false => { Optional::Some(move nm) }
		};
		acc = acc + describe(move o, i).byte_length();
		val a = "a-" + fmt.format_int(i);
		val sep = ":" + fmt.format_int(i);
		val p = "p-" + fmt.format_int(i);
		acc = acc + join3(move a, move sep, move p).byte_length();
		acc = acc + (try reject(i) catch { 1 });
		i = i + 1;
	}
	if acc > 0 { return 0; }
	return 1;
}
"""


def test_move_operand_concat_release_balanced(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(MOVE_CONCAT_SOURCE)
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
		f"moved concat operand reads as Invalid read/free.\n{vg_output[-1500:]}"
	)
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert lost == 0, (
		f"{lost} bytes definitely lost — MISSING RELEASE: a moved concat "
		f"operand's stake leaked (TLR-8 family materialization).\n"
		f"{vg_output[-1500:]}"
	)
	for bad in ("Invalid read", "Invalid write", "Invalid free"):
		assert bad not in vg_output, f"{bad} under valgrind:\n{vg_output[-1500:]}"
