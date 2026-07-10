# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""DriftQuery 2026-07-09 regression pins: `&call(move x)` and the
location-less non-lvalue diagnostic.

Root cause (assessment 2026-07-09T235900Z): a pass-contract gap —
`borrow_materialize` skipped materializing any borrow whose subject
"contains a move" ANYWHERE (recursing into call arguments), assuming the
type checker rejects those; the checker's targeted rejection only covered
`&mut`. The move predicate is now SPLIT: materialization is blocked only
when the borrow would alias the moved value ITSELF (direct `&(move x)`,
or a projection/ternary chain over it); moves inside call/ctor arguments
feed construction of the borrowed RESULT and materialize like any other
rvalue borrow.

Decision pinned here for `&mut` (option 1 of the review): `&mut
mk(move s)` is SUPPORTED by the same temp-materialization as every
`&mut <rvalue>` — stage1 lifts the call result into a mutable temp
(`borrow_materialize`'s documented v1 design; `&mut mk("x")` already
compiled this way, so the old broad contains-move gate made the move
variant an inconsistent exception). Direct `&mut (move x)` keeps its
targeted assign-to-var-first rejection.

Diagnostic pins: user `&` borrows carry a real source location
(ast_to_hir attaches loc), and spanless diagnostics never masquerade as
the first source file (text renders `<unknown location>`/`?:?`; JSON
keeps `file: null`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_COMMON = """\
module main;

struct Widget { name: String }

fn mk_widget(s: String) nothrow -> Widget { return Widget(name = s + ""); }
fn check_widget(w: &Widget) nothrow -> Bool { return (w.name + "") == "hello"; }
"""

# (B) THE report shape: explicit & of a call whose arg moves a local.
_MOVE_ARG_BORROW = _COMMON + """
fn probe() nothrow -> Bool {
	var s = "hello";
	return check_widget(&mk_widget(move s));
}

pub fn main() nothrow -> Int {
	if probe() { return 0; }
	return 1;
}
"""

# (A) no-move sibling — must stay green.
_NO_MOVE_BORROW = _COMMON + """
fn probe() nothrow -> Bool {
	return check_widget(&mk_widget("hello" + ""));
}

pub fn main() nothrow -> Int {
	if probe() { return 0; }
	return 1;
}
"""

# DriftQuery's real shape: & of a METHOD call with a move arg, nested as
# an argument beside a second & arg.
_METHOD_RECV_SHAPE = """\
module main;

struct Entry { key: String }
struct Catalog { first_key: String }
struct Env { tag: Int }

struct Src { salt: String }

implement Src {
	fn catalog(self: &Src, e: Entry) nothrow -> Catalog {
		return Catalog(first_key = e.key + (self.salt + ""));
	}
	fn validate(self: &Src, c: &Catalog, env: &Env) nothrow -> Bool {
		if env.tag == 7 { return (c.first_key + "") == "ksalt"; }
		return false;
	}
}

pub fn main() nothrow -> Int {
	val src = Src(salt = "salt");
	val env = Env(tag = 7);
	var e = Entry(key = "k");
	if src.validate(&src.catalog(move e), &env) { return 0; }
	return 1;
}
"""

# Direct `&(move x)` — the case the guardrail is FOR: stays rejected,
# now with the targeted message and a REAL location.
_DIRECT_MOVE_BORROW = _COMMON + """
pub fn main() nothrow -> Int {
	var s = "hello";
	val w = Widget(name = s + "x");
	val r = check_widget(&(move w));
	if r { return 0; }
	return 1;
}
"""

# `&mut` of a CALL rvalue materializes into a mutable temp — with or
# without a move in the call's args (consistency pin: both variants in
# one program, both dispatch correctly).
_MUT_CALL_MOVE = _COMMON + """
fn touch(w: &mut Widget) nothrow -> Bool { return (w.name + "") == "hello"; }

pub fn main() nothrow -> Int {
	var s = "hello";
	if touch(&mut mk_widget(move s)) {
		if touch(&mut mk_widget("hello" + "")) { return 0; }
		return 2;
	}
	return 1;
}
"""

# Direct `&mut (move x)`: keeps the targeted assign-to-var-first message.
_MUT_DIRECT_MOVE = _COMMON + """
fn touch(w: &mut Widget) nothrow -> Bool { return (w.name + "") == "hello"; }

pub fn main() nothrow -> Int {
	var w = Widget(name = "hello" + "");
	if touch(&mut (move w)) { return 0; }
	return 1;
}
"""


def _compile(tmp_path: Path, source: str, *extra: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 *extra, str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)


def _run_ok(tmp_path: Path, source: str, *extra: str) -> None:
	res = _compile(tmp_path, source, *extra)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"


def test_borrow_of_call_with_move_arg_compiles_and_runs(tmp_path: Path) -> None:
	"""(B): `&mk_widget(move s)` materializes like any rvalue borrow."""
	_run_ok(tmp_path, _MOVE_ARG_BORROW)


def test_borrow_of_call_with_move_arg_asan(tmp_path: Path) -> None:
	"""ASAN row: the materialized temp is released exactly once; the
	moved-in String transfers cleanly."""
	res = _compile(tmp_path, _MOVE_ARG_BORROW, "--sanitize=address,undefined")
	assert res.returncode == 0, res.stderr[-1500:]
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, run.stderr[-800:]
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-800:]


