# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: Bug A — auto-try contract leaking across the
lambda body boundary (0.31.38).

The auto-try contract eager-unwraps `Result<T, E>` to `T` inside any
`throws` function or `try {}` block.  Pre-fix, the relevant state
(`fn_declared_throws`, `try_block_depth`) was a closure-captured local
in `type_checker.py:_check_function_body` set ONCE from the outer
function's signature with **no save/restore around lambda body
type-checking**.  A nothrow lambda nested inside a `throws` outer fn
inherited `fn_declared_throws=True` for the duration of its body
type-check, eager-unwrapped `Result`-bearing bindings, and
subsequently broke user code that explicitly pattern-matched on the
Result (`val r = call(); match &r { Ok(_)=>..., Err(_)=>... }`) — the
unwrapped `r: T` failed the variant-scrutinee check with "match
scrutinee must be a variant type."

Surfaced by the bookkeeper / web-rest 0.4.1 middleware report on
driftc 0.31.37 (2026-04-30).  The user's A/B narrowing showed flipping
the enclosing function between `nothrow` and `throws` was the only
variable toggling the build outcome.  Same lambda body, same import
set, same call site.

Coverage gap: the 0.31.33 / 0.31.35 `match &Variant` cert tests
(`test_match_by_ref_variant.py`,
`test_match_by_mut_ref_variant_probes.py`) all use `nothrow` test
fixtures; none exercise the `throws outer fn × nothrow lambda × match
&Result` shape.  This file pins that shape.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[str]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d.get("message", "") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


_PRE = """
module main;
import std.core as core;

error Bang {}
fn make_ok() nothrow -> core.Result<Int, String> {
\treturn core.Result::Ok(1);
}
fn run_cb(cb: core.Callback0<Int>) nothrow -> Int {
\treturn cb.call();
}
fn run_cb_throws(cb: core.CallbackThrow0<Int>) throws -> Int {
\treturn cb.call();
}
fn maybe_throw() throws -> Int {
\tthrow Bang();
}
"""


# ── Bug A regression ────────────────────────────────────────────────


