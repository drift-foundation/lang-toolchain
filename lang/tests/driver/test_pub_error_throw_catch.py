# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: throw/catch surface for `pub error` types.

Pins:

  1. `throw E(...)` over a `pub error` type unwinds into a typed
     `catch E(e) { ... }` arm, with field access on the bound `e`.
     Typed catch-binding works because synthesized projection comes
     paired with synthesized internal reverse parsing (spec §7.5).
  2. Multiple typed catch arms over distinct `pub error` types
     compile and route by event identity.
  3. `catch *` fallback covers any thrown `pub error` not matched
     by a preceding typed arm; the implicit binder is the opaque
     envelope handle (no typed field access).

**Out of scope:** `Result.or_throw()` (test_pub_error_or_throw.py),
manual `Diagnostic` impls (test_pub_error_manual_diagnostic.py),
re-throw event-identity preservation (deferred to
implementation-phase tests).

Spec: `work/exception-diagnostics-context/slice5-spec.md` §4-§5.
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


# ── Probe 1 ─ throw + typed catch with field access ────────────────


@_SLICE_5_PENDING
def test_throw_pub_error_typed_catch_field_access(tmp_path, capsys):
	"""`throw ParseError(...)` lands in `catch ParseError(e)`; the
	bound `e` supports field access on declared fields.  Typed
	catch-binding is the dominant product path and works for free
	via synthesized projection."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\tmessage: String,
\toffset: Int,
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(message = "bad", offset = 12);
}

fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\treturn e.offset;
\t}
}
""")
	_ok(rc, errs, "throw + typed catch with field access")


# ── Probe 2 ─ two typed catch arms compile ─────────────────────────


@_SLICE_5_PENDING
def test_two_typed_catch_arms_compile(tmp_path, capsys):
	"""Two typed catch arms over distinct `pub error` types both
	compile.  Runtime routing by event identity is verified at the
	e2e level; this probe pins the static surface."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

pub error CodecError {
\tkind: String,
}

fn risky(which: Int) throws ParseError, CodecError -> Int {
\tif which == 0 {
\t\tthrow ParseError(offset = 1);
\t}
\tthrow CodecError(kind = "utf8");
}

fn main() nothrow -> Int {
\ttry {
\t\treturn risky(0);
\t} catch ParseError(e) {
\t\treturn e.offset;
\t} catch CodecError(e) {
\t\treturn 99;
\t}
}
""")
	_ok(rc, errs, "two typed catch arms compile")


# ── Probe 3 ─ catch * fallback after typed arms ────────────────────


@_SLICE_5_PENDING
def test_catch_wildcard_after_typed_arm(tmp_path, capsys):
	"""`catch *` fallback after a typed arm compiles; pins that
	wildcard catch remains available alongside typed catch."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

pub error OtherError {}

fn risky() throws OtherError -> Int {
\tthrow OtherError();
}

fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\treturn e.offset;
\t} catch * {
\t\treturn -1;
\t}
}
""")
	_ok(rc, errs, "catch wildcard after typed arm")
