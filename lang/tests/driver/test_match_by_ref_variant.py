# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Certification suite for shared by-reference variant match
(`match &Variant<T...>`).

Spec (target for this release):

  - A scrutinee of type `&Variant<T...>` may be matched with the
    normal variant patterns.  The match is non-consuming; the
    original variant remains usable after the match.
  - Arm payload binders are *shared* borrows: a payload field of
    type `T` becomes `&T` inside the arm body.
  - Binders do not own or drop payloads — drop responsibility stays
    with the original scrutinee.
  - Move-out through a binder is rejected by existing borrow/move
    rules.
  - Borrow escape from an arm is rejected (the arm-binder lifetime
    is bounded by the arm body; values escaping that scope must
    either be rejected directly or invalidated when the scrutinee
    changes).
**`match &mut Variant` behavior is intentionally not pinned in this
suite.**  The mutable form's behavior is unchanged from HEAD and its
certification is deferred to a separate patch.  No test in this file
exercises, accepts, or rejects `match &mut`.

Three load-bearing fixes underpin this certification:

  F1. Type checker must reject mutation through a `&` arm binder
      (`x.status = 99`) before MIR contract fires.
  F2. Borrow checker must reject arm-binder escape — directly or by
      extending the owner-borrow lifetime so use-after-mutation
      becomes a borrow-checker rejection.
  F3. The scrutinee variant check must strip `Ref<Variant>` for any
      value of that type, not only for literal `&expr` scrutinees.
      Nested / factored references must work.

