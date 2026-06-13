# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: conditional-move-from-`Array.remove()` loop
compiles cleanly and runs to the correct result.

## History (pre-fix)

A loop that pops items from an `Array<T>` via `arr.remove(0)`,
conditionally moves the popped value into another container,
and implicitly drops it on the other branch, crashed stage2
with `RuntimeError: drop_before_overwrite: ledger returned
PathDependent` at `string_arc.py:960`.

Root cause: at the next iteration's `var w = arr.remove(0)`,
the ledger merge of "user moved w on then-branch" (MOVED_OUT)
and "no move on else-branch" (LIVE) yielded `MAYBE_UNINIT`,
classifying to `PATH_DEPENDENT` at the `drop_before_overwrite`
site.

## The fix (Bug 2 architecture flip, 2026-05-15)

Per-arm cleanup elaboration in `cleanup_authoring`:

  - `drop_flags` planning runs BEFORE `cleanup_authoring`,
    selecting `w` as flag-managed (criterion 2b: non-variant
    PathDependent at any reachable CleanupHook).
  - `cleanup_authoring` sees the hook for `w` at end of the
    loop body.  All hook candidates are non-variant PD +
    flag-managed → per-arm elaboration activates.
  - For each predecessor edge of the hook block: query
    `state_post(L)`.  LIVE-w predecessors (no-move branches)
    get `MoveOut + DropValue + flag-clear` inserted at end of
    the block before the terminator (single-successor) or on a
    split edge (multi-successor).
  - After elaboration: all predecessor edges arrive at the
    hook with `w` in `MOVED_OUT`.  Lattice merge is uniform.
    Loop-back propagates `MOVED_OUT` to the next iteration's
    `StoreLocal w`; no PathDependent at
    `string_arc.drop_before_overwrite`.

## What this test pins

End-to-end: the canonical Bug 2 carrier compiles, runs, and
returns the expected count of "kept" items (2 of 3 inputs).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout


_REPRO_SOURCE = """\
module main;

import std.core as core;

struct Holder { pub raw: Int }
implement core.Destructible for Holder {
	pub fn destroy(var self: Holder) nothrow -> Void { return; }
}

fn drain(arr: &mut Array<Holder>) nothrow -> Array<Holder> {
	var out: Array<Holder> = [];
	while arr.len > 0 {
		var w = arr.remove(0);
		if w.raw > 0 {
			out.push(move w);
		}
		// else: w dropped at end of iteration via cleanup_authoring
		// per-arm elaboration (drop emitted on the LIVE-w edge,
		// before the merge at the loop-body end).
	}
	return move out;
}

fn main() nothrow -> Int {
	var a: Array<Holder> = [];
	a.push(Holder(raw = 1));
	a.push(Holder(raw = 0));
	a.push(Holder(raw = 2));
	val out = drain(&mut a);
	return out.len;
}
"""


def test_conditional_move_from_array_remove_compiles_and_runs(tmp_path: Path) -> None:
	"""Bug 2 fix regression: the conditional-move-from-Array.remove()
	loop must compile cleanly and run to completion.  Exit code 2 =
	count of items where `raw > 0`."""
	src = tmp_path / "main.drift"
	src.write_text(_REPRO_SOURCE, encoding="utf-8")
	out_bin = tmp_path / "out"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--dev",
		"--entry", "main::main",
		str(src),
		"-o", str(out_bin),
	]
	root = stdlib_root()
	if root:
		cmd.insert(-2, "--stdlib-root")
		cmd.insert(-2, str(root))
	res = subprocess.run(
		cmd,
		cwd=Path(__file__).parents[3],
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, (
		"Bug 2 architecture flip should make this compile cleanly.\n"
		"compile output:\n" + (res.stdout + res.stderr)[-2000:]
	)
	run = subprocess.run(
		[str(out_bin)],
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(30),
	)
	assert run.returncode == 2, (
		f"binary should return 2 (count of `raw > 0` items kept by drain); "
		f"got {run.returncode}.\nstdout: {run.stdout}\nstderr: {run.stderr}"
	)
