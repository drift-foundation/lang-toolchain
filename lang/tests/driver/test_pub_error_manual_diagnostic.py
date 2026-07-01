# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: manual `Diagnostic` override on `pub error` types.

This file protects the manual/auto boundary (spec §7.5):

  * A type has exactly ONE Diagnostic JSON owner.
  * If a type manually implements `Diagnostic`, that impl owns the
    complete JSON shape — no blending of manual field behavior with
    compiler-generated outer shape.
  * If a `pub error` has no manual `Diagnostic` impl, the compiler
    may synthesize one (only if all fields are projectable).

Pins:

  1. A manual `implement core.Diagnostic for E` override skips
     synthesis; the user-supplied `to_json_text` is dispatched in
     place of the synthesized projection.
  2. **Binder-less catch works:** `catch SecretError { ... }`
     (no typed binder) compiles for a manually-projected `pub
     error`.  Envelope access via `e.params.get(...)` remains
     available.
  3. **Typed catch-binding is REJECTED** for manually-projected
     `pub error` types — diagnostic
     `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED` (spec §7.5).  This
     is the load-bearing assertion that keeps Slice 5 from
     ballooning into manual reverse-parsing territory.

**Out of scope:** `DiagnosticParse` trait (deliberately deferred —
spec §7.5); `std.json`-inside-projection (compile-only here, e2e
verifies output).

Spec: `work/exception-diagnostics-context/slice5-spec.md` §7.5-§7.6.
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

use trait core.Diagnostic;
"""


# ── Probe 1 ─ manual override compiles + skips synthesis ───────────


def test_manual_diagnostic_override_compiles(tmp_path, capsys):
	"""User-supplied `implement Diagnostic for E` skips synthesis;
	the manual `to_json_text` is dispatched.  Built-in helpers
	from std.core (diagnostic_json_int) are usable inside the
	body."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error SecretError {
\tuser_id: Int,
\tsecret_token: String,
}

implement core.Diagnostic for SecretError {
\tpub fn to_json_text(self: &SecretError) nothrow -> String {
\t\t// Redacted projection — secret_token never appears in output.
\t\treturn "{\\"user_id\\":" + core.diagnostic_json_int(self.user_id) + "}";
\t}
}

pub fn main() nothrow -> Int {
\tval e: SecretError = SecretError(user_id = 42, secret_token = "shhh");
\tval s: String = e.to_json_text();
\treturn 0;
}
""")
	_ok(rc, errs, "manual Diagnostic override compiles")


# ── Probe 2 ─ binder-less catch works ──────────────────────────────


def test_manual_projection_binderless_catch_works(tmp_path, capsys):
	"""`catch SecretError { ... }` (no typed binder) compiles when
	SecretError has a manual `Diagnostic` impl.  Envelope access
	is via the implicit `e` (opaque envelope handle)."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error SecretError {
\tuser_id: Int,
\tsecret_token: String,
}

implement core.Diagnostic for SecretError {
\tpub fn to_json_text(self: &SecretError) nothrow -> String {
\t\treturn "{\\"user_id\\":" + core.diagnostic_json_int(self.user_id) + "}";
\t}
}

fn risky() throws SecretError -> Int {
\tthrow SecretError(user_id = 42, secret_token = "shhh");
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch SecretError {
\t\treturn 0;
\t}
}
""")
	_ok(rc, errs, "manual projection binder-less catch")


# ── Probe 3 ─ typed binder rejected on manual projection ──────────


def test_manual_projection_typed_binder_rejected(tmp_path, capsys):
	"""`catch SecretError(e)` (typed binder) is REJECTED at compile
	time when SecretError has a manual `Diagnostic` impl —
	diagnostic `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED` (spec
	§7.5).  This is the v1 scope-cut boundary; manual reverse
	parsing is a follow-up design track."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error SecretError {
\tuser_id: Int,
\tsecret_token: String,
}

implement core.Diagnostic for SecretError {
\tpub fn to_json_text(self: &SecretError) nothrow -> String {
\t\treturn "{\\"user_id\\":" + core.diagnostic_json_int(self.user_id) + "}";
\t}
}

fn risky() throws SecretError -> Int {
\tthrow SecretError(user_id = 42, secret_token = "shhh");
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch SecretError(e) {
\t\treturn e.user_id;
\t}
}
""")
	_fails_with_code(rc, errs, 'E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED',
		"typed binder rejected on manual projection")


# ── Probe 4 ─ typed binder rejected on manual projection in
#               EXPRESSION-form try/catch — same boundary as Probe 3
#               extended to `try expr catch X(e) { e.field }` ─────────


def test_manual_projection_typed_binder_rejected_expression_form(tmp_path, capsys):
	"""Mirror of Probe 3 for the expression-form `val x = try
	risky() catch SecretError(e) { e.user_id }`.  Slice 7a follow-up
	(2026-05-05): the manual-Diagnostic boundary applies regardless of
	statement-form or expression-form try/catch — they share the same
	binder semantics.  Without an expression-form gate, user code
	could bypass `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED` simply by
	rewriting `try { ... } catch X(e) { ... }` to `try ... catch X(e)
	{ ... }`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error SecretError {
\tuser_id: Int,
\tsecret_token: String,
}

implement core.Diagnostic for SecretError {
\tpub fn to_json_text(self: &SecretError) nothrow -> String {
\t\treturn "{\\"user_id\\":" + core.diagnostic_json_int(self.user_id) + "}";
\t}
}

fn risky() throws SecretError -> Int {
\tthrow SecretError(user_id = 42, secret_token = "shhh");
}

pub fn main() nothrow -> Int {
\tval x = try risky() catch SecretError(e) {
\t\te.user_id
\t} catch {
\t\t-1
\t};
\treturn x;
}
""")
	_fails_with_code(rc, errs, 'E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED',
		"typed binder rejected on manual projection (expression-form)")
