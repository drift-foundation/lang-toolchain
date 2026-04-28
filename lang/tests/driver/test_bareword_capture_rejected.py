# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG carrier: bareword `captures(varname)` (no explicit
`copy` / `move` / `share` / `&`) silently mapped to a borrowed
reference into the outer binding cell.  In a loop, closures
constructed inside the loop body all captured the SAME outer cell;
when the loop reseated the cell, every closure observed the final
iteration's value.  Pre-fix shape:

	var cb: core.Callback0<Int> = core.callback0(|| nothrow => 0);
	var i = 0;
	while i < 3 {
		cb = core.callback0(|| captures(i) nothrow => i);
		i = i + 1;
	}
	cb.call()  // returns 3 (silent wrong); should return 2 or be a compile error

Surfaced by web-team report 2026-04-28 against 0.31.20 and 0.31.21.

**Fix (0.31.22)**: bare `NAME` removed from
`lambda_capture_item` grammar.  Capture-list entries MUST spell the
mode explicitly:

	captures(copy x)    — value-like duplicate, requires T: Copy
	captures(move x)    — owned transfer; outer binding consumed
	captures(share x)   — second-owner alias; requires T: Share
	captures(&x)        — borrowed read-only; non-escaping
	captures(&mut x)    — borrowed write; non-escaping

Bareword `captures(x)` now produces a parse-time diagnostic
listing the allowed explicit modes (`copy` / `move` / `share` /
`AMP` / `&mut`).  The parser cannot type-suggest because it
doesn't know the captured value's type — picking the right mode
is the user's choice; the diagnostic just enumerates the menu.
This eliminates the silent-miscompile class entirely — there is
no longer an "auto" capture mode that resolves at lowering time.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile_with_stdlib(
	tmp_path: Path,
	capsys: pytest.CaptureFixture[str],
	source: str,
) -> tuple[int, dict]:
	main_path = tmp_path / "main.drift"
	_write_file(main_path, source)
	argv = ["--stdlib-root", "stdlib", "--test-build-only", str(main_path)]
	return _run_driftc_json(argv, capsys)


def test_bareword_capture_single_var_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Minimal: `|| captures(x) => x` (no keyword) must error.

	Pre-fix: silently mapped to `captures(&x)` (borrowed-cell), which
	silently miscompiles when the closure escapes (e.g. into
	`core.callback0`) and the outer binding is reseated later.
	Post-fix: parse/check-time diagnostic naming the available
	keywords.
	"""
	source = """
module main;

import std.core as core;

pub fn main() nothrow -> Int {
	val x: Int = 42;
	val cb = core.callback0(|| captures(x) nothrow => x);
	return cb.call();
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc != 0, "compile should reject bareword captures(x)"
	all_msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	# Must NOT silently lower as a borrowed capture.  Diagnostic
	# must name the available capture modes so the user can pick
	# one — even when it comes from the parser as an "expected one
	# of" list, the keyword tokens MUST appear so the user knows
	# what to write instead.
	keyword_names = ("COPY", "MOVE", "SHARE", "AMP", "copy", "move", "share")
	assert any(
		sum(1 for kw in keyword_names if kw in m) >= 3
		for m in all_msgs
	), (
		f"diagnostic must list at least three of "
		f"{{COPY, MOVE, SHARE, AMP/&}} so the user knows the "
		f"available explicit capture modes; got: {all_msgs}"
	)


def test_bareword_capture_loop_callback_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Web-team motivating shape: closure built inside a loop using
	bareword `captures(i)`, escaping into `core.callback0`.  Pre-fix
	silently miscompiled (every closure observed final-iteration
	value of `i`).  Post-fix: rejected at parse/check time, NOT a
	silent compile success that produces wrong runtime results.

	The point of this carrier is the SHAPE (loop-built escaping
	closure with bareword capture).  Even if the diagnostic message
	is short on hints, it MUST be a hard error — never a successful
	compile of a closure-loop with bareword captures.
	"""
	source = """
module main;

import std.core as core;

pub fn main() nothrow -> Int {
	var i = 0;
	var cb: core.Callback0<Int> = core.callback0(|| nothrow => 0);
	while i < 3 {
		cb = core.callback0(|| captures(i) nothrow => i);
		i = i + 1;
	}
	return cb.call();
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc != 0, (
		"compile must reject bareword `captures(i)` in a loop "
		"that escapes into core.callback0; pre-fix this silently "
		"compiled and produced wrong runtime results (every "
		"closure observed the loop-final i value)."
	)


def test_bareword_capture_multi_var_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Multi-var: `captures(a, b)` — even one bareword in the list
	must reject the whole list."""
	source = """
module main;

import std.core as core;

pub fn main() nothrow -> Int {
	val a: Int = 1;
	val b: Int = 2;
	val cb = core.callback0(|| captures(a, b) nothrow => a + b);
	return cb.call();
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc != 0, "compile should reject bareword captures(a, b)"


def test_explicit_keyword_captures_still_work(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Sanity: every explicit spelling continues to compile cleanly
	post-fix.  Pins that the grammar tightening only removed the
	bareword path."""
	source = """
module main;

import std.core as core;
import std.concurrent as conc;

struct Holder { v: Int }

implement core.Destructible for Holder {
	pub fn destroy(self: Holder) nothrow -> Void { return; }
}

pub fn main() nothrow -> Int {
	val c: Int = 7;                              // Copy
	val arc = conc.arc(Holder(v = 42));          // Share
	val owned = Holder(v = 100);                 // non-Copy non-Share
	var x: Int = 1;                              // for &
	var y: Int = 10;                             // for &mut

	val cb_copy = core.callback0(|| captures(copy c) nothrow => c);
	val cb_share = core.callback0(|| captures(share arc) nothrow => arc.get().v);
	val cb_move = core.callback0(|| captures(move owned) nothrow => owned.v);

	val r1 = cb_copy.call();
	val r2 = cb_share.call();
	val r3 = cb_move.call();
	// & / &mut captures are non-escaping; immediate-invoked form below.
	val r4 = (|| captures(&x) nothrow => x)();
	val r5 = (|| captures(&mut y) nothrow => { y = y + 5; return y; })();
	return r1 + r2 + r3 + r4 + r5;
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	errors = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"explicit-keyword captures must still compile; got "
		f"rc={rc} diagnostics={[d.get('message') for d in errors]}"
	)