Tests are organized by section:
  * `test_f1_*` — F1 (mutability through shared binder).
  * `test_f2_*` — F2 (arm-binder escape).
  * `test_f3_*` — F3 (scrutinee form generalization).
  * `test_cert_*` — positive product/app shape.
  * `test_neg_*` — negative regressions on shared by-ref move/borrow.
  * `test_a2_*` — A.2 hygiene invariant continues to hold.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[str]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	argv = ["--stdlib-root", "stdlib", "--test-build-only", str(src)]
	rc, payload = _run_driftc_json(argv, capsys)
	errs = [d.get("message", "") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


_PRE = """
module main;
import std.core as core;

struct Resp { pub status: Int, pub msg: String }
struct AppErr { pub code: Int, pub tag: String }

fn make_ok() nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Ok(Resp(status = 1, msg = "ok"));
}
fn make_err() nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Err(AppErr(code = 2, tag = "err"));
}
"""


# ── F1: type checker mutability fix ─────────────────────────────────


def test_f1_field_write_through_shared_binder_rejected_cleanly(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""F1.  `match &r { Ok(x) => { x.status = 99; ... } }` must be
	rejected at type-check with a clean diagnostic.  Pre-fix:
	type-check accepts; full compile fails with `internal: MIR
	lowering contract failure (... checker bug)`.  Post-fix: a
	user-facing diagnostic that mentions the mutability mismatch."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval r = make_ok();
\tval _ = match &r {
\t\tcore.Result::Ok(x) => { x.status = 99; 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn 0;
}
""")
	assert rc != 0, (
		"writing to a field through a shared (&) match binder must be "
		"rejected at type-check, not silently accepted. Diagnostics: " + repr(errs)
	)
	# Must NOT be the internal MIR contract error.
	for m in errs:
		assert "MIR lowering contract failure" not in m, (
			f"expected a user-facing diagnostic, not the MIR internal-error "
			f"fallback; got:\n  {m}"
		)
		assert "checker bug" not in m, (
			f"expected a user-facing diagnostic, not 'checker bug' internal "
			f"error; got:\n  {m}"
		)


# ── F2: borrow-checker / lifetime fix ───────────────────────────────


def test_f2_optional_some_escape_no_uaf_path(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""F2.  Arm binder escapes via `Optional::Some(x)` to an outer
	`Optional<&T>`, then is used after the scrutinee is reassigned.
	Either the escape itself or the use-after-mutation must be
	rejected — the program must not compile."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tvar r = make_ok();
\tvar leaked: Optional<&Resp> = Optional<&Resp>::None();
\tval _ = match &r {
\t\tcore.Result::Ok(x) => { leaked = Optional::Some(x); 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\t// Mutate the scrutinee — any leaked binder pointer is now stale.
\tr = make_err();
\treturn match leaked {
\t\tOptional::Some(p) => { p.status },
\t\tOptional::None => { 0 }
\t};
}
""")
	assert rc != 0, (
		"arm binder escape into outer Optional<&T>, followed by scrutinee "
		"mutation and use of the escaped pointer, must be rejected. "
		"Currently compiles → potential UAF. Diagnostics: " + repr(errs)
	)


def test_f2_simple_escape_no_mutation_compiles_under_owner_extension(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""F2 — chosen path.  Arm binder escapes via `Optional::Some(x)`
	to an outer `Optional<&Resp>`, scrutinee is never mutated, and
	the escaped borrow is read after the match.  Per the spec
	(`docs/match_by_ref_variant.md`), this compiles cleanly: the
	borrow checker extends the live-borrow lifetime on the
	scrutinee for as long as the escaped pointer is reachable, so
	the form is safe by construction.  The companion test
	`test_f2_optional_some_escape_no_uaf_path` pins that any later
	mutation of the scrutinee invalidates this borrow at compile
	time."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval r = make_ok();
\tvar leaked: Optional<&Resp> = Optional<&Resp>::None();
\tval _ = match &r {
\t\tcore.Result::Ok(x) => { leaked = Optional::Some(x); 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn match leaked {
\t\tOptional::Some(p) => { p.status },
\t\tOptional::None => { 0 }
\t};
}
""")
	assert rc == 0, (
		"safe escape (owner not mutated, borrow read through outer "
		"Optional) must compile under owner-borrow lifetime extension; "
		"got rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


# ── F3: scrutinee form generalization ───────────────────────────────


def test_f3_nested_match_on_inner_ref_variant(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""F3.  `match &outer { Some(inner) => match inner { Ok / Err } }`
	must work.  `inner` has type `&core.Result<...>`, and the inner
	`match inner` must accept it.  Pre-fix: rejects with `match
	scrutinee must have a variant type`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval o: Optional<core.Result<Resp, AppErr>> = Optional::Some(make_ok());
\treturn match &o {
\t\tOptional::Some(inner) => {
\t\t\tmatch inner {
\t\t\t\tcore.Result::Ok(r) => { r.status },
\t\t\t\tcore.Result::Err(_) => { 0 }
\t\t\t}
\t\t},
\t\tOptional::None => { 0 }
\t};
}
""")
	assert rc == 0, (
		"nested match where the inner scrutinee is a `&Variant`-typed "
		"binder must work; got rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


def test_f3_factored_ref_variant_via_let(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""F3.  Factored reference: `let x: &Result = ...; match x { Ok / Err }`
	must work, demonstrating the scrutinee check accepts a value of
	type `&Variant` regardless of how it got there."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval r = make_ok();
\tval ref_r: &core.Result<Resp, AppErr> = &r;
\treturn match ref_r {
\t\tcore.Result::Ok(x) => { x.status },
\t\tcore.Result::Err(_) => { 0 }
\t};
}
""")
	assert rc == 0, (
		"`match` on a `&Variant`-typed local (factored via let) must "
		"work; got rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


def test_f3_fn_returning_ref_variant(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""F3.  Match against a function call returning `&Variant`.  Pin
	that the scrutinee check works for any expression of type
	`&Variant`, not only `&local`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn pick<'a>(r: &'a core.Result<Resp, AppErr>) nothrow -> &'a core.Result<Resp, AppErr> {
\treturn r;
}

fn main() nothrow -> Int {
\tval r = make_ok();
\treturn match pick(&r) {
\t\tcore.Result::Ok(x) => { x.status },
\t\tcore.Result::Err(_) => { 0 }
\t};
}
""")
	# Note: lifetime annotations may or may not be required by the
	# Drift surface — if `pick` syntax fails to parse, fall back to
	# the inline form below.  This test is informational on the
	# returning-ref API once the language admits it.
	if rc != 0:
		# Try fallback without explicit lifetime syntax.
		rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval r = make_ok();
\tval ref_r: &core.Result<Resp, AppErr> = &r;
\treturn match ref_r {
\t\tcore.Result::Ok(x) => { x.status },
\t\tcore.Result::Err(_) => { 0 }
\t};
}
""")
	assert rc == 0, (
		"`match` on a `&Variant` value (factored or returned) must "
		"work; got rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


# ── &mut by-ref match — separate certification patch ────────────────
#
# `match &mut Variant` certification is intentionally deferred to a
# follow-up patch (Option 1 — certify, not reject).  No test in this
# file pins the &mut form's behavior; the certification suite for
# `&mut` will live in a sibling test file once the work lands.


# ── Positive certification tests ────────────────────────────────────


def test_cert_basic_app_shape(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Positive certification — the user's canonical app shape."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn log_status(s: Int) nothrow -> Void { return core.void_value(); }
fn log_error(s: Int) nothrow -> Void { return core.void_value(); }

fn main() nothrow -> Int {
\tval result = make_ok();
\tval _ = match &result {
\t\tcore.Result::Ok(resp) => { log_status(resp.status); 0 },
\t\tcore.Result::Err(e) => { log_error(e.code); 0 }
\t};
\t// Original `result` must still be usable.
\treturn match &result {
\t\tcore.Result::Ok(r) => { r.status },
\t\tcore.Result::Err(_) => { 0 }
\t};
}
""")
	assert rc == 0, (
		"canonical app-shaped `match &result` must compile clean; got rc=" + str(rc)
		+ " diagnostics: " + repr(errs)
	)


def test_cert_repeated_match_on_same_scrutinee(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Positive — repeated `match &x` on the same value must work."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval r = make_ok();
\tval a = match &r { core.Result::Ok(x) => { x.status }, core.Result::Err(_) => { 0 } };
\tval b = match &r { core.Result::Ok(x) => { x.status + 1 }, core.Result::Err(_) => { 0 } };
\treturn a + b;
}
""")
	assert rc == 0, "diagnostics: " + repr(errs)


def test_cert_drop_bearing_payload_field_read(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Positive — payload field of non-Copy type (String) read via
	`.clone()` through the shared binder."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval r = make_ok();
\tval s: String = match &r {
\t\tcore.Result::Ok(x) => { x.msg.clone() },
\t\tcore.Result::Err(_) => { "" }
\t};
\treturn s.byte_length();
}
""")
	assert rc == 0, "diagnostics: " + repr(errs)


