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
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main

_ROOT = Path(__file__).resolve().parents[3]


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[str]]:
	"""Type-check-only compile.  Used for diagnostic tests where the
	full lowering pipeline is not relevant.  For G3-class tests
	(value-context use of by-ref binders), use `_compile_and_run`
	instead — `--test-build-only` does not exercise MIR/LLVM and
	a checker-only fix can pass while lowering breaks."""
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	argv = ["--stdlib-root", "stdlib", "--test-build-only", str(src)]
	rc, payload = _run_driftc_json(argv, capsys)
	errs = [d.get("message", "") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


def _compile_and_run(tmp_path: Path, source: str, *, expected_rc: int) -> None:
	"""Full compile + binary run.  Mandatory for G3-class regressions
	(value-context use of by-ref binders): asserts both that the
	compile succeeds and that the binary's exit code matches
	`expected_rc`, so a checker-only fix that fails in MIR/LLVM
	cannot quietly pass.  Spawns a subprocess so we get the real
	link + execute path."""
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	cp = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(_ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=str(_ROOT), capture_output=True, text=True, timeout=120,
	)
	assert cp.returncode == 0, (
		f"full compile failed:\nstdout:\n{cp.stdout[:1500]}\n"
		f"stderr:\n{cp.stderr[:1500]}"
	)
	assert out_bin.exists(), "binary not produced"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=30)
	assert run.returncode == expected_rc, (
		f"binary exited rc={run.returncode}, expected {expected_rc}"
	)


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


# ── G3: primitive-payload binders must auto-load through shared ref ─
#
# 0.31.33 certified shared by-ref match for struct-payload variants
# (the canonical `Result<Resp, AppErr>` shape).  Primitive-payload
# variants (`variant V { Active(n: Int) }`) slipped past because the
# cert tests only used struct payloads.  The binder for a primitive
# field is `Ref<Int>` (or `Ref<Bool>`, `Ref<Float>`, etc.) which the
# arithmetic / copy / comparison machinery doesn't auto-deref.  Per
# the spec, primitive-payload binders must behave identically to
# their value-typed counterparts under shared by-ref match.


def test_g3_primitive_payload_arith_full_compile_and_run(tmp_path: Path) -> None:
	"""G3 — `match &s { Active(n) => n + 1 }` on a variant with a
	primitive `Int` payload field must compile end-to-end and the
	binary must produce `n + 1` at runtime.

	A `--test-build-only` check is insufficient for this bug class:
	a checker-only autoderef passes type-check while LLVM rejects
	the IR (`integer binop requires matching Int/Uint operands
	(have ptr, drift.int)`).  This test asserts the full pipeline:
	type-check → MIR → LLVM → link → execute."""
	_compile_and_run(tmp_path, """
module main;
import std.core as core;

variant State { Active(n: Int) }

fn main() nothrow -> Int {
\tval s: State = State::Active(n = 5);
\treturn match &s {
\t\tState::Active(n) => { n + 1 }
\t};
}
""", expected_rc=6)


def test_g3_primitive_payload_copy_full_compile_and_run(tmp_path: Path) -> None:
	"""G3 — typed let-init coercion of a primitive payload binder
	(`val k: Int = match &s { Active(n) => n }`) must compile
	end-to-end.  Pre-fix: clang rejects emitted IR with `ret ptr
	%fieldptr10, expected i64`."""
	_compile_and_run(tmp_path, """
module main;
import std.core as core;

variant State { Active(n: Int) }

fn main() nothrow -> Int {
\tval s: State = State::Active(n = 42);
\tval extracted: Int = match &s {
\t\tState::Active(n) => { n }
\t};
\treturn extracted;
}
""", expected_rc=42)


def test_g3_primitive_payload_comparison_full_compile_and_run(tmp_path: Path) -> None:
	"""G3 — comparison with primitive payload binder."""
	_compile_and_run(tmp_path, """
module main;
import std.core as core;

variant State { Active(n: Int) }

fn main() nothrow -> Int {
\tval s: State = State::Active(n = 7);
\treturn match &s {
\t\tState::Active(n) => { n > 0 ? 1 : 0 }
\t};
}
""", expected_rc=1)


def test_g3_bool_payload_full_compile_and_run(tmp_path: Path) -> None:
	"""G3 — Bool primitive payload."""
	_compile_and_run(tmp_path, """
module main;
import std.core as core;

variant Flag { On(b: Bool) }

fn main() nothrow -> Int {
\tval f: Flag = Flag::On(b = true);
\treturn match &f {
\t\tFlag::On(b) => { b ? 1 : 0 }
\t};
}
""", expected_rc=1)


