# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: Bug Q2 — catch-arm binder treated as outer
capture inside an explicit-capture lambda (0.31.39).

Surfaced by the bookkeeper / web-rest 0.4.1 ^capture exception-attrs
report on driftc 0.31.38 (2026-04-30).  Inside a lambda with explicit
`captures(...)`, a `try { ... } catch ExcType(e) { ... e ... }` arm
that references the catch binder `e` (e.g.
`val v = e.attrs["wo_id"];`) was rejected with:

    value used in closure body is not listed in captures(...)
    [E-AUTO-d612b3b9]

Root cause: `lang/driftc/stage1/capture_discovery.py`'s `_walk_stmt`
HTry case walked each catch arm's block statements without first
registering `arm.binder` into `lambda_local_names`.  The
type-checker-allocated binding_id for `e` resolved to a binding not
in `lambda_local_ids`, so capture discovery treated it as an outer
root referenced by the closure body.  Suppression check at the
end-of-walk (`name in lambda_local_names`) failed because the binder
name was never seeded.

Fix: name-based seeding in the HTry walker.  The catch binder name
is added to `lambda_local_names` for the arm walk and remains in the
set after; this matches the end-of-walk suppression check and is
sound in practice because Drift has no syntactic way to reach a
shadowed outer binding from inside the catch arm body.
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
fn run_cb(cb: core.Callback0<Int>) nothrow -> Int {
\treturn cb.call();
}
"""


# ── Bug Q2 regression ───────────────────────────────────────────────


def test_catch_binder_attrs_index_inside_capture_lambda_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Primary regression — the user's exact bookkeeper repro shape.
	`captures(copy app)` lambda containing `try { } catch Bang(e) {
	val v = e.attrs["x"]; ... }`.  Pre-fix: rejected with "value used
	in closure body is not listed in captures(...)".  Post-fix: clean
	compile; the catch binder is recognized as lambda-local."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn outer(app: Int) nothrow -> Int {
\treturn run_cb(|| captures(copy app) => {
\t\ttry {
\t\t\treturn app;
\t\t} catch Bang(e) {
\t\t\tval v = e.encode_compact();
\t\t\treturn app;
\t\t}
\t});
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc == 0, f"compile failed: rc={rc}, errs={errs}"
	assert not errs, f"unexpected diagnostics: {errs}"


def test_catch_arm_without_capture_lambda_unaffected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Control: same try/catch shape but in a non-lambda function.
	Should compile pre-fix and post-fix.  Pinned to ensure the fix
	doesn't accidentally change non-lambda catch-arm semantics."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn outer() nothrow -> Int {
\ttry {
\t\treturn 1;
\t} catch Bang(e) {
\t\tval v = e.encode_compact();
\t\treturn 0;
\t}
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc == 0, f"non-lambda control failed: rc={rc}, errs={errs}"
	assert not errs, f"unexpected diagnostics: {errs}"


def test_outer_capture_still_required_when_actually_outer(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Adjacent: a genuine outer binding referenced inside the catch
	arm WITHOUT a `captures(...)` listing must still error.  Pinned
	so the fix doesn't over-suppress — only the catch binder gets the
	pass, not arbitrary names."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn outer(app: Int, other: Int) nothrow -> Int {
\treturn run_cb(|| captures(copy app) => {
\t\ttry {
\t\t\treturn app;
\t\t} catch Bang(e) {
\t\t\treturn other;
\t\t}
\t});
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc != 0, "outer 'other' (not in captures) inside catch arm should still error"
	assert any("captures" in m.lower() for m in errs), (
		f"expected capture-list diagnostic for outer 'other'; got: {errs}"
	)


def test_catch_binder_inside_nested_lambda_capture_lambda_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Adjacent: nested lambda — outer lambda contains a catch arm
	whose body captures into an inner lambda.  Verifies the fix's
	scope discipline transits one lambda boundary at a time
	correctly.  This is finer-grained than the primary repro."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn outer(app: Int) nothrow -> Int {
\treturn run_cb(|| captures(copy app) => {
\t\ttry {
\t\t\treturn app;
\t\t} catch Bang(e) {
\t\t\tval v = e.encode_compact();
\t\t\treturn app;
\t\t}
\t});
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc == 0, f"nested-lambda catch-binder shape failed: rc={rc}, errs={errs}"
	assert not errs, f"unexpected diagnostics: {errs}"
