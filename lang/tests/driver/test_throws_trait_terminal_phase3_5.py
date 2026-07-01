# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3.5 of the terminal-`throws` work: trait call terminality and trait/impl
terminal-throws compatibility.

Phase 3.5 closes two gaps that block Phase 4 (std.core.Throw trait):

  1. TRAIT calls treated as terminal: `_is_terminal_throws_call_expr` must
     recognize a generic trait method call as terminal when the trait
     declaration has `declared_terminal_throws=True`. Without this, the
     load-bearing `Throw::throw_self(move e);` call inside a generic
     `fn f<T>(e: T) throws require T is Throw` body is not treated as
     a terminator and the body-flow check rejects it.

  2. Trait/impl terminal-throws match: an impl method must exactly match
     the trait declaration's terminal-throws status. An impl that declares
     a value-returning signature for a trait method declared as bare
     terminal `throws` must be a checker error.

Trait method calls use qualified call syntax (`Trait::method(e)`) because
the regular method call path (`e.method()`) does not resolve trait impl
methods. This is a pre-existing language constraint; Phase 4 will use
the qualified form in the `Try` impl body as well.

Each test introspects the lowered checker diagnostics directly via
`driftc_main`, not just `rc == 0`.
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
# Positive: generic trait terminal call (qualified syntax) treated as terminator
# ---------------------------------------------------------------------------


def test_generic_trait_terminal_call_is_terminator(tmp_path: Path, capsys) -> None:
	"""A generic function with `require T is Throw` whose body calls
	`Throw::throw_self(move e);` — where `throw_self` is a bare terminal
	`throws` trait method — must compile cleanly. The trait call must
	be treated as a terminator for body-flow analysis."""
	source = """
module m;

error Boom {}
trait Throw {
	fn throw_self(self: Self) throws;
}

struct E {}

implement Throw for E {
	pub fn throw_self(self: E) throws {
		throw Boom();
	}
}

fn f<T>(e: T) throws require T is Throw {
	Throw::throw_self(move e);
}

pub fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"generic trait terminal call should be treated as terminator; diags={_diag_messages(payload)}"


def test_generic_trait_terminal_call_in_match_arm(tmp_path: Path, capsys) -> None:
	"""A match arm whose block contains only a generic terminal-throws
	trait call must be locally terminal — same as a direct call."""
	source = """
module m;

error Boom {}
trait Throw {
	fn throw_self(self: Self) throws;
}

struct E {}

implement Throw for E {
	pub fn throw_self(self: E) throws {
		throw Boom();
	}
}

variant Choice { A, B }

fn handle<T>(c: Choice, e: T) throws require T is Throw {
	match c {
		A => { Throw::throw_self(move e); },
		B => { throw Boom(); },
	}
}

pub fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"trait terminal call in match arm should be terminal; diags={_diag_messages(payload)}"


def test_concrete_trait_terminal_call_is_terminator(tmp_path: Path, capsys) -> None:
	"""A concrete (non-generic) qualified call to a terminal-throws trait
	method must also be treated as a terminator."""
	source = """
module m;

error Boom {}
trait Throw {
	fn throw_self(self: Self) throws;
}

struct MyError {}

implement Throw for MyError {
	pub fn throw_self(self: MyError) throws {
		throw Boom();
	}
}

fn fail_with(e: MyError) throws {
	Throw::throw_self(move e);
}

pub fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"concrete trait terminal call should be terminal; diags={_diag_messages(payload)}"


# ---------------------------------------------------------------------------
# Positive: non-terminal trait method call is NOT a terminator
# ---------------------------------------------------------------------------


def test_non_terminal_trait_method_not_treated_as_terminator(tmp_path: Path, capsys) -> None:
	"""A trait method that returns a value (not bare terminal `throws`)
	must NOT be treated as a terminator. A `throws` body that only calls
	such a method must fail body-flow analysis."""
	source = """
module m;

error Boom {}
trait Converter {
	fn convert(self: Self) -> Int;
}

struct E {}

implement Converter for E {
	pub fn convert(self: E) -> Int {
		return 42;
	}
}

fn f<T>(e: T) throws require T is Converter {
	Converter::convert(move e);
}

pub fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	msgs = _diag_messages(payload)
	assert rc != 0, f"non-terminal trait call should not satisfy body-flow; diags={msgs}"
	assert any("terminal" in m.lower() or "throw" in m.lower() or "return" in m.lower() for m in msgs), \
		f"expected a body-flow diagnostic; got: {msgs}"


# ---------------------------------------------------------------------------
# Negative: trait/impl terminal-throws mismatch
# ---------------------------------------------------------------------------


def test_impl_returns_value_for_terminal_throws_trait_method(tmp_path: Path, capsys) -> None:
	"""An impl declares `fn throw_self(self: E) -> Int` for a trait method
	declared as bare terminal `throws`. This must be a checker error —
	the impl violates the terminal contract."""
	source = """
module m;

trait Throw {
	fn throw_self(self: Self) throws;
}

struct E {}

implement Throw for E {
	pub fn throw_self(self: E) -> Int {
		return 0;
	}
}

pub fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	msgs = _diag_messages(payload)
	assert rc != 0, f"impl returning value for terminal-throws trait method should fail; diags={msgs}"
	assert any("terminal" in m.lower() or "throws" in m.lower() or "mismatch" in m.lower() for m in msgs), \
		f"expected a terminal-throws mismatch diagnostic; got: {msgs}"


def test_impl_terminal_throws_for_value_returning_trait_method(tmp_path: Path, capsys) -> None:
	"""An impl declares `fn convert(self: E) throws` (bare terminal) for
	a trait method declared as `fn convert(self: Self) -> Int`. This is
	also a mismatch and must be a checker error."""
	source = """
module m;

error Boom {}
trait Converter {
	fn convert(self: Self) -> Int;
}

struct E {}

implement Converter for E {
	pub fn convert(self: E) throws {
		throw Boom();
	}
}

pub fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	msgs = _diag_messages(payload)
	assert rc != 0, f"impl with terminal throws for value-returning trait method should fail; diags={msgs}"
	assert any("terminal" in m.lower() or "throws" in m.lower() or "mismatch" in m.lower() for m in msgs), \
		f"expected a terminal-throws mismatch diagnostic; got: {msgs}"


def test_impl_matches_terminal_throws_exactly(tmp_path: Path, capsys) -> None:
	"""An impl that exactly matches a terminal-throws trait method —
	both declaration and impl are bare `throws` — must compile cleanly."""
	source = """
module m;

error Boom {}
trait Throw {
	fn throw_self(self: Self) throws;
}

struct E {}

implement Throw for E {
	pub fn throw_self(self: E) throws {
		throw Boom();
	}
}

pub fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"impl matching terminal-throws trait method should compile; diags={_diag_messages(payload)}"
