# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 0 of the terminal-`throws` work: close the missing-return checker hole.

Today, a non-Void function whose body falls off the end without returning
slips past the checker entirely and trips an `AssertionError("missing return
reached MIR lowering (checker bug)")` at hir_to_mir.py:5149. That assertion
is a bug-canary, not a user-facing diagnostic — it crashes the compiler with
a stack trace instead of producing a checker error.

This test pins the new contract: the checker must reject any function with a
non-Void return type whose body has at least one CFG path that falls off the
end. The diagnostic message is matched loosely so the wording can evolve, but
the test enforces:

  - rc != 0
  - no AssertionError stack trace
  - a checker diagnostic mentioning the offending function and the
    "must return" phrasing
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_non_void_fn_falls_off_end_is_checker_error(tmp_path: Path, capsys) -> None:
	"""Bare repro: function declares `-> Int` but the body has zero return statements."""
	source = """
module m_main;

fn dangling() nothrow -> Int {
	val x = 1;
}

fn main() nothrow -> Int {
	return dangling();
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"checker should reject missing return; payload={payload}"
	msgs = _diag_messages(payload)
	# Loose wording — the diagnostic should mention "return" and identify dangling.
	assert any("return" in m.lower() for m in msgs), f"no return-related diagnostic; msgs={msgs}"
	assert any("dangling" in m for m in msgs), f"diagnostic does not name the function; msgs={msgs}"
	# Negative: no internal compiler bug surfaced.
	for m in msgs:
		assert "checker bug" not in m.lower(), f"compiler emitted internal-bug message: {m}"
		assert "AssertionError" not in m, f"compiler emitted assertion stack trace: {m}"


def test_non_void_fn_partial_if_branch_is_checker_error(tmp_path: Path, capsys) -> None:
	"""`if` without an `else` cannot satisfy the terminal contract on its own."""
	source = """
module m_main;

fn maybe_one(b: Bool) nothrow -> Int {
	if b {
		return 1;
	}
}

fn main() nothrow -> Int {
	return maybe_one(true);
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"checker should reject if-without-else fallthrough; payload={payload}"
	msgs = _diag_messages(payload)
	assert any("return" in m.lower() for m in msgs), f"msgs={msgs}"
	assert any("maybe_one" in m for m in msgs), f"msgs={msgs}"
	for m in msgs:
		assert "checker bug" not in m.lower(), m


def test_non_void_fn_match_with_non_terminal_arm_is_checker_error(tmp_path: Path, capsys) -> None:
	"""A match-as-statement where one arm fails to return must be rejected."""
	source = """
module m_main;

variant Choice { A, B }

fn pick(c: Choice) nothrow -> Int {
	match c {
		A => { return 1; },
		B => { val x = 2; }
	}
}

fn main() nothrow -> Int {
	return pick(Choice::A());
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"checker should reject non-terminal match arm; payload={payload}"
	msgs = _diag_messages(payload)
	assert any("return" in m.lower() for m in msgs), f"msgs={msgs}"
	assert any("pick" in m for m in msgs), f"msgs={msgs}"
	for m in msgs:
		assert "checker bug" not in m.lower(), m


def test_non_void_fn_terminal_via_if_else_returns_is_accepted(tmp_path: Path, capsys) -> None:
	"""Positive: an if/else where both branches return is terminal — must compile cleanly."""
	source = """
module m_main;

fn pick(b: Bool) nothrow -> Int {
	if b {
		return 1;
	} else {
		return 2;
	}
}

fn main() nothrow -> Int {
	return pick(true);
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"if/else with both arms returning should be accepted; payload={payload}"


def test_non_void_fn_terminal_via_match_returns_is_accepted(tmp_path: Path, capsys) -> None:
	"""Positive: a match-as-statement where every arm returns is terminal."""
	source = """
module m_main;

variant Choice { A, B }

fn pick(c: Choice) nothrow -> Int {
	match c {
		A => { return 1; },
		B => { return 2; }
	}
}

fn main() nothrow -> Int {
	return pick(Choice::A());
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"match-as-statement with all-returning arms should be accepted; payload={payload}"


def test_void_fn_with_no_return_is_accepted(tmp_path: Path, capsys) -> None:
	"""Negative-control: Void functions are unaffected — implicit return remains legal."""
	source = """
module m_main;

fn do_nothing() nothrow -> Void {
	val x = 1;
}

fn main() nothrow -> Int {
	do_nothing();
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"Void function should still allow implicit return; payload={payload}"


def test_while_true_with_unconditional_return_is_accepted(tmp_path: Path, capsys) -> None:
	"""Positive: `while true { ... return X; }` is terminal — the literal-true
	condition desugars to a `loop { if true { body } else { break } }` shape
	where the synthesized else-break is dead code, and the user body
	unconditionally returns. Pinned by the existing
	`test_loop_all_paths_return_no_internal.py` shape.
	"""
	source = """
module m;

fn pick(flag: Int) nothrow -> Int {
	while true {
		if flag == 1 {
			return 1;
		}
		return 2;
	}
}

fn main() nothrow -> Int {
	return pick(1);
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"while-true with unconditional return should be accepted; payload={payload}"


def test_while_true_with_unconditional_return_one_stmt_is_accepted(tmp_path: Path, capsys) -> None:
	"""Positive: `while true { return 1; }` — the body's only statement is a
	return, so the loop body is unconditionally terminal."""
	source = """
module m;

fn always_one() nothrow -> Int {
	while true {
		return 1;
	}
}

fn main() nothrow -> Int {
	return always_one();
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"while-true with single-stmt return should be accepted; payload={payload}"


def test_while_dynamic_cond_with_inner_return_is_rejected(tmp_path: Path, capsys) -> None:
	"""Negative: `while cond { return X; }` for a dynamic cond is NOT terminal —
	the loop can exit normally (when cond is false) and fall through to the
	post-loop point, which has nothing to return. The function must add an
	explicit post-loop return.
	"""
	source = """
module m;

fn maybe(flag: Int) nothrow -> Int {
	while flag == 1 {
		return 1;
	}
}

fn main() nothrow -> Int {
	return maybe(1);
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"while-dynamic-cond should be rejected (loop can exit normally); payload={payload}"
	msgs = _diag_messages(payload)
	assert any("maybe" in m and "return" in m.lower() for m in msgs), f"msgs={msgs}"


def test_while_true_with_break_inside_branch_is_rejected(tmp_path: Path, capsys) -> None:
	"""Negative: `while true { if cond { break; } return 2; }` has a reachable
	break that exits the loop without returning. The function then falls
	through past the loop and must be rejected.
	"""
	source = """
module m;

fn looping(flag: Int) nothrow -> Int {
	while true {
		if flag == 1 {
			break;
		}
		return 2;
	}
}

fn main() nothrow -> Int {
	return looping(0);
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"while-true with reachable break should be rejected; payload={payload}"
	msgs = _diag_messages(payload)
	assert any("looping" in m for m in msgs), f"msgs={msgs}"


def test_throwing_fn_terminal_via_inline_throw_is_accepted(tmp_path: Path, capsys) -> None:
	"""Positive: a non-Void may-throw function whose body is a single inline throw
	is terminal.

	This pins the existing behavior — the new checker pass must not regress
	functions that escape via throw rather than return. Phase 2 will extend
	this to cover tail calls of `throws`-terminal functions.
	"""
	source = """
module m;

error Boom {}
fn always_fail() -> Int {
	throw Boom();
}

fn main() nothrow -> Int {
	try {
		always_fail();
	} catch {
		return 0;
	}
	return 1;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"throw-terminal function should be accepted; payload={payload}"
