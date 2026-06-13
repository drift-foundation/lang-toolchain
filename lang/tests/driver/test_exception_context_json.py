# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 2 of the DV→JSON diagnostics-context migration —
`<error>.context.encode_compact()` and `^`-capture frame projection
to canonical context JSON array.

Slice 2 lands:

  - `<Error>.context` field-access returns an opaque
    `core.ErrorContextView` whose `encode_compact() -> String`
    method yields the canonical context JSON array document.
  - Throw-side: each function on the unwind path that has
    `^`-captured locals appends a frame JSON object via the
    runtime `drift_error_append_context_frame` helper (Phase 1
    substrate).
  - Frame shape: `{"fn":"<fqn>","locals":{<k>:<v>,...}}`.
  - Frame ordering: innermost-first (matches unwind observation
    — the function that threw appears first; outer functions
    follow as the unwind propagates).
  - Recursive calls contribute repeated frame objects — NOT
    merged into a single frame.
  - Locals inside a frame are lex-utf8-sorted at compile time
    for determinism.

Out of scope for Slice 2:

  - JsonCursor / `e.context.get(...)` typed lookup (Slice 4).
  - DV public removal (Slice 5).

Slice 2 was ADDITIVE alongside the legacy `e.captures[...]` DV
path; Slice 7a/7b retired that DV path (`e.captures[...]` from
user source is rejected with `E_EXC_CAPTURES_REMOVED`, and the
`drift_error_add_local_dv` emission is gone).  The
`e.context.encode_compact()` JSON path covered by this file is
now the sole user-facing read of `^`-capture frames.

Note on test form: tests use the statement-form try/catch.  The
inline expression-form catch-binder bug is pinned separately in
`test_inline_try_catch_attrs_lang_bug.py`.
"""
from __future__ import annotations

import json as pyjson
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout


ROOT = Path(__file__).resolve().parents[3]


def _build_run(tmp_path: Path, source: str) -> tuple[int, str, str]:
	"""Compile + run a Drift program; return (exit_code, stdout, stderr)."""
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "test_bin"
	env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
	env["PYTHONPATH"] = str(ROOT)
	build = subprocess.run(
		[
			sys.executable,
			"-m",
			"lang.driftc.driftc",
			"--stdlib-root",
			str(ROOT / "stdlib"),
			str(src),
			"--entry",
			"main::main",
			"-o",
			str(out_bin),
		],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(120),
		env=env,
	)
	if build.returncode != 0:
		return (build.returncode, build.stdout, build.stderr)
	run = subprocess.run(
		[str(out_bin)],
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(30),
	)
	return (run.returncode, run.stdout, run.stderr)


def _ok(rc: int, stdout: str, stderr: str, label: str) -> None:
	assert rc == 0, (
		f"{label}: rc={rc}\n"
		f"stdout:\n{stdout[:2000]}\n"
		f"stderr:\n{stderr[:2000]}"
	)


# ─────────────────────────────────────────────────────────────────
# Test 1: no `^` captures → context == "[]"
# ─────────────────────────────────────────────────────────────────


def test_no_captures_context_is_empty_array(tmp_path):
	"""Throw with zero `^`-captured locals on the unwind path
	produces `e.context.encode_compact() == "[]"`."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error SimpleErr { tag: String }
