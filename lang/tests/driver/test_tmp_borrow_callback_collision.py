# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: `&"literal"` borrow followed by
`core.callbackN(|...|=>{...})` with N>=1 in the same function
triggers
`cannot borrow from moved or uninitialized '__tmp_borrowN'
[E-AUTO-e57d22a5]` at the literal-borrow site.

Reported by app team in `compiler-findings.md` #4
(2026-05-17), deterministic repro pinned in their reply at
`/tmp/sgw-repro4/REPRO.drift` after a 12-row bisect table.

**Trigger recipe** (both clauses required, in source order,
same function body):

  1. `&"literal"` into a method/function call (anonymous
     `__tmp_borrowN` temp materialization).
  2. `core.callbackN(|...|=>{...})` construction with
     `N >= 1` later in the same function.

**Bisect axes** (from app team's 12-row table):

  - Borrow source matters: `&"literal"` fails; `&named_local`
    / `&parameter` are fine.
  - Closure shape matters: `callbackN` with `N >= 1` fails;
    `callback0` and plain function calls are fine.
  - Closure captures don't matter: with/without
    `captures(move ...)`, both trigger.
  - Closure param type doesn't matter: Int, Variant, generic
    all trigger.
  - Closure throw mode doesn't matter: `callback1` and
    `callback_throw1` both trigger.
  - Order matters: borrow-then-closure fails;
    closure-then-borrow is fine.

**App-team hypothesis** (~repro reply, K's read):

The `callbackN`-with-`N>=1` constructor synthesizes a typed-
erasure thunk that takes an `(args, env)` tuple.  Codegen for
the thunk introduces an SSA temp slot index that COLLIDES
with `__tmp_borrowN` from the earlier literal borrow.  The
borrow-checker's flow analysis then sees the literal's storage
as reused / no-longer-live at the borrow site and emits
E-AUTO-e57d22a5.

`callback0` is exempt because its thunk has no args -- no
temp slot to collide.

`&named_local` is exempt because the borrow points at an
addressable place with its own SSA slot, not an anonymous
`__tmp_borrowN`.

This test pins the FAILURE shape (V1) and each axis the
bisect identified as either-triggering (V6 callback2, V7
callback_throw1) or exempt (V2 let-bind, V3 callback0, V4
swap order, V5 named-local borrow).  Post-fix: V1/V6/V7
must compile + run; V2-V5 must keep compiling + running.

Repro source: `/tmp/sgw-repro4/REPRO.drift` + variants v10a-v10k.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(
	tmp_path: Path,
	module_name: str,
	source: str,
) -> tuple[int, str, int, str]:
	"""Compile + execute.  Returns (cc_rc, cc_err, run_rc, run_stderr)."""
	src_path = tmp_path / f"{module_name}.drift"
	src_path.write_text(source)
	out_bin = tmp_path / f"{module_name}_bin"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--stdlib-root", str(ROOT / "stdlib"),
		"--entry", f"{module_name}::main",
		str(src_path),
		"-o", str(out_bin),
	]
	cc = subprocess.run(
		cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60,
	)
	if cc.returncode != 0 or not out_bin.exists():
		return cc.returncode, cc.stderr, -1, ""
	run = subprocess.run(
		[str(out_bin)], capture_output=True, text=True, timeout=10,
	)
	return cc.returncode, cc.stderr, run.returncode, run.stderr


# ─── V1: THE BUG -- minimum repro ──────────────────────────────────

_V1_LITERAL_BORROW_THEN_CALLBACK1 = """\
module v1;

import std.core as core;
import std.json as json;

fn use_node(node: &json.JsonNode) nothrow -> Void {
	val _ = node.get(&"key");
	val _: core.Callback1<Int, Void> = core.callback1(|i: Int| => {});
}

pub fn main() nothrow -> Int {
	val obj = json.new_object();
	val node: json.JsonNode = obj.to_node();
	use_node(&node);
	return 0;
}
"""


def test_v1_literal_borrow_then_callback1_compiles(tmp_path: Path) -> None:
	"""THE BUG: literal borrow then `callback1` MUST compile.
	Pre-fix fails with E-AUTO-e57d22a5 on the `&"key"` borrow."""
	cc_rc, cc_err, run_rc, _ = _compile_and_run(tmp_path, "v1", _V1_LITERAL_BORROW_THEN_CALLBACK1)
	assert "E-AUTO-e57d22a5" not in cc_err, (
		f"V1: literal borrow + callback1 still emits "
		f"E-AUTO-e57d22a5 (`cannot borrow from moved or "
		f"uninitialized '__tmp_borrowN'`).  See app-team "
		f"compiler-findings.md #4 + bisect table at "
		f"/tmp/sgw-repro4/.  Hypothesis: callbackN(N>=1) "
		f"typed-erasure thunk synthesis allocates an SSA "
		f"temp that collides with `__tmp_borrowN` from the "
		f"earlier literal borrow.\n\n{cc_err[-1500:]}"
	)
	assert cc_rc == 0, (
		f"V1 compile failed but NOT with the known E-AUTO-e57d22a5 "
		f"shape -- something else is wrong:\n{cc_err[-1500:]}"
	)
	assert run_rc == 0, f"V1 binary returned {run_rc}, expected 0"


# ─── V2: let-bind workaround -- positive control ────────────────────

_V2_LETBIND_WORKAROUND = """\
module v2;

import std.core as core;
import std.json as json;

fn use_node(node: &json.JsonNode) nothrow -> Void {
	val key: String = "key";
	val _ = node.get(&key);
	val _: core.Callback1<Int, Void> = core.callback1(|i: Int| => {});
}

pub fn main() nothrow -> Int {
	val obj = json.new_object();
	val node: json.JsonNode = obj.to_node();
	use_node(&node);
	return 0;
}
"""


def test_v2_letbind_workaround_compiles(tmp_path: Path) -> None:
	"""App team's workaround: bind the literal to a `val` first,
	then borrow `&key`.  Worked pre-fix; must keep working."""
	cc_rc, cc_err, run_rc, _ = _compile_and_run(tmp_path, "v2", _V2_LETBIND_WORKAROUND)
	assert cc_rc == 0, f"V2 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 0, f"V2 binary returned {run_rc}, expected 0"


# ─── V3: callback0 -- exempt per bisect (no thunk args) ─────────────

_V3_CALLBACK0_NO_COLLISION = """\
module v3;

import std.core as core;
import std.json as json;

fn use_node(node: &json.JsonNode) nothrow -> Void {
	val _ = node.get(&"key");
	val _: core.Callback0<Void> = core.callback0(|| => {});
}

pub fn main() nothrow -> Int {
	val obj = json.new_object();
	val node: json.JsonNode = obj.to_node();
	use_node(&node);
	return 0;
}
"""


def test_v3_callback0_no_collision(tmp_path: Path) -> None:
	"""`callback0` (zero-arg lambda) does NOT trigger the
	collision -- the thunk has no args, no temp slot to
	collide with.  Pinned so a fix doesn't accidentally
	break the callback0 path."""
	cc_rc, cc_err, run_rc, _ = _compile_and_run(tmp_path, "v3", _V3_CALLBACK0_NO_COLLISION)
	assert cc_rc == 0, f"V3 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 0, f"V3 binary returned {run_rc}, expected 0"


# ─── V4: swap order -- closure BEFORE borrow ────────────────────────

_V4_CLOSURE_BEFORE_BORROW = """\
module v4;

import std.core as core;
import std.json as json;

fn use_node(node: &json.JsonNode) nothrow -> Void {
	val _: core.Callback1<Int, Void> = core.callback1(|i: Int| => {});
	val _ = node.get(&"key");
}

pub fn main() nothrow -> Int {
	val obj = json.new_object();
	val node: json.JsonNode = obj.to_node();
	use_node(&node);
	return 0;
}
"""


def test_v4_closure_before_borrow_no_collision(tmp_path: Path) -> None:
	"""Closure-then-borrow is fine.  The bisect axis "order
	matters" means a fix that reorders or unifies temp
	allocation shouldn't accidentally break the
	closure-first shape."""
	cc_rc, cc_err, run_rc, _ = _compile_and_run(tmp_path, "v4", _V4_CLOSURE_BEFORE_BORROW)
	assert cc_rc == 0, f"V4 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 0, f"V4 binary returned {run_rc}, expected 0"


# ─── V5: borrow of named local -- exempt per bisect ─────────────────

_V5_NAMED_LOCAL_BORROW = """\
module v5;

import std.core as core;
import std.json as json;

fn use_node(node: &json.JsonNode) nothrow -> Void {
	val key: String = "key";
	val _ = node.get(&key);
	val _: core.Callback1<Int, Void> = core.callback1(|i: Int| => {});
}

pub fn main() nothrow -> Int {
	val obj = json.new_object();
	val node: json.JsonNode = obj.to_node();
	use_node(&node);
	return 0;
}
"""


def test_v5_named_local_borrow_no_collision(tmp_path: Path) -> None:
	"""`&named_local` (where the local has its own SSA slot)
	doesn't trigger -- only `&"literal"` (which materializes
	as anonymous `__tmp_borrowN`) does.  Pinned so future
	collision fixes don't accidentally affect this path."""
	cc_rc, cc_err, run_rc, _ = _compile_and_run(tmp_path, "v5", _V5_NAMED_LOCAL_BORROW)
	assert cc_rc == 0, f"V5 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 0, f"V5 binary returned {run_rc}, expected 0"


# ─── V6: callback2 also triggers ────────────────────────────────────

_V6_CALLBACK2 = """\
module v6;

import std.core as core;
import std.json as json;

fn use_node(node: &json.JsonNode) nothrow -> Void {
	val _ = node.get(&"key");
	val _: core.Callback2<Int, Int, Void> = core.callback2(|a: Int, b: Int| => {});
}

pub fn main() nothrow -> Int {
	val obj = json.new_object();
	val node: json.JsonNode = obj.to_node();
	use_node(&node);
	return 0;
}
"""


def test_v6_callback2_also_triggers(tmp_path: Path) -> None:
	"""Per bisect: `callback2` also triggers the collision (any
	`callbackN` with N>=1).  Same root cause; pin so the fix
	closes both."""
	cc_rc, cc_err, run_rc, _ = _compile_and_run(tmp_path, "v6", _V6_CALLBACK2)
	assert "E-AUTO-e57d22a5" not in cc_err, (
		f"V6 (callback2): E-AUTO-e57d22a5 still fires.\n"
		f"{cc_err[-1500:]}"
	)
	assert cc_rc == 0, f"V6 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 0, f"V6 binary returned {run_rc}, expected 0"


# ─── V7: callback_throw1 also triggers ──────────────────────────────

_V7_CALLBACK_THROW1 = """\
module v7;

import std.core as core;
import std.json as json;

fn use_node(node: &json.JsonNode) -> Void {
	val _ = node.get(&"key");
	val _: core.CallbackThrow1<Int, Void> = core.callback_throw1(|i: Int| => {});
}

pub fn main() nothrow -> Int {
	try {
		val obj = json.new_object();
		val node: json.JsonNode = obj.to_node();
		use_node(&node);
		return 0;
	} catch any { return 99; }
}
"""


def test_v7_callback_throw1_also_triggers(tmp_path: Path) -> None:
	"""Per bisect: throwing callback variant also triggers.
	Closure throw mode doesn't matter -- pin so the fix
	closes both flavors."""
	cc_rc, cc_err, run_rc, _ = _compile_and_run(tmp_path, "v7", _V7_CALLBACK_THROW1)
	assert "E-AUTO-e57d22a5" not in cc_err, (
		f"V7 (callback_throw1): E-AUTO-e57d22a5 still fires.\n"
		f"{cc_err[-1500:]}"
	)
	assert cc_rc == 0, f"V7 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 0, f"V7 binary returned {run_rc}, expected 0"
