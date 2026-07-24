# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression (B5 probe discovery, 2026-07-23): a fully
NON-GENERIC free-function call in ARGUMENT POSITION of a method call was
silently typed Unknown, so `cb.call(make_ptr(&b))` failed with
"Callback1.call argument 1 type mismatch" while the identical value
bound inline (`val p = …; cb.call(p)`) succeeded.

Root cause (suspected subsystem recorded per policy: checker
CALL-RESOLUTION, `lang/driftc/checker/call_resolver.py` — the
`defer_infer_diag` argument-position deferral): `resolve_method_call`
marks nested `HCall` arguments `defer_infer_diag=True` before pre-typing
them, and `resolve_call_expr` bailed to `unknown_ty` WHENEVER that flag
was set and no expected type was supplied — BEFORE attempting
resolution — even though the callee's signature required no inference at
all.  The deferral was meant to suppress inference DIAGNOSTICS for calls
that genuinely need an expected type (generic ctors, lambdas), not to
preempt resolution of already-resolvable calls.

`doc/refactor_triggers.md` scan: no matching trigger (confirmed at fix
time).  The fix probes the deferred call under an EXPLICIT-OWNER state
transaction (`FnCheckState`/`CheckerStateTxn` in type_checker.py: the
owner holds the recorder side tables as undo-logged overlays plus the
allocator cells; the probed HIR subtree gets a per-node attribute log
with descendant identities preserved; a fail-closed shape allowlist
gates which subtrees may be probed at all) and models three outcomes
classified by STRUCTURED diagnostic codes: COMPLETE (live resolution
stands, transaction commits), NEEDS_EXPECTED (full rollback — no
diagnostics, HIR rewrites, callsite metadata, expression types,
coercions, instantiations, or allocator movement remain — then the
enclosing call retries with the parameter type exactly as pre-fix),
and HARD_ERROR (invalid regardless of expected type: the live
resolution with its REAL diagnostics is committed and the node is
marked so the retry cannot duplicate them).  Unexpected exceptions
inside the probe roll back and RE-RAISE (ICE containment).

This file pins the four mandated behaviors: (1) non-generic
nested-call success (MUST fail pre-fix), (2) argument-inferable
generic success (MUST fail pre-fix), (3) expected-return-dependent
generic retry (worked pre-fix, preserved), (4) hard-error diagnostic
preservation on both the free-call and interface-method-argument
paths (real diagnostic present exactly once).  State-identity across
rollbacks — including allocator state and the exception path — is
pinned separately by the invariant tooth
`lang/tests/checker/test_defer_probe_state_transaction.py`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

SRC = """module main;

import std.core as core;
import std.mem as mem;

fn make_ptr(b: &Byte) nothrow -> mem.Ptr<Byte> {
	return unsafe { mem.ptr_from_ref<type Byte>(b) };
}

fn make_int(b: &Byte) nothrow -> Int {
	return 3;
}

// Non-generic: nested call in method-call argument position.
pub fn direct(cb: core.Callback1<mem.Ptr<Byte>, Int>) nothrow -> Int {
	val b = core.string_byte_at("z", 0);
	return cb.call(make_ptr(&b));
}

// Same shape through a generic require-bound.
pub fn via_bound<T, F>(body: F) nothrow -> T require F is core.Fn1<mem.Ptr<Byte>, T> {
	val b = core.string_byte_at("z", 0);
	return body.call(make_ptr(&b));
}

// Int-returning nested call in argument position (non-pointer control).
pub fn int_arg(cb: core.Callback1<Int, Int>) nothrow -> Int {
	val b = core.string_byte_at("z", 0);
	return cb.call(make_int(&b));
}

pub fn main() nothrow -> Int {
	val cb: core.Callback1<mem.Ptr<Byte>, Int> = core.callback1(|p: mem.Ptr<Byte>| => { 7 });
	val a = direct(move cb);
	val cb2: core.Callback1<mem.Ptr<Byte>, Int> = core.callback1(|p: mem.Ptr<Byte>| => { 5 });
	val g = via_bound<type Int, core.Callback1<mem.Ptr<Byte>, Int> >(cb2);
	val cbi: core.Callback1<Int, Int> = core.callback1(|n: Int| => { n });
	val i = int_arg(move cbi);
	return (a - 7) + (g - 5) + (i - 3);
}
"""


def test_nested_nongeneric_call_in_arg_position_resolves(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(SRC)
	out_bin = tmp_path / "bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(180),
	)
	assert res.returncode == 0, (
		"nested non-generic call in argument position must typecheck "
		f"(defer_infer_diag must not preempt resolution):\n{res.stdout}\n---\n{res.stderr[:2000]}"
	)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert run.returncode == 0, f"exit={run.returncode}\n{run.stderr[:500]}"