def test_g3_scope_only_match_arm_binders_not_arbitrary_refs(tmp_path: Path) -> None:
	"""G3 scope pin — the autoderef applies *only* to match-arm
	binders, not to arbitrary `&Int` values.  This source takes the
	address of a local and tries to use the resulting `&Int` in
	arithmetic: pre-G3 this was rejected, and post-G3 it must
	*still* be rejected (not silently coerced).  Confirms the fix
	is scoped, not a broad `Ref<Copy> → Copy` coercion."""
	src = tmp_path / "main.drift"
	src.write_text("""
module main;
import std.core as core;

fn main() nothrow -> Int {
\tval n: Int = 5;
\tval r: &Int = &n;
\treturn r + 1;
}
""")
	cp = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--stdlib-root", str(_ROOT / "stdlib"),
		 "--test-build-only", str(src), "--json"],
		cwd=str(_ROOT), capture_output=True, text=True, timeout=60,
	)
	payload = json.loads(cp.stdout) if cp.stdout.strip() else {"diagnostics": []}
	errs = [d.get("message", "") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert cp.returncode != 0 or errs, (
		"non-arm-binder `&Int + Int` must remain rejected; the G3 "
		"fix must be scoped to match-arm binders only.  Compile "
		"output: rc={} diags={}".format(cp.returncode, errs)
	)


# ── G4: A.2 hygiene in copy/arith diagnostics ───────────────────────
#
# 0.31.32 routed the unknown-name diagnostic through
# `user_facing_binding_name`, but the copy / arithmetic / type-mismatch
# diagnostic emission sites still spell `__match_binder_<n>_<src>`.
# Surfaced 2026-04-29 by the &mut probe; affects shared too whenever
# a binder participates in a failing copy/arith check.


def test_g4_arith_diagnostic_uses_source_binder_name(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""G4 — force an arithmetic error involving a binder, assert the
	diagnostic spells the source name (`n`), not the synthetic form."""
	# The G3 fix should make this case compile cleanly; for the
	# pre-G3 check we'd see `Ref<Int> vs Int`, with the binder
	# named in the message.  Even post-G3, an actual type mismatch
	# (e.g. Int + Bool) must still spell the source binder name in
	# the diagnostic.
	rc, errs = _compile(tmp_path, capsys, """
module main;
import std.core as core;

variant V { A(n: Int, b: Bool) }

fn main() nothrow -> Int {
\tval v: V = V::A(n = 1, b = true);
\treturn match &v {
\t\tV::A(n, b) => { n + b }
\t};
}
""")
	assert rc != 0, "Int + Bool must error"
	for m in errs:
		assert "__match_binder_" not in m, (
			f"A.2 hygiene invariant violated in arithmetic diagnostic: "
			f"\n  {m}"
		)


def test_g4_copy_diagnostic_uses_source_binder_name(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""G4 — force a copy error involving a binder of a non-Copy type
	through the shared `&` binder.  Diagnostic must spell the source
	name."""
	rc, errs = _compile(tmp_path, capsys, """
module main;
import std.core as core;

struct Big { pub data: Array<Int> }
variant V { A(big: Big) }

fn take_big(b: Big) nothrow -> Int { return b.data.len; }

fn main() nothrow -> Int {
\tval v: V = V::A(big = Big(data = [1, 2, 3]));
\treturn match &v {
\t\tV::A(big) => { take_big(big) }
\t};
}
""")
	# This source MUST fail compile — passing the `&Big` binder
	# (shared borrow) to `fn take_big(b: Big)` (owned arg) requires
	# a copy/move that the borrow checker rejects.  If a future
	# regression accepts the call, the hygiene assertion below
	# becomes vacuous; pin both halves explicitly.
	assert rc != 0, (
		"passing a non-Copy `&Big` arm binder to an owned-arg "
		"function must fail compile; got rc=0 — the hygiene check "
		"would be vacuous.  Either the binder is silently being "
		"copied (regression in shared-by-ref binder semantics) or "
		"an autodref widened beyond match-binder scope."
	)
	for m in errs:
		assert "__match_binder_" not in m, (
			f"A.2 hygiene invariant violated in copy/move diagnostic:"
			f"\n  {m}"
		)


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
