# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: `Array<T>.len()` (method-call syntax) resolves and
returns the same `Int` value as the field-access form `arr.len`.

## History (pre-fix)

Drift had `arr.len` (field-access syntax) as a compiler-builtin
magic in `type_checker.py` at the `HField` handler
(`expr.name in ("len", "cap", "capacity", "gen")`), but the
method-call form `arr.len()` had no parallel routing.  All three
receiver shapes failed with "no matching method 'len' for receiver
...":

| Receiver         | Diagnostic code           |
|------------------|---------------------------|
| `Array<T>`       | `E-AUTO-7b9868f6` (owned) |
| `&Array<T>`      | `E-AUTO-40773e95` (Ref)   |
| `&mut Array<T>`  | `E-AUTO-b9bf78ff` (RefMut)|

This blocked the canonical pattern of iterating a `MutexGuard<Array<T>>`
inside `std.concurrent.Condvar`'s waiter-list helpers.

## The fix

Two-line route added at two layers:

1. `lang/driftc/checker/call_resolver.py` — extended the Array
   intrinsic method dispatch to accept `len`/`cap`/`capacity`/`gen`
   as zero-arg, returns-Int, read-only.
2. `lang/driftc/stage2/hir_to_mir.py` — extended
   `_lower_array_intrinsic_method` to emit the same
   `M.ArrayLen` / `M.ArrayCap` / `M.ArrayGen` instructions the
   field-access lowering uses.

## What this regression test pins

Both the **method-call** form (`arr.len()`) AND the **field-access**
form (`arr.len`, no parens) return identical `Int` values across
all three receiver shapes: owned `Array<T>`, `&Array<T>`,
`&mut Array<T>`.  Run as the actual binary to confirm the values
match at runtime; checker acceptance alone is not enough since the
MIR lowering must produce the same value.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout


_PROBE_SOURCE = """\
module main;

// Probe 1: .len() and .len on owned Array<T>.
fn _owned() nothrow -> Int {
	var arr: Array<Int> = [];
	arr.push(10);
	arr.push(20);
	arr.push(30);
	val via_method = arr.len();
	val via_field = arr.len;
	if via_method != via_field { return -1; }
	return via_method;
}

// Probe 2: .len() and .len on &Array<T>.
fn _ref(arr: &Array<Int>) nothrow -> Int {
	val via_method = arr.len();
	val via_field = arr.len;
	if via_method != via_field { return -2; }
	return via_method;
}

// Probe 3: .len() and .len on &mut Array<T>.
fn _refmut(arr: &mut Array<Int>) nothrow -> Int {
	val via_method = arr.len();
	val via_field = arr.len;
	if via_method != via_field { return -3; }
	return via_method;
}

// Probe 4: .cap()/.gen() on owned Array<T> agree with field form.
fn _cap_gen() nothrow -> Int {
	var arr: Array<Int> = [];
	arr.push(7);
	if arr.cap() != arr.cap { return -4; }
	if arr.capacity() != arr.capacity { return -5; }
	if arr.gen() != arr.gen { return -6; }
	return 0;
}

// Probe 5: `val arr` (immutable binding) receiver.  K-review
// regression (2026-05-15): pre-fix, `_lower_array_intrinsic_method`
// took `_lower_addr_of_place(..., is_mut=True)` for `len`/`cap`/
// `capacity`/`gen`, rejecting val-bound receivers.  Field-access
// form never required mut; the method-call form must match.
fn _val_receiver() nothrow -> Int {
	var src: Array<Int> = [];
	src.push(100);
	src.push(200);
	val arr = move src;        // immutable val binding
	if arr.len() != 2 { return -7; }
	if arr.cap() != arr.cap { return -8; }
	if arr.gen() != arr.gen { return -9; }
	return 0;
}

pub fn main() nothrow -> Int {
	val owned = _owned();
	if owned != 3 { return owned; }
	var a: Array<Int> = [];
	a.push(1);
	a.push(2);
	val r = _ref(a);
	if r != 2 { return r; }
	val rm = _refmut(a);
	if rm != 2 { return rm; }
	val cg = _cap_gen();
	if cg != 0 { return cg; }
	val vr = _val_receiver();
	if vr != 0 { return vr; }
	return 42;
}
"""


def test_array_len_method_call_matches_field_access(tmp_path: Path) -> None:
	"""`arr.len()` and `arr.len` produce identical Int values
	across owned, `&`, and `&mut` receivers; same for
	`cap()`/`capacity()`/`gen()`.  Pins the post-fix behavior."""
	src = tmp_path / "main.drift"
	src.write_text(_PROBE_SOURCE, encoding="utf-8")
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
		"compile should succeed (Bug 1 was fixed by routing "
		"`.len()/.cap()/.capacity()/.gen()` through the same MIR "
		"lowering as the field-access form).  Got:\n"
		+ (res.stdout + res.stderr)[-2000:]
	)
	run = subprocess.run(
		[str(out_bin)],
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(30),
	)
	assert run.returncode == 42, (
		f"binary should return 42 on success; got {run.returncode}.\n"
		f"stdout: {run.stdout}\nstderr: {run.stderr}\n"
		"Negative return codes indicate a specific probe failed — "
		"see the source for the legend."
	)