def test_throws_outer_with_nothrow_lambda_match_on_result_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Bug A primary regression.  `throws` outer fn containing a nothrow
	Callback0 lambda that does `val r = make_ok(); match &r { Ok, Err }`.
	Pre-fix: cascade of "match scrutinee must be a variant type", "lambda
	can throw but is expected to be nothrow", "callback0 expects a function
	value".  Post-fix: clean compile."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn outer_throws() throws -> Int {
\treturn run_cb(|| => {
\t\tval r = make_ok();
\t\tval n = match &r {
\t\t\tcore.Result::Ok(v) => { *v },
\t\t\tcore.Result::Err(_) => { 0 }
\t\t};
\t\treturn n;
\t});
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc == 0, f"compile failed: rc={rc}, errs={errs}"
	assert not errs, f"unexpected diagnostics: {errs}"


def test_nothrow_outer_with_nothrow_lambda_match_on_result_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Control case: nothrow outer fn — same lambda body, same shape.
	Pre-fix and post-fix this works (no auto-try context to leak).
	Pinned to verify the fix doesn't regress the working baseline."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn outer_nothrow() nothrow -> Int {
\treturn run_cb(|| => {
\t\tval r = make_ok();
\t\tval n = match &r {
\t\t\tcore.Result::Ok(v) => { *v },
\t\t\tcore.Result::Err(_) => { 0 }
\t\t};
\t\treturn n;
\t});
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc == 0, f"control compile failed: rc={rc}, errs={errs}"
	assert not errs, f"unexpected diagnostics: {errs}"


def test_throws_outer_with_throws_lambda_auto_try_still_fires(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Auto-try inside a throws lambda must STILL fire — the lambda's
	OWN throwable surface determines auto-try eligibility, not the outer
	fn.  Verifies the fix doesn't accidentally suppress auto-try in the
	correct case.

	The throws lambda's body has `val r = make_ok();` (no annotation) —
	auto-try should eagerly unwrap to `Int`.  Then `match &r { Ok, Err }`
	would fail because `r: Int` is not a variant.  Asserting the
	"variant" diagnostic confirms auto-try fired correctly inside the
	throws lambda — without leaking from the throws *outer*."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn outer_throws() throws -> Int {
\treturn run_cb_throws(|| => {
\t\tval r = make_ok();
\t\tval n = match &r {
\t\t\tcore.Result::Ok(v) => { *v },
\t\t\tcore.Result::Err(_) => { 0 }
\t\t};
\t\treturn n;
\t});
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc != 0, "throws-lambda (via CallbackThrow0 expected-type) auto-try should still unwrap r → Int → match rejects"
	assert any("variant" in m for m in errs), (
		f"expected 'must be a variant type' diagnostic to confirm auto-try fired in throws lambda; got: {errs}"
	)


def test_try_block_around_lambda_does_not_leak_into_lambda_body(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Bug A — second half of the lambda-boundary state leak.

	`_auto_try_context()` returns True when
	`try_block_depth > 0 OR fn_declared_throws`.  The primary regression
	(`test_throws_outer_with_nothrow_lambda_match_on_result_compiles`)
	covers the `fn_declared_throws` half.  This test covers the
	`try_block_depth` half: a `try {}` block opened in the outer fn body
	bumps `try_block_depth` to 1; a lambda constructed inside that try
	body must NOT inherit that bump.

	Pre-fix: the lambda body's type-check sees `try_block_depth > 0`
	from the enclosing `try {}`; eager auto-try synthesizes `or_throw()`
	on `val r = make_ok()`, unwrapping `r` to `Int`; the subsequent
	`match &r { Ok, Err }` rejects with "must be a variant type."
	Post-fix: the lambda boundary resets `try_block_depth = 0`;
	auto-try does not fire; match works.

	Independence from the `fn_declared_throws` half: even if only
	`fn_declared_throws` were reset at the lambda boundary,
	`try_block_depth > 0` alone would still drive `_auto_try_context()`
	to True inside the lambda body — so this test independently
	validates that the `try_block_depth` reset is in place.

	(Note: an outer NOTHROW fn with `try {}` would more cleanly isolate
	`try_block_depth`, but Drift's current may-throw inference does not
	accept fully-absorbing catches as proof that the enclosing fn is
	nothrow — a separate issue, out of scope for this regression.)"""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn outer_throws_with_try() throws -> Int {
\ttry {
\t\tval x = maybe_throw();
\t\tval cb_ret = run_cb(|| => {
\t\t\tval r = make_ok();
\t\t\tval n = match &r {
\t\t\t\tcore.Result::Ok(v) => { *v },
\t\t\t\tcore.Result::Err(_) => { 0 }
\t\t\t};
\t\t\treturn n;
\t\t});
\t\treturn cb_ret + x;
\t} catch Bang(e) {
\t\treturn 0;
\t}
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc == 0, f"try-block lambda-boundary leak — compile failed: rc={rc}, errs={errs}"
	assert not errs, f"unexpected diagnostics: {errs}"


def test_throws_outer_nested_nothrow_lambda_explicit_result_annotation_works(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Adjacent path: explicit `Result<T,E>` type annotation opts out of
	auto-try (per the auto-try contract).  Inside a nothrow lambda
	nested in a throws outer, the annotation should still work — the
	fix's lambda-boundary reset means auto-try is OFF here regardless,
	but pin the case so a future regression that re-enables auto-try
	via a different code path still respects the opt-out."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn outer_throws() throws -> Int {
\treturn run_cb(|| => {
\t\tval r: core.Result<Int, String> = make_ok();
\t\tval n = match &r {
\t\t\tcore.Result::Ok(v) => { *v },
\t\t\tcore.Result::Err(_) => { 0 }
\t\t};
\t\treturn n;
\t});
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc == 0, f"explicit Result annotation in nested lambda failed: rc={rc}, errs={errs}"
	assert not errs, f"unexpected diagnostics: {errs}"
