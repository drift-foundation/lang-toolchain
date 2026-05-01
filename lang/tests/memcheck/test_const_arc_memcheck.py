# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""`ConstArc<T: Frozen>` — memcheck carriers for the construction,
retain (clone), and release (drop) refcount lifecycle.

ConstArc is a thin wrapper over `conc.Arc<T>` (see
`stdlib/std/core/const_arc.drift`).  All retain / release / drop is
delegated to Arc<T>'s existing compiler-owned intrinsics
(`ARC_CLONE`, `ARC_GET`, `ARC_DESTROY`) via the inner field.  These
carriers pin that the wrapper does not introduce any new leak or
double-free shape — the structural drop of the inner Arc field on
`ConstArc<T>` scope-exit must atomically decrement the strong count
and free the allocation on last release, exactly like a bare
`Arc<T>`.

The carriers all use heap-bearing payloads (`String` or a struct
containing `String`) so any leaked allocation is visible to valgrind
under `definitely lost` or `indirectly lost` rather than being
hidden behind Arc's inline `RawBuffer` storage.

If any of these tests fail, the regression is in one of:
  - `stdlib/std/core/const_arc.drift` (constructor / methods)
  - `lang/driftc/codegen/...` Arc intrinsic dispatch (ARC_CLONE /
    ARC_GET / ARC_DESTROY) — the wrapper exercises every one of
    those via the inner field
  - structural drop of struct fields whose type is itself a
    refcount-bearing handle (`stage2/string_arc.py` and friends)

This file MUST be in the memcheck gate for any future ConstArc /
ConstShare substrate work — driver tests prove typing only; only
valgrind catches refcount imbalance.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


# Shape 1: construct + immediate drop, String payload.  The smallest
# carrier: one `const_arc` call, no clone, scope-exit drop.  String
# is heap-bearing-but-Frozen so the inner allocation must be
# released on last drop.
CONST_ARC_CONSTRUCT_DROP_SOURCE = """\
module main;

import std.core as core;
import std.core.const_arc as ca;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tvar a = ca.const_arc<type String>(move s);
\tval ref_s: &String = a.get();
\treturn ref_s.byte_length();
\t// `a` drops here -> Arc<String> field destruct -> last release
\t//   on the ArcBox<String> -> structural drop of the inner String
\t//   -> heap free.
}
"""


# Shape 2: construct + clone + drop both.  Two ConstArc handles over
# the same allocation; each drop decrements the strong count by 1.
# The first drop must NOT free (count goes 2 -> 1); the second drop
# must free (1 -> 0).  An imbalance here would surface as either a
# leak (if first drop didn't decrement) or a double-free / invalid
# read (if first drop ran the structural drop).
CONST_ARC_CLONE_DROP_SOURCE = """\
module main;

import std.core as core;
import std.core.const_arc as ca;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tvar a = ca.const_arc<type String>(move s);
\tvar b = a.clone();
\tval ref_a: &String = a.get();
\tval ref_b: &String = b.get();
\tval total = ref_a.byte_length() + ref_b.byte_length();
\treturn total;
\t// `b` drops first (LIFO) -> count 2 -> 1, no free.
\t// `a` drops next            -> count 1 -> 0, free ArcBox<String>
\t//                              + inner String allocation.
}
"""


# Shape 3: clone in a loop.  The strong count walks up to N+1 (N
# clones plus the original) and then back down to 0 as each clone
# leaves its iteration's scope and the original drops at function
# exit.  An off-by-one in the retain-vs-release pairing would either
# leak the ArcBox (count never hits 0) or double-free at one of the
# release points.
#
# Each clone is consumed in-iteration to keep the lifetime bounded;
# we only return the last `.get()` length to make sure the loop body
# isn't optimized away.
CONST_ARC_CLONE_LOOP_SOURCE = """\
module main;

import std.core as core;
import std.core.const_arc as ca;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tvar a = ca.const_arc<type String>(move s);
\tvar i = 0;
\tvar last = 0;
\twhile i < 16 {
\t\tvar c = a.clone();
\t\tval ref_c: &String = c.get();
\t\tlast = ref_c.byte_length();
\t\ti = i + 1;
\t\t// `c` drops here -> count N+1 -> N (no free until i hits 16
\t\t//   AND `a` drops at function exit).
\t}
\treturn last;
\t// `a` drops -> count 1 -> 0 -> free ArcBox + inner String.
}
"""


