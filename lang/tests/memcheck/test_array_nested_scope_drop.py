# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Nested-Array scope-exit — memcheck carrier for `Array<Array<T>>`.

**The shape under test.**  Recursive drop-helper correctness when
an `Array<Array<T>>` local goes out of scope.  The drop chain:

  outer scope-exit
    → ArrayDrop(outer)
        → outer drop helper iterates outer
            → ArrayDrop(inner_i)
                → inner drop helper iterates inner_i
                    → element drop (e.g. StringRelease, recursive
                      ArrayDrop, struct destructor, …)
                → drift_free_array(inner_i)
        → drift_free_array(outer)

If any layer drops a step (missed inner helper, missed element
drop, double-free of inner buffer), valgrind sees `definitely lost`
or `Invalid free` / `Invalid read`.

**Why this carrier exists.**  The Array audit (2026-04-26) found
no memcheck coverage for nested arrays — only a single e2e codegen
carrier (`array_pop_move_out_non_copy`) which does not run under
valgrind and exercises pop, not pure scope-exit.  This file pins
the recursive scope-exit chain at refcount/leak-check granularity
under raw stdlib, mirroring `test_site3_return_source_alias_walk.py`
in-process compile pattern.

The carrier exercises two element-type shapes:

  1. **Array<Array<Int>>** — inner element type is Copy (Int);
     inner drop helper degenerates to "free buffer".  The
     test surface is the outer recursive helper that calls
     `ArrayDrop` per inner.

  2. **Array<Array<String>>** — inner element type is non-Copy
     with refcount semantics; full recursion is exercised.
     Each inner String is heap-allocated via `fmt.format_int`
     so the leak surface is concrete (definitely-lost bytes per
     missed string release, plus the inner buffers, plus the
     outer buffer).

Each shape is exercised across multiple inner / outer lengths so
that any per-element leak surfaces as a multi-block valgrind
report (single-element shapes can mask off-by-one drops).

**Constraint.**  No Array semantic changes.  No cleanup unification
patch — this carrier only pins the existing path.  If either test
fails on the current compiler, freeze the failing state and report
LANGUAGE_BUG (per AGENTS.md regression-first).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]


# Array<Array<Int>>: inner element type is Copy.  Inner drop helper
# only needs to free the inner buffer; no per-element work.  The
# outer drop helper's per-element call to ArrayDrop on each inner
# is the load-bearing chain.
#
# Shape: outer of length 4, inner lengths 0/1/2/3 — covers the
# empty-inner case (helper must not deref a null/empty data pointer)
# and short/medium-inner cases.
ARRAY_ARRAY_INT_SOURCE = """\
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

fn use_nested() nothrow -> Int {
\tvar outer: Array<Array<Int>> = [];
\touter.push(build_inner(0));
\touter.push(build_inner(1));
\touter.push(build_inner(2));
\touter.push(build_inner(3));
\tvar total = 0;
\tvar i = 0;
\twhile i < outer.len {
\t\tval inner_ref = &outer[i];
\t\ttotal = total + inner_ref.len;
\t\ti = i + 1;
\t}
\treturn total;
\t// outer (Array<Array<Int>>) drops here.  Each inner array's
\t// buffer must be freed by the recursive outer helper.
}

pub fn main() nothrow -> Int {
\tval a = use_nested();
\tval b = use_nested();
\tval c = use_nested();
\treturn a + b + c;
}
"""


