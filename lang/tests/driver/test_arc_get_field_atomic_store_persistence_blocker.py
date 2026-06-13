# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pin: `Arc<Box>::get().struct_field.atomic_method(...)` chain
silently drops the store — the AtomicBool field is COPIED during
the chained projection, and the `&self`-method (`store`) mutates
the copy.

Discovered 2026-05-15 during Condvar e2e #5 (close-before-wait).
Classified LANGUAGE_BUG: this is not a memory-model issue — the
load following the store happens in the same VT, the same call
frame, with no concurrency.  The Acquire load reads the stale
pre-store value because the Release store went to a different
AtomicBool instance than the one being loaded.

## Behavioral evidence

Identical store/load sequence on `Box { flag: AtomicBool }`:

  | Receiver shape                               | Persists? |
  |----------------------------------------------|-----------|
  | direct local: `var b = Box(...); b.flag.store(...)` | ✓ |
  | named ref:   `val r = b_arc.get(); r.flag.store(...)` | ✓ |
  | helper fn:   `fn h(b: &Box) { b.flag.store(...) }` | ✓ |
  | **inline:    `b_arc.get().flag.store(...)`**       | **✗** |

The inline-chained form drops the effect.  Bisected against the
`Arc<T>::get(self: &Arc<T>) -> &T` intrinsic that returns `&T`:
the returned reference is not being treated as a place when the
NEXT projection (`.flag`) is followed by an `&self`-method call.

## Why it matters

`std.concurrent.Condvar`'s entire wait/signal/close path is
written in this exact shape:

    cv.state.get().closed.store(true, ...)         // close()
    cv.state.get().closed.load(Acquire)            // _wait_inner fast-path
    waiter.get().active.compare_exchange(...)      // CAS-before-unpark

Every site is `Arc::get().struct_field.atomic_method(...)`.
Without this fix, the entire Condvar slice silently miscompiles
to no-ops on the shared atomics.  Condvar e2e #5 (close-before-
wait) hangs because the fast-path closed-check at the top of
`_wait_inner` reads `closed=false` even after `close()` set it
to `true` in the same VT.

## What this pin tests

Two assertions over `Box { flag: AtomicBool }` accessed via
`Arc<Box>`:

1. **Named-intermediate baseline (currently works)**: bind the
   `Arc::get()` to a local first, then mutate the field.  Must
   load `true` after the store.

2. **Inline-chained shape (currently fails)**: same logical
   operation but inlined as `b_arc.get().flag.store(...)`.  Must
   ALSO load `true` after the store.

Both assertions are same-VT, single-threaded, no concurrency —
this keeps the diagnosis squarely in place-projection / receiver
lowering, not atomic memory semantics.

When the fix lands, the inline form persists; this test flips
from "expects inline path to fail" to fully-green.

## Suspected fix location

`Arc::get().field.method(&self)` should lower through a real
place/ref to the field, not a value copy.  Likely overlaps with
the earlier `arc.get().field.method()` autoborrow fix
(0.31.23 in MEMORY.md, `lambda_alias_resolution`), but THIS one
is mutation-observable rather than diagnostic-only.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout


_INLINE_REPRO = """\
module m;
import std.core as core;
import std.concurrent as conc;
import lang.atomic as atomic;

struct Box { flag: conc.AtomicBool }

pub fn main() nothrow -> Int {
	val b_arc = core.arc(Box(flag = conc.atomic_bool(false)));
	val pre = b_arc.get().flag.load(atomic.MemoryOrder::Acquire());
	if pre { return 90; }  // pre-store must be false

	// THE FAILING SHAPE — inline chained store through Arc::get().
	b_arc.get().flag.store(true, atomic.MemoryOrder::Release());

	val post = b_arc.get().flag.load(atomic.MemoryOrder::Acquire());
	if post { return 0; }
	return 91;  // store dropped — bug reproduces
}
"""


