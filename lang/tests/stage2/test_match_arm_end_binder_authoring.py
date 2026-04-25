# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 site-1 patch 6a — arm-end binder cleanup pins.

Patch 6a migrated `_lower_match`'s arm-end drainage (legacy
`for local in reversed(arm_drop_locals): if local in _moved_locals:
continue; emit MoveOut + DropValue`) to a per-arm `M.CleanupHook`.
After this patch, binder locals like `v` in `match X { Some(v) => ... }`
are cleaned by the same `cleanup_authoring` pass as every other site-1
scope drop.  Two regressions pin the contract:

1. **Binder cleaned exactly once via authored hook path**: a `Some(v)`
   arm whose body does NOT consume `v` produces exactly one
   destructor call for the binder type.  Pre-6a (legacy drainage):
   one drop.  Post-6a (authored hook): still one drop, but emitted
   by `cleanup_authoring` instead of inline.  Pinned via destroy-
   call count in the LLVM IR for `drift_main` — same shape as the
   patch-5 / consume-via-intrinsic pins.

2. **No double-drop on partial-move arm**: a `Pair(a, b)` arm whose
   body consumes one binder while leaving the other live, where the
   arm body also moves a payload out via the per-field cleanup path
   (the patch-5 surface).  Both the arm-end CleanupHook (which
   covers binders + drop_tmps) and `match_cleanup_authoring` (which
   already authored arm-end MoveOut+DropValue for drop_tmps) must
   coexist without double-dropping the partial-move slot.  Pinned
   via destroy-call count.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]


_FIXTURE_PROLOGUE = """\
module main;

import std.core as core;

pub struct Box {
\tpub n: Int,
}

implement core.Destructible for Box {
\tpub fn destroy(var self: Box) nothrow -> Void {
\t\treturn;
\t}
}

pub variant Either {
\tSome(b: Box),
\tNone,
}
"""


_FIXTURE_BINDER_NOT_CONSUMED = """\
fn run() nothrow -> Void {
\tval e: Either = Either::Some(Box(n = 1));
\tmatch e {
\t\tSome(v) => { },
\t\tNone => { },
\t}
\treturn;
}

pub fn main() nothrow -> Int {
\trun();
\treturn 0;
}
"""


_FIXTURE_BINDER_CONSUMED = """\
fn sink(var b: Box) nothrow -> Void {
\treturn;
}

fn run() nothrow -> Void {
\tval e: Either = Either::Some(Box(n = 1));
\tmatch e {
\t\tSome(v) => { sink(move v); },
\t\tNone => { },
\t}
\treturn;
}

pub fn main() nothrow -> Int {
\trun();
\treturn 0;
}
"""


_FIXTURE_PARTIAL_MOVE = """\
pub variant Pair {
\tBoth(a: Box, b: Box),
\tNone,
}

fn sink(var b: Box) nothrow -> Void {
\treturn;
}

fn run() nothrow -> Void {
\tval p: Pair = Pair::Both(Box(n = 1), Box(n = 2));
\tmatch p {
\t\tBoth(a, b) => { sink(move a); },
\t\tNone => { },
\t}
\treturn;
}

pub fn main() nothrow -> Int {
\trun();
\treturn 0;
}
"""


_DESTROY_RE = re.compile(r'call void @"Box::std.core.Destructible::destroy"')


def _compile_to_ir(tmp_path: Path, source: str) -> str:
	src = tmp_path / "main.drift"
	src.write_text(source)
	ir_path = tmp_path / "out.ll"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(_ROOT / "stdlib"),
		 str(src), "--entry", "main::main",
		 "--emit-ir", str(ir_path)],
		cwd=_ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:500]}"
	return ir_path.read_text()


def _count_destroys_in_function(ir: str, fn_name: str) -> int:
	# match `define ... @<fn_name>(...) ... { ... }` body
	pattern = re.compile(
		r"define\s+[^@]+@" + re.escape(fn_name) + r"\([^)]*\)[^{]*\{(.*?)^}",
		re.DOTALL | re.MULTILINE,
	)
	m = pattern.search(ir)
	assert m, f"could not locate function body for {fn_name!r}"
	return len(_DESTROY_RE.findall(m.group(1)))


