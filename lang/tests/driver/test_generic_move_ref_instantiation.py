# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: monomorphizing a generic body containing
`move v` where `v: T` (TypeVar) to `T = &mut Foo` (or any REF type)
fired `cannot move from a reference type; move requires owned
storage` at the instantiation's body type-check, even though the
original generic body had been accepted.

Reported as app-team `compiler-findings.md` #3 (2026-05-17, reply 5):
`just check` against singular gateway with the freshly-rebuilt
mariadb-rpc@0.5.0 failed under `--entry singular.tests.unit.uuid_test
::main` at a fixed source position `<source>:333:27`.  Coordinate
turned out to be stdlib's `Result<T, E>::or_throw` in
`std/core/core.drift:333` -- the `Ok(v) => { return move v; }` arm.
Triggered by `lease.conn().or_throw()` where `lease.conn():
Result<&mut RpcConnection, ManagedError>`.

**Pre-fix shape** (verified deterministic 2026-05-17 against user's
singular workspace at 0.31.103):

  <source>:333:27: error: cannot move from a reference type;
    move requires owned storage [E-AUTO-578aa66a]

Note the file rendering: `<source>` instead of
`stdlib/std/core/core.drift`.  Span.file IS populated correctly by
the parser; the non-JSON diagnostic formatter at `driftc.py:8901`
unconditionally uses the `<source>` placeholder via
`_source_label()`.  Quality-of-diagnostic issue, separate slice.

**Root cause** (read-only diagnosis 2026-05-17 confirmed by
instrumenting the rejection site):

The move-of-ref check at `type_checker.py:8097-8104` rejects every
`HMove` whose subject has `TypeKind.REF`.  Originally intended to
block user code like `var r = &x; val y = move r;` (see existing
`move_from_ref_rejected` fixture), the check fires UNCONDITIONALLY
-- even inside a generic body that was already accepted at its
non-monomorphized type-check (where the subject's type was a
TypeVar, not REF).

When `--entry` triggers full elaboration, the consumer's call to
`lease.conn().or_throw()` monomorphizes `or_throw<T=&mut Conn>`.
The instantiated body's `match self { Ok(v) => { return move v; } }`
arm gets re-type-checked with `v: &mut Conn` (REF) -- so the
strict check fires on stdlib code that the user never wrote.

This silently turned `--entry` (full elaboration) into a STRICTER
SUPERSET of `compile-check`: source that compile-check accepted
fails at entry-point elaboration, with the diagnostic pointing at
stdlib internals rather than the user's call site.

**Fix shape (applied 2026-05-17):**
Skip the move-of-ref check at `type_checker.py:8097` when the
current `FnSignature.is_instantiation` is True.  The original
generic body's type-check already validated the program;
re-firing the strictness check on a monomorphization that the
user can't see and can't avoid is a UX bug.  Non-generic user
source (where the user wrote `move <ref>` directly) still
rejects -- the `move_from_ref_rejected` fixture pins that.

Carriers (positive coverage + two distinct negatives to pin the
exemption's narrowness):

  V1.  Positive (THE BUG, minimum): a generic free fn
       `take<T>(v: T) -> T` with `return move v;` body,
       instantiated with `T = &mut Foo` via `--entry`.  Pre-fix:
       rejected at the instantiation's body type-check with
       `cannot move from a reference type`.  Post-fix: compile +
       run; binary returns 42.
  V2.  Positive (app shape): the actual user case --
       `Result<&mut Foo, MyErr>::or_throw()`.  Proves the fix
       carries through the full stdlib `or_throw` lowering + link
       + run path, not just the abstract generic-body case.  This
       is the exact shape that broke maria's `lease.conn().or_throw()`.
  V3.  Negative (legacy control): non-generic `move r` where
       `r: &Int` -- MUST still reject.  Pinned alongside the
       legacy `move_from_ref_rejected` codegen fixture so a future
       over-eager relaxation can't silently widen.
  V4.  Negative (narrowness): a GENERIC fn with a CONCRETE
       `&Int` param -- `pub fn bad<T>(r: &Int, x: T) nothrow ->
       Int { return move r; ... }` -- MUST still reject at the
       generic body's check.  The `is_instantiation`-based
       exemption protects ONLY moves of monomorphized TypeVars
       that become refs; it does NOT widen to "generic functions
       may move refs in general".  Without this carrier, a future
       widened fix (e.g. "skip for all generic bodies") would
       silently accept moves of non-generic refs in generic
       functions and break the user-source rule for any code
       that happens to live inside a generic.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, source: str, with_entry: bool) -> subprocess.CompletedProcess[str]:
	src_dir = tmp_path / "src"
	src_dir.mkdir(exist_ok=True)
	src = src_dir / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / "main_bin"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--stdlib-root", str(ROOT / "stdlib"),
	]
	if with_entry:
		cmd += ["--entry", "main::main", str(src), "-o", str(out_bin)]
	else:
		cmd += ["--dev", str(src)]
	return subprocess.run(
		cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60,
	)


