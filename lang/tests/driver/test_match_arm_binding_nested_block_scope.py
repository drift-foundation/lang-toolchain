# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""A `match`-arm pattern binding is visible inside nested `try`/`catch` and bare
`{ }` blocks within the arm — exactly like a plain `val` local (LANGUAGE_BUG).

The match-arm binder alpha-renamer in `ast_to_hir._rename_stmt` recursed into
`HIf`/`HLoop` bodies but fell through (returned the statement unchanged) for
`HTry` (statement try/catch) and `HBlock` (bare block) — so binder USES inside
those nested blocks were never renamed to the arm's unique internal binder name,
and the type-checker reported `unknown name 's'`.  `HIf`/`HLoop` worked only
because they were explicitly handled; plain `val` locals were unaffected (they
resolve by lexical scope, no renaming).

(The try-containing cases use a throwing helper wrapped by a catch-all `main`:
a typed `catch Me(e)` does not discharge a `nothrow` throw-effect in v1 — an
orthogonal, pre-existing behavior — so the helper is declared throwing to keep
this test focused purely on binder-scope resolution.)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--stdlib-root", str(stdlib),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	return subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))


_PRELUDE = """\
module main;
import std.core as core;

error Me { tag: String }
fn boom() -> Int { throw Me(tag = "x"); }
fn rs() nothrow -> core.Result<String, Int> { return core.Result<String, Int>::Ok("aa"); }
fn use_s(s: &String) nothrow -> Int { return s.byte_length(); }
"""


def test_matcharm_binding_visible_in_try_catch(tmp_path: Path) -> None:
	"""`s` from `Ok(s) => { … }` resolves inside a nested `try`/`catch` (both the
	try body and the catch handler)."""
	src = _PRELUDE + """\
fn f() -> Int {
	match rs() {
		core.Result::Err(e) => { return -1; },
		core.Result::Ok(s) => {
			try { val n = boom(); return n + use_s(&s); }
			catch Me(e) { return use_s(&s); }
		}
	}
}
fn main() nothrow -> Int { try { return f(); } catch { return 99; } }
"""
	# boom() throws; catch Me handles it; use_s(&s) = byte_length("aa") = 2.
	assert _compile_and_run(tmp_path, src).returncode == 2


def test_matcharm_binding_visible_in_bare_block(tmp_path: Path) -> None:
	"""`s` resolves inside a nested bare `{ }` block."""
	src = _PRELUDE + """\
fn f() nothrow -> Int {
	match rs() {
		core.Result::Err(e) => { return -1; },
		core.Result::Ok(s) => { { return use_s(&s); } }
	}
}
fn main() nothrow -> Int { return f(); }
"""
	assert _compile_and_run(tmp_path, src).returncode == 2


def test_matcharm_binding_visible_in_nested_try_in_bare_block(tmp_path: Path) -> None:
	"""Deeper nesting: a bare block containing a try/catch, binder used in both."""
	src = _PRELUDE + """\
fn f() -> Int {
	match rs() {
		core.Result::Err(e) => { return -1; },
		core.Result::Ok(s) => {
			{
				try { val n = boom(); return n + use_s(&s); }
				catch Me(e) { return use_s(&s); }
			}
		}
	}
}
fn main() nothrow -> Int { try { return f(); } catch { return 99; } }
"""
	assert _compile_and_run(tmp_path, src).returncode == 2


def test_matcharm_binding_still_visible_in_if_while_match(tmp_path: Path) -> None:
	"""Control: the previously-working child blocks (`if`/`while`) keep seeing the
	binder (regression guard for the explicitly-handled cases)."""
	src = _PRELUDE + """\
fn f() nothrow -> Int {
	match rs() {
		core.Result::Err(e) => { return -1; },
		core.Result::Ok(s) => {
			if true { return use_s(&s); }
			var i = 0; while i < 1 { return use_s(&s); }
			return 0;
		}
	}
}
fn main() nothrow -> Int { return f(); }
"""
	assert _compile_and_run(tmp_path, src).returncode == 2


def test_catch_binder_shadows_matcharm_binding_in_try(tmp_path: Path) -> None:
	"""A catch binder that shadows the match-arm binder name resolves to the
	CATCH binder inside the catch block, not the outer match binding.  The try
	body's `&s` is the match `String` binder (proving the scope fix), while the
	catch's `s` is the `Me` typed-catch binder — `s.tag` is valid only for the
	latter, so the shadow must be correct for this to compile."""
	src = _PRELUDE + """\
fn f() -> Int {
	match rs() {
		core.Result::Err(e) => { return -1; },
		core.Result::Ok(s) => {
			try { val n = boom(); return n + use_s(&s); }
			catch Me(s) { val t: String = s.tag; return use_s(&t); }
		}
	}
}
fn main() nothrow -> Int { try { return f(); } catch { return 99; } }
"""
	# boom() throws; catch runs; the shadowing `s` is the Me binder, so
	# `s.tag` = "x" and use_s(&t) = byte_length("x") = 1.
	assert _compile_and_run(tmp_path, src).returncode == 1


def test_matcharm_binding_visible_in_assert(tmp_path: Path) -> None:
	"""`s` resolves in BOTH operands of an `assert(cond, msg)` statement within
	the arm — `assert` is a direct arm statement, but its `cond`/`msg`
	expressions still needed binder renaming."""
	src = _PRELUDE + """\
fn f() nothrow -> Int {
	match rs() {
		core.Result::Err(e) => { return -1; },
		core.Result::Ok(s) => { assert(s.byte_length() == 2, s); return s.byte_length(); }
	}
}
fn main() nothrow -> Int { return f(); }
"""
	assert _compile_and_run(tmp_path, src).returncode == 2


def test_expr_form_catch_binder_shadows_matcharm_binding(tmp_path: Path) -> None:
	"""Expression-form `try … catch Me(s) { … }`: a catch binder shadowing the
	match-arm binder name resolves to the CATCH binder inside its handler, not
	the outer match binding.  `s.tag` is valid only for the `Me` catch view, so
	the shadow must be correct for this to compile."""
	src = _PRELUDE + """\
fn f() -> Int {
	match rs() {
		core.Result::Err(e) => { return -1; },
		core.Result::Ok(s) => { return try boom() catch Me(s) { s.tag.byte_length() }; }
	}
}
fn main() nothrow -> Int { try { return f(); } catch { return 99; } }
"""
	# boom() throws; the catch arm yields `s.tag.byte_length()` where `s` is the
	# Me catch binder; tag = "x" → byte_length = 1.
	assert _compile_and_run(tmp_path, src).returncode == 1