def test_binder_not_consumed_authored_hook_emits_exactly_one_drop(tmp_path: Path) -> None:
	"""K-requested pin (1): a binder local that would previously be
	drained via `arm_drop_locals` is now cleaned by the authored
	hook path exactly once.  Body does NOT consume `v` — verdict_at
	at the new arm-end CleanupHook sees `v` LIVE → cleanup_authoring
	emits MoveOut+DropValue for `v` → exactly ONE Box destroy call
	in `run`."""
	ir = _compile_to_ir(tmp_path, _FIXTURE_PROLOGUE + _FIXTURE_BINDER_NOT_CONSUMED)
	count = _count_destroys_in_function(ir, "run")
	assert count == 1, (
		f"patch 6a regression: an unconsumed `Some(v)` binder must be "
		f"dropped exactly once via the authored arm-end CleanupHook.  "
		f"Got {count} Box destroy calls in `run`.  Pre-6a (legacy "
		f"`arm_drop_locals` drainage) emitted exactly 1; this pin "
		f"asserts the migration produces the same count via the "
		f"authored hook path."
	)


def test_binder_consumed_authored_hook_skips_drop(tmp_path: Path) -> None:
	"""Companion: a binder consumed by a `var` parameter call is
	MOVED_OUT at the arm-end CleanupHook position.  verdict_at
	returns MUST_NOT_DROP → no authored drop in the arm.  The single
	destroy in `run` is the one inside `sink`'s frame (not in `run`'s
	IR)."""
	ir = _compile_to_ir(tmp_path, _FIXTURE_PROLOGUE + _FIXTURE_BINDER_CONSUMED)
	count = _count_destroys_in_function(ir, "run")
	assert count == 0, (
		f"patch 6a regression: a `Some(v)` binder consumed in the arm "
		f"body must NOT be drop-authored at arm-end (verdict_at sees "
		f"it MOVED_OUT).  Got {count} Box destroy calls in `run`.  "
		f"Pre-6a (legacy `arm_drop_locals` drainage with `if local in "
		f"_moved_locals: continue`) skipped here; this pin asserts the "
		f"migration preserves that semantics via lattice state."
	)


def test_partial_move_arm_no_double_drop(tmp_path: Path) -> None:
	"""K-requested pin (2): drop_tmp locals already authored by
	`match_cleanup_authoring` at the arm-end MoveOut+DropValue chain
	must NOT be dropped a second time by the new arm-end CleanupHook.
	verdict_at at the CleanupHook position sees them MOVED_OUT (the
	authored MoveOut transitions state).  Fixture exercises the
	combined surface: binder `a` consumed (no drop), binder `b` live
	(authored drop), no partial-move slot (Both binds both fields)."""
	ir = _compile_to_ir(tmp_path, _FIXTURE_PROLOGUE + _FIXTURE_PARTIAL_MOVE)
	count = _count_destroys_in_function(ir, "run")
	# Expected: 1 destroy — `b` is live at arm-end → authored drop.
	# `a` is consumed by `sink` → no drop in `run`.  No partial-move
	# slot (both fields bound).  If the arm-end CleanupHook authored
	# a SECOND drop of any drop_tmp authored by match_cleanup_authoring,
	# count would be 2+.
	assert count == 1, (
		f"patch 6a regression: arm-end CleanupHook double-drop risk.  "
		f"Expected 1 Box destroy in `run` (live binder `b`).  Got "
		f"{count}.  This indicates either (a) the new CleanupHook is "
		f"emitting drops for already-MOVED_OUT drop_tmps that "
		f"match_cleanup_authoring already drained, or (b) a binder "
		f"consumed in the arm body wasn't recognized as MOVED_OUT by "
		f"verdict_at."
	)
