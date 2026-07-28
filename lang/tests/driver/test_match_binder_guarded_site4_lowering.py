# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Real lowering proof for the `__match_binder_*` drop-flag allowlist
(2026-07-27).

`drop_flags` allow-lists `__match_binder_*` locals (proper compiler
support) so a `var` match binder that is CONDITIONALLY MOVED and then
OVERWRITTEN — genuinely PathDependent at the overwrite — is flag-managed
and gets a FLAG-GUARDED site-4 drop-before-overwrite, instead of the
site-4 authority failing closed.  A unit test proving the binder enters
the flag map is not enough: this compiles + RUNS real source that creates
the match binder, reaches guarded site-4 emission, and executes BOTH the
moved and the live branch, asserting exactly-once destruction (the memcheck
twin proves no leak / double-free).  This is the boundary an earlier
attempt reportedly lowered to invalid MIR.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

SRC = r"""
module main;

import std.core as core;
import std.console as cons;
import std.format as fmt;

// zero-storage-UNSAFE destructible (owns a heap String); prints on drop.
struct Res {
	tag: Int,
	name: String
}
implement core.Destructible for Res {
	pub fn destroy(var self: Res) nothrow -> Void {
		cons.println("drop " + fmt.format_int(self.tag));
	}
}
fn mk(t: Int) nothrow -> Res {
	var s = "r";
	s = s + fmt.format_int(t);
	return Res(tag = t, name = move s);
}
fn consume(r: Res) nothrow -> Int { return r.tag; }   // drops r at scope exit
fn borrow_tag(r: &Res) nothrow -> Int { return r.tag; }

variant Holder {
	Full(r: Res),
	@tombstone Empty
}

// The `var r` MATCH BINDER (a `__match_binder_*` destructible) is
// conditionally moved on the `take` branch and then OVERWRITTEN — genuinely
// PathDependent at the `r = mk(...)` store, so drop_flags must flag it
// (allowlist) and overwrite_cleanup emits a FLAG-GUARDED site-4 drop.
fn via_binder(h: Holder, take: Bool, over: Int) nothrow -> Int {
	var acc = 0;
	match h {
		Holder::Full(var r) => {
			if take {
				acc = acc + consume(move r);   // conditional move-out of the binder
			} else {
				acc = acc + borrow_tag(&r);     // binder stays live
			}
			r = mk(over);                       // OVERWRITE the binder (guarded site-4)
			acc = acc + r.tag;
			return acc;                          // binder dropped here
		},
		default => { return -1; }
	}
}

pub fn main() nothrow -> Int {
	var acc = 0;
	// moved branch: r(1) moved→consumed (drop 1); overwrite sees moved slot
	// (no drop); r(101) dropped at return.
	acc = acc + via_binder(Holder::Full(r = mk(1)), true, 101);
	// live branch: r(2) live at overwrite → guarded drop fires (drop 2);
	// r(102) dropped at return.
	acc = acc + via_binder(Holder::Full(r = mk(2)), false, 102);
	cons.println("acc=" + fmt.format_int(acc));
	return 0;
}
"""

# moved:  drop 1 (consume), drop 101 (return)
# live:   drop 2 (guarded overwrite), drop 102 (return)
_EXPECTED_DROPS = {"drop 1": 1, "drop 101": 1, "drop 2": 1, "drop 102": 1}
# via_binder(true,101):  consume(1)=1 ; +101  → 102
# via_binder(false,102): borrow(2)=2 ; +102  → 104
_EXPECTED_ACC = 102 + 104


def _compile(tmp_path: Path) -> Path:
	src = tmp_path / "main.drift"
	src.write_text(SRC)
	out_bin = tmp_path / "mb.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240))
	assert res.returncode == 0, (
		"compile failed — the __match_binder_* guarded site-4 lowering "
		f"regressed (invalid MIR?):\n{res.stdout}\n{res.stderr[-2500:]}")
	return out_bin


def test_match_binder_guarded_site4_compiles_and_runs_exactly_once(tmp_path: Path) -> None:
	out_bin = _compile(tmp_path)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
	                     timeout=sanitizer_timeout(60))
	assert run.returncode == 0, f"rc={run.returncode}\n{run.stdout}\n{run.stderr[:600]}"
	lines = run.stdout.splitlines()
	assert f"acc={_EXPECTED_ACC}" in lines, f"result drifted:\n{run.stdout}"
	drops = [l for l in lines if l.startswith("drop ")]
	assert Counter(drops) == Counter(_EXPECTED_DROPS), (
		"match-binder guarded site-4 destruction not exactly-once "
		f"(double-free or leak):\n  expected {sorted(_EXPECTED_DROPS.items())}\n"
		f"  actual   {sorted(Counter(drops).items())}")


def test_match_binder_guarded_site4_valgrind_clean(tmp_path: Path) -> None:
	if shutil.which("valgrind") is None:
		import pytest
		pytest.skip("valgrind required")
	out_bin = _compile(tmp_path)
	vg_log = tmp_path / "vg.log"
	res = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97", f"--log-file={vg_log}", str(out_bin)),
		capture_output=True, text=True, timeout=sanitizer_timeout(120))
	# The RETURN CODE is load-bearing: --error-exitcode=97 fires on any leak
	# or invalid access; a crash is a negative/other code.  A discarded rc
	# could let a crash pass as "0 leaked bytes".
	assert res.returncode == 0, (
		f"valgrind exited {res.returncode} on the match-binder guarded site-4 "
		f"run (leak / invalid access / crash):\n{res.stdout[:400]}\n"
		f"{(vg_log.read_text() if vg_log.exists() else res.stderr)[-1800:]}")
	assert vg_log.exists(), "valgrind log missing — cannot verify cleanliness"
	vg = vg_log.read_text()
	assert "ERROR SUMMARY: 0 errors" in vg, f"valgrind reported errors:\n{vg[-1800:]}"
	# Either an explicit zero-lost line, or the all-freed message.
	m = re.search(r"definitely lost: (\d[\d,]*) bytes", vg)
	if m is not None:
		assert int(m.group(1).replace(",", "")) == 0, f"leak:\n{vg[-1800:]}"
	else:
		assert "no leaks are possible" in vg, f"no leak summary found:\n{vg[-1800:]}"
	for bad in ("Invalid free", "Invalid read", "Invalid write"):
		assert bad not in vg, f"'{bad}' — guarded drop fired on the moved slot:\n{vg[-1800:]}"
	# Re-verify SEMANTIC correctness UNDER valgrind: the same exact-once drop
	# multiset + result, so a memory-clean run that computed the wrong thing
	# is still caught.
	assert f"acc={_EXPECTED_ACC}" in res.stdout, f"result drifted under valgrind:\n{res.stdout}"
	drops = [l for l in res.stdout.splitlines() if l.startswith("drop ")]
	assert Counter(drops) == Counter(_EXPECTED_DROPS), (
		f"drop multiset wrong under valgrind:\n{res.stdout}")
