# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 2 of the terminal-`throws` work: body-flow enforcement and call-site
terminator semantics.

Phase 2 adds the real semantics to the bare terminal `throws` form:

  1. Body rule: a `fn f(...) throws` body must terminate on every CFG path
     via `throw`/`rethrow` or a tail call to another terminal-`throws`
     function. `return;`, `return value;`, and fallthrough are checker
     errors.

  2. Call-site rule: a call to a terminal-`throws` function counts as
     terminal in callers. This applies to the missing-return analysis in
     value-returning functions (Phase 0's check) AND to the body-flow check
     for terminal callers (Phase 2's check). Inside match arms / branch
     flow, an arm whose only meaningful statement is a call to a
     terminal-`throws` function is locally terminal.

Phase 2 does NOT touch:
  - Package metadata round-trip of `declared_terminal_throws` (Phase 3).
  - The `std.core.Throw` trait or any `Try`/`or_throw` rebind (Phase 4).
  - The framework-local typed-catch regression (Phase 4d).

Each test introspects the lowered checker diagnostics directly via
`parse_drift_workspace_to_hir` + the checker driver, not just `rc == 0`.
"""
from __future__ import annotations

import json
from pathlib import Path

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc(tmp_path: Path, capsys, source: str, module_name: str = "m_main") -> tuple[int, dict]:
	root = tmp_path / "mods"
	main_path = root / module_name / "main.drift"
	_write_file(main_path, source)
	rc = driftc_main(["-M", str(root), str(main_path), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _diag_messages(payload: dict) -> list[str]:
	diags = payload.get("diagnostics") or []
	return [str(d.get("message", "")) for d in diags]


# ---------------------------------------------------------------------------
# Body-flow positives: terminal `throws` bodies that terminate correctly.
# ---------------------------------------------------------------------------


def test_terminal_throws_body_ending_in_throw_is_accepted(tmp_path: Path, capsys) -> None:
	"""Bare terminal `throws` body whose last statement is `throw` — terminal
	on the only CFG path."""
	source = """
module m;

exception Boom()

fn fail() throws {
	throw Boom();
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"throw-only terminal body should be accepted; payload={payload}"


def test_terminal_throws_body_ending_in_tail_call_to_terminal_throws_is_accepted(tmp_path: Path, capsys) -> None:
	"""`fn outer() throws { inner(); }` where `inner` is also terminal-throws.
	The tail call to `inner` is itself terminal — control never returns from
	it — so `outer` is terminal on its only CFG path even without a literal
	`throw`."""
	source = """
module m;

exception Boom()

fn inner() throws {
	throw Boom();
}

fn outer() throws {
	inner();
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"tail call to terminal-throws should be terminal; payload={payload}"


def test_terminal_throws_body_with_branches_all_terminal_is_accepted(tmp_path: Path, capsys) -> None:
	"""`if c { throw E1(); } else { throw E2(); }` — both branches terminal,
	so the body is terminal."""
	source = """
module m;

exception A()
exception B()

fn fail(flag: Bool) throws {
	if flag {
		throw A();
	} else {
		throw B();
	}
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"if/else terminal body should be accepted; payload={payload}"


def test_terminal_throws_body_with_match_arms_all_terminal_is_accepted(tmp_path: Path, capsys) -> None:
	"""`match c { A => { throw ... }, B => { throw ... } }` — every arm
	terminal, so the match-as-statement is terminal and the body is
	terminal."""
	source = """
module m;

variant Choice { A, B }

exception ExA()
exception ExB()

fn fail(c: Choice) throws {
	match c {
		A => { throw ExA(); },
		B => { throw ExB(); }
	}
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"match-with-all-terminal-arms body should be accepted; payload={payload}"


# ---------------------------------------------------------------------------
# Body-flow negatives: terminal `throws` bodies that fail the contract.
# ---------------------------------------------------------------------------


def test_terminal_throws_body_with_bare_return_is_rejected(tmp_path: Path, capsys) -> None:
	"""`fn f() throws { return; }` — `return` is not allowed inside a terminal
	`throws` body."""
	source = """
module m;

fn fail() throws {
	return;
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"bare return in terminal body should be rejected; payload={payload}"
	msgs = _diag_messages(payload)
	assert any("return" in m.lower() and ("terminal" in m.lower() or "throws" in m.lower()) for m in msgs), (
		f"diagnostic should explain that return is not allowed in terminal throws bodies; msgs={msgs}"
	)


def test_terminal_throws_body_with_value_return_is_rejected(tmp_path: Path, capsys) -> None:
	"""`fn f() throws { return 0; }` — value return is also rejected."""
	source = """
module m;

fn fail() throws {
	return 0;
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"value return in terminal body should be rejected; payload={payload}"


def test_terminal_throws_body_falling_through_is_rejected(tmp_path: Path, capsys) -> None:
	"""`fn f() throws { val x = 1; }` — body has no terminator at all."""
	source = """
module m;

fn fail() throws {
	val x = 1;
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"falling-through terminal body should be rejected; payload={payload}"
	msgs = _diag_messages(payload)
	assert any("fail" in m and ("terminate" in m.lower() or "throw" in m.lower()) for m in msgs), (
		f"diagnostic should mention `fail` and the terminate/throw requirement; msgs={msgs}"
	)


def test_terminal_throws_body_with_partial_if_branch_is_rejected(tmp_path: Path, capsys) -> None:
	"""`if c { throw E(); }` (no `else`) — fallthrough on the false branch."""
	source = """
module m;

exception Boom()

fn fail(c: Bool) throws {
	if c {
		throw Boom();
	}
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"if without else in terminal body should be rejected; payload={payload}"


def test_terminal_throws_body_with_non_terminal_match_arm_is_rejected(tmp_path: Path, capsys) -> None:
	"""`match c { A => { throw ... }, B => { val x = 1; } }` — one arm fails
	to terminate."""
	source = """
module m;

variant Choice { A, B }

exception Boom()

fn fail(c: Choice) throws {
	match c {
		A => { throw Boom(); },
		B => { val x = 1; }
	}
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"non-terminal match arm in terminal body should be rejected; payload={payload}"


# ---------------------------------------------------------------------------
# Call-site terminator behavior: terminal-throws calls count as terminal in
# their callers' missing-return analysis (Phase 0's existing check).
# ---------------------------------------------------------------------------


def test_value_returning_match_arm_calling_terminal_throws_is_terminal(tmp_path: Path, capsys) -> None:
	"""A may-throw value-returning function `pick(c: Choice) -> Int` whose
	body is a match where one arm returns a value and the other arm calls a
	terminal-`throws` function. The terminal-call arm must count as terminal
	for Phase 0's missing-return analysis — without Phase 2's call-site
	terminator extension, Phase 0 would reject this function as non-terminal
	(the call arm has no `return` and no `throw`).

	`pick` itself must be may-throw (no `nothrow`) because calling
	terminal-throws functions transitively may throw — the nothrow checker
	would otherwise reject the call before the terminal-flow check ever ran.
	"""
	source = """
module m;

variant Choice { A, B }

exception Boom()

fn fail() throws {
	throw Boom();
}

fn pick(c: Choice) -> Int {
	match c {
		A => { return 1; },
		B => { fail(); }
	}
}

fn main() nothrow -> Int {
	return try pick(Choice::A()) catch { 99 };
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, (
		f"match arm calling a terminal-throws function should count as terminal "
		f"for Phase 0's missing-return analysis; payload={payload}"
	)


def test_value_returning_if_branch_calling_terminal_throws_is_terminal(tmp_path: Path, capsys) -> None:
	"""Same shape with if/else: one branch returns, the other tail-calls a
	terminal-throws function. Both branches must be recognized as terminal
	by Phase 0's check. `pick` is may-throw for the same reason as above."""
	source = """
module m;

exception Boom()

fn fail() throws {
	throw Boom();
}

fn pick(b: Bool) -> Int {
	if b {
		return 1;
	} else {
		fail();
	}
}

fn main() nothrow -> Int {
	return try pick(true) catch { 99 };
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, (
		f"if branch calling a terminal-throws function should count as terminal "
		f"for Phase 0's missing-return analysis; payload={payload}"
	)


# ---------------------------------------------------------------------------
# Value-position rejection: a terminal-`throws` call may appear ONLY at
# statement position (`HExprStmt(HCall)`), where its value is discarded.
# Anywhere else (return value, let binding, call argument, operator operand,
# match arm.result, ternary branch, etc.) is a checker error — terminal-throws
# functions have no return type, so a value-position use lowers as Unknown
# and crashes MIR validation. The rejection rule closes that hole at the
# checker level. (Caught by code review of the v1 Phase 2 patch.)
# ---------------------------------------------------------------------------


def test_terminal_throws_call_in_return_value_position_is_rejected(tmp_path: Path, capsys) -> None:
	"""`return fail();` where `fail` is terminal-throws — `fail` never returns
	a value, so it cannot be used as the value of a `return`. The user's
	original repro: passes typecheck under v1 Phase 2 then crashes MIR with
	`unresolved layout type Unknown in MoveOut`. Phase 2 must reject this at
	the checker level."""
	source = """
module m;

exception Boom()

fn fail() throws {
	throw Boom();
}

fn pick(b: Bool) -> Int {
	if b {
		return 1;
	} else {
		return fail();
	}
}

pub fn main() nothrow -> Int {
	return try pick(false) catch { 99 };
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, (
		f"return fail() (terminal-throws in value position) should be rejected; "
		f"payload={payload}"
	)
	msgs = _diag_messages(payload)
	assert any("fail" in m and ("value" in m.lower() or "never returns" in m.lower() or "throws" in m.lower())
		for m in msgs), (
		f"diagnostic should mention `fail` and the value-position rule; msgs={msgs}"
	)
	# Defensive: must NOT leak the MIR contract failure to the user.
	for m in msgs:
		assert "Unknown" not in m or "value" in m.lower(), f"MIR Unknown leaked to user: {m}"
		assert "MoveOut" not in m, f"MIR MoveOut leaked to user: {m}"
		assert "MIR validation contract failure" not in m, f"MIR contract failure leaked: {m}"


def test_terminal_throws_call_in_let_binding_is_rejected(tmp_path: Path, capsys) -> None:
	"""`val x = fail();` — let-binding a terminal-throws call. `fail` has no
	return value, so the binding has no type."""
	source = """
module m;

exception Boom()

fn fail() throws {
	throw Boom();
}

fn pick() -> Int {
	val x = fail();
	return x;
}

pub fn main() nothrow -> Int {
	return try pick() catch { 99 };
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"let val x = fail() should be rejected; payload={payload}"


def test_terminal_throws_call_as_operand_is_rejected(tmp_path: Path, capsys) -> None:
	"""`return 1 + fail();` — terminal-throws call as a binary operator
	operand. The operator has no operand value to add to."""
	source = """
module m;

exception Boom()

fn fail() throws {
	throw Boom();
}

fn pick() -> Int {
	return 1 + fail();
}

pub fn main() nothrow -> Int {
	return try pick() catch { 99 };
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"1 + fail() should be rejected; payload={payload}"


def test_terminal_throws_call_in_function_argument_is_rejected(tmp_path: Path, capsys) -> None:
	"""`other(fail())` — passing a terminal-throws call as an argument."""
	source = """
module m;

exception Boom()

fn fail() throws {
	throw Boom();
}

fn other(x: Int) nothrow -> Int {
	return x + 1;
}

fn pick() -> Int {
	return other(fail());
}

pub fn main() nothrow -> Int {
	return try pick() catch { 99 };
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"other(fail()) should be rejected; payload={payload}"


def test_terminal_throws_call_as_match_arm_result_expression_is_rejected(tmp_path: Path, capsys) -> None:
	"""`B => fail()` (bare-expression-form arm) puts the terminal-throws call
	in arm.result, which is value position. The block-form `B => { fail(); }`
	is the correct shape — that puts it at statement position."""
	source = """
module m;

variant Choice { A, B }

exception Boom()

fn fail() throws {
	throw Boom();
}

fn pick(c: Choice) -> Int {
	return match c {
		A => { 1 },
		B => { fail() }
	};
}

pub fn main() nothrow -> Int {
	return try pick(Choice::A()) catch { 99 };
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, (
		f"match arm with terminal-throws call as arm.result should be rejected; "
		f"payload={payload}"
	)


def test_terminal_throws_call_at_statement_position_is_still_accepted(tmp_path: Path, capsys) -> None:
	"""Negative-control: `fail();` as a bare statement (HExprStmt) is the
	allowed shape and must continue to compile cleanly. The user's
	`B => { fail(); }` arm shape — block form, not arm.result form — must
	still work after the value-position rejection lands."""
	source = """
module m;

variant Choice { A, B }

exception Boom()

fn fail() throws {
	throw Boom();
}

fn pick(c: Choice) -> Int {
	match c {
		A => { return 1; },
		B => { fail(); }
	}
}

pub fn main() nothrow -> Int {
	return try pick(Choice::A()) catch { 99 };
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, (
		f"statement-position terminal-throws call should still be accepted; "
		f"payload={payload}"
	)


def test_terminal_throws_body_with_call_to_non_terminal_function_is_rejected(tmp_path: Path, capsys) -> None:
	"""`fn outer() throws { plain_call(); }` where `plain_call` is NOT
	terminal-throws — `outer`'s body is not terminal because `plain_call`
	may return normally. Must be rejected.
	"""
	source = """
module m;

fn plain_call() nothrow -> Int {
	return 0;
}

fn outer() throws {
	plain_call();
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, (
		f"call to non-terminal-throws function should NOT make terminal body terminal; "
		f"payload={payload}"
	)
