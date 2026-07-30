# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Move-containing borrow pins, POST reject-redundant-call-borrows.

History: the DriftQuery 2026-07-09 pins covered `&call(move x)` at call-
ARGUMENT position. The redundant-borrow rule (0.33.91) retired that
spelling: a source-written borrow at a declared `&T` argument slot is
rejected (E_REDUNDANT_ARG_BORROW), and its bare form `check_widget(
mk_widget(move s))` auto-borrows through the SAME BorrowMaterializeRewriter
predicate — the split move predicate (materialize unless the borrow would
alias the moved value ITSELF) is now pinned via the bare spelling.

D1b(b) decision pinned here: `&mut <rvalue>` has NO argument spelling —
the argument form is rejected with E_MUT_RVALUE_ARG_BINDING_REQUIRED and
the bare form keeps its addressable-place rejection; the supported shape
is a BINDING (`val p = &mut mk_widget(move s); touch(p)`), which is this
file's migration exemplar (cited by the 0.33.91 MIGRATION notes). The
move guardrails (`&(move x)`, `&mut (move x)`, match-result forwarding)
keep their targeted messages, pinned in binding position where the
spellings remain legal.

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

# (B) The report shape, bare form: the call whose arg moves a local
# auto-borrows at the &Widget parameter (same materializer predicate).
_MOVE_ARG_BORROW = _COMMON + """
fn probe() nothrow -> Bool {
	var s = "hello";
	return check_widget(mk_widget(move s));
}

pub fn main() nothrow -> Int {
	if probe() { return 0; }
	return 1;
}
"""

# (A) no-move sibling — must stay green.
_NO_MOVE_BORROW = _COMMON + """
fn probe() nothrow -> Bool {
	return check_widget(mk_widget("hello" + ""));
}

pub fn main() nothrow -> Int {
	if probe() { return 0; }
	return 1;
}
"""

# DriftQuery's real shape, bare form: a METHOD call with a move arg,
# nested as an argument beside a second borrowed-place arg.
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
	if src.validate(src.catalog(move e), env) { return 0; }
	return 1;
}
"""

# Direct `&(move x)` in BINDING position — the case the guardrail is
# FOR: stays rejected with the targeted message and a REAL location.
_DIRECT_MOVE_BORROW = _COMMON + """
pub fn main() nothrow -> Int {
	var s = "hello";
	val w = Widget(name = s + "x");
	val b = &(move w);
	val r = check_widget(b);
	if r { return 0; }
	return 1;
}
"""

# `&mut` of a CALL rvalue in BINDING position (the D1b(b) migration
# exemplar): materializes into a mutable temp, with or without a move
# in the call's args; the `&mut`-typed value is then passed bare.
_MUT_CALL_MOVE = _COMMON + """
fn touch(w: &mut Widget) nothrow -> Bool { return (w.name + "") == "hello"; }

pub fn main() nothrow -> Int {
	var s = "hello";
	val p = &mut mk_widget(move s);
	if touch(p) {
		val q = &mut mk_widget("hello" + "");
		if touch(q) { return 0; }
		return 2;
	}
	return 1;
}
"""

# Direct `&mut (move x)` in BINDING position: keeps the targeted
# assign-to-var-first message.
_MUT_DIRECT_MOVE = _COMMON + """
fn touch(w: &mut Widget) nothrow -> Bool { return (w.name + "") == "hello"; }

pub fn main() nothrow -> Int {
	var w = Widget(name = "hello" + "");
	val p = &mut (move w);
	if touch(p) { return 0; }
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
	val b = &(match c {
		true => { move w1 },
		false => { move w2 },
	});
	val r = check_widget(b);
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
