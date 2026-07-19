# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""string-arc-endgame-array-sweep — RAII destruction-order carrier
(maintainer spec pin 5).

A PATH_DEPENDENT `Array<D>` (`bytes`: moved on one match arm, live on
the other — the `std.fs::read_to_bytes` shape) declared AFTER a live
array (`a1`) and an interleaved destructible struct (`mid`).  The pin
runs BOTH condition outcomes and asserts the exact destroy-marker
sequence:

- scope-exit cleanup runs in reverse-declaration RAII order —
  `bytes` (when live), then `mid`, then `a1`'s element;
- on the consumed outcome the by-value callee param drops at the
  callee's exit (inside the match), which precedes the fn-exit
  cleanup — so the TOTAL marker order is identical on both outcomes;
- the sweep-era anomaly (PD arrays dropping in sorted-name order
  AFTER every hook drop) is gone: `bytes` must precede `mid`.

The order must hold regardless of which cleanup mechanism authors the
PD drop (Arm M unguarded, flag-guarded, or edge-elaborated) — that is
the point: PD arrays are normalized onto the live-array RAII order
contract.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_SOURCE = """\
module main;

import std.core as core;
import std.console as console;

struct D { name: String }

implement core.Destructible for D {
	pub fn destroy(var self: D) nothrow -> Void {
		console.print(self.name);
		console.print("\\n");
		return;
	}
}

fn consume(a: Array<D>) nothrow -> Int {
	return 1;
}

fn carrier(ok: Bool) nothrow -> Int {
	var a1: Array<D> = [];
	a1.push(D(name = "live1"));
	var mid: D = D(name = "mid");
	var bytes: Array<D> = [];
	bytes.push(D(name = "pd"));
	val n = match ok {
		true  => { consume(move bytes) },
		false => { 2 }
	};
	return n;
}

pub fn main() nothrow -> Int {
	val x = carrier(true);
	val y = carrier(false);
	if x == 1 { if y == 2 { return 0; } }
	return 1;
}
"""

# Both outcomes produce the same total order: `pd` first (arm-scope
# drop of the moved copy on ok=true; reverse-decl fn-exit cleanup on
# ok=false), then `mid`, then `a1`'s element.
_EXPECTED = "pd\nmid\nlive1\npd\nmid\nlive1\n"


def test_pd_array_drops_in_reverse_decl_raii_order(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(_SOURCE)
	out = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240), env=os.environ.copy(),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1800:]}"
	run = subprocess.run(
		[str(out)], capture_output=True, text=True,
		timeout=sanitizer_timeout(10),
	)
	assert run.returncode == 0, (
		f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"
	)
	assert run.stdout == _EXPECTED, (
		"destroy order regressed — PD arrays must follow the "
		f"reverse-declaration RAII contract:\nexpected: {_EXPECTED!r}\n"
		f"actual:   {run.stdout!r}"
	)
