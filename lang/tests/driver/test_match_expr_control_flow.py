# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression (bug #6, bookkeeper team): an expression-form `match` arm must not
reroute control flow out of the enclosing function — its arms produce values.

Two layers:
  - an arm with NO trailing value (`{ return v; }`) is rejected by the parser
    (E_EXPR_BLOCK_MISSING_VALUE) — covered by test_match_expr_return_disallowed;
  - an arm WITH a trailing value AND a `return` (`{ return v; 0 }`) used to ICE
    at MIR lowering ("value-producing match arm ... block terminates"); it is now
    rejected by the checker with the intentional E-MATCHEXPR-CONTROLFLOW.

Must keep working: `return match e { ... }`, and statement-form `match` whose
arms `return`.

An inline `throw` / `rethrow` STATEMENT in a value-producing arm is also banned
(it reroutes out of the value expression, same as `return`).  A call that merely
*may* throw, used AS the arm's value, is an ordinary expression with a declared
throw effect and is NOT banned — that distinction (HThrow statement vs. throwing
call expression) is the whole point of the rule.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]
_HDR = "module repro;\nvariant E { A(v: Int), B(v: Int) }\n"


def _compile(tmp_path: Path, src: str, *, out: str) -> subprocess.CompletedProcess:
	p = tmp_path / "main.drift"
	p.write_text(src, encoding="utf-8")
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(p), "--entry", "repro::main", "--target-word-bits", "64",
		"-o", str(tmp_path / out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)


def test_return_with_trailing_value_arm_rejected_not_ice(tmp_path: Path) -> None:
	# `{ return v; 0 }` — was an internal MIR ICE; now a clear checker diagnostic.
	src = _HDR + (
		"fn f(e: E) nothrow -> Int {\n"
		"\tval x = match e { E::A(v) => { return v; 0 }, E::B(v) => { v } };\n"
		"\treturn x;\n}\n"
		"fn main() nothrow -> Int { val e = E::A(5); return f(e); }\n"
	)
	r = _compile(tmp_path, src, out="ice")
	assert r.returncode != 0
	assert "E-MATCHEXPR-CONTROLFLOW" in r.stderr, r.stderr
	assert "checker bug" not in r.stderr, "must not surface an internal-error/ICE message"


def test_return_the_whole_match_runs(tmp_path: Path) -> None:
	src = _HDR + (
		"fn f(e: E) nothrow -> Int { return match e { E::A(v) => { v }, E::B(v) => { v + 10 } }; }\n"
		"fn main() nothrow -> Int { val e = E::A(5); return f(e) - 5; }\n"
	)
	r = _compile(tmp_path, src, out="ret")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "ret")]).returncode == 0


def test_statement_form_match_return_arms_runs(tmp_path: Path) -> None:
	# Statement-form match whose arms `return` must keep working.
	src = _HDR + (
		"fn f(e: E) nothrow -> Int { match e { E::A(v) => { return v; }, E::B(v) => { return v + 10; } } }\n"
		"fn main() nothrow -> Int { val e = E::A(5); return f(e) - 5; }\n"
	)
	r = _compile(tmp_path, src, out="stmt")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "stmt")]).returncode == 0


def test_return_nested_in_if_is_caught(tmp_path: Path) -> None:
	# `{ if cond { return v; } 0 }` — return is nested in an HIf, not top-level,
	# but `return` escapes the function regardless of nesting: must be caught.
	src = _HDR + (
		"fn f(e: E) nothrow -> Int {\n"
		"\tval x = match e { E::A(v) => { if v > 0 { return v; } 0 }, E::B(v) => { v } };\n"
		"\treturn x;\n}\n"
		"fn main() nothrow -> Int { val e = E::B(5); return f(e) - 5; }\n"
	)
	r = _compile(tmp_path, src, out="nif")
	assert r.returncode != 0
	assert "E-MATCHEXPR-CONTROLFLOW" in r.stderr and "`return`" in r.stderr, r.stderr


def test_break_inside_inner_loop_in_arm_is_allowed(tmp_path: Path) -> None:
	# A `break` inside an inner loop within the arm targets that loop, not the
	# enclosing context — it must NOT be flagged.
	src = _HDR + (
		"fn f(e: E) nothrow -> Int {\n"
		"\tval x = match e { E::A(v) => { var i = 0; while i < v { if i > 2 { break; } i = i + 1; } i }, E::B(v) => { v } };\n"
		"\treturn x;\n}\n"
		"fn main() nothrow -> Int { val e = E::B(5); return f(e) - 5; }\n"
	)
	r = _compile(tmp_path, src, out="inb")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "inb")]).returncode == 0


def test_break_at_arm_level_reports_break_specific_message(tmp_path: Path) -> None:
	# A `break` at the arm level (targeting a loop enclosing the match) is
	# rejected, and the message names `break`, not `return`.
	src = _HDR + (
		"fn f(e: E) nothrow -> Int {\n"
		"\tvar t = 0; var i = 0;\n"
		"\twhile i < 3 {\n"
		"\t\tval x = match e { E::A(v) => { break; 0 }, E::B(v) => { v } };\n"
		"\t\tt = t + x; i = i + 1;\n"
		"\t}\n"
		"\treturn t;\n}\n"
		"fn main() nothrow -> Int { val e = E::B(5); return f(e); }\n"
	)
	r = _compile(tmp_path, src, out="brk")
	assert r.returncode != 0
	assert "E-MATCHEXPR-CONTROLFLOW" in r.stderr and "`break`" in r.stderr, r.stderr
	assert "return match" not in r.stderr, "break message must not suggest `return match`"