fn _run() nothrow -> String {
\ttry {
\t\tthrow SimpleErr(tag = "x");
\t} catch SimpleErr(e) {
\t\treturn e.context.encode_compact();
\t} catch e {
\t\treturn "WRONG_CATCH";
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "no captures context dump")
	assert stdout.strip() == "[]", f"context expected '[]', got: {stdout!r}"


# ─────────────────────────────────────────────────────────────────
# Test 2: one `^`-captured local → one frame with fn + locals
# ─────────────────────────────────────────────────────────────────


def test_single_capture_one_frame(tmp_path):
	"""A single function with one `^`-captured local on the throw
	path produces a one-element context array containing a single
	frame object: `[{"fn":"<fqn>","locals":{"<key>":<value>}}]`."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error ParseFail { tag: String }
fn _inner() throws -> Int {
\tval ^record_id: String as "record_id" = "rec-42";
\tthrow ParseFail(tag = "fail");
}

fn _run() nothrow -> String {
\ttry {
\t\tval _ = _inner();
\t} catch ParseFail(e) {
\t\treturn e.context.encode_compact();
\t} catch e {
\t\treturn "WRONG_CATCH";
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "single capture context dump")
	doc = pyjson.loads(stdout.strip())
	assert isinstance(doc, list), f"context must be JSON array; got: {doc!r}"
	assert len(doc) == 1, f"expected 1 frame, got {len(doc)}: {doc!r}"
	frame = doc[0]
	assert "fn" in frame and "locals" in frame, (
		f"frame missing fn/locals keys: {frame!r}"
	)
	# fn FQN is the function symbol; we don't pin the exact mangled
	# form, just that it identifies _inner.
	assert "_inner" in frame["fn"], f"frame fn does not name _inner: {frame!r}"
	assert frame["locals"] == {"record_id": "rec-42"}, (
		f"frame locals mismatch: {frame['locals']!r}"
	)


# ─────────────────────────────────────────────────────────────────
# Test 3: nested frames — array order locked (innermost-first)
# ─────────────────────────────────────────────────────────────────


def test_nested_frames_innermost_first(tmp_path):
	"""Nested `^`-capturing functions on the throw path produce
	frames in unwind observation order: the function that throws
	appears first; outer functions follow."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error NestedErr { tag: String }
fn _innermost() throws -> Int {
\tval ^level: String as "level" = "innermost";
\tthrow NestedErr(tag = "boom");
}

fn _outer() throws -> Int {
\tval ^level: String as "level" = "outer";
\tval _ = _innermost();
\treturn 0;
}

fn _run() nothrow -> String {
\ttry {
\t\tval _ = _outer();
\t} catch NestedErr(e) {
\t\treturn e.context.encode_compact();
\t} catch e {
\t\treturn "WRONG_CATCH";
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "nested frames context dump")
	doc = pyjson.loads(stdout.strip())
	assert isinstance(doc, list), f"context must be JSON array; got: {doc!r}"
	assert len(doc) == 2, f"expected 2 frames, got {len(doc)}: {doc!r}"
	# Innermost first: _innermost frame appears at index 0.
	assert "_innermost" in doc[0]["fn"], (
		f"frame[0] should name _innermost (innermost-first ordering); got: {doc[0]!r}"
	)
	assert doc[0]["locals"] == {"level": "innermost"}, (
		f"frame[0] locals mismatch: {doc[0]['locals']!r}"
	)
	assert "_outer" in doc[1]["fn"], (
		f"frame[1] should name _outer; got: {doc[1]!r}"
	)
	assert doc[1]["locals"] == {"level": "outer"}, (
		f"frame[1] locals mismatch: {doc[1]['locals']!r}"
	)


# ─────────────────────────────────────────────────────────────────
# Test 4: recursive calls preserve repeated frame objects
# ─────────────────────────────────────────────────────────────────


def test_recursive_frames_repeated(tmp_path):
	"""A recursive function on the throw path contributes ONE frame
	per call invocation — frames are NOT merged.  Each frame
	captures the local value at its own call site."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error RecurErr { tag: String }
fn _recur(depth: Int) throws -> Int {
\tval ^depth_val: Int as "depth" = depth;
\tif depth == 0 {
\t\tthrow RecurErr(tag = "bottom");
\t}
\tval _ = _recur(depth - 1);
\treturn 0;
}

fn _run() nothrow -> String {
\ttry {
\t\tval _ = _recur(2);
\t} catch RecurErr(e) {
\t\treturn e.context.encode_compact();
\t} catch e {
\t\treturn "WRONG_CATCH";
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "recursive frames context dump")
	doc = pyjson.loads(stdout.strip())
	assert isinstance(doc, list), f"context must be JSON array; got: {doc!r}"
	# Three frames: recur(0) [throws], recur(1), recur(2).
	# Innermost-first: frame[0] = depth 0, frame[1] = depth 1, frame[2] = depth 2.
	assert len(doc) == 3, (
		f"expected 3 frames (one per recursive level), got {len(doc)}: {doc!r}"
	)
	for i, frame in enumerate(doc):
		assert "_recur" in frame["fn"], (
			f"frame[{i}].fn should name _recur; got: {frame!r}"
		)
	# Innermost-first → depth 0, then 1, then 2.
	assert doc[0]["locals"] == {"depth": 0}, f"frame[0]: {doc[0]!r}"
	assert doc[1]["locals"] == {"depth": 1}, f"frame[1]: {doc[1]!r}"
	assert doc[2]["locals"] == {"depth": 2}, f"frame[2]: {doc[2]!r}"


# ─────────────────────────────────────────────────────────────────
# Test 5: K regression — can-throw call inside try with active
# `^` capture in the outer function must append the outer frame.
#
# Symmetric with the direct-throw case (test 2 / 3): when a try
# in function F catches an error from an inner call, F's active
# `^` captures must contribute a frame just as they would if F
# threw directly.  Pre-fix, the can-throw-call lowering at the
# `_try_stack` branches (hir_to_mir.py:9358, :9432, :9459) routed
# the error to the local catch via StoreLocal + Goto without
# emitting `_emit_captured_locals`, so F's frame was silently
# dropped.  Direct-throw was already correct because the throw-
# statement lowering emits captures before the same StoreLocal +
# Goto sequence.
# ─────────────────────────────────────────────────────────────────


def test_outer_captures_recorded_when_catching_can_throw_call(tmp_path):
	"""Can-throw call inside a try-block in a function with active
	`^` captures must append the outer function's frame to
	`e.context`, not silently drop it."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error Boom { tag: String }
fn _inner() throws -> Int {
\tthrow Boom(tag = "x");
}

fn _run() nothrow -> String {
\tval ^outer_id: String as "outer_id" = "outer-1";
\ttry {
\t\tval _ = _inner();
\t} catch Boom(e) {
\t\treturn e.context.encode_compact();
\t} catch e {
\t\treturn "WRONG_CATCH";
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "outer-captures-on-can-throw-call")
	doc = pyjson.loads(stdout.strip())
	assert isinstance(doc, list), f"context must be JSON array; got: {doc!r}"
	assert len(doc) == 1, (
		f"expected 1 frame for _run's outer capture; got {len(doc)}: {doc!r}"
	)
	frame = doc[0]
	assert "_run" in frame["fn"], f"frame.fn should name _run; got: {frame!r}"
	assert frame["locals"] == {"outer_id": "outer-1"}, (
		f"frame.locals should record outer_id='outer-1'; got: {frame['locals']!r}"
	)