def test_borrow_of_call_no_move_still_green(tmp_path: Path) -> None:
	_run_ok(tmp_path, _NO_MOVE_BORROW)


def test_method_call_receiver_shape(tmp_path: Path) -> None:
	"""DriftQuery's production shape: `&src.catalog(move e)` nested as a
	method-call argument beside `&env`."""
	_run_ok(tmp_path, _METHOD_RECV_SHAPE)


def test_direct_move_borrow_targeted_diag_with_location(tmp_path: Path) -> None:
	"""`&(move w)` stays rejected — targeted message, REAL file:line."""
	res = _compile(tmp_path, _DIRECT_MOVE_BORROW)
	assert res.returncode != 0, "direct move borrow must reject"
	err = res.stderr + res.stdout
	assert "bind" in err or "moves the borrowed value" in err, err[-800:]
	assert "None:None" not in err, f"diagnostic lost its location:\n{err[-800:]}"
	m = re.search(r"main\.drift:(\d+):(\d+)", err)
	assert m, f"expected file:line:column pointing at main.drift:\n{err[-800:]}"


def test_mut_borrow_of_move_call_materializes(tmp_path: Path) -> None:
	"""`&mut mk_widget(move s)` materializes into a mutable temp exactly
	like `&mut mk_widget("x")` (both pinned in one program)."""
	_run_ok(tmp_path, _MUT_CALL_MOVE)


def test_mut_direct_move_keeps_targeted_diag(tmp_path: Path) -> None:
	res = _compile(tmp_path, _MUT_DIRECT_MOVE)
	assert res.returncode != 0
	err = res.stderr + res.stdout
	assert "assign to a var first" in err or "var first" in err, err[-800:]
	assert "None:None" not in err, err[-800:]


def test_spanless_diag_never_names_first_file(tmp_path: Path) -> None:
	"""Renderer hardening: a diagnostic whose span is fully empty must
	not masquerade as the first source file (text or JSON). Pinned via a
	unit-level call into the renderer helpers."""
	sys.path.insert(0, str(ROOT))
	from lang.driftc.core.span import Span
	from lang.driftc.core.diagnostics import Diagnostic
	from lang.driftc.driftc import _diag_label, _diag_to_json

	d = Diagnostic(message="x", span=Span())
	label = _diag_label(d, Path("first_file.drift"))
	assert "first_file" not in label, label
	j = _diag_to_json(d, "borrowcheck", Path("first_file.drift"))
	assert j.get("file") in (None, "<unknown>"), j
	# A diagnostic WITH a line but no file keeps the primary-source
	# fallback (single-file flows rely on it).
	d2 = Diagnostic(message="x", span=Span(line=3, column=1))
	assert "first_file" in _diag_label(d2, Path("first_file.drift"))


# Forwarding RESULT SLOTS (review finding 2): a `move` in a
# match-expression arm result makes the moved value the borrow subject —
# the guardrail must treat block-expression results as forwarding, not
# as fresh producers. (Casts and try/unsafe results take the same
# conservative walk; the match form is the surface-reachable pin.)
_MATCH_RESULT_MOVE = _COMMON + """
pub fn main() nothrow -> Int {
	var w1 = Widget(name = "hello" + "");
	var w2 = Widget(name = "bye" + "");
	val c = true;
	val r = check_widget(&(match c {
		true => { move w1 },
		false => { move w2 },
	}));
	if r { return 0; }
	return 1;
}
"""


def test_match_result_move_borrow_rejected(tmp_path: Path) -> None:
	"""`&(match ... => move w ...)` forwards the moved value into the
	borrow — rejected with the targeted message and a real location."""
	res = _compile(tmp_path, _MATCH_RESULT_MOVE)
	assert res.returncode != 0, "match-result move borrow must reject"
	err = res.stderr + res.stdout
	assert "bind" in err or "moves the borrowed value" in err or "var first" in err, err[-900:]
	assert "None:None" not in err, err[-900:]