def test_return_in_nested_statement_match_is_caught(tmp_path: Path) -> None:
	# A nested STATEMENT-form match in the arm prelude whose arm `return`s
	# escapes the outer value arm — must be caught (it would otherwise hit the
	# same value-arm/terminating MIR contradiction).
	src = _HDR + (
		"fn f(e: E, e2: E) nothrow -> Int {\n"
		"\tval x = match e {\n"
		"\t\tE::A(v) => { match e2 { E::A(w) => { return w; }, E::B(w) => { } } 0 },\n"
		"\t\tE::B(v) => { v }\n"
		"\t};\n"
		"\treturn x;\n}\n"
		"fn main() nothrow -> Int { val e = E::B(5); return f(e, e); }\n"
	)
	r = _compile(tmp_path, src, out="nestret")
	assert r.returncode != 0
	assert "E-MATCHEXPR-CONTROLFLOW" in r.stderr and "`return`" in r.stderr, r.stderr


def test_nested_statement_match_without_escape_compiles(tmp_path: Path) -> None:
	# A nested statement-match with NO escaping control flow must still compile.
	src = _HDR + (
		"fn f(e: E, e2: E) nothrow -> Int {\n"
		"\tval x = match e {\n"
		"\t\tE::A(v) => { match e2 { E::A(w) => { }, E::B(w) => { } } v },\n"
		"\t\tE::B(v) => { v }\n"
		"\t};\n"
		"\treturn x;\n}\n"
		"fn main() nothrow -> Int { val e = E::B(5); return f(e, e) - 5; }\n"
	)
	r = _compile(tmp_path, src, out="nestok")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "nestok")]).returncode == 0


def test_inline_throw_statement_in_value_arm_rejected(tmp_path: Path) -> None:
	# An inline `throw` STATEMENT (with a trailing value, so the arm is
	# value-producing) reroutes out of the value expression — banned, same as
	# `return`.  The message names `throw` and must NOT suggest `return match`.
	src = _HDR + (
		"pub error Bad { code: Int }\n"
		"fn f(e: E) -> Int {\n"
		"\tval x = match e { E::A(v) => { throw Bad(code = v); 0 }, E::B(v) => { v } };\n"
		"\treturn x;\n}\n"
		"fn main() nothrow -> Int { val e = E::B(5); val r = try f(e) catch { 0 }; return r - 5; }\n"
	)
	r = _compile(tmp_path, src, out="ithrow")
	assert r.returncode != 0
	assert "E-MATCHEXPR-CONTROLFLOW" in r.stderr and "throw" in r.stderr, r.stderr
	assert "return match" not in r.stderr, "throw message must not suggest `return match`"


def test_inline_rethrow_statement_in_value_arm_rejected(tmp_path: Path) -> None:
	# Inline `rethrow` in a value-producing arm is likewise banned.  `rethrow`
	# is only valid inside a catch handler, so the value-producing match sits in
	# a (single-expression) `try ... catch` arm.
	src = _HDR + (
		"pub error Bad { code: Int }\n"
		"fn risky() -> Int { throw Bad(code = 1); }\n"
		"fn f(e: E) -> Int {\n"
		"\tval x = try risky() catch { match e { E::A(v) => { rethrow; 0 }, E::B(v) => { v } } };\n"
		"\treturn x;\n}\n"
		"fn main() nothrow -> Int { val e = E::B(5); val r = try f(e) catch { 0 }; return r - 5; }\n"
	)
	r = _compile(tmp_path, src, out="irethrow")
	assert r.returncode != 0
	assert "E-MATCHEXPR-CONTROLFLOW" in r.stderr and "rethrow" in r.stderr, r.stderr


def test_throwing_call_as_arm_value_allowed(tmp_path: Path) -> None:
	# A call that may throw, used AS the arm's value, is a normal expression
	# with a declared throw effect — NOT banned.  (Contrast with an inline
	# `throw` statement above.)  Exercise BOTH arms at runtime so we actually
	# prove the carve-out: the throwing arm's effect propagates out of the
	# value-producing match and is caught by the caller, while the non-throwing
	# arm produces its value normally.
	src = _HDR + (
		"pub error Bad { code: Int }\n"
		"fn fail(e: Int) -> Int { throw Bad(code = e); }\n"
		"fn f(e: E) -> Int { val x = match e { E::A(v) => { fail(v) }, E::B(v) => { v } }; return x; }\n"
		"fn main() nothrow -> Int {\n"
		# A-arm: fail(7) throws -> the throw propagates out of the match and out
		# of f -> caught here as 100.  If the arm did NOT throw/propagate, `thrown`
		# would be 7 and the test fails (rc=1).
		"\tval thrown = try f(E::A(7)) catch { 100 };\n"
		# B-arm: non-throwing arm yields its value (9), catch not taken.
		"\tval normal = try f(E::B(9)) catch { 200 };\n"
		"\tif thrown != 100 { return 1; }\n"
		"\tif normal != 9 { return 2; }\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="tcall")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "tcall")]).returncode == 0