# Shape 4: ConstArc as a struct field, with the wrapper struct
# itself dropping in the middle of the function via early return.
# Tests that the field-drop path on a user struct routes through
# ConstArc's structural drop -> Arc<T>::Destructible::destroy
# correctly.  Inner Payload carries a String to make leaked
# allocations visible.
CONST_ARC_STRUCT_FIELD_SOURCE = """\
module main;

import std.core as core;
import std.core.const_arc as ca;
import std.format as fmt;

pub struct Payload {
\tpub name: String,
\tpub tag: Int
}

pub struct Holder {
\tpub handle: ca.ConstArc<Payload>
}

pub fn main() nothrow -> Int {
\tval p = Payload(name = fmt.format_int(700), tag = 7);
\tvar h = Holder(handle = ca.const_arc<type Payload>(p));
\tval ref_payload: &Payload = h.handle.get();
\tval len = ref_payload.name.byte_length();
\treturn len + ref_payload.tag;
\t// `h` drops here -> structural drop of `handle` field
\t//   -> ConstArc<Payload> structural drop of `inner: Arc<Payload>`
\t//   -> ARC_DESTROY -> count 1 -> 0
\t//   -> structural drop of inner Payload (releases String `name`).
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str, int]:
	"""Compile under raw stdlib and run under valgrind.  Returns
	(definitely_lost_bytes, valgrind_log_text, error_count)."""
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:1500]}"
	assert out_bin.exists(), f"[{label}] binary not produced"

	vg_log = tmp_path / f"valgrind_{label}.log"
	subprocess.run(
		["valgrind", "--tool=memcheck", "--leak-check=full",
		 "--show-leak-kinds=definite,indirect",
		 "--errors-for-leak-kinds=definite,indirect",
		 "--error-exitcode=97",
		 f"--log-file={vg_log}",
		 str(out_bin)],
		capture_output=True, text=True, timeout=120,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	err_match = re.search(r"ERROR SUMMARY: (\d+) errors", vg_output)
	error_count = int(err_match.group(1)) if err_match else 0
	return definitely_lost, vg_output, error_count


def _assert_clean(lost: int, vg_log: str, errors: int, *, label: str, hint: str) -> None:
	assert lost == 0, (
		f"[{label}] {lost} bytes definitely lost. {hint}\n"
		f"Valgrind log tail:\n{vg_log[-1500:]}"
	)
	if "Invalid read" in vg_log or "Invalid write" in vg_log or "Invalid free" in vg_log:
		raise AssertionError(
			f"[{label}] valgrind reported invalid memory access. {hint}\n"
			f"Touch points:\n"
			f"  - `stdlib/std/core/const_arc.drift`\n"
			f"  - Arc intrinsic dispatch (ARC_CLONE / ARC_GET / ARC_DESTROY)\n"
			f"  - structural drop of refcount-bearing fields\n"
			f"Valgrind error count: {errors}\n\n"
			f"Valgrind log tail:\n{vg_log[-2000:]}"
		)


def test_const_arc_construct_drop_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CONST_ARC_CONSTRUCT_DROP_SOURCE, label="const_arc_construct_drop"
	)
	_assert_clean(
		lost, vg, errors,
		label="const_arc_construct_drop",
		hint="construct + drop of `ConstArc<String>` must release the "
		     "inner allocation on last drop.",
	)


def test_const_arc_clone_drop_balanced(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CONST_ARC_CLONE_DROP_SOURCE, label="const_arc_clone_drop"
	)
	_assert_clean(
		lost, vg, errors,
		label="const_arc_clone_drop",
		hint="clone + drop must produce exactly 1 retain and 2 releases "
		     "across the two handles; an imbalance leaks or double-frees.",
	)


def test_const_arc_clone_loop_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CONST_ARC_CLONE_LOOP_SOURCE, label="const_arc_clone_loop"
	)
	_assert_clean(
		lost, vg, errors,
		label="const_arc_clone_loop",
		hint="N clones in a loop must be matched by N releases (each "
		     "clone drops at iteration end); the original's drop at "
		     "function exit takes count to 0.",
	)


def test_const_arc_struct_field_drop_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CONST_ARC_STRUCT_FIELD_SOURCE, label="const_arc_struct_field"
	)
	_assert_clean(
		lost, vg, errors,
		label="const_arc_struct_field",
		hint="structural drop of a struct that owns a `ConstArc<T>` "
		     "field must route through the wrapper's inner Arc field "
		     "and free the payload on last release.",
	)