# ─── V1: THE BUG (minimum) -- generic body's `move v` monomorphized to REF ─


_V1_SRC = """\
module main;
import std.core as core;

pub struct Foo { pub n: Int }

// Generic free fn with `move v` on a TypeVar at value position (a
// `let`-binder consumes the move).  At the generic body's own type-
// check, `v: T` is a TypeVar -- NOT TypeKind.REF -- so the
// move-of-ref check at type_checker.py:8097 does not fire.  When
// `--entry` triggers full elaboration and the body is re-type-checked
// for T = &mut Foo (instantiation), `v: &mut Foo` is REF -- and the
// strict check fires on stdlib-style helper code the user wrote
// once, generically.  Body doesn't return the moved ref (would hit
// the MVP escape-rule for ref returns through a generic, unrelated
// to this bug); a `_y = move v` binder is enough to exercise HMove
// on the TypeVar.
pub fn consume<T>(v: T) nothrow -> Int {
	val _y = move v;
	return 1;
}

pub fn main() nothrow -> Int {
	var x: Foo = Foo(n = 42);
	val _n: Int = consume<type &mut Foo>(&mut x);
	return x.n;
}
"""


def test_v1_generic_move_on_ref_instantiation(tmp_path: Path) -> None:
	"""THE BUG: generic body's `move v` was rejected when v's
	TypeVar got monomorphized to REF.  Without the
	`--entry`-triggered instantiation, the bug is invisible
	(compile-check accepts).  Sub-test runs both modes to lock the
	fix's scope.

	Post-fix expectation: both `--dev` (no entry) AND `--entry
	main::main` compile cleanly; binary returns 42 (Foo.n)."""
	# (a) compile-check (no --entry) must succeed both pre- and post-
	# fix.  Locks the failure axis to instantiation, not source.
	res_dev = _compile(tmp_path, _V1_SRC, with_entry=False)
	assert res_dev.returncode == 0, (
		f"V1 compile-check (--dev, no instantiation) failed -- bug is "
		f"in the generic body itself, not the instantiation:\n"
		f"{res_dev.stderr[-1500:]}"
	)
	# (b) --entry triggers monomorphization.  Pre-fix: rejected.
	# Post-fix: compile + run; binary returns 42.
	res_entry = _compile(tmp_path, _V1_SRC, with_entry=True)
	assert "cannot move from a reference type" not in res_entry.stderr, (
		f"V1 REGRESSION: `--entry` full elaboration fails on a "
		f"generic body whose `move v` is on a monomorphized REF "
		f"TypeVar.  The instantiation-context carve-out at "
		f"`type_checker.py:8097` (skip move-of-ref check when "
		f"`sig.is_instantiation` is True) was reverted or never "
		f"landed.\n\n{res_entry.stderr[-1500:]}"
	)
	assert res_entry.returncode == 0, (
		f"V1 --entry compile failed but NOT with the known shape:\n"
		f"{res_entry.stderr[-1500:]}"
	)
	out_bin = tmp_path / "main_bin"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 42, (
		f"V1 binary exited {run.returncode}; expected 42 (x.n after move-then-drop)"
	)


# ─── V2: Positive app shape -- stdlib's Result<&mut Foo, E>::or_throw() ──


_V2_SRC = """\
module main;
import std.core as core;

pub struct Foo { pub n: Int }

pub error MyErr { tag: String }

// Mirror of the maria team's `lease.conn(): Result<&mut Conn, _>`
// shape.  Returning a `Result<&mut Foo, MyErr>` and unwrapping it
// via `.or_throw()` was the original failing call path.  The
// monomorphized `Result<&mut Foo, MyErr>::or_throw` body has
// `match self { Ok(v) => { return move v; }, ... }` with
// `v: &mut Foo` -- the exact stdlib code the move-of-ref check
// was firing on at `<source>:333:27`.  Post-fix: this carrier
// compiles + links + runs end-to-end.  Type inference on `Result
// ::Ok(...)` figures out the result type from the return-type
// annotation, matching how stdlib code constructs Results.
pub fn maybe_lease(x: &mut Foo, want: Bool) nothrow -> core.Result<&mut Foo, MyErr> {
	if want { return core.Result::Ok(x); }
	return core.Result::Err(MyErr(tag = "no"));
}

pub fn doit(x: &mut Foo) throws MyErr -> Int {
	val conn: &mut Foo = maybe_lease(x, true).or_throw();
	return conn.n;
}

pub fn main() nothrow -> Int {
	var x: Foo = Foo(n = 7);
	try {
		val n = doit(&mut x);
		if n == 7 { return 0; }
		return 1;
	} catch main:MyErr(e) {
		return 2;
	}
}
"""


