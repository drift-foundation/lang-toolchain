# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: throw/catch surface for `pub error` types.

Pins:

  1. `throw E(...)` over a `pub error` type lands in a typed
     `catch E(e) { ... }` arm and the typed catch arm claims
     coverage of narrow declared throws (no `nothrow` violation
     in the outer scope).  Probe bodies use literal returns —
     they do NOT exercise typed-binder field access.
  2. Multiple typed catch arms over distinct `pub error` types
     compile and route by event identity (verified at e2e level).
  3. Bare `catch` (catch-all-no-binder) fallback covers any
     thrown `pub error` not matched by a preceding typed arm.
  4. (DEFERRED, strict-xfail) Genuine typed-binder typing —
     `catch ParseError(e)` should bind `e: ParseError` (the
     parallel struct), not `e: Error`.  Slice 2B did NOT land
     this; the deferred probe (`test_typed_catch_binder_is_struct_xfail`)
     exercises the binder TYPE directly via a fn-arg type
     mismatch and stays xfailed pending slice 3.

**Out of scope:** `Result.or_throw()` (test_pub_error_or_throw.py),
manual `Diagnostic` impls (test_pub_error_manual_diagnostic.py),
re-throw event-identity preservation (deferred to
implementation-phase tests).

Note: an earlier draft of this file claimed typed-binder field
access (`e.offset`) was pinned by these probes.  That was a false
positive — `e.offset` on `e: Error` happens to compile via a
permissive HField fallback to Unknown.  The probes have been
trimmed to literal-return bodies; the genuine typed-binder probe
is split out as the deferred xfail.

Spec: `work/exception-diagnostics-context/slice5-spec.md` §4-§5,
§23.5 (binder-typing deferral).
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


def _fails_with_code(rc: int, errs: list[dict], code: str, label: str) -> None:
	codes = [e.get('code') for e in errs]
	assert rc != 0, f"{label}: expected compile failure but rc=0"
	assert code in codes, (
		f"{label}: expected diagnostic {code} not in {codes}\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


_PRE = """
module main;

import std.core as core;
"""


# ── Probe 1 ─ throw + typed catch with field access ────────────────


def test_throw_pub_error_typed_catch_field_access(tmp_path, capsys):
	"""`throw ParseError(...)` lands in `catch ParseError(e)`; the
	bound `e` supports field access on declared fields.  Typed
	catch-binding is the dominant product path and works for free
	via synthesized projection.

	Slice 5 / 2B note: this probe asserts only that typed catch
	covers the throws and the body type-checks.  Typed-binder field
	access (`e.offset`) is NOT exercised here — see
	`test_typed_catch_binder_is_struct_xfail` below for the
	deferred probe."""
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
\t\treturn 12;
\t}
}
""")
	_ok(rc, errs, "throw + typed catch coverage")


# ── Probe 2 ─ two typed catch arms compile ─────────────────────────


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
\t\treturn 12;
\t} catch CodecError(e) {
\t\treturn 99;
\t}
}
""")
	_ok(rc, errs, "two typed catch arms compile")


# ── Probe 3 ─ catch-all fallback after typed arms ──────────────────


def test_catch_wildcard_after_typed_arm(tmp_path, capsys):
	"""Bare `catch` (catch-all) fallback after a typed arm compiles;
	pins that catch-all remains available alongside typed catch.
	(Drift spells the catch-all as bare `catch` or `catch e`, not
	`catch *`.)"""
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
\t\treturn 12;
\t} catch {
\t\treturn -1;
\t}
}
""")
	_ok(rc, errs, "catch-all fallback after typed arm")


# ── Probe 4 ─ typed binder field access (DEFERRED to slice 3) ──────


_FIELD_ACCESS_DEFERRED = pytest.mark.xfail(
	strict=True,
	reason=(
		"Typed catch-binder materialization for `pub error` types is "
		"deferred to slice 3 (alongside synthesized reverse projection). "
		"The current `e: Error` binding type-checks `e.offset` as a "
		"permissive HField fallback (Unknown) — that's not real typed "
		"field access and makes any test relying on it a false positive. "
		"This probe exists so the eventual flip pins the genuine binder + "
		"struct field-access surface."
	),
)


@_FIELD_ACCESS_DEFERRED
def test_typed_catch_binder_is_struct_xfail(tmp_path, capsys):
	"""Genuine typed-binder type check: `catch ParseError(e)` binds
	`e: ParseError` (the parallel struct from Path A), not `e: Error`.
	The probe pins this by passing `e` to a function whose parameter
	type IS ParseError — under the current slice 2B revert, `e: Error`
	makes that call fail with a type mismatch.

	Asserting field access via `e.offset` alone would XPASS today
	even with `e: Error` because Error's HField has a permissive
	Unknown fallback that the type-checker accepts; that's a false
	positive.  This probe avoids the fallback by exercising the
	binder TYPE directly.

	Deferred to slice 3: catch-binder materialization (struct method
	injection or HField fallback lookup) lands alongside synthesized
	reverse projection.
	"""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

fn assert_parse_err(p: ParseError) nothrow -> Int {
\treturn p.offset;
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(offset = 12);
}

fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\treturn assert_parse_err(e);
\t}
}
""")
	_ok(rc, errs, "typed catch binder is ParseError struct (deferred)")


# ── Probe 5 ─ throws clause rejects non-error types ────────────────


def test_throws_clause_rejects_non_error_type(tmp_path, capsys):
	"""`fn f() throws E -> T` where `E` is not a `pub error` /
	`error` kind is rejected with `E_THROWS_NOT_ERROR_TYPE` at the
	throws-clause site.  Pins the typed-throws validation that
	landed in slice 2B (`_resolve_declared_throws_types`)."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn risky() throws Int -> Int {
\treturn 0;
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_THROWS_NOT_ERROR_TYPE',
		"throws clause rejects non-error type")


# ── Probe 6 ─ public function leaks private error in throws ────────


def test_pub_fn_throws_private_error_rejected(tmp_path, capsys):
	"""`pub fn f() throws PrivateError` is rejected with
	`E_PRIVATE_ERROR_LEAKED_VIA_PUB` — slice 2B visibility coherence
	(spec §2.3.1).  A module-private `error E { ... }` declaration
	cannot appear in the throws clause of a `pub fn` because that
	would leak the private type through the API surface."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
error PrivateE {}

pub fn f() throws PrivateE -> Int {
\tthrow PrivateE();
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_PRIVATE_ERROR_LEAKED_VIA_PUB',
		"public function leaks private error in throws clause")
