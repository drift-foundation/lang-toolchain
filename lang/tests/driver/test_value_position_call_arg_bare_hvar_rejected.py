# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression: value-position by-value call args with
a bare non-Copy named owner must be rejected with the friendly
`cannot copy 'X': type 'T' is not Copy (use move X)` diagnostic —
same shape as statement-position
(`test_use_move_call_arg_friendly_diag.py`).

The Drift ownership contract (spec §1.3) requires explicit `move`
at every named non-Copy ownership transfer.  Statement-position
already enforces this via the MIR validator's friendly diagnostic;
value-position (the call's result is bound by `val r = f(x)` or
nested inside a larger expression) silently emitted `MoveOut` for
non-Copy bare HVar, accepting source that violates the contract.

Two repros pinned here:

1. `val r = consume(x);` — value-position, top-level bound result.
2. `val r = wrap(consume(x));` — value-position, nested in a larger
   expression.

Both must fail compile with the friendly diagnostic naming `x`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _compile_json(tmp_path: Path, source: str, *, stem: str) -> tuple[int, dict]:
	src = tmp_path / f"{stem}.drift"
	src.write_text(source)
	out = tmp_path / f"{stem}_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out),
		 "--json"],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	payload = json.loads(res.stdout) if res.stdout.strip() else {}
	return res.returncode, payload


def _assert_friendly_use_move(payload: dict, *, binder: str) -> None:
	diags = payload.get("diagnostics") or []
	assert diags, payload
	messages = [str(d.get("message", "")) for d in diags]
	friendly = [
		m for m in messages
		if f"use move {binder}" in m and f"'{binder}'" in m
	]
	assert friendly, (
		f"expected friendly use-move diagnostic for '{binder}', got: "
		f"{messages}"
	)
	for m in messages:
		assert "internal:" not in m, m
		assert "MIR validation" not in m, m
		assert "MIR invariant" not in m, m


_TOP_LEVEL_SOURCE = """\
module main;

import std.core as core;
import std.core.arc as arc;

pub struct Gateway {
\tpub name: String
}

fn consume(g: arc.Arc<Gateway>) nothrow -> Int {
\tval a = g.get();
\treturn 42;
}

fn main() nothrow -> Int {
\tval x = arc.arc(Gateway(name = "t"));
\t// VALUE-POSITION: result bound by `val r = ...`.
\t// `x` is a named non-Copy owner.  Per spec §1.3, bare `x`
\t// here must be rejected with the friendly use-move diag.
\tval r = consume(x);
\treturn r;
}
"""


_NESTED_SOURCE = """\
module main;

import std.core as core;
import std.core.arc as arc;

pub struct Gateway {
\tpub name: String
}

fn consume(g: arc.Arc<Gateway>) nothrow -> Int {
\tval a = g.get();
\treturn 42;
}

fn wrap(n: Int) nothrow -> Int {
\treturn n + 1;
}

fn main() nothrow -> Int {
\tval x = arc.arc(Gateway(name = "t"));
\t// VALUE-POSITION nested in a larger expression.  Same
\t// rule: bare `x` at by-value call arg is rejected.
\tval r = wrap(consume(x));
\treturn r;
}
"""


def test_value_position_top_level_bare_hvar_rejected(tmp_path: Path) -> None:
	"""Pins `val r = consume(x);` (value-position top-level) must
	fail compile with the friendly `cannot copy 'x' ... use move x`
	diag.  Pre-fix the compiler silently emitted MoveOut at the
	call-arg site and the program compiled.
	"""
	rc, payload = _compile_json(tmp_path, _TOP_LEVEL_SOURCE, stem="top")
	assert rc != 0, (
		"value-position bare HVar at by-value call arg compiled — "
		"violates Drift's explicit-ownership-transfer contract "
		"(spec §1.3).\npayload: " + str(payload)
	)
	_assert_friendly_use_move(payload, binder="x")


def test_value_position_nested_bare_hvar_rejected(tmp_path: Path) -> None:
	"""Pins `val r = wrap(consume(x));` (value-position nested in a
	larger expression) must fail compile with the same friendly
	diag.  Same contract; the call-arg site is the consume target
	regardless of where the result is used.
	"""
	rc, payload = _compile_json(tmp_path, _NESTED_SOURCE, stem="nested")
	assert rc != 0, (
		"nested value-position bare HVar at by-value call arg "
		"compiled — violates Drift's explicit-ownership-transfer "
		"contract (spec §1.3).\npayload: " + str(payload)
	)
	_assert_friendly_use_move(payload, binder="x")


_TOP_LEVEL_WITH_MOVE = _TOP_LEVEL_SOURCE.replace(
	"val r = consume(x);", "val r = consume(move x);"
)
_NESTED_WITH_MOVE = _NESTED_SOURCE.replace(
	"val r = wrap(consume(x));", "val r = wrap(consume(move x));"
)


