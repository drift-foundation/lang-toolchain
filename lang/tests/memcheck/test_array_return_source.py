# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Array return-source — memcheck carrier proving that the natural
`return move arr;` pattern is leak-free without depending on the
site-3 alias-walk's `array_locals` branch.

**The claim under test.**  Arrays are non-Copy; returning one
requires explicit `return move arr;`, which lowers to
`MoveOut(t, arr) + Return(t)`.  The Phase 4 Return-as-move lattice
recognises this shape and transitions `arr` to `MOVED_OUT` at the
`MoveOut` index — independently of the alias-walk's `array_locals`
branch (historically `string_arc.py::Return-terminator`; the decision
now lives in the plan authorities)
(historically string_arc.py:1486-1491).

If that claim holds, the natural shape leaves the +1 with the
caller and no function-exit cleanup runs on the source local.  If
the claim is wrong (e.g. some Array return path slips through the
explicit-move lowering and the alias-walk IS load-bearing), this
carrier fails with definitely-lost or Invalid-read symptoms.

**Why this carrier exists.**  The Array audit (2026-04-26) found
that `test_site3_return_source_alias_walk.py` deliberately pins
only the String shapes and explicitly leaves Array as a future
focus pin (lines 49-61 of that file).  The String alias-walk
branch is load-bearing because of the late-rewrite `StringRetain`
synthesis; the Array half of that alias-walk skip had no analogous
synthesis, was concluded structurally dead by the audit, and was
REMOVED in the review-closure round of string-arc-endgame-array-
sweep (2026-07-19) — the alias walk is String-only now, and
scope-exit array drops are cleanup_authoring's (Return-as-move at
the ledger for returned sources).  This carrier keeps the
valgrind-level evidence for the returned-Array shapes.

The carrier exercises three element-type shapes:

  1. **Array<String>** — element has refcount semantics; per-call
     missed release is ~24 bytes per element.

  2. **Array<Array<Int>>** — element is itself an Array; per-call
     missed inner ArrayDrop leaks the inner buffer.

  3. **Array<Wrap> where Wrap = { s: String }** — element is a
     destructible struct (composite drop chain through the struct
     destructor); per-call missed struct drop leaks the contained
     String plus any field overhead.

Each producer is invoked multiple times so per-call leaks surface
as multi-block valgrind reports.

**Constraint.**  No Array semantic changes; no cleanup unification
patch.  If any test fails on the current compiler, freeze and
report LANGUAGE_BUG (per AGENTS.md regression-first).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]


# Shape 1: Array<String> returned via explicit move.
ARRAY_STRING_RETURN_MOVE_SOURCE = """\
module main;

import std.format as fmt;

fn build_strings(n: Int) nothrow -> Array<String> {
\tvar xs: Array<String> = [];
\tvar i = 0;
\twhile i < n {
\t\txs.push(fmt.format_int(i + 200));
\t\ti = i + 1;
\t}
\treturn move xs;
}

fn consume(xs: Array<String>) nothrow -> Int {
\tvar total = 0;
\tvar i = 0;
\twhile i < xs.len {
\t\tval s_ref = &xs[i];
\t\ttotal = total + s_ref.byte_length();
\t\ti = i + 1;
\t}
\treturn total;
\t// xs drops here on consume's scope-exit.
}

pub fn main() nothrow -> Int {
\tval a = consume(build_strings(1));
\tval b = consume(build_strings(3));
\tval c = consume(build_strings(5));
\treturn a + b + c;
}
"""


# Shape 2: Array<Array<Int>> returned via explicit move.  Tests the
# recursive container element type on the producer's return path.
ARRAY_ARRAY_INT_RETURN_MOVE_SOURCE = """\
module main;

fn build_inner(n: Int) nothrow -> Array<Int> {
\tvar inner: Array<Int> = [];
\tvar i = 0;
\twhile i < n {
\t\tinner.push(i);
\t\ti = i + 1;
\t}
\treturn move inner;
}

fn build_nested(outer_len: Int) nothrow -> Array<Array<Int>> {
\tvar outer: Array<Array<Int>> = [];
\tvar i = 0;
\twhile i < outer_len {
\t\touter.push(build_inner(i + 1));
\t\ti = i + 1;
\t}
\treturn move outer;
}

fn consume_nested(outer: Array<Array<Int>>) nothrow -> Int {
\tvar total = 0;
\tvar i = 0;
\twhile i < outer.len {
\t\tval inner_ref = &outer[i];
\t\ttotal = total + inner_ref.len;
\t\ti = i + 1;
\t}
\treturn total;
\t// outer drops here, recursing through each inner.
}

pub fn main() nothrow -> Int {
\tval a = consume_nested(build_nested(2));
\tval b = consume_nested(build_nested(3));
\tval c = consume_nested(build_nested(4));
\treturn a + b + c;
}
"""


