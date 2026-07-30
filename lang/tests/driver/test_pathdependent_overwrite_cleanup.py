# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression-first driver coverage for the PathDependent
drop-before-overwrite (site-4) fix.

A destructible local that is CONDITIONALLY MOVED (one branch) and then
OVERWRITTEN is a valid program whose liveness at the overwrite is
genuinely path-dependent.  The former Tier-1 site-4 authority ICE'd on
this (`destructible_authority.site4_disposition` PathDependent tripwire —
`issues/json-destructible-plan-pathdependent-ice/`).  The fix emits the
correct cleanup per ownership class: zero-storage-safe values
(variants/arrays) get an UNCONDITIONAL canonical drop-before-overwrite
(dropping moved-out zeroed storage is safe); zero-storage-UNSAFE values
(a struct owning a String) get a FLAG-GUARDED drop at the overwrite.

Each fixture prints `drop res <tag>` from a user destructor, so the run
asserts EXACTLY-ONCE destruction across the moved AND live branches,
loops/backedges, THROWING and nothrow forms, repeated overwrites, and the
2c overwrite-site carrier (uniformly moved at exit — flagged ONLY by the
overwrite-site criterion, not exit-liveness).  Every fixture's return
value is also pinned, so a mis-emitted drop that happened to keep the
count right is still caught by a wrong result.  The memcheck twin
(test_pathdependent_overwrite_cleanup_memcheck.py) proves no leak /
double-free on the zero-unsafe (heap String) shapes under valgrind.
"""
from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


# A zero-storage-UNSAFE destructible (struct owning a heap String) and a
# zero-storage-SAFE destructible (variant), each observable on drop.
SRC = r"""
module main;

import std.core as core;
import std.console as cons;
import std.format as fmt;

error Bang {}

// zero-storage-UNSAFE: owns a heap String; user destructor observes drop.
struct Res {
	tag: Int,
	name: String
}
implement core.Destructible for Res {
	pub fn destroy(var self: Res) nothrow -> Void {
		cons.println("drop res " + fmt.format_int(self.tag));
	}
}

fn mk_res(tag: Int) nothrow -> Res {
	var s = "res-";
	s = s + fmt.format_int(tag);
	return Res(tag = tag, name = move s);
}

// zero-storage-SAFE: a variant carrying a destructible payload.
variant Cell {
	Empty,
	Full(r: Res),
	@tombstone Tombstone
}

fn use_res(r: &Res) nothrow -> Int { return r.tag; }
fn consume(r: Res) nothrow -> Int { return r.tag; }   // drops r at scope exit
fn boom() -> Int { throw Bang(); }

// Zero-UNSAFE, straight-line: conditionally move `cur`, then overwrite.
// `cur` is LIVE at the Return (re-stored), so it is admitted via 2a.
fn zu_straight(moved: Bool) nothrow -> Int {
	var acc = 0;
	var cur = mk_res(1);
	if moved {
		val taken = move cur;      // moved on this branch only
		acc = acc + use_res(taken);
	} else {
		acc = acc + use_res(cur); // live on this branch
	}
	cur = mk_res(2);               // OVERWRITE — drop old iff live
	acc = acc + cur.tag;
	return acc;
}

// Zero-UNSAFE, loop with repeated overwrites (backedge re-enters the
// flag-loading origin block).
fn zu_loop(reps: Int, moved: Bool) nothrow -> Int {
	var acc = 0;
	var cur = mk_res(100);
	var i = 0;
	while i < reps {
		if moved and (i % 2 == 0) {
			val taken = move cur;
			acc = acc + use_res(taken);
		} else {
			acc = acc + use_res(cur);
		}
		cur = mk_res(200 + i);     // repeated OVERWRITE
		i = i + 1;
	}
	return acc;
}

// Zero-SAFE variant, straight-line conditional move then overwrite.
fn zs_straight(moved: Bool) nothrow -> Int {
	var acc = 0;
	var c: Cell = Cell::Full(r = mk_res(7));
	if moved {
		match move c {
			Cell::Full(r) => { acc = acc + r.tag; },
			default => { }
		}
	} else {
		match c {
			Cell::Full(r) => { acc = acc + r.tag; },
			default => { }
		}
	}
	c = Cell::Empty();             // OVERWRITE the variant
	acc = acc + 1;
	return acc;
}

// Zero-UNSAFE, 2c carrier: conditionally move, overwrite, then
// UNCONDITIONALLY move `cur` out again before the Return.  At the exit
// `cur` is MOVED (not live) → criterion 2a does NOT admit it; there is no
// path-dependent cleanup hook → 2b does not admit it; only the OVERWRITE
// site is PathDependent → criterion 2c admits it.  Without 2c the site-4
// authority fails closed on this shape.
fn zu_2c(moved: Bool) nothrow -> Int {
	var acc = 0;
	var cur = mk_res(40);
	if moved {
		val taken = move cur;
		acc = acc + use_res(taken);
	} else {
		acc = acc + use_res(cur);
	}
	cur = mk_res(41);              // OVERWRITE — 2c PathDependent site
	acc = acc + consume(move cur); // UNCONDITIONAL move-out → moved at exit
	return acc;
}