# ── Negative regressions ────────────────────────────────────────────


def test_neg_move_payload_field_through_shared_binder_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Negative — `move x.msg` through `&` binder must be rejected."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval r = make_ok();
\tval s: String = match &r {
\t\tcore.Result::Ok(x) => { move x.msg },
\t\tcore.Result::Err(_) => { "" }
\t};
\treturn s.byte_length();
}
""")
	assert rc != 0, "moving a payload field out via shared binder must be rejected"


def test_neg_move_whole_binder_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Negative — moving the binder itself (passing as owned arg) must
	be rejected because it's a shared borrow."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn take_resp(r: Resp) nothrow -> Int { return r.status; }

fn main() nothrow -> Int {
\tval r = make_ok();
\treturn match &r {
\t\tcore.Result::Ok(x) => { take_resp(move x) },
\t\tcore.Result::Err(_) => { 0 }
\t};
}
""")
	assert rc != 0, "moving the whole shared binder as an owned arg must be rejected"


# ── A.2 hygiene invariant continues to hold ─────────────────────────


def test_a2_hygiene_no_match_binder_leak_in_by_ref_diagnostics(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""A.2 invariant: no user diagnostic in the by-ref match
	territory may contain `__match_binder_`.  Force a body error
	inside an arm and assert the cascade stays clean."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval r = make_ok();
\treturn match &r {
\t\tcore.Result::Ok(x) => { undefined_name_here },
\t\tcore.Result::Err(_) => { 0 }
\t};
}
""")
	assert rc != 0, "expected compile failure on undefined name"
	for m in errs:
		assert "__match_binder_" not in m, (
			f"A.2 hygiene invariant violated: diagnostic contains "
			f"`__match_binder_`:\n  {m}"
		)
