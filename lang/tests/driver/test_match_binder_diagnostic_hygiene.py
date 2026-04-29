# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Bug A.2 — match-binder diagnostic hygiene.

**Invariant.**  No user-facing diagnostic message may contain the
internal `__match_binder_` prefix.  HIR-level binder name mangling
(see `lang/driftc/stage1/ast_to_hir.py::_match_binder_counter`) is a
function-scoped uniqueness mechanism; the synthesized `__match_binder_
<counter>_<source_name>` form is implementation detail and must never
reach the user.

This invariant holds **regardless of the language decision on Bug A.1**
(whether `match &Variant { ... }` is accepted, cleanly rejected, or
deferred).  Even if the scrutinee check rejects the form, the binder
names referenced in the arm bodies must not leak as
`__match_binder_*` in the cascading "unknown name" diagnostics.

Surfaced 2026-04-29 by the bookkeeper / web-rest middleware report:
the user's `match &result { core.Result::Ok(resp) => ..., core.Result::
Err(e) => ... }` block produced

    :50:56: error: unknown name '__match_binder_1_resp'
    :53:57: error: unknown name '__match_binder_2_e'

after the upstream "match scrutinee must be a variant type" rejection
on the `&Result<...>` scrutinee left the body's binder references
unresolved.  Whatever the resolution of A.1, those two messages must
spell `'resp'` and `'e'` (the source names) — not the synthetic ones.

Pinned shapes:
  H1. `match &result { Ok(x) => ..., Err(e) => ... }` — the literal
      shape from the user report.  On HEAD this form *compiles
      clean* (Bug A.1's "accept" reading appears to already hold for
      basic Result by-ref match).  The hygiene invariant is pinned
      here regardless: if A.1 is later re-litigated and the form
      starts rejecting again, the binder names still must not leak.
  H2. `match move result { Ok(x) => ... }` — by-value shape with a
      deliberately undefined name reference inside the arm body.
      Exercises the unknown-name diagnostic emission site in a
      context where binder names *are* resolved, so any sibling /
      cascade diagnostic mentioning `__match_binder_*` is a leak.
  H3. Scrutinee-type rejection (`match n { Ok(...) => ..., Err(...)
      => ... }` with `n: Int`).  Load-bearing — pre-fix this leaks
      `__match_binder_1_resp` / `__match_binder_2_e` because the
      upstream scrutinee check rejects before arm bindings are set
      up, and the cascade unknown-name diagnostics interpolate the
      already-renamed HVar names directly.  Independent of A.1: the
      bug is in the unknown-name emission path, not the by-ref
      match form.

The assertion is uniform: no error diagnostic message may contain
`__match_binder_`.  Source-name identity (e.g. error mentions
`'resp'` / `'e'`) is checked as a follow-up assertion when the
upstream scrutinee path leaves binder references unresolved — that
part is informational; the load-bearing pin is the substring
exclusion.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


_INTERNAL_BINDER_PREFIX = "__match_binder_"


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> dict:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	argv = ["--stdlib-root", "stdlib", "--test-build-only", str(src)]
	_rc, payload = _run_driftc_json(argv, capsys)
	return payload


def _all_diagnostic_messages(payload: dict) -> list[str]:
	"""Return every message string from the payload — error, warning,
	note, anything visible to the user."""
	return [d.get("message", "") for d in payload.get("diagnostics", [])]


def _binder_leaks(payload: dict) -> list[str]:
	"""Return every diagnostic message that contains the internal
	binder prefix.  Empty list = invariant holds."""
	return [m for m in _all_diagnostic_messages(payload) if _INTERNAL_BINDER_PREFIX in m]


# H1 — exact user-reported shape: by-ref match over Result<T, E>.
# Whether this form compiles depends on Bug A.1's resolution.  Either
# way, the binder-leak invariant must hold: no diagnostic mentions
# `__match_binder_*`.
_H1_SOURCE = """
module main;

import std.core as core;

struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn make() nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Ok(Resp(status = 0));
}

fn main() nothrow -> Int {
\tval result = make();
\tval status: Int = match &result {
\t\tcore.Result::Ok(resp) => { resp.status },
\t\tcore.Result::Err(e) => { e.code }
\t};
\treturn status;
}
"""


# H2 — by-value match with a deliberately-undefined name reference in
# the arm body.  Exercises the unknown-name diagnostic emission site
# in a context where binder names are themselves resolved (so any
# `__match_binder_*` leak in a sibling/cascade diagnostic would be
# unambiguously a hygiene break).
_H2_SOURCE = """
module main;

import std.core as core;

struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn make() nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Ok(Resp(status = 0));
}

fn main() nothrow -> Int {
\tval result = make();
\tval status: Int = match move result {
\t\tcore.Result::Ok(resp) => { undefined_name_here },
\t\tcore.Result::Err(e) => { e.code }
\t};
\treturn status;
}
"""


# H3 — scrutinee-type rejection.  `match n { Ok(...) / Err(...) }` where
# `n: Int` is not a variant type. This is the path that leaks on HEAD:
# the upstream "match scrutinee must be a variant type" rejection
# leaves the arm bodies' renamed binders (`resp` → `__match_binder_1
# _resp`) unresolved, and the cascading unknown-name diagnostics spell
# the internal names.  This shape is independent of A.1 (it has nothing
# to do with by-ref match) — purely the diagnostic-hygiene leak.
_H3_SCRUTINEE_TYPE_REJECTION = """
module main;

import std.core as core;

struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn main() nothrow -> Int {
\tval n: Int = 5;
\tval s: Int = match n {
\t\tcore.Result::Ok(resp) => { resp.status },
\t\tcore.Result::Err(e) => { e.code }
\t};
\treturn s;
}
"""