// Zero-UNSAFE, THROWING form: the site-4 store is lowered with landing
// pads.  When `blow`, `boom()` throws AFTER the overwrite, so the unwind
// path must drop the live `cur` exactly once.
fn zu_throwing(moved: Bool, blow: Bool) -> Int {
	var acc = 0;
	var cur = mk_res(30);
	if moved {
		val taken = move cur;
		acc = acc + use_res(taken);
	} else {
		acc = acc + use_res(cur);
	}
	cur = mk_res(31);              // OVERWRITE (guarded, throwing lowering)
	if blow {
		acc = acc + boom();        // throws → unwind drops live cur(31) once
	}
	acc = acc + cur.tag;
	return acc;
}

pub fn main() nothrow -> Int {
	var acc = 0;
	acc = acc + zu_straight(true);   // 3
	acc = acc + zu_straight(false);  // 3
	acc = acc + zu_loop(4, true);    // 703
	acc = acc + zu_loop(4, false);   // 703
	acc = acc + zs_straight(true);   // 8
	acc = acc + zs_straight(false);  // 8
	acc = acc + zu_2c(true);         // 81
	acc = acc + zu_2c(false);        // 81
	cons.println("acc=" + fmt.format_int(acc));

	var tacc = 0;
	try {
		tacc = tacc + zu_throwing(true, false);   // returns 61
	} catch e { tacc = tacc + 9000; }
	try {
		tacc = tacc + zu_throwing(false, true);   // throws → caught
	} catch e { tacc = tacc + 7000; }
	cons.println("tacc=" + fmt.format_int(tacc));
	return 0;
}
"""


# Every constructed Res, and where it is destroyed exactly once:
#   zu_straight(true):  1 (moved→taken scope), 2 (fn-scope)
#   zu_straight(false): 1 (live→dropped at overwrite), 2 (fn-scope)
#   zu_loop(4,true):    100 (taken i0), 200 (overwrite i1), 201 (taken i2),
#                       202 (overwrite i3), 203 (fn-scope)
#   zu_loop(4,false):   100 (overwrite i0), 200 (i1), 201 (i2), 202 (i3),
#                       203 (fn-scope)
#   zs_straight(true):  7 (moved into arm binder r, arm scope)
#   zs_straight(false): 7 (live variant dropped at overwrite)
#   zu_2c(true):        40 (taken scope), 41 (consume)
#   zu_2c(false):       40 (dropped at overwrite), 41 (consume)
#   zu_throwing(true,false):  30 (taken scope), 31 (fn-scope)
#   zu_throwing(false,true):  30 (dropped at overwrite), 31 (unwind)
# Each tag is constructed twice (once per call in its pair) and dropped
# exactly once per construction → EXACTLY 2 of each tag, none more (a
# double-free would push a tag past 2; a leak would drop it below 2).
EXPECTED_DROPS = {
	"drop res 1": 2,
	"drop res 2": 2,
	"drop res 7": 2,
	"drop res 30": 2,
	"drop res 31": 2,
	"drop res 40": 2,
	"drop res 41": 2,
	"drop res 100": 2,
	"drop res 200": 2,
	"drop res 201": 2,
	"drop res 202": 2,
	"drop res 203": 2,
}


def _compile(tmp_path: Path) -> Path:
	src = tmp_path / "main.drift"
	src.write_text(SRC)
	out_bin = tmp_path / "pd.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240))
	assert res.returncode == 0, (
		"compile failed (the PathDependent site-4 ICE, or a regression):\n"
		f"{res.stdout}\n---\n{res.stderr[-3000:]}")
	return out_bin


def test_pathdependent_overwrite_exact_once_destruction(tmp_path: Path) -> None:
	out_bin = _compile(tmp_path)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
	                     timeout=sanitizer_timeout(120))
	assert run.returncode == 0, f"rc={run.returncode}\n{run.stdout}\n{run.stderr[:800]}"
	lines = run.stdout.splitlines()
	drops = [l for l in lines if l.startswith("drop res ")]

	# Exact result parity: a mis-emitted drop that kept the count right is
	# still caught by a wrong computed value.
	assert "acc=1590" in lines, f"nothrow result drifted:\n{run.stdout}"
	assert "tacc=7061" in lines, f"throwing result drifted:\n{run.stdout}"

	# EXACTLY-ONCE destruction: the full drop MULTISET, not a bare count —
	# no double-free (a moved branch must NOT also drop the overwritten
	# slot) and no leak (a live branch MUST drop at the overwrite; the
	# throwing unwind MUST drop the live slot exactly once).
	assert Counter(drops) == Counter(EXPECTED_DROPS), (
		"drop multiset mismatch (double-free or leak):\n"
		f"  expected: {sorted(EXPECTED_DROPS.items())}\n"
		f"  actual:   {sorted(Counter(drops).items())}")
