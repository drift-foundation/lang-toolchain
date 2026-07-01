# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: Bug R1 — generic Callback{N}<T> fallback
silently flipped nothrow → throws when the interface's type-args
contained typevars (0.31.40).

Surfaced as 6 e2e regressions in 0.31.39's full driver gate
(concurrent_cancel_before_start_race_stress,
concurrent_cancel_before_start_race_stress_diagnostic,
concurrent_sleep_task_join_timeout_regression,
et_budget_yield_forward_progress, et_close_no_stale_replay,
et_pending_replay_no_hang) — all reduced to the same root cause:

    var t = conc.spawn(| | => {
        val _ = conc.sleep(conc.Duration(millis = 1));
        return 1;
    });

failed with:

    error: lambda can throw but is expected to be nothrow for Fn() nothrow -> Unknown
    error: callback0 expects a function value
    error: cannot infer type arguments for 'spawn': T

**Root cause.**  In `lang/driftc/checker/call_resolver.py`'s
candidate-driven HLambda pre-typing path, the kind detector used the
strict `_callback_param_kind`, which returns `None` for any
`Callback{N}<T>` interface whose type-args contain typevars (per its
guard at `:190-191`).  For `conc.spawn<T>(cb: Callback0<T>)`, the
parameter `Callback0<T>` has typevar T → strict detector returns
`None` → `_cand_kind` stays `None` → the fallback branch at
`:5983` emitted `ensure_function(..., can_throw=True)` (hardcoded).

The 0.31.38 lambda-boundary fix (Bug A) faithfully propagated that
`can_throw=True` into the lambda body's
`fn_declared_throws` via `expected_fn[2]`, so unannotated
Result-returning calls inside the lambda
(`val _ = conc.sleep(...)`, `var cr = net.connect(...)`) got
auto-try'd, the synthesized `or_throw()` made the lambda implicitly
throwing, and `match cr { Ok... }` then collapsed because `cr` had
been unwrapped to `Void` / `Unknown`.

**Fix.**  Switch the kind detector to
`_callback_param_kind_permissive`, which detects the
`(arity, can_throw)` from the interface's BASE NAME (`Callback0` vs
`CallbackThrow0`) regardless of type-arg resolution.  The throwability
bit is well-defined even when the type-args contain typevars; the
concretization branch below the kind-detection still uses its own
typevar gate, so concretization is not widened.
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


# ── Bug R1 regression ───────────────────────────────────────────────


def test_conc_spawn_nothrow_lambda_with_result_returning_call_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Primary regression — the team's exact minimal repro.
	`conc.spawn<T>(cb: Callback0<T>)` accepts a bare nothrow lambda
	whose body calls `conc.sleep(...)` (returns `Result<Void, _>`) and
	returns an Int."""
	rc, errs = _compile(tmp_path, capsys, """
module m;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
\tvar t = conc.spawn(| | => {
\t\tval _ = conc.sleep(conc.Duration(millis = 1));
\t\treturn 1;
\t});
\treturn 0;
}
""")
	assert rc == 0, f"compile failed: rc={rc}, errs={errs}"
	assert not errs, f"unexpected diagnostics: {errs}"


def test_conc_spawn_lambda_match_on_result_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Adjacent: bare-lambda body that explicitly matches on a Result
	from a nothrow call.  Pre-fix the auto-try cascade unwrapped the
	Result to its Ok payload, breaking the match.  Post-fix the
	Result is preserved."""
	rc, errs = _compile(tmp_path, capsys, """
module m;
import std.core as core;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
\tvar t = conc.spawn(| | => {
\t\tvar cr = conc.sleep(conc.Duration(millis = 1));
\t\tval n = match cr {
\t\t\tcore.Result::Ok(_) => { 1 },
\t\t\tcore.Result::Err(_) => { 0 }
\t\t};
\t\treturn n;
\t});
\treturn 0;
}
""")
	assert rc == 0, f"compile failed: rc={rc}, errs={errs}"
	assert not errs, f"unexpected diagnostics: {errs}"


def test_callback_throw_generic_still_permits_auto_try_inside(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Symmetric control: a generic `CallbackThrow{N}<T>`-taking call
	site must still drive auto-try inside the lambda body — the fix
	must not flip throws lambdas to nothrow.  Pinned via observable
	side effect: a Result-returning call's auto-try produces a
	`match` rejection (`r: T` after unwrap, not `Result`), which
	confirms auto-try DID fire — the right behavior for a throwable
	expected-type."""
	rc, errs = _compile(tmp_path, capsys, """
module m;
import std.core as core;

fn run_throwing_cb<T>(cb: core.CallbackThrow0<T>) throws -> T {
\treturn cb.call();
}

fn make_ok() nothrow -> core.Result<Int, String> {
\treturn core.Result::Ok(1);
}

fn outer() throws -> Int {
\treturn run_throwing_cb(| | => {
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
	assert rc != 0, "throws-callback generic auto-try should unwrap r → Int → match rejects"
	assert any("variant" in m for m in errs), (
		f"expected 'must be a variant type' diagnostic confirming auto-try fired in throws-callback lambda; got: {errs}"
	)
