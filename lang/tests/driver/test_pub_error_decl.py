# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: `pub error` declaration + value-semantic regressions.

Foundational shape/template for the Slice 5 migration.  Pins the
parser/checker/type-kind work for `pub error E { ... }`:

  1. Declaration parses and type-checks (with-fields, empty-payload,
     explicit event_code).
  2. Construction via named-arg constructor produces a usable value.
  3. Field access on the bound value works.
  4. Pass-by-value to a function compiles.
  5. `pub error` is acceptable as the Err type of `Result<T, E>`
     (compile-only — no `.or_throw()` here; that belongs in
     test_pub_error_or_throw.py).

**Throw/catch is deliberately OUT OF SCOPE for this file** — see
test_pub_error_throw_catch.py.  The point of this file is to isolate
parser / checker / type-kind work from runtime/lowering shape, so a
regression in either layer is easy to localize.

All probes are strict-xfail until the Slice 5 implementation lands.
Spec: `work/exception-diagnostics-context/slice5-spec.md`.

When the implementation lands the `_SLICE_5_PENDING` decorator is
removed (one decorator per probe) and the test flips to live.  This
mirrors the strict-xfail-then-flip protocol used for Slices 1-4A.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


_SLICE_5_PENDING = pytest.mark.xfail(
	strict=True,
	reason=(
		"Slice 5 (pub error language migration) not yet implemented; "
		"spec locked at work/exception-diagnostics-context/slice5-spec.md"
	),
)


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[dict]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


def _ok(rc: int, errs: list[dict], label: str) -> None:
	assert rc == 0, (
		f"{label}: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


_PRE = """
module main;

import std.core as core;
"""


# ── Probe 1 ─ declaration parses ───────────────────────────────────


def test_pub_error_decl_with_fields_parses(tmp_path, capsys):
	"""`pub error E { f: T, ... }` declaration parses and
	type-checks.  Foundational gate — every other Slice 5 test
	depends on this."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\tmessage: String,
\toffset: Int,
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_ok(rc, errs, "pub error declaration with fields parses")


def test_pub_error_decl_empty_payload_parses(tmp_path, capsys):
	"""`pub error E {}` (no fields) is a valid declaration.  Empty
	payload is the minimum-shape probe; synthesized projection
	(returning `"{}"`) is asserted in
	test_pub_error_synthesized_diagnostic.py."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ConnectionLost {}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_ok(rc, errs, "pub error empty-payload declaration")


def test_pub_error_decl_explicit_event_code_parses(tmp_path, capsys):
	"""`pub error E(0x1234) { ... }` — explicit event_code form
	parses; the code is bound to the type's event identity (event
	dispatch behavior is asserted in
	test_pub_error_throw_catch.py)."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error CodecError(0x4543) {
\tkind: String,
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_ok(rc, errs, "pub error explicit event_code declaration")


# ── Probe 2 ─ construction with named args ─────────────────────────


def test_pub_error_named_arg_construction(tmp_path, capsys):
	"""Construct a `pub error` value via named-arg constructor;
	produces a usable value bound to a local.  Mirrors the existing
	struct constructor syntax."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\tmessage: String,
\toffset: Int,
}

fn main() nothrow -> Int {
\tval e: ParseError = ParseError(message = "bad", offset = 12);
\treturn 0;
}
""")
	_ok(rc, errs, "pub error named-arg construction")


# ── Probe 3 ─ field access ─────────────────────────────────────────


def test_pub_error_field_access(tmp_path, capsys):
	"""Bound `pub error` value supports field access on its declared
	fields, exactly like a struct.  Tests the read-side of the
	type-kind's value-type contract."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\tmessage: String,
\toffset: Int,
}

fn main() nothrow -> Int {
\tval e: ParseError = ParseError(message = "bad", offset = 12);
\treturn e.offset;
}
""")
	_ok(rc, errs, "pub error field access")


# ── Probe 4 ─ pass-by-value ────────────────────────────────────────


def test_pub_error_pass_by_value(tmp_path, capsys):
	"""`pub error` values pass into functions by value.  Copy
	semantics are inherited from field types per the composition
	rule (String is Copy as of 0.31.53; Int is Copy)."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\tmessage: String,
\toffset: Int,
}

fn use_err(e: ParseError) nothrow -> Int {
\treturn e.offset;
}

fn main() nothrow -> Int {
\tval e: ParseError = ParseError(message = "bad", offset = 12);
\treturn use_err(e);
}
""")
	_ok(rc, errs, "pub error pass-by-value")


# ── Probe 5 ─ Result<T, E> Err type acceptance ─────────────────────


def test_pub_error_as_result_err_type(tmp_path, capsys):
	"""`Result<T, E>` accepts a `pub error` as its Err type.
	Compile-only — no `.or_throw()` here (see
	test_pub_error_or_throw.py).  This pins that the type system
	recognises `pub error` as a valid Err-position type before the
	throw/catch surface comes into scope."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\tmessage: String,
\toffset: Int,
}

fn try_parse(s: String) nothrow -> core.Result<Int, ParseError> {
\treturn core.Result::Err(ParseError(message = "bad", offset = 0));
}

fn main() nothrow -> Int {
\tval r = try_parse("hello");
\treturn 0;
}
""")
	_ok(rc, errs, "pub error as Result Err type")