_NAMED_REF_REPRO = """\
module m;
import std.core as core;
import std.concurrent as conc;
import lang.atomic as atomic;

struct Box { flag: conc.AtomicBool }

pub fn main() nothrow -> Int {
	val b_arc = core.arc(Box(flag = conc.atomic_bool(false)));
	val b_ref = b_arc.get();  // bind &Box to a local FIRST
	val pre = b_ref.flag.load(atomic.MemoryOrder::Acquire());
	if pre { return 90; }

	// Working baseline: store on the named &Box.
	b_ref.flag.store(true, atomic.MemoryOrder::Release());

	val post = b_ref.flag.load(atomic.MemoryOrder::Acquire());
	if post { return 0; }
	return 91;
}
"""


def _compile_and_run(src_text: str, tmp_path: Path) -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess | None]:
	src = tmp_path / "main.drift"
	src.write_text(src_text, encoding="utf-8")
	out_bin = tmp_path / "out"
	cmd = [sys.executable, "-m", "lang.driftc", "--dev", "--entry", "m::main", str(src), "-o", str(out_bin)]
	root = stdlib_root()
	if root:
		cmd.insert(-2, "--stdlib-root")
		cmd.insert(-2, str(root))
	cres = subprocess.run(cmd, cwd=Path(__file__).parents[3], capture_output=True, text=True, timeout=sanitizer_timeout(120))
	if cres.returncode != 0:
		return cres, None
	rres = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	return cres, rres


def test_named_intermediate_atomic_store_via_arc_get_persists(tmp_path: Path) -> None:
	"""Working baseline: `val r = arc.get(); r.flag.store(...)` then
	a load of `r.flag` returns the stored value.  This MUST keep
	working — if it stops, the workaround folks have been using
	throughout the stdlib is broken too."""
	cres, rres = _compile_and_run(_NAMED_REF_REPRO, tmp_path)
	assert cres.returncode == 0, (
		"named-intermediate baseline should compile cleanly.  "
		"compile output:\n" + (cres.stdout + cres.stderr)[-2000:]
	)
	assert rres is not None
	assert rres.returncode == 0, (
		f"named-intermediate baseline: store must persist; binary returned "
		f"{rres.returncode} (90=pre-load not false, 91=post-load not true).\n"
		f"stdout: {rres.stdout}\nstderr: {rres.stderr}"
	)


def test_inline_arc_get_field_atomic_store_persists(tmp_path: Path) -> None:
	"""Regression pin (post-fix, 2026-05-15): inline
	`b_arc.get().flag.store(true, Release)` followed by
	`b_arc.get().flag.load(Acquire)` returns `true`.

	## History (pre-fix bug)

	The type checker's autoborrow-on-rvalue-call branch
	(`type_checker.py:8704`) wraps the receiver in
	`HBorrow(subject=HField(HCall, "flag"), allow_rvalue=True)`.
	The wrapping happens AFTER stage1's `borrow_materialize` ran,
	so `_split_lift_place_chain` never saw it.  HIR→MIR's
	`_visit_expr_HBorrow` then took the whole-expr-materialization
	branch — copying the AtomicBool field VALUE into a temp local
	and returning `&temp`.  Subsequent `.store(...)` mutated the
	copy.

	## Fix

	`hir_to_mir.py::_lift_rvalue_ref_base_for_borrow` detects the
	`HBorrow(HField(...HCall_returning_ref, field, ...))` shape and
	lifts the call's `&T` result, then walks the field chain via
	`AddrOfField` to produce the leaf `&Field` pointer directly —
	no value copy.  Pinned end-to-end (compile + run + same-VT
	load assertion).
	"""
	cres, rres = _compile_and_run(_INLINE_REPRO, tmp_path)
	assert cres.returncode == 0, (
		"inline-chained shape should compile cleanly (the bug is at "
		"codegen / lowering, not at typecheck).\n"
		+ (cres.stdout + cres.stderr)[-2000:]
	)
	assert rres is not None
	assert rres.returncode == 0, (
		f"inline-chained shape: store must persist (post-store load must "
		f"return true).  Returned {rres.returncode} (91 = store dropped, "
		f"90 = pre-load was unexpectedly true).\n"
		f"stdout: {rres.stdout}\nstderr: {rres.stderr}"
	)
