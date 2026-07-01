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


# ── Probe 1 ─ DiagnosticValue user-code naming rejected ───────────
#
# Slice 7a (0.31.62, 2026-05-05): flipped live alongside the workspace
# pre-scan DV/DiagnosticEntry rejection in `_resolve_type_expr_in_file`.


def test_diagnostic_value_user_code_rejected(tmp_path, capsys):
	"""`core.DiagnosticValue::Int(...)` from user code fails
	compile with `E_DV_PUBLIC_REMOVED`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tval _dv = core.DiagnosticValue::Int(42);
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_DV_PUBLIC_REMOVED',
		"DiagnosticValue user-code naming rejected")


# ── Probe 2 ─ DiagnosticEntry user-code naming rejected ────────────
#
# Slice 7a (0.31.62, 2026-05-05): flipped live with the same rejection
# pass that catches DiagnosticValue.


def test_diagnostic_entry_user_code_rejected(tmp_path, capsys):
	"""`core.DiagnosticEntry` named from user code fails compile
	with `E_DV_PUBLIC_REMOVED`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn make_one() nothrow -> core.DiagnosticEntry {
\treturn core.diagnostic_entry("k", core.DiagnosticValue::Null());
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_DV_PUBLIC_REMOVED',
		"DiagnosticEntry user-code naming rejected")


# ── Probe 3 ─ e.attrs[k] indexer rejected ──────────────────────────
#
# Slice 7a (0.31.62, 2026-05-05): flipped live with the stdlib-gated
# rejection in checker + type_checker for `Error.attrs[...]`.


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

pub fn main() nothrow -> Int {
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
#
# Slice 7a (0.31.62, 2026-05-05): flipped live with the stdlib-gated
# rejection in checker + type_checker for `Error.captures[...]`.


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

pub fn main() nothrow -> Int {
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


# ── Probe 6 ─ unqualified `DiagnosticValue::Variant` ctor rejected ─
#
# Slice 7a follow-up (2026-05-05, K finding 1): the workspace pre-scan
# rejection in `_resolve_type_expr_in_file` only fired when DV arrived
# through a module alias (`core.DiagnosticValue`).  Unqualified
# `DiagnosticValue::Int(...)` slipped through because stage1 normalize
# rewrites it directly to `HDVInit` without consulting the alias map.
# This probe pins that the unqualified variant ctor surface is also
# rejected with `E_DV_PUBLIC_REMOVED`.


def test_unqualified_diagnostic_value_variant_ctor_rejected(tmp_path, capsys):
	"""`DiagnosticValue::Int(42)` (unqualified) from user code fails
	compile with `E_DV_PUBLIC_REMOVED`.  Closes the path through
	`stage1/normalize.py:_rewrite_expr` HDVInit lowering."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tval _dv = DiagnosticValue::Int(42);
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_DV_PUBLIC_REMOVED',
		"unqualified DiagnosticValue variant ctor rejected")


# ── Probe 7 ─ unqualified `DiagnosticValue` type position rejected ─


def test_unqualified_diagnostic_value_type_position_rejected(tmp_path, capsys):
	"""`DiagnosticValue` as a type (e.g. struct field, fn param,
	`pub error` field) from user code fails compile with
	`E_DV_PUBLIC_REMOVED`.  Closes the type-position path through the
	resolver's built-in name fallback."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error DvErr {
\tpayload: DiagnosticValue,
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_DV_PUBLIC_REMOVED',
		"unqualified DiagnosticValue type position rejected")


# ── Probe 8a ─ `core.diagnostic_entry(...)` direct value-call rejected ─


def test_qualified_diagnostic_entry_value_call_rejected(tmp_path, capsys):
	"""`core.diagnostic_entry("k", v)` direct value-call (no
	`DiagnosticEntry` annotation, no `DiagnosticValue::*` ctor) from
	user code fails compile with `E_DV_PUBLIC_REMOVED`.  Pins the
	value-reference surface in isolation — without this gate the
	diagnostic falls through to the generic "module 'std.core' does
	not export symbol 'diagnostic_entry'", which doesn't point at
	the migration."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tval _ = core.diagnostic_entry("k", core.diagnostic_json_null());
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_DV_PUBLIC_REMOVED',
		"core.diagnostic_entry value-call rejected")


# ── Probe 8b ─ user-defined `fn diagnostic_entry(...)` compiles ─────


def test_user_fn_named_diagnostic_entry_compiles(tmp_path, capsys):
	"""A user-defined `fn diagnostic_entry(...)` of the same name as
	the retired std.core helper must still compile and resolve to the
	user's function.  Slice 7a follow-up (K finding 1 v2,
	2026-05-05): the prior spelling-based parser gate created a
	false-positive rejection on this shape.  The qualified
	`core.diagnostic_entry(...)` rejection is identity-keyed (in the
	module-qualified call rewriter) and unaffected; user code still
	owns the bare-spelling namespace."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn diagnostic_entry(key: String, value: Int) nothrow -> Int {
\tval _ = key;
\treturn value;
}

pub fn main() nothrow -> Int {
\treturn diagnostic_entry("k", 42);
}
""")
	_ok(rc, errs, "user-defined fn diagnostic_entry compiles")


# ── Probe 9 ─ unqualified `DiagnosticEntry` type position rejected ─


def test_unqualified_diagnostic_entry_type_position_rejected(tmp_path, capsys):
	"""`DiagnosticEntry` as a type (e.g. fn return) from user code
	fails compile with `E_DV_PUBLIC_REMOVED`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn make_one() nothrow -> DiagnosticEntry {
\treturn diagnostic_entry("k", DiagnosticValue::Null());
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_DV_PUBLIC_REMOVED',
		"unqualified DiagnosticEntry type position rejected")


# ── Probe 5 ─ user to_diag impl rejected ───────────────────────────
#
# Slice 7a (0.31.62, 2026-05-05): flipped live with
# `_reject_deprecated_trait_method_shapes` workspace pre-scan rejection.


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

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_TO_DIAG_DEPRECATED',
		"user to_diag impl rejected")