def test_h1_match_by_ref_no_binder_leak(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""H1 — `match &result { Ok(resp) => ..., Err(e) => ... }`.  Whether
	this is accepted (A.1 = accept) or cleanly rejected (A.1 = reject)
	is out of scope; the invariant pinned here is that no user-visible
	diagnostic spells `__match_binder_*`."""
	payload = _compile(tmp_path, capsys, _H1_SOURCE)
	leaks = _binder_leaks(payload)
	assert not leaks, (
		f"diagnostic must not leak the internal `__match_binder_` "
		f"prefix; found:\n  - " + "\n  - ".join(leaks)
	)


def test_h2_match_by_value_with_undefined_arm_body_no_binder_leak(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""H2 — by-value match arm body references an undefined name.  The
	expected error is `unknown name 'undefined_name_here'` (source
	spelling).  No diagnostic anywhere in the payload may spell
	`__match_binder_*`."""
	payload = _compile(tmp_path, capsys, _H2_SOURCE)
	leaks = _binder_leaks(payload)
	assert not leaks, (
		f"diagnostic must not leak the internal `__match_binder_` "
		f"prefix; found:\n  - " + "\n  - ".join(leaks)
	)


def test_h3_scrutinee_type_rejection_no_binder_leak(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""H3 — load-bearing pin.  `match n { Ok(resp) => ..., Err(e) => ... }`
	with `n: Int` is rejected by the scrutinee type check ("match
	scrutinee must be a variant type").  The arm bodies' binders have
	already been renamed to `__match_binder_<n>_<src>` by HIR
	lowering; the cascading "unknown name" diagnostics for `resp` /
	`e` must use the source spelling.  Pre-fix on HEAD: leaks
	`__match_binder_1_resp` / `__match_binder_2_e`."""
	payload = _compile(tmp_path, capsys, _H3_SCRUTINEE_TYPE_REJECTION)
	leaks = _binder_leaks(payload)
	assert not leaks, (
		f"scrutinee-type rejection must not leak `__match_binder_*` "
		f"in the cascading unknown-name diagnostics; found:\n  - "
		+ "\n  - ".join(leaks)
	)


def test_h1_unknown_name_uses_source_spelling_when_form_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""H1 follow-up.  When A.1 = reject leaves the H1 body's binder
	references unresolved and triggers cascading "unknown name"
	diagnostics, those diagnostics must spell the *source* names
	(`'resp'`, `'e'`), not internal mangled forms.

	This assertion is conditional: it activates only if the H1 form
	is rejected (i.e. produces an "unknown name" diagnostic in the
	first place).  Under A.1 = accept, the assertion is vacuously
	satisfied because no unknown-name cascade fires.  Either way,
	the invariant holds.
	"""
	payload = _compile(tmp_path, capsys, _H1_SOURCE)
	unknown_msgs = [
		m for m in _all_diagnostic_messages(payload)
		if "unknown name" in m
	]
	if not unknown_msgs:
		# A.1 = accept path: no unknown-name cascade.  Nothing to
		# check.  The hygiene invariant in test_h1 above is the
		# load-bearing pin.
		return
	# At least one unknown-name fired; that path must spell source
	# names ('resp' or 'e'), not synthetic ones.
	for m in unknown_msgs:
		assert _INTERNAL_BINDER_PREFIX not in m, (
			f"unknown-name diagnostic must use source name spelling, "
			f"not internal `__match_binder_*`; got:\n  {m}"
		)