def test_v2_app_shape_result_or_throw_with_ref_t(tmp_path: Path) -> None:
	"""Positive (app shape): the actual user case -- a `Result<&mut
	Foo, MyErr>` unwrapped via `.or_throw()`.  This is the exact
	stdlib call path that was failing for the maria team
	(`lease.conn().or_throw()` returning `&mut RpcConnection`).
	Proves the fix carries through the full stdlib `or_throw`
	lowering + link + run path.

	Post-fix expectation: compile + run; binary returns 0."""
	res = _compile(tmp_path, _V2_SRC, with_entry=True)
	assert "cannot move from a reference type" not in res.stderr, (
		f"V2 REGRESSION: stdlib's `Result<T, E>::or_throw` monomorphized "
		f"with `T = &mut Foo` still fires the move-of-ref check at "
		f"core.drift:333.  Same root as V1 -- if V1 passes but V2 fails, "
		f"something downstream of the type-check (lowering / link) is "
		f"newly tripping on `&mut` move semantics in the instantiated "
		f"body.\n\n{res.stderr[-1500:]}"
	)
	assert res.returncode == 0, (
		f"V2 compile failed but NOT with the known shape:\n"
		f"{res.stderr[-1500:]}"
	)
	out_bin = tmp_path / "main_bin"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 0, (
		f"V2 binary exited {run.returncode}; expected 0"
	)


# ─── V3: Negative (legacy) -- non-generic move-of-ref STILL rejects ─


_V3_SRC = """\
module main;

pub fn main() nothrow -> Int {
	var x: Int = 1;
	var r: &Int = &x;
	val y: &Int = move r;  // user-source move on &Int -- MUST reject
	return 0;
}
"""


def test_v3_user_source_move_of_ref_still_rejected(tmp_path: Path) -> None:
	"""Control: user-source `move r` where `r: &Int` (immutable ref)
	must STILL fire `cannot move from a reference type`.  The V1+V2
	fix narrows by instantiation context only -- it does NOT silently
	relax the check for non-generic user code.

	If this regresses (the fix accidentally widens to all REF moves),
	a deliberate user footgun silently becomes a no-op."""
	res = _compile(tmp_path, _V3_SRC, with_entry=False)
	assert "cannot move from a reference type" in res.stderr, (
		f"V3 CONTROL REGRESSION: non-generic user-source `move r` "
		f"where r is `&Int` should STILL fire the move-of-ref "
		f"diagnostic.  If V1 was widened to accept all REF moves, "
		f"this control silently became a no-op.\n\n{res.stderr[-1500:]}"
	)
	assert res.returncode != 0, (
		f"V3 control compile exited 0 -- the move-of-ref rejection "
		f"silently relaxed for non-generic code"
	)


# ─── V4: Negative (narrowness) -- generic fn with concrete &T param ─


_V4_SRC = """\
module main;

// Generic fn whose `move` is on a CONCRETE `&Int` param (not a TypeVar).
// The `is_instantiation` exemption MUST NOT cover this: the move's
// subject type was REF at the generic body's own type-check too.
// Without this distinction a future "skip for all generic bodies"
// over-fix would silently accept moves of any ref inside any generic
// function, breaking the user-source rule whenever a developer
// happened to wrap an existing function in a generic.
pub fn bad<T>(r: &Int, x: T) nothrow -> T {
	val _y: &Int = move r;  // move on a CONCRETE &Int -- MUST reject
	return x;
}

pub fn main() nothrow -> Int { return 0; }
"""


def test_v4_generic_fn_with_concrete_ref_param_still_rejected(tmp_path: Path) -> None:
	"""Narrowness: a GENERIC fn with a CONCRETE `&Int` parameter --
	`bad<T>(r: &Int, x: T)` with `move r` -- MUST still reject at
	the generic body's check.

	The `is_instantiation`-based exemption protects ONLY moves of
	monomorphized TypeVars that become refs; it does NOT widen to
	"generic functions may move refs in general".

	Without this carrier, a future widened fix (e.g. "skip for all
	generic bodies", or "skip whenever fn has type_params") would
	silently accept moves of non-generic refs in generic functions
	and break the user-source rule for any code that happens to
	live inside a generic.  This pins the failure axis precisely:
	exempt the MONOMORPHIZED `v: T` case (V1), keep the
	NON-MONOMORPHIZED `r: &Int` case (V4) rejecting -- even when
	the enclosing fn is generic."""
	res = _compile(tmp_path, _V4_SRC, with_entry=False)
	assert "cannot move from a reference type" in res.stderr, (
		f"V4 NARROWNESS REGRESSION: a generic fn `bad<T>(r: &Int, "
		f"x: T)` with `move r` (on the concrete &Int param) should "
		f"STILL fire the move-of-ref diagnostic at the generic "
		f"body's type-check.  If this passes, the "
		f"`is_instantiation` exemption was widened too far -- the "
		f"check would now silently accept moves of any ref inside "
		f"any generic function.\n\n{res.stderr[-1500:]}"
	)
	assert res.returncode != 0, (
		f"V4 narrowness compile exited 0 -- the move-of-ref check "
		f"silently relaxed for generic fns with concrete ref params"
	)
