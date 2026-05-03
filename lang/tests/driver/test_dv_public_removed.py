# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: removed DV public surfaces emit clear diagnostics.

This file is the negative-side counterpart to the positive Slice 5
tests.  Each probe asserts that a removed surface from Slice 5
fails compile with a specific, actionable diagnostic — protecting
against silent regression of the DV deletion.

Probes:

  1. `core.DiagnosticValue::Int(...)` from user code →
     `E_DV_PUBLIC_REMOVED`.
  2. `core.DiagnosticEntry` named from user code →
     `E_DV_PUBLIC_REMOVED`.
  3. `e.attrs[k]` indexer in catch arm → `E_EXC_ATTRS_REMOVED`.
  4. `e.captures[fr][k]` indexer in catch arm →
     `E_EXC_CAPTURES_REMOVED`.
  5. User `to_diag(...) -> DiagnosticValue` impl on a
     `Diagnostic` trait → `E_TO_DIAG_DEPRECATED`.

**Out of scope:** `to_debug` rejection (covered in
test_debuggable_migration.py); `pub exception` deprecation
warning (test_pub_exception_deprecated.py); throw of
non-`pub-error` (covered in test_pub_error_throw_catch — the
positive shape).

Spec: `work/exception-diagnostics-context/slice5-spec.md` §13.1-§13.2.
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


# ── Probe 1 ─ DiagnosticValue user-code naming rejected ───────────


@_SLICE_5_PENDING
def test_diagnostic_value_user_code_rejected(tmp_path, capsys):
	"""`core.DiagnosticValue::Int(...)` from user code fails
	compile with `E_DV_PUBLIC_REMOVED`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval _dv = core.DiagnosticValue::Int(42);
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_DV_PUBLIC_REMOVED',
		"DiagnosticValue user-code naming rejected")


# ── Probe 2 ─ DiagnosticEntry user-code naming rejected ────────────


@_SLICE_5_PENDING
def test_diagnostic_entry_user_code_rejected(tmp_path, capsys):
	"""`core.DiagnosticEntry` named from user code fails compile
	with `E_DV_PUBLIC_REMOVED`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn make_one() nothrow -> core.DiagnosticEntry {
\treturn core.diagnostic_entry("k", core.DiagnosticValue::Null());
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_DV_PUBLIC_REMOVED',
		"DiagnosticEntry user-code naming rejected")


# ── Probe 3 ─ e.attrs[k] indexer rejected ──────────────────────────


@_SLICE_5_PENDING
def test_exception_attrs_indexer_rejected(tmp_path, capsys):
	"""`e.attrs[k]` in a catch arm fails compile with
	`E_EXC_ATTRS_REMOVED`.  Migration: `e.params.get(k).as_*()`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(offset = 12);
}

fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\tval _attr = e.attrs["offset"];
\t\treturn 0;
\t}
}
""")
	_fails_with_code(rc, errs, 'E_EXC_ATTRS_REMOVED',
		"e.attrs[k] indexer rejected")


# ── Probe 4 ─ e.captures[fr][k] indexer rejected ───────────────────


@_SLICE_5_PENDING
def test_exception_captures_indexer_rejected(tmp_path, capsys):
	"""`e.captures[fr][k]` in a catch arm fails compile with
	`E_EXC_CAPTURES_REMOVED`.  Migration:
	`e.context.encode_compact()` (Slice 4B's typed cursor is
	deferred)."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(offset = 12);
}

fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\tval _cap = e.captures[0]["offset"];
\t\treturn 0;
\t}
}
""")
	_fails_with_code(rc, errs, 'E_EXC_CAPTURES_REMOVED',
		"e.captures[fr][k] indexer rejected")


# ── Probe 5 ─ user to_diag impl rejected ───────────────────────────


@_SLICE_5_PENDING
def test_user_to_diag_impl_rejected(tmp_path, capsys):
	"""User impl of `Diagnostic.to_diag(...) -> DiagnosticValue`
	(the OLD shape) fails compile with `E_TO_DIAG_DEPRECATED`.
	Migration: `to_json_text(&Self) nothrow -> String`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct UserId {
\tvalue: Int,
}

implement core.Diagnostic for UserId {
\tpub fn to_diag(self: &UserId) nothrow -> core.DiagnosticValue {
\t\treturn core.DiagnosticValue::Int(self.value);
\t}
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_TO_DIAG_DEPRECATED',
		"user to_diag impl rejected")