def test_value_position_with_move_compiles(tmp_path: Path) -> None:
	"""Positive companion: writing `move x` at the value-position
	call arg compiles and runs.  Pins the diagnostic's prescription
	is sound for both top-level and nested forms.
	"""
	rc, _payload = _compile_json(tmp_path, _TOP_LEVEL_WITH_MOVE, stem="top_move")
	assert rc == 0, _payload
	rc, _payload = _compile_json(tmp_path, _NESTED_WITH_MOVE, stem="nested_move")
	assert rc == 0, _payload


_METHOD_POSITIONAL_SOURCE = """\
module main;

import std.core as core;
import std.core.arc as arc;

pub struct Gateway {
\tpub name: String
}

pub struct Sink {
\tpub tag: Int
}

implement Sink {
\tpub fn absorb(self: &Sink, g: arc.Arc<Gateway>) nothrow -> Int {
\t\tval a = g.get();
\t\treturn self.tag;
\t}
}

fn main() nothrow -> Int {
\tval sink = Sink(tag = 5);
\tval x = arc.arc(Gateway(name = "t"));
\t// Method call, POSITIONAL by-value non-Copy arg — bare HVar
\t// must be rejected (spec §1.3); receiver `sink` is `&self`
\t// (auto-borrow, unaffected).
\tval r = sink.absorb(x);
\treturn r;
}
"""


def test_method_call_positional_bare_hvar_rejected(tmp_path: Path) -> None:
	"""`sink.absorb(x)` — method call with a bare non-Copy named
	owner at a positional by-value arg.  Must fail with the
	friendly `cannot copy 'x' ... use move x` diag.  Pins that the
	gate covers HMethodCall positional args, not just free-function
	HCall args (the receiver `&self` auto-borrow is unaffected).
	"""
	rc, payload = _compile_json(tmp_path, _METHOD_POSITIONAL_SOURCE, stem="method_pos")
	assert rc != 0, (
		"method-call positional bare HVar at by-value arg compiled — "
		"violates spec §1.3.\npayload: " + str(payload)
	)
	_assert_friendly_use_move(payload, binder="x")


def test_method_call_positional_with_move_compiles(tmp_path: Path) -> None:
	"""Positive companion: `sink.absorb(move x)` compiles."""
	src = _METHOD_POSITIONAL_SOURCE.replace(
		"val r = sink.absorb(x);", "val r = sink.absorb(move x);"
	)
	rc, payload = _compile_json(tmp_path, src, stem="method_pos_move")
	assert rc == 0, payload


_FUNCTION_VALUE_INVOKE_SOURCE = """\
module main;

import std.core as core;
import std.core.arc as arc;

pub struct Gateway {
\tpub name: String
}

fn absorb(g: arc.Arc<Gateway>) nothrow -> Int {
\tval a = g.get();
\treturn 7;
}

fn main() nothrow -> Int {
\tval f: Fn(arc.Arc<Gateway>) nothrow -> Int = absorb;
\tval x = arc.arc(Gateway(name = "t"));
\t// HInvoke: call through a function VALUE `f`.  A bare non-Copy
\t// named owner at the by-value arg must be rejected (spec §1.3),
\t// same as direct HCall / HMethodCall.
\tval r = f(x);
\treturn r;
}
"""


def test_function_value_invoke_bare_hvar_rejected(tmp_path: Path) -> None:
	"""`f(x)` where `f` is a function VALUE (HInvoke) and `x` is a
	bare non-Copy named owner at a by-value arg.  Must fail with
	the friendly `cannot copy 'x' ... use move x` diag.  Pins that
	the gate covers HInvoke (function-value / callable
	invocation), not just direct HCall / HMethodCall — the
	`_lower_indirect_call` path routes HInvoke args through
	`_lower_call_arg` and would otherwise silent-move them.
	"""
	rc, payload = _compile_json(tmp_path, _FUNCTION_VALUE_INVOKE_SOURCE, stem="invoke")
	assert rc != 0, (
		"HInvoke (function-value) bare HVar at by-value arg "
		"compiled — violates spec §1.3.\npayload: " + str(payload)
	)
	_assert_friendly_use_move(payload, binder="x")


def test_function_value_invoke_with_move_compiles(tmp_path: Path) -> None:
	"""Positive companion: `f(move x)` through a function value
	compiles."""
	src = _FUNCTION_VALUE_INVOKE_SOURCE.replace(
		"val r = f(x);", "val r = f(move x);"
	)
	rc, payload = _compile_json(tmp_path, src, stem="invoke_move")
	assert rc == 0, payload