# Shape 3: Array<Wrap> where Wrap is a destructible struct (contains
# a String field).  Each element drop runs the struct destructor,
# which releases the String.  Producer returns via explicit move.
ARRAY_DESTRUCTIBLE_RETURN_MOVE_SOURCE = """\
module main;

import std.format as fmt;

struct Wrap {
\ts: String,
\ttag: Int
}

fn build_wraps(n: Int) nothrow -> Array<Wrap> {
\tvar xs: Array<Wrap> = [];
\tvar i = 0;
\twhile i < n {
\t\tvar item = Wrap(s = fmt.format_int(i + 300), tag = i);
\t\txs.push(move item);
\t\ti = i + 1;
\t}
\treturn move xs;
}

fn consume_wraps(xs: Array<Wrap>) nothrow -> Int {
\tvar total = 0;
\tvar i = 0;
\twhile i < xs.len {
\t\tval w_ref = &xs[i];
\t\ttotal = total + w_ref.s.byte_length() + w_ref.tag;
\t\ti = i + 1;
\t}
\treturn total;
\t// xs drops here, calling Wrap's destructor on each element to
\t// release each contained String.
}

pub fn main() nothrow -> Int {
\tval a = consume_wraps(build_wraps(1));
\tval b = consume_wraps(build_wraps(2));
\tval c = consume_wraps(build_wraps(3));
\treturn a + b + c;
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str]:
	"""Compile under raw stdlib and run under valgrind.  Returns
	(definitely_lost_bytes, valgrind_log_text)."""
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
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=120,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	return definitely_lost, vg_output


def _assert_valgrind_clean(lost: int, vg_log: str, *, label: str, broken_state_hint: str) -> None:
	"""Assert zero definitely-lost bytes AND no valgrind errors."""
	assert lost == 0, (
		f"[{label}] LANGUAGE_BUG: Array return-source leak — "
		f"{lost} bytes definitely lost.\n"
		f"Expected symptom if `return move arr;` lowering / Phase 4 "
		f"Return-as-move regresses for Arrays: {broken_state_hint}\n"
		f"Touch points: HIR→MIR explicit-move lowering, "
		f"`MoveOut + Return` shape recognition in the lattice, "
		f"scope-exit cleanup in `cleanup_authoring.py` (the sole "
		f"array authority since the sweep's B-U deletion).\n\n"
		f"Valgrind log tail:\n{vg_log[-1500:]}"
	)
	if "Invalid read" in vg_log or "Invalid write" in vg_log or "Invalid free" in vg_log:
		raise AssertionError(
			f"[{label}] valgrind reported invalid memory access — "
			f"likely double-release of the moved-out Array (function-exit "
			f"cleanup fired on a local that was already moved into the "
			f"return value).\n\n{vg_log[-1500:]}"
		)


def test_array_string_return_move_no_leak(tmp_path: Path) -> None:
	"""Array<String> returned via explicit `return move xs;` —
	exercises the canonical refcount-element return shape across
	multiple call sites and inner lengths.

	If the residual `array_locals` branch in
	the historical Return-terminator branch were load-bearing here, removing
	it would cause function-exit cleanup to run on `xs` AFTER the
	caller acquired ownership → double-release of every contained
	String.  This test demonstrates that the natural shape does NOT
	depend on that branch — Phase 4 Return-as-move handles it via
	the explicit `MoveOut`.
	"""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, ARRAY_STRING_RETURN_MOVE_SOURCE, label="array_string_return_move"
	)
	_assert_valgrind_clean(
		lost, vg_log,
		label="array_string_return_move",
		broken_state_hint=(
			"function-exit cleanup released `xs` despite the explicit "
			"MoveOut; each contained String double-released → caller's "
			"drop fires on freed buffers → ~24 bytes per element "
			"definitely lost AND/OR Invalid read."
		),
	)


def test_array_array_int_return_move_no_leak(tmp_path: Path) -> None:
	"""Array<Array<Int>> returned via explicit `return move outer;`.

	Tests the recursive-element return shape: producer constructs
	the outer, populates with inner arrays via push (which moves
	inner ownership into the outer's storage), then returns the
	outer via explicit move.  Function-exit must NOT run any drop on
	the outer or its contents.
	"""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, ARRAY_ARRAY_INT_RETURN_MOVE_SOURCE, label="array_array_int_return_move"
	)
	_assert_valgrind_clean(
		lost, vg_log,
		label="array_array_int_return_move",
		broken_state_hint=(
			"function-exit ran a drop on the moved-out outer → the "
			"recursive helper double-freed every inner buffer → consumer's "
			"drop hits already-freed memory → Invalid free / definitely "
			"lost."
		),
	)


def test_array_destructible_return_move_no_leak(tmp_path: Path) -> None:
	"""Array<Wrap> where Wrap is a destructible struct, returned via
	explicit `return move xs;`.

	This is the most demanding shape: per-element drop runs the
	struct destructor, which itself releases a String.  If the
	function-exit cleanup runs on the moved-out Array, every element's
	struct destructor fires twice → every contained String
	double-released.
	"""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, ARRAY_DESTRUCTIBLE_RETURN_MOVE_SOURCE, label="array_destructible_return_move"
	)
	_assert_valgrind_clean(
		lost, vg_log,
		label="array_destructible_return_move",
		broken_state_hint=(
			"function-exit cleanup ran the per-element struct destructor "
			"on the moved-out Array → each Wrap.s String double-released → "
			"consumer's drop hits freed buffers → ~24 bytes per element "
			"definitely lost AND/OR Invalid read."
		),
	)
