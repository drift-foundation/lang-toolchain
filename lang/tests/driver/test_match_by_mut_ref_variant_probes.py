# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Certification suite for `match &mut Variant` (0.31.35).

Pins the v1 contract for mutable by-reference variant matching.
All tests are load-bearing regressions; failure here means a
certification break.  See `doc/match_by_ref_variant.md` for the
user-facing semantics this suite enforces.

Coverage:

  G1.  No-escape borrow lifetime — the scrutinee borrow ends at
       the match expression for both `&` and `&mut` forms when
       no arm binder escapes.  Pins acceptance of mut+shared,
       mut+mut, shared+mut sequencing, use/move/return after
       a `&mut` match, and mutation visible after match (full
       compile + run).

  G2.  Escape-aware loan retention.  Direct escape (`outer = x`,
       direct container wrap `Optional::Some(x)`) is rejected at
       the escape site for `&mut`.  Call-mediated escape
       (helper(x), x.next(), store(&mut leaked, x)) keeps the
       loan retained conservatively; later owner mutation /
       move / reborrow may reject by the standard loan-conflict
       check, closing the UAF path without breaking the iterator
       pattern.

  Stdlib guard: `match self` where `self: &mut Variant` (the
  iterator pattern that load-bears for `std.iter`,
  `std.json::JsonEntriesIter::next`, etc.) stays green.

  Primitive payload behavior pin: explicit `*n` deref / write
  through `*n = ...` is the documented surface.  Bare
  `n + 1` / `n = ...` ergonomics are intentionally *not*
  asserted here — those are a separate ergonomics decision
  scoped to shared binders only (G3, 0.31.34).

The suite uses full compile + run for shapes that need to be
proven sound at runtime (mutation visible after match), and
checker-only `--test-build-only` for diagnostic-shape probes
(escape rejection, sequencing acceptance) where the type-check
verdict is the load-bearing pin.

Naming convention:
  - `test_g1_*` — no-escape borrow lifetime pins.
  - `test_g2_*` — escape-aware loan retention pins.
  - `test_stdlib_*` — iterator-shape guards.
  - `test_primitive_*` — primitive payload deref/write pins.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main

from lang.codegen.llvm.test_utils import sanitizer_timeout

