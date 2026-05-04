# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: `Result<T, E>.or_throw()` over `pub error` Errs.

Pins:

  1. `Result::Err(E(...)).or_throw()` throws `E` directly — caller
     catches it as `catch E(e)` with field access.  No
     `ResultError` wrapping (spec §3.3).
  2. `Result::Ok(v).or_throw()` returns `v` without throwing.
  3. `or_throw()` on a non-`pub error` Err type is rejected at
     compile time with diagnostic `E_OR_THROW_NOT_ERROR_TYPE` —
     Phase 5a strict enforcement (spec §3.2).

**Out of scope:** Phase 5c global `Result<T, E>` strictness (spec
§3.2 — deferred to 0.33.0 unless cheap; warning probe at
non-error Err position is left for implementation-phase tests).

Spec: `work/exception-diagnostics-context/slice5-spec.md` §3.
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


# ── Probe 1 ─ or_throw throws pub error directly ───────────────────


def test_or_throw_throws_pub_error_directly(tmp_path, capsys):
	"""`Result::Err(ParseError(...)).or_throw()` throws ParseError
	directly; `catch ParseError(e)` binds it with typed field
	access.  No `ResultError` wrapper appears."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

fn try_parse() nothrow -> core.Result<Int, ParseError> {
\treturn core.Result::Err(ParseError(offset = 7));
}

fn main() nothrow -> Int {
\ttry {
\t\treturn try_parse().or_throw();
\t} catch ParseError(e) {
\t\treturn e.offset;
\t}
}
""")
	_ok(rc, errs, "or_throw throws pub error directly")


# ── Probe 2 ─ or_throw returns Ok value ────────────────────────────


def test_or_throw_returns_ok_value(tmp_path, capsys):
	"""`Result::Ok(v).or_throw()` returns `v` without throwing —
	pins the success-path semantics of or_throw."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

fn try_parse() nothrow -> core.Result<Int, ParseError> {
\treturn core.Result::Ok(42);
}

fn main() nothrow -> Int {
\ttry {
\t\treturn try_parse().or_throw();
\t} catch ParseError(e) {
\t\treturn -1;
\t}
}
""")
	_ok(rc, errs, "or_throw returns Ok value")


# ── Probe 3 ─ or_throw rejects non-pub-error Err ───────────────────


def test_or_throw_rejects_non_error_err_type(tmp_path, capsys):
	"""`Result<Int, Int>.or_throw()` is a compile error — Phase 5a
	strict enforcement requires the Err type to be a `pub error`.
	Diagnostic: `E_OR_THROW_NOT_ERROR_TYPE`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval r: core.Result<Int, Int> = core.Result::Err(42);
\ttry {
\t\treturn r.or_throw();
\t} catch {
\t\treturn -1;
\t}
}
""")
	_fails_with_code(rc, errs, 'E_OR_THROW_NOT_ERROR_TYPE',
		"or_throw rejects non-pub-error Err")
