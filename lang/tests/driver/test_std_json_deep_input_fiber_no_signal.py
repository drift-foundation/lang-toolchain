# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Security proof: deeply-nested client input cannot crash a default
256 KiB serve fiber (2026-07-27).

The original blocker was a client-triggerable fiber-stack overflow — the
RECURSIVE std.json parser recursed one native frame per nesting level, so
a deeply-nested document drove a 256 KiB serve fiber into its guard page
(SIGSEGV), debug-lane-first (bigger frames overflow sooner).  The web app
worked around it with a 2 MiB fiber stack.

The iterative parser removes the vector two ways, together:
  * nesting costs HEAP frames, not native/fiber-stack frames; and
  * the standard profiles carry a finite default `max_depth = 128`, so
    untrusted input bounded at the cap returns a clean `limit-depth`
    error after ~128 heap frames — no deep node is ever built (so neither
    the parse nor the result's drop can recurse).

This test spawns the parse on a DEFAULT 256 KiB VirtualThread (NOT the
app's 2 MiB mitigation) with 5000-deep input under the default config and
proves it returns `limit-depth` and the process exits 0 — no signal — in
BOTH the debug lane (`DRIFT_DEBUG=1`, larger frames, the original crash
lane) and the release lane (`-O2`).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

SRC = r"""
module main;

import std.concurrent as conc;
import std.json as json;
import std.core as core;
import std.console as cons;
import std.format as fmt;

fn nested(depth: Int) nothrow -> String {
	var s = "";
	var i = 0;
	while i < depth { s = s + "["; i = i + 1; }
	var j = 0;
	while j < depth { s = s + "]"; j = j + 1; }
	return move s;
}

// Parse `depth`-deep input on a DEFAULT 256 KiB fiber, under the default
// (finite-cap) profile.  Returns 0=Ok, 1=limit-depth, 2=other error,
// 9=fiber error.  A recursive parser would overflow the 256 KiB stack
// here (SIGSEGV); the iterative parser + default cap returns limit-depth.
fn parse_deep_on_fiber(depth: Int) nothrow -> Int {
	var t = conc.spawn(| | => {
		val s = nested(depth);
		val cfg = json.permissive();          // default max_depth = 128
		match json.parse_with_config(s, cfg) {
			core.Result::Ok(_n) => { return 0; },
			core.Result::Err(e) => {
				if e.tag == "limit-depth" { return 1; }
				return 2;
			}
		}
	});
	match t.join() {
		core.Result::Ok(v) => { return v; },
		core.Result::Err(_e) => { return 9; }
	}
}

pub fn main() nothrow -> Int {
	// 5000 nesting levels — far past any 256 KiB native-recursion limit.
	val outcome = parse_deep_on_fiber(5000);
	cons.println("deep_fiber=" + fmt.format_int(outcome));
	if outcome == 1 { return 0; }             // limit-depth, no crash
	return 1;
}
"""


def _build_and_run(tmp_path: Path, *, debug_lane: bool) -> subprocess.CompletedProcess:
	src = tmp_path / "main.drift"
	src.write_text(SRC)
	lane = "debug" if debug_lane else "release"
	out_bin = tmp_path / f"deep_{lane}.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	env = dict(os.environ)
	if debug_lane:
		env["DRIFT_DEBUG"] = "1"      # suppress -O2, debug runtime variant (bigger frames)
	else:
		env.pop("DRIFT_DEBUG", None)
	comp = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, env=env,
		timeout=sanitizer_timeout(300))
	assert comp.returncode == 0, f"[{lane}] compile failed:\n{comp.stderr[-2000:]}"
	try:
		return subprocess.run([str(out_bin)], capture_output=True, text=True,
		                      timeout=sanitizer_timeout(120))
	finally:
		# Remove the built binary so the session-end lane audit does not flag
		# the debug-variant binary (built under DRIFT_DEBUG=1) as a leaked
		# non-normal-lane artifact in this normal-lane session.
		try:
			out_bin.unlink()
		except OSError:
			pass


@pytest.mark.parametrize("debug_lane", [True, False], ids=["debug", "release"])
def test_deep_input_returns_limit_depth_not_sigsegv_on_256k_fiber(
	tmp_path: Path, debug_lane: bool,
) -> None:
	run = _build_and_run(tmp_path, debug_lane=debug_lane)
	# rc 0 = clean exit.  A fiber-stack overflow would be a negative return
	# code (killed by SIGSEGV = -11); assert NO signal.
	assert run.returncode >= 0, (
		f"process died by signal {-run.returncode} (SIGSEGV=11 → fiber-stack "
		f"overflow: the deep-nesting DoS vector is NOT closed).\n{run.stderr[:800]}")
	assert run.returncode == 0, (
		f"rc={run.returncode}\nstdout={run.stdout}\nstderr={run.stderr[:800]}")
	assert "deep_fiber=1" in run.stdout.splitlines(), (
		f"expected limit-depth (1) on the 256 KiB fiber, got:\n{run.stdout}")