# Array<Array<String>>: inner element type is non-Copy with
# refcount; each String is heap-allocated.  Full recursion:
# outer helper iterates → ArrayDrop on each inner → inner helper
# iterates → StringRelease on each String → free inner buffer →
# free outer buffer.
#
# Shape: outer of length 3, inner lengths 1/2/3.  Strings come from
# fmt.format_int so each allocates its own heap buffer (no string
# interning shortcuts).  Total heap allocations per call:
#   3 outer slots * sizeof(Array<String>)  → 1 outer buffer
#   3 inner buffers (lengths 1, 2, 3)
#   1+2+3 = 6 String allocations
ARRAY_ARRAY_STRING_SOURCE = """\
module main;

import std.format as fmt;

fn build_string_inner(n: Int) nothrow -> Array<String> {
\tvar inner: Array<String> = [];
\tvar i = 0;
\twhile i < n {
\t\tinner.push(fmt.format_int(i + 100));
\t\ti = i + 1;
\t}
\treturn move inner;
}

fn use_nested_strings() nothrow -> Int {
\tvar outer: Array<Array<String>> = [];
\touter.push(build_string_inner(1));
\touter.push(build_string_inner(2));
\touter.push(build_string_inner(3));
\tvar total = 0;
\tvar i = 0;
\twhile i < outer.len {
\t\tval inner_ref = &outer[i];
\t\tvar j = 0;
\t\twhile j < inner_ref.len {
\t\t\tval s_ref = &inner_ref[j];
\t\t\ttotal = total + s_ref.byte_length();
\t\t\tj = j + 1;
\t\t}
\t\ti = i + 1;
\t}
\treturn total;
\t// outer drops here.  Each inner Array<String> drops, releasing
\t// each contained String, then freeing the inner buffer.  Then
\t// the outer buffer is freed.  Any missed step is visible as
\t// definitely-lost bytes.
}

pub fn main() nothrow -> Int {
\tval a = use_nested_strings();
\tval b = use_nested_strings();
\treturn a + b;
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
		f"[{label}] LANGUAGE_BUG: nested-Array scope-exit leak — "
		f"{lost} bytes definitely lost.\n"
		f"Expected symptom if the recursive Array<Array<T>> drop chain "
		f"misses a layer: {broken_state_hint}\n"
		f"Touch points: `_ensure_array_drop_helper` (LLVM), "
		f"`ArrayDrop` lowering, scope-exit authoring in "
		f"`cleanup_authoring.py`, the overwrite-path "
		f"array overwrite drop in `overwrite_cleanup.py` (moved out of "
		f"string_arc in Slice B1, 2026-07-20).\n\n"
		f"Valgrind log tail:\n{vg_log[-1500:]}"
	)
	if "Invalid read" in vg_log or "Invalid write" in vg_log or "Invalid free" in vg_log:
		raise AssertionError(
			f"[{label}] valgrind reported invalid memory access — "
			f"likely double-free of an inner buffer or use-after-free "
			f"in the recursive drop helper.\n\n{vg_log[-1500:]}"
		)


def test_array_array_int_scope_drop_no_leak(tmp_path: Path) -> None:
	"""Array<Array<Int>> — exercises the outer recursive drop helper
	calling ArrayDrop on each inner.  Inner element type is Copy so
	the inner helper degenerates to "free buffer"; this test pins
	the outer iteration + per-inner ArrayDrop chain.

	Inner-length 0 case verifies the empty-inner code path (helper
	must not deref a null/empty data pointer).
	"""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, ARRAY_ARRAY_INT_SOURCE, label="array_array_int"
	)
	_assert_valgrind_clean(
		lost, vg_log,
		label="array_array_int",
		broken_state_hint=(
			"outer recursive helper skipped a per-inner ArrayDrop call → "
			"inner buffer leaked (~16-32 bytes per inner, len-dependent); "
			"OR helper dereferenced empty inner data pointer → segfault."
		),
	)


def test_array_array_string_scope_drop_no_leak(tmp_path: Path) -> None:
	"""Array<Array<String>> — full recursion: outer helper iterates,
	calls ArrayDrop on each inner; inner helper iterates, calls
	StringRelease on each element; both buffers freed.

	Each String is heap-allocated via `fmt.format_int`, so any missed
	StringRelease shows up as definitely-lost bytes proportional to
	the un-released String count.
	"""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, ARRAY_ARRAY_STRING_SOURCE, label="array_array_string"
	)
	_assert_valgrind_clean(
		lost, vg_log,
		label="array_array_string",
		broken_state_hint=(
			"recursive drop chain missed a layer: most likely the inner "
			"helper failed to call StringRelease per element (each missed "
			"release = ~24 bytes definitely lost), OR the inner buffer "
			"was leaked while strings were correctly released."
		),
	)