SRC_GENERIC = """module main;

import std.core as core;
import std.mem as mem;

fn make_ptr(b: &Byte) nothrow -> mem.Ptr<Byte> {
	return unsafe { mem.ptr_from_ref<type Byte>(b) };
}

// Argument-inferable generic: T infers from the argument alone.
fn ident<T>(x: T) nothrow -> T {
	return x;
}

// Expected-return-dependent target: Optional<Int>'s type argument is
// only inferable from this parameter type at the callsite.
fn take_opt(o: Optional<Int>) nothrow -> Int {
	match o {
		Some(v) => { return v; },
		None() => { return 0; },
	}
}

pub fn main() nothrow -> Int {
	val b = core.string_byte_at("z", 0);

	// (2) argument-inferable generic callee nested in argument position
	// of an INTERFACE method call (the path with no expected-type retry):
	// the transactional attempt must succeed and COMMIT.  Confirmed
	// failing pre-fix ("Callback1.call argument 1 type mismatch").
	val cb: core.Callback1<mem.Ptr<Byte>, Int> = core.callback1(|p: mem.Ptr<Byte>| => { 7 });
	val a = cb.call(ident(make_ptr(&b)));
	val cbi: core.Callback1<Int, Int> = core.callback1(|n: Int| => { n });
	val i = cbi.call(ident(4));

	// (3) expected-return-dependent retry: a qualified generic variant
	// ctor (HQualifiedMember, defer_infer_diag=True on the free-call
	// path) whose type argument resolves ONLY against the expected
	// parameter type — the transactional attempt must fail SILENTLY and
	// the enclosing retry must succeed (the pre-fix deferral contract,
	// preserved; confirmed working pre-fix).
	val s = take_opt(Optional::Some(3));

	return (a - 7) + (i - 4) + (s - 3);
}
"""

SRC_INVALID_FREE = """module main;

import std.core as core;
import std.mem as mem;

fn make_ptr(b: &Byte) nothrow -> mem.Ptr<Byte> {
	return unsafe { mem.ptr_from_ref<type Byte>(b) };
}

fn take_ptr(p: mem.Ptr<Byte>) nothrow -> Int {
	return 7;
}

pub fn main() nothrow -> Int {
	// INVALID: make_ptr expects &Byte, given Int.  On the free-call path
	// the nested call is typed eagerly and its REAL diagnostic must be
	// emitted, naming make_ptr (identical pre/post fix).
	return take_ptr(make_ptr(42));
}
"""

SRC_INVALID_IFACE = """module main;

import std.core as core;
import std.mem as mem;

fn make_ptr(b: &Byte) nothrow -> mem.Ptr<Byte> {
	return unsafe { mem.ptr_from_ref<type Byte>(b) };
}

pub fn main() nothrow -> Int {
	val cb: core.Callback1<mem.Ptr<Byte>, Int> = core.callback1(|p: mem.Ptr<Byte>| => { 7 });
	// INVALID nested call in INTERFACE-method argument position: the
	// probe classifies this as HARD_ERROR (invalid regardless of any
	// expected type), COMMITS the live resolution with its REAL
	// diagnostic naming make_ptr — exactly once, no duplication — and
	// the interface arg check additionally reports the mismatch.
	return cb.call(make_ptr(42));
}
"""


def _compile(tmp_path: Path, src: str):
	f = tmp_path / "main.drift"
	f.write_text(src)
	out_bin = tmp_path / "bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(f), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(180),
	)
	return res, out_bin


def test_generic_inferable_and_expected_dependent_paths(tmp_path: Path) -> None:
	"""(2) argument-inferable generic nested calls COMMIT via the
	transaction; (3) expected-return-dependent forms (unqualified ctor
	sugar) still defer silently and succeed on the enclosing retry —
	and the committed/retried CallInfo produces a correct binary (no
	stale residue: the program runs and returns 0)."""
	res, out_bin = _compile(tmp_path, SRC_GENERIC)
	assert res.returncode == 0, f"{res.stdout}\n---\n{res.stderr[:2000]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert run.returncode == 0, f"exit={run.returncode}\n{run.stderr[:500]}"


def test_invalid_nested_call_keeps_diagnostic_free_call(tmp_path: Path) -> None:
	"""(4a) an INVALID nested call in free-call argument position still
	fails with its real diagnostic naming the failing callee — the
	transactional machinery must not swallow it."""
	res, _ = _compile(tmp_path, SRC_INVALID_FREE)
	assert res.returncode != 0, "invalid nested call must fail to compile"
	err = res.stdout + res.stderr
	assert err.count("no matching overload for function 'make_ptr'") == 1, (
		f"diagnostic must name the failing call exactly once:\n{err[:1500]}"
	)


def test_invalid_nested_call_keeps_diagnostic_iface_arg(tmp_path: Path) -> None:
	"""(4b) an INVALID nested call in interface-method argument position
	is a HARD_ERROR probe outcome: its REAL diagnostic (naming make_ptr)
	is committed exactly once — never swallowed, never duplicated — and
	the compile fails."""
	res, _ = _compile(tmp_path, SRC_INVALID_IFACE)
	assert res.returncode != 0, "invalid nested call must fail to compile"
	err = res.stdout + res.stderr
	assert err.count("no matching overload for function 'make_ptr'") == 1, (
		f"the nested call's real diagnostic must appear exactly once:\n{err[:1500]}"
	)
	assert "Callback1.call argument 1 type mismatch" in err, (
		f"interface arg mismatch diagnostic must also be reported:\n{err[:1500]}"
	)