_ROOT = Path(__file__).resolve().parents[3]


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _check_only(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[str]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	argv = ["--stdlib-root", "stdlib", "--test-build-only", str(src)]
	rc, payload = _run_driftc_json(argv, capsys)
	errs = [d.get("message", "") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


def _compile_and_run(tmp_path: Path, source: str, *, expected_rc: int) -> None:
	"""Full compile + run.  Mandatory whenever the test asserts a
	runtime effect (e.g. mutation visible after the match expression
	scope ends)."""
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	cp = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(_ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=str(_ROOT), capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert cp.returncode == 0, (
		f"full compile failed:\nstdout:\n{cp.stdout[:1500]}\n"
		f"stderr:\n{cp.stderr[:1500]}"
	)
	assert out_bin.exists(), "binary not produced"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
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
"""


# ── G1: borrow lifetime must end at match expression ───────────────


def test_g1_mut_then_shared(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""G1.  After `match &mut r { ... }` finishes, taking a fresh
	`&r` must be allowed when no arm binder escaped.  Pre-fix
	HEAD rejects: "cannot take shared borrow while mutable borrow
	active on 'r'"."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { x.status = 99; 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn match &r {
\t\tcore.Result::Ok(x) => { x.status },
\t\tcore.Result::Err(_) => { 0 }
\t};
}
""")
	assert rc == 0, (
		"G1: shared borrow after &mut match should be accepted (no "
		"binder escaped); got rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


def test_g1_mut_then_mut(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""G1.  Two consecutive `match &mut r` blocks must be allowed
	when neither escapes a binder.  Pre-fix HEAD rejects: "cannot
	take mutable borrow while borrow active on 'r'"."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { x.status = 1; 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { x.status = 2; 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn 0;
}
""")
	assert rc == 0, (
		"G1: second &mut match after first should be accepted; got "
		"rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


def test_g1_shared_then_mut(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""G1.  After `match &r { ... }` finishes, taking `&mut r` must
	be allowed when no binder escaped from the shared match."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tval _ = match &r {
\t\tcore.Result::Ok(x) => { x.status },
\t\tcore.Result::Err(_) => { 0 }
\t};
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { x.status = 99; 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn 0;
}
""")
	assert rc == 0, (
		"G1: &mut match after shared match should be accepted; got "
		"rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


def test_g1_use_after_mut_match(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""G1.  Reading the scrutinee local `r` directly (no borrow)
	after the `match &mut r` expression must be accepted when no
	binder escaped.  Pre-fix HEAD rejects with the same overshoot
	message."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
fn read_status(r: &core.Result<Resp, AppErr>) nothrow -> Int {
\treturn match r {
\t\tcore.Result::Ok(x) => { x.status },
\t\tcore.Result::Err(_) => { 0 }
\t};
}

pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { x.status = 7; 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn read_status(r);
}
""")
	assert rc == 0, (
		"G1: read of scrutinee after &mut match should be accepted; "
		"got rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


def test_g1_move_after_mut_match(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""G1.  Moving the scrutinee `r` (consume) after a `match &mut
	r` expression that didn't escape must be accepted.  This pins
	end-of-borrow handoff for downstream consume sites."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
fn take_result(r: core.Result<Resp, AppErr>) nothrow -> Int {
\treturn match r {
\t\tcore.Result::Ok(x) => { x.status },
\t\tcore.Result::Err(_) => { 0 }
\t};
}

pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { x.status = 11; 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn take_result(move r);
}
""")
	assert rc == 0, (
		"G1: move of scrutinee after &mut match should be accepted; "
		"got rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


def test_g1_mutation_visible_after_match(tmp_path: Path) -> None:
	"""G1 + runtime correctness.  Full compile + run: a write
	through `&mut` arm binder must be visible when the scrutinee is
	read after the match expression scope ends.  Asserts both that
	the borrow lifetime ends correctly (compile succeeds) AND that
	the mutation actually lands (binary exits with the new value)."""
	_compile_and_run(tmp_path, _PRE + """
pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { x.status = 42; 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn match &r {
\t\tcore.Result::Ok(x) => { x.status },
\t\tcore.Result::Err(_) => { 0 }
\t};
}
""", expected_rc=42)


# ── G2: escape from &mut arm must not create aliasing/UAF ──────────


def test_g2_mut_binder_escape_to_optional_must_reject(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""G2.  Assigning a `&mut` arm binder into an outer
	`Optional<&mut T>` must be rejected (or, if accepted, exclusive
	lifetime tracking must reject any subsequent borrow on the
	scrutinee — but we prefer direct rejection per user direction).
	Pre-fix HEAD currently *accepts* this, which is the soundness
	gap G2 names."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tvar leaked: Optional<&mut Resp> = Optional<&mut Resp>::None();
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { leaked = Optional::Some(x); 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn 0;
}
""")
	assert rc != 0, (
		"G2: `&mut` arm binder escape via Optional<&mut> must be "
		"rejected; HEAD accepts.  diagnostics: " + repr(errs)
	)


def test_g2_direct_let_mut_binder_escape_must_reject(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""G2.  Even direct binding `var leaked: &mut T = match &mut r
	{ Ok(x) => x }` must be rejected — the borrow must not outlive
	the match arm scope.  Per the matrix probe HEAD already rejects
	this shape, but pin it as a regression so a future change
	doesn't loosen it."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tvar leaked: &mut Resp = match &mut r {
\t\tcore.Result::Ok(x) => { x },
\t\tcore.Result::Err(_) => { return 0; }
\t};
\tleaked.status = 99;
\treturn 0;
}
""")
	assert rc != 0, (
		"G2: direct `&mut` binder escape via let-binding must be "
		"rejected; HEAD currently rejects this — pin as regression. "
		"diagnostics: " + repr(errs)
	)


def test_g2_mut_binder_escape_through_helper_call_uaf_must_reject(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""G2 / call-escape UAF.  A helper call passing the `&mut` arm
	binder can stash the borrow into a reachable container; later
	owner reassignment plus use of the stashed pointer is the
	canonical UAF.  Reviewer-found 2026-04-29: direct-only escape
	detection missed this shape.

	The fix uses conservative call-escape detection — when an arm
	binder is passed to any call, the scrutinee loan is kept live
	(rather than dropped at end-of-match), so the subsequent owner
	reassignment is rejected by the standard loan-conflict check.
	No diagnostic at the call site itself (preserves load-bearing
	stdlib patterns like `match self { Ctor(it) => it.next() }`),
	but the downstream rejection points back to the still-live
	borrow."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
fn make_err() nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Err(AppErr(code = 2, tag = "err"));
}

fn store(slot: &mut Optional<&mut Resp>, p: &mut Resp) nothrow -> Void {
\t*slot = Optional::Some(p);
\treturn core.void_value();
}

pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tvar leaked: Optional<&mut Resp> = Optional<&mut Resp>::None();
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { store(leaked, x); 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\tr = make_err();
\treturn match leaked {
\t\tOptional::Some(p) => { p.status },
\t\tOptional::None => { 0 }
\t};
}
""")
	assert rc != 0, (
		"call-escape UAF: arm binder passed to helper that stashes "
		"it, then owner reassigned, then escaped pointer used.  "
		"Compile must fail.  Pre-fix HEAD accepts this.  "
		"diagnostics: " + repr(errs)
	)


def test_g2_call_mediated_escape_blocks_subsequent_shared_match(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Call-mediated escape — exclusivity pin.

	When a helper call may have stashed the `&mut` arm binder
	(`store(&mut leaked, x)`), the scrutinee loan is retained
	conservatively.  Any attempt to take a fresh `&r` while the
	escaped pointer is reachable must reject by the standard
	loan-conflict check.  Companion to the UAF carrier test —
	this one pins the conflict at a *read* site rather than a
	write site, exercising the same retain-loan guarantee from
	the read direction."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
fn store(slot: &mut Optional<&mut Resp>, p: &mut Resp) nothrow -> Void {
\t*slot = Optional::Some(p);
\treturn core.void_value();
}

pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tvar leaked: Optional<&mut Resp> = Optional<&mut Resp>::None();
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { store(leaked, x); 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\tval _ = match &r {
\t\tcore.Result::Ok(x) => { x.status },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn 0;
}
""")
	assert rc != 0, (
		"call-mediated escape must block subsequent shared match on "
		"the scrutinee while the escaped &mut pointer is reachable; "
		"diagnostics: " + repr(errs)
	)


def test_g2_call_through_arm_binder_compiles_when_scrutinee_unused(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Stdlib-style call-through pin.  Method / function call that
	takes the arm binder as receiver / arg must compile when the
	scrutinee isn't subsequently used after the match.  This is
	the load-bearing iterator pattern — `match self { Ctor(it)
	=> it.next() }`.  No call-site rejection should fire.

	Note: the conservative loan retention means a *follow-up*
	`match &mut r` or other scrutinee borrow would reject —
	that's covered by `test_g2_call_mediated_escape_blocks_*`.
	The contract is "call-through compiles when isolated"; once
	a call may have stashed the binder, the borrow is retained
	conservatively."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
fn helper(x: &mut Resp) nothrow -> Int {
\tx.status = 99;
\treturn x.status;
}

implement Resp {
\tpub fn touch(self: &mut Resp) nothrow -> Int {
\t\treturn self.status;
\t}
}

pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { helper(x) },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn 0;
}
""")
	assert rc == 0, (
		"single `match &mut r { Ok(x) => helper(x) }` must compile; "
		"got rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


def test_g2_method_call_through_arm_binder_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Stdlib iterator-shape pin: `match &mut self { Ctor(it) =>
	it.next() }`.  Pure method-call-through pattern — the load-
	bearing pattern for `std.iter`,
	`std.json::JsonEntriesIter::next`, etc."""
	rc, errs = _check_only(tmp_path, capsys, """
module main;
import std.core as core;

struct Counter { pub n: Int }

implement Counter {
\tpub fn step(self: &mut Counter) nothrow -> Int {
\t\tself.n = self.n + 1;
\t\treturn self.n;
\t}
}

variant State { Idle(), Counting(c: Counter) }

implement State {
\tpub fn tick(self: &mut State) nothrow -> Int {
\t\tmatch self {
\t\t\tState::Counting(c) => { return c.step(); },
\t\t\tdefault => { return 0; }
\t\t}
\t}
}

pub fn main() nothrow -> Int {
\tvar s: State = State::Counting(c = Counter(n = 0));
\treturn s.tick();
}
""")
	assert rc == 0, (
		"`match self { Ctor(c) => c.method() }` iterator pattern must "
		"compile; got rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


def test_g2_direct_container_wrap_escape_rejects(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Direct container-wrap escape stays rejected at the escape
	site (separate from call-mediated escape, which is loan-retained
	without a call-site diagnostic).  `leaked = Optional::Some(x)`
	is the canonical direct-wrap shape — the user explicitly stores
	the `&mut` arm binder past the arm scope."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tvar leaked: Optional<&mut Resp> = Optional<&mut Resp>::None();
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { leaked = Optional::Some(x); 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\treturn 0;
}
""")
	assert rc != 0, (
		"direct container-wrap escape (`leaked = Optional::Some(x)`) "
		"must reject at the escape site; diagnostics: " + repr(errs)
	)


def test_g2_mut_binder_use_after_scrutinee_mutate(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""G2 / no-UAF pin.  If escape is somehow allowed, mutating the
	scrutinee through a separate path while the escaped pointer is
	live MUST be rejected — this is the no-UAF guarantee.  Either
	the escape itself is rejected (G2 default) or this rejection
	fires.  Either way, the program must not compile."""
	rc, errs = _check_only(tmp_path, capsys, _PRE + """
fn make_err() nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Err(AppErr(code = 2, tag = "err"));
}

pub fn main() nothrow -> Int {
\tvar r = make_ok();
\tvar leaked: Optional<&mut Resp> = Optional<&mut Resp>::None();
\tval _ = match &mut r {
\t\tcore.Result::Ok(x) => { leaked = Optional::Some(x); 0 },
\t\tcore.Result::Err(_) => { 0 }
\t};
\tr = make_err();
\treturn match leaked {
\t\tOptional::Some(p) => { p.status },
\t\tOptional::None => { 0 }
\t};
}
""")
	assert rc != 0, (
		"G2 no-UAF: escape + scrutinee reassign + use of escaped "
		"pointer must be rejected.  HEAD outcome: " + repr(errs)
	)


# ── Stdlib guard: `match self` where `self: &mut Variant` ──────────


def test_stdlib_match_self_struct_payload_stays_green(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""The iterator-style pattern `fn next(self: &mut V) { match
	self { ... } }` is load-bearing for `std.iter`,
	`std.json::JsonEntriesIter::next`, and similar.  Whatever G1
	fixes happen, this shape MUST stay accepted."""
	rc, errs = _check_only(tmp_path, capsys, """
module main;
import std.core as core;

struct Pair { pub a: Int, pub b: Int }
variant State { Idle(), Active(p: Pair) }

implement State {
\tpub fn step(self: &mut State) nothrow -> Int {
\t\tmatch self {
\t\t\tState::Active(p) => { p.a = p.a + 1; return p.b; },
\t\t\tdefault => { return 0; }
\t\t}
\t}
}

pub fn main() nothrow -> Int {
\tvar s: State = State::Active(p = Pair(a = 1, b = 2));
\treturn s.step();
}
""")
	assert rc == 0, (
		"stdlib guard: `match self` where self: &mut Variant must "
		"stay green; got rc=" + str(rc) + " diagnostics: " + repr(errs)
	)


def test_stdlib_match_self_runtime_correctness(tmp_path: Path) -> None:
	"""Stdlib guard, end-to-end: same shape as above, full compile
	+ run.  Asserts the mutation through `match self` is visible to
	a subsequent read."""
	_compile_and_run(tmp_path, """
module main;
import std.core as core;

struct Pair { pub a: Int, pub b: Int }
variant State { Idle(), Active(p: Pair) }

implement State {
\tpub fn step(self: &mut State) nothrow -> Int {
\t\tmatch self {
\t\t\tState::Active(p) => { p.a = p.a + 100; return p.a; },
\t\t\tdefault => { return 0; }
\t\t}
\t}
}

pub fn main() nothrow -> Int {
\tvar s: State = State::Active(p = Pair(a = 5, b = 0));
\treturn s.step();
}
""", expected_rc=105)


# ── Primitive payload under &mut ───────────────────────────────────
#
# These pin the *current surface* with explicit `*n` deref / write
# through `*n = ...`, which is the documented form HEAD already
# accepts.  Whether bare `n + 1` / `n = ...` ergonomics should be
# extended to the &mut form is a separate decision and is NOT
# pinned here.


def test_primitive_payload_explicit_deref_read(tmp_path: Path) -> None:
	"""Primitive payload: read via explicit `*n`.  This is the
	stdlib pattern — `match &self { Errno(code) => *code }`."""
	_compile_and_run(tmp_path, """
module main;
import std.core as core;

variant State { Active(n: Int) }

pub fn main() nothrow -> Int {
\tvar s: State = State::Active(n = 42);
\treturn match &mut s {
\t\tState::Active(n) => { *n }
\t};
}
""", expected_rc=42)


def test_primitive_payload_explicit_deref_write(tmp_path: Path) -> None:
	"""Primitive payload: write via explicit `*n = ...`.  Pin
	end-to-end that the write lands in the original variant
	payload."""
	_compile_and_run(tmp_path, """
module main;
import std.core as core;

variant State { Active(n: Int) }

pub fn main() nothrow -> Int {
\tvar s: State = State::Active(n = 1);
\tval _ = match &mut s {
\t\tState::Active(n) => { *n = 99; 0 }
\t};
\treturn match &s {
\t\tState::Active(n) => { n }
\t};
}
""", expected_rc=99)
