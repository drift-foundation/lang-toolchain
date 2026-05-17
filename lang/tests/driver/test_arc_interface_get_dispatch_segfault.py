# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: method dispatch through
`Arc<Interface>.get().method()` segfaults at runtime.

Reported by the SingularGateway app team 2026-05-17 after their
0.31.97+abi14 bisect of the three earlier blockers (shared
binder names; Void callback lambda; `_arc_fat_bump_strong_via_ctrl`
link error -- all confirmed fixed in 0.31.97).  This is the
**fourth** distinct bug uncovered by the sgw-stub work and the
remaining ship-blocker for the `Arc<SingularGateway>` shape:
without working runtime dispatch through interface refs there is
no event-sink / observer / pluggable-implementation pattern.

**Three-way discriminator** (app team's bisect, verified
locally on 0.31.97 staged):

  A. `g.get().greet()` on a concrete `Arc<G>` -- direct
     dispatch -- **works** (returns 42).
  B. `g.as_interface<type Greeter>()` construction only --
     **works** (returns the post-construction value with no
     dispatch).
  C. `gw.get().greet()` on `gw: Arc<Greeter>` built via
     as_interface -- **SIGSEGV** on the first method call
     through the interface vtable.

Critical: this is NOT a recurrence of the
`_arc_fat_bump_strong_via_ctrl` link error fixed in 0.31.97.
The bump-helper-reachability fix landed and is independently
verified by
`test_fat_arc_interface_views.py::test_pkg_fat_arc_as_interface_helper_body_pulled_in`.
0.31.97 makes the program compile + link cleanly; the new bug
fires at runtime, on first method dispatch.

**Suspect area** (per app-team report + my note when the
segfault was first observed during blocker-3 verification):
fat-Arc interface-view / vtable receiver lowering.  Likely
suspects:
  - `M.ArcAsInterface` MIR op or `_emit_arc_as_interface`
    LLVM lowering (`lang/codegen/llvm/llvm_codegen.py`,
    step 4: T-as-I vtable resolution via
    `_emit_interface_view_fields`);
  - `M.ArcFatGet` MIR op or its LLVM lowering (which
    extracts `{data, vtable}` into a fresh DRIFT_IFACE_TYPE
    slot and returns it as `&I`);
  - interface method dispatch codegen on the borrowed `&I`
    returned by `.get()`.

The IR diff between `Arc<G>.get().greet()` (works) and
`gw.get().greet()` (segfaults) -- combined with the
construction-only positive control -- localizes the bug to
the dispatch path, not construction.  This file pins the
exact failure shape for the upcoming patch to land against.

**Carriers** (matching the reviewer's coverage requirements):

  - V1 / DISCRIMINATOR_A: direct dispatch on `Arc<G>` -- must
    keep returning 42 (no regression in the working path).
  - V2 / DISCRIMINATOR_B: as_interface construction with no
    dispatch -- must keep returning 7 (no regression in the
    working construction path).
  - V3 / DISCRIMINATOR_C: **the bug.**  `gw.get().greet()`
    on `gw: Arc<Greeter>` -- must return 42, currently
    segfaults.
  - V4: dispatch through a method with non-trivial body
    (field access via `self.value` plus arithmetic) -- pins
    that `self` is correctly resolved through the fat-Arc
    interface view.
  - V5: dispatch through SECOND method on the same
    interface -- catches vtable-slot-index mistakes (if the
    bug is "always dispatches to slot 0" or similar, V5
    will surface it).
  - V6: SECOND implementor type implementing the same
    interface -- catches per-T vtable mistakes (if the bug
    is "all Arc<I> share one vtable" or similar, V6 will
    surface it).

Every carrier compiles AND runs the produced binary to
completion -- pure compile-time validation would miss this
bug entirely (the IR compiles cleanly today; the segfault
fires at runtime).

If a future fix touches export/reachability again (as the
0.31.97 bump-helper-seed fix did), package-consumer
coverage should be added to this file at that time -- per
the app team's note, "local-source builds already masked
the previous Arc helper bug."  Defer until the fix shape
is known.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(
	tmp_path: Path,
	module_name: str,
	source: str,
) -> tuple[int, str, str, int]:
	"""Compile via subprocess + execute the produced binary.

	Returns (compile_rc, compile_stderr, run_stderr, run_rc).

	Uses subprocess for BOTH steps so the SIGSEGV from the bug
	is visible as a non-zero run_rc rather than crashing the
	pytest worker.  Compile-only would not detect this bug --
	the IR compiles and links cleanly today; the failure is at
	first method-dispatch in the executed binary.
	"""
	src_path = tmp_path / f"{module_name}.drift"
	src_path.write_text(source)
	out_bin = tmp_path / f"{module_name}_bin"

	cc = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc",
			"--stdlib-root", str(ROOT / "stdlib"),
			"--entry", f"{module_name}::main",
			str(src_path),
			"-o", str(out_bin),
		],
		cwd=str(ROOT), capture_output=True, text=True, timeout=120,
	)
	if cc.returncode != 0 or not out_bin.exists():
		return cc.returncode, cc.stderr, "", -1

	run = subprocess.run(
		[str(out_bin)],
		capture_output=True, text=True, timeout=30,
	)
	return cc.returncode, cc.stderr, run.stderr, run.returncode


# ─── V1 / DISCRIMINATOR_A: direct dispatch on concrete Arc<G> ────

_V1_DIRECT_DISPATCH = """\
module v1_direct;

import std.core as core;
import std.core.arc as arc;

pub interface Greeter { fn greet(self: &Self) -> Int }
struct G { value: Int }
implement Greeter for G {
\tpub fn greet(self: &G) nothrow -> Int { return self.value; }
}

pub fn main() nothrow -> Int {
\tval g: arc.Arc<G> = arc.arc(G(value = 42));
\ttry { return g.get().greet(); } catch e { return 99; }
}
"""


def test_v1_arc_concrete_get_method_returns_42(tmp_path: Path) -> None:
	"""DISCRIMINATOR A (positive control): direct dispatch on
	concrete `Arc<G>.get().greet()` must keep working post-fix.

	If this regresses, the fix broke the non-interface dispatch
	path -- the fat-Arc interface-view fix should not touch
	thin-Arc direct dispatch."""
	cc_rc, cc_err, run_err, run_rc = _compile_and_run(
		tmp_path, "v1_direct", _V1_DIRECT_DISPATCH,
	)
	assert cc_rc == 0, f"V1 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 42, (
		f"V1 (direct Arc<G>.get().greet()) returned {run_rc}, "
		f"expected 42.  If SIGSEGV (-11 / 139): the fix has "
		f"broken the working direct-dispatch path.\n"
		f"run stderr: {run_err[-500:]}"
	)


# ─── V2 / DISCRIMINATOR_B: as_interface construction only ────────

_V2_CONSTRUCT_ONLY = """\
module v2_construct;

import std.core as core;
import std.core.arc as arc;

pub interface Greeter { fn greet(self: &Self) -> Int }
struct G { value: Int }
implement Greeter for G {
\tpub fn greet(self: &G) nothrow -> Int { return self.value; }
}

pub fn main() nothrow -> Int {
\tval g: arc.Arc<G> = arc.arc(G(value = 42));
\tval gw: arc.Arc<Greeter> = g.as_interface<type Greeter>();
\treturn 7;
}
"""


def test_v2_as_interface_construction_only_returns_7(tmp_path: Path) -> None:
	"""DISCRIMINATOR B (positive control): `as_interface<type I>()`
	construction without subsequent dispatch must keep working.

	If this regresses (or segfaults), the bug is in the
	construction path (likely `ArcAsInterface` lowering or
	the strong-bump call), not the dispatch path -- different
	suspect area."""
	cc_rc, cc_err, run_err, run_rc = _compile_and_run(
		tmp_path, "v2_construct", _V2_CONSTRUCT_ONLY,
	)
	assert cc_rc == 0, f"V2 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 7, (
		f"V2 (as_interface construction only) returned {run_rc}, "
		f"expected 7.  If SIGSEGV: the construction path broke; "
		f"the bug is upstream of dispatch.\n"
		f"run stderr: {run_err[-500:]}"
	)


# ─── V3 / DISCRIMINATOR_C: THE BUG ───────────────────────────────

_V3_INTERFACE_DISPATCH = """\
module v3_dispatch;

import std.core as core;
import std.core.arc as arc;

pub interface Greeter { fn greet(self: &Self) -> Int }
struct G { value: Int }
implement Greeter for G {
\tpub fn greet(self: &G) nothrow -> Int { return self.value; }
}

pub fn main() nothrow -> Int {
\tval g: arc.Arc<G> = arc.arc(G(value = 42));
\tval gw: arc.Arc<Greeter> = g.as_interface<type Greeter>();
\ttry { return gw.get().greet(); } catch e { return 99; }
}
"""


def test_v3_arc_interface_get_method_returns_42_not_segfault(tmp_path: Path) -> None:
	"""DISCRIMINATOR C (THE BUG): `gw.get().greet()` on
	`gw: Arc<Greeter>` built via `as_interface<type Greeter>()`
	must return 42, not SIGSEGV.

	Pre-fix shape (verified on 0.31.97 staged 2026-05-17):
	  - compile + link: clean
	  - binary run: SIGSEGV (run_rc = 139 or -11) on first
	    method dispatch through the interface vtable

	Post-fix expectation:
	  - returns 42 (the value field on G)

	Suspect area (per app-team report + assistant's
	investigation note from blocker-3 verification): fat-Arc
	interface-view / vtable receiver lowering.  Candidates:
	  - `M.ArcAsInterface` lowering at
	    `lang/codegen/llvm/llvm_codegen.py::_emit_arc_as_interface`
	    (vtable resolution at step 4 via
	    `_emit_interface_view_fields`);
	  - `M.ArcFatGet` lowering (extracts `{data, vtable}` into
	    a fresh DRIFT_IFACE_TYPE slot, returns the alloca ptr
	    as `&I`);
	  - interface-method dispatch on the borrowed `&I`
	    returned by `.get()`.

	The IR diff between V1 (works) and V3 (segfaults) plus
	the V2 positive control localize the bug to the dispatch
	path after `.get()`, not to construction.

	NOT a recurrence of `_arc_fat_bump_strong_via_ctrl`
	link error -- that's a compile-time / link-time bug fixed
	in 0.31.97 and pinned by
	`test_fat_arc_interface_views.py::test_pkg_fat_arc_as_interface_helper_body_pulled_in`.
	This is purely runtime.
	"""
	cc_rc, cc_err, run_err, run_rc = _compile_and_run(
		tmp_path, "v3_dispatch", _V3_INTERFACE_DISPATCH,
	)
	assert cc_rc == 0, (
		f"V3 compile/link failed -- the runtime bug requires a "
		f"successful compile to surface.  If compile is broken, "
		f"the 0.31.97 bump-helper-seed fix may have regressed.\n"
		f"{cc_err[-1500:]}"
	)
	assert run_rc not in (-11, 139), (
		f"V3 (Arc<Greeter>.get().greet()) SIGSEGV -- the runtime "
		f"dispatch through the fat-Arc interface vtable is wired "
		f"up incorrectly.  Suspect: `M.ArcAsInterface` lowering, "
		f"`M.ArcFatGet` lowering, or interface dispatch on the "
		f"borrowed `&I` returned by `.get()`.  See "
		f"`_emit_arc_as_interface` and `_emit_interface_view_fields` "
		f"in `lang/codegen/llvm/llvm_codegen.py`.\n"
		f"run stderr: {run_err[-500:]}"
	)
	assert run_rc == 42, (
		f"V3 (Arc<Greeter>.get().greet()) returned {run_rc}, "
		f"expected 42 (the value field).  No SIGSEGV but wrong "
		f"value -- the dispatch reached SOMETHING but not the "
		f"correct method body or `self` pointer.\n"
		f"run stderr: {run_err[-500:]}"
	)


# ─── V4: dispatch with non-trivial body (field access + arithmetic) ──

_V4_NONTRIVIAL_BODY = """\
module v4_nontrivial;

import std.core as core;
import std.core.arc as arc;

pub interface Computer { fn compute(self: &Self, n: Int) -> Int }
struct C { base: Int, multiplier: Int }
implement Computer for C {
\tpub fn compute(self: &C, n: Int) nothrow -> Int {
\t\treturn self.base + self.multiplier * n;
\t}
}

pub fn main() nothrow -> Int {
\tval c: arc.Arc<C> = arc.arc(C(base = 10, multiplier = 3));
\tval cw: arc.Arc<Computer> = c.as_interface<type Computer>();
\ttry { return cw.get().compute(4); } catch e { return 99; }
}
"""


def test_v4_dispatch_with_field_access_and_arithmetic(tmp_path: Path) -> None:
	"""Dispatch through a non-trivial method that reads TWO
	`self` fields and does arithmetic.  Pins that `self` is
	correctly resolved through the fat-Arc interface view --
	if the bug is "wrong self pointer," V4 would either
	return 0 (zeroed self) or garbage.

	Expected: 10 + 3*4 = 22."""
	cc_rc, cc_err, run_err, run_rc = _compile_and_run(
		tmp_path, "v4_nontrivial", _V4_NONTRIVIAL_BODY,
	)
	assert cc_rc == 0, f"V4 compile failed:\n{cc_err[-1500:]}"
	assert run_rc not in (-11, 139), (
		f"V4 SIGSEGV -- same suspect area as V3, dispatch "
		f"path through fat-Arc interface view.\n"
		f"run stderr: {run_err[-500:]}"
	)
	assert run_rc == 22, (
		f"V4 returned {run_rc}, expected 22 (10 + 3*4).  "
		f"Dispatch reached something but `self` resolution "
		f"or method body is wrong.\n"
		f"run stderr: {run_err[-500:]}"
	)


# ─── V5: dispatch through SECOND method on same interface (vtable slot) ──

_V5_SECOND_METHOD = """\
module v5_second_method;

import std.core as core;
import std.core.arc as arc;

pub interface Pair {
\tfn first(self: &Self) -> Int;
\tfn second(self: &Self) -> Int
}
struct P { a: Int, b: Int }
implement Pair for P {
\tpub fn first(self: &P) nothrow -> Int { return self.a; }
\tpub fn second(self: &P) nothrow -> Int { return self.b; }
}

pub fn main() nothrow -> Int {
\tval p: arc.Arc<P> = arc.arc(P(a = 11, b = 22));
\tval pw: arc.Arc<Pair> = p.as_interface<type Pair>();
\ttry { return pw.get().second(); } catch e { return 99; }
}
"""


def test_v5_dispatch_second_method_returns_correct_field(tmp_path: Path) -> None:
	"""Dispatch through the SECOND method declared on an
	interface (interface has two methods; we call `second`,
	not `first`).  Catches vtable-slot-index mistakes -- if
	the bug is "always dispatches to slot 0" or "off-by-one
	in vtable indexing," V5 would return 11 (from `first`)
	instead of 22 (from `second`), or segfault.

	Expected: 22 (P.b)."""
	cc_rc, cc_err, run_err, run_rc = _compile_and_run(
		tmp_path, "v5_second_method", _V5_SECOND_METHOD,
	)
	assert cc_rc == 0, f"V5 compile failed:\n{cc_err[-1500:]}"
	assert run_rc not in (-11, 139), (
		f"V5 SIGSEGV.\nrun stderr: {run_err[-500:]}"
	)
	assert run_rc != 11, (
		f"V5 returned 11 -- vtable dispatch reached `first` "
		f"instead of `second`.  Likely a vtable-slot-index "
		f"bug (off-by-one or always-slot-0).\n"
		f"run stderr: {run_err[-500:]}"
	)
	assert run_rc == 22, (
		f"V5 returned {run_rc}, expected 22 (P.b via .second()).\n"
		f"run stderr: {run_err[-500:]}"
	)


# ─── V6: SECOND implementor of the same interface (per-T vtable) ──

_V6_SECOND_IMPLEMENTOR = """\
module v6_second_impl;

import std.core as core;
import std.core.arc as arc;

pub interface Greeter { fn greet(self: &Self) -> Int }

struct G1 { value: Int }
implement Greeter for G1 {
\tpub fn greet(self: &G1) nothrow -> Int { return self.value; }
}

struct G2 { tag: Int }
implement Greeter for G2 {
\tpub fn greet(self: &G2) nothrow -> Int { return self.tag + 100; }
}

pub fn main() nothrow -> Int {
\tval g1: arc.Arc<G1> = arc.arc(G1(value = 42));
\tval g2: arc.Arc<G2> = arc.arc(G2(tag = 7));
\tval gw1: arc.Arc<Greeter> = g1.as_interface<type Greeter>();
\tval gw2: arc.Arc<Greeter> = g2.as_interface<type Greeter>();
\ttry {
\t\tval n1 = gw1.get().greet();   // expect 42
\t\tval n2 = gw2.get().greet();   // expect 107
\t\treturn n1 + n2;               // expect 149
\t} catch e { return 99; }
}
"""


def test_v6_two_implementors_dispatch_per_t_vtables(tmp_path: Path) -> None:
	"""Two concrete types (G1, G2) both implement the same
	interface (Greeter); their fields and method bodies
	differ.  Calls `greet()` through Arc<Greeter> for each,
	sums the results.

	Catches per-T vtable mistakes -- if the bug is "all
	Arc<I> share one vtable" (e.g., always G1's vtable), V6
	would return 42+42=84 instead of 149.  If `self` is
	mis-resolved per-T, V6 might segfault on G2 even if G1
	works.

	Expected: 42 + (7 + 100) = 149."""
	cc_rc, cc_err, run_err, run_rc = _compile_and_run(
		tmp_path, "v6_second_impl", _V6_SECOND_IMPLEMENTOR,
	)
	assert cc_rc == 0, f"V6 compile failed:\n{cc_err[-1500:]}"
	assert run_rc not in (-11, 139), (
		f"V6 SIGSEGV -- per-T vtable resolution or per-T `self` "
		f"casting is wrong for at least one implementor.\n"
		f"run stderr: {run_err[-500:]}"
	)
	assert run_rc != 84, (
		f"V6 returned 84 -- both Arc<Greeter> resolved to G1's "
		f"vtable.  Per-T vtable distinction is broken (likely "
		f"`_emit_interface_view_fields` reusing the same vtable "
		f"symbol regardless of concrete T).\n"
		f"run stderr: {run_err[-500:]}"
	)
	assert run_rc == 149, (
		f"V6 returned {run_rc}, expected 149 (42 + 107).\n"
		f"run stderr: {run_err[-500:]}"
	)


# ─── V7: matched ABI -- both nothrow ────────────────────────────────
#
# Added 2026-05-17 with the thunk ABI-bridge fix.  V3 (the bug) had
# iface=can-throw + impl=nothrow.  V7 covers the matched-nothrow side
# of the configuration matrix: thunk should be a pass-through, no
# Ok-wrapping needed.  Returns 42 like V3 post-fix.

_V7_BOTH_NOTHROW = """\
module v7_both_nothrow;

import std.core as core;
import std.core.arc as arc;

pub interface Greeter { fn greet(self: &Self) nothrow -> Int }
struct G { value: Int }
implement Greeter for G {
\tpub fn greet(self: &G) nothrow -> Int { return self.value; }
}

pub fn main() nothrow -> Int {
\tval g: arc.Arc<G> = arc.arc(G(value = 42));
\tval gw: arc.Arc<Greeter> = g.as_interface<type Greeter>();
\treturn gw.get().greet();
}
"""


def test_v7_matched_nothrow_thunk_passes_through(tmp_path: Path) -> None:
	"""Matched ABI (both nothrow): thunk is a simple pass-through,
	no Ok-wrapping.  Verifies the matched branch of the
	`iface_can_throw == impl_can_throw` decision in
	`_emit_iface_method_thunk` still works correctly post-fix.

	Note no `try`/`catch` -- the nothrow interface contract means
	the call site doesn't need an exception handler.

	Expected: 42."""
	cc_rc, cc_err, run_err, run_rc = _compile_and_run(
		tmp_path, "v7_both_nothrow", _V7_BOTH_NOTHROW,
	)
	assert cc_rc == 0, f"V7 compile failed:\n{cc_err[-1500:]}"
	assert run_rc not in (-11, 139), (
		f"V7 SIGSEGV -- matched-nothrow path regressed.  Check the "
		f"`iface_can_throw == impl_can_throw` branch in "
		f"`_emit_iface_method_thunk`.\n"
		f"run stderr: {run_err[-500:]}"
	)
	assert run_rc == 42, (
		f"V7 (matched nothrow) returned {run_rc}, expected 42.\n"
		f"run stderr: {run_err[-500:]}"
	)


# ─── V8: matched ABI -- both can-throw (forced effective can-throw) ──
#
# Same as V7 but for the matched-can-throw side of the matrix.  The
# impl body has a real throw path on a branch the runtime input
# never takes (`value < 0`); this forces the checker's effective-
# can-throw analysis to classify the impl as can-throw rather than
# silently normalizing it to nothrow (which would turn this test
# into a silent duplicate of V3's adapter branch -- the original V8
# had this exact flaw flagged in K-review 2026-05-17).
#
# The throw branch is unreachable at runtime (value=42, never < 0),
# so the call still returns 42 -- but the IR has the impl emitted
# with the can-throw ABI and the thunk goes through the matched
# pass-through branch.

_V8_BOTH_CAN_THROW = """\
module v8_both_can_throw;

import std.core as core;
import std.core.arc as arc;

pub error V8Err { tag: Int }

pub interface Greeter { fn greet(self: &Self) -> Int }
struct G { value: Int }
implement Greeter for G {
	pub fn greet(self: &G) -> Int {
		if self.value < 0 { throw V8Err(tag = 1); }
		return self.value;
	}
}

pub fn main() nothrow -> Int {
	val g: arc.Arc<G> = arc.arc(G(value = 42));
	val gw: arc.Arc<Greeter> = g.as_interface<type Greeter>();
	try { return gw.get().greet(); } catch e { return 99; }
}
"""


def test_v8_matched_can_throw_thunk_passes_through(tmp_path: Path) -> None:
	"""Matched ABI (both can-throw, effective): thunk passes
	FnResult through unchanged, no Ok-wrapping needed.

	The impl body has a real throw path (`if self.value < 0 throw`)
	to force the checker's effective-can-throw analysis to classify
	the impl as can-throw -- prevents silent normalization to
	nothrow that would make this test a duplicate of V3's adapter
	branch.  The throw branch is unreachable at runtime (value=42),
	so the call still returns 42.

	Expected: 42 (the value, NOT 99 from the catch)."""
	cc_rc, cc_err, run_err, run_rc = _compile_and_run(
		tmp_path, "v8_both_can_throw", _V8_BOTH_CAN_THROW,
	)
	assert cc_rc == 0, f"V8 compile failed:\n{cc_err[-1500:]}"
	assert run_rc not in (-11, 139), (
		f"V8 SIGSEGV -- matched-can-throw path regressed.\n"
		f"run stderr: {run_err[-500:]}"
	)
	assert run_rc == 42, (
		f"V8 (matched can-throw) returned {run_rc}, expected 42 "
		f"(the value field; rc=99 would indicate the throw branch "
		f"unexpectedly fired or the caught-error path triggered).\n"
		f"run stderr: {run_err[-500:]}"
	)


# ─── V10: surface mismatch (impl WITHOUT nothrow, body proves nothrow) ─
#
# The checker accepts an impl whose surface declaration says
# can-throw (no `nothrow` keyword) against a nothrow interface
# IF the body provably never throws -- it normalizes the impl's
# EFFECTIVE can-throw to False on `FnInfo.declared_can_throw`,
# even though `FnInfo.signature.declared_can_throw` remains True.
#
# Codegen body emission (`_FuncBuilder::Return` ~line 7310) reads
# the EFFECTIVE bit, so the impl is emitted as nothrow ABI
# (returns i64, not FnResult).  The thunk MUST also read the
# effective bit, otherwise it would emit a mis-typed `call`
# expecting FnResult -- the same shape that produced the original
# sgw-stub SIGSEGV, just on the surface-vs-effective axis.
#
# Pre-fix (using signature.declared_can_throw): SIGSEGV on
# dispatch because the thunk emits FnResult-return inner call
# against an i64-returning body.
# Post-fix (using fn_info.declared_can_throw): thunk matches
# body's actual ABI; returns 42 cleanly.

_V10_SURFACE_MISMATCH = """\
module v10_surface_mismatch;

import std.core as core;
import std.core.arc as arc;

pub interface Greeter { fn greet(self: &Self) nothrow -> Int }
struct G { value: Int }
implement Greeter for G {
	pub fn greet(self: &G) -> Int { return self.value; }
}

pub fn main() nothrow -> Int {
	val g: arc.Arc<G> = arc.arc(G(value = 42));
	val gw: arc.Arc<Greeter> = g.as_interface<type Greeter>();
	return gw.get().greet();
}
"""


def test_v10_surface_can_throw_effective_nothrow_impl(tmp_path: Path) -> None:
	"""Impl declared WITHOUT `nothrow` but body provably doesn't
	throw, against a `nothrow` interface.  The checker accepts
	this and normalizes the impl's effective can-throw to False
	on `FnInfo`.  Codegen body emission reads the effective bit
	from `FnInfo`, so the impl is emitted as nothrow ABI.

	The thunk MUST also read the effective bit (from
	`impl_info.declared_can_throw`, not
	`impl_info.signature.declared_can_throw`), otherwise it emits
	a mis-typed inner call (expects FnResult, body returns i64).

	No `try`/`catch` -- the interface contract says it can't
	throw, so the call site doesn't wrap.  Pre-fix
	(signature-bit): SIGSEGV at dispatch (same shape as V3 on
	the surface-vs-effective axis).
	Post-fix (effective-bit): returns 42.

	Expected: 42."""
	cc_rc, cc_err, run_err, run_rc = _compile_and_run(
		tmp_path, "v10_surface_mismatch", _V10_SURFACE_MISMATCH,
	)
	assert cc_rc == 0, (
		f"V10 compile failed -- if 'declared nothrow but may throw' "
		f"diagnostic appears, the checker has tightened to surface-"
		f"level rejection and the test source needs adjusting:\n"
		f"{cc_err[-1500:]}"
	)
	assert run_rc not in (-11, 139), (
		f"V10 SIGSEGV -- the thunk inner-call ABI is reading the "
		f"SURFACE bit (`impl_info.signature.declared_can_throw`) "
		f"instead of the EFFECTIVE bit "
		f"(`impl_info.declared_can_throw`).  Body emission reads "
		f"the effective bit at `_FuncBuilder::Return` (~line 7310); "
		f"thunk must match.\n"
		f"run stderr: {run_err[-500:]}"
	)
	assert run_rc == 42, (
		f"V10 returned {run_rc}, expected 42.\n"
		f"run stderr: {run_err[-500:]}"
	)


# ─── V9: nothrow interface + impl that actually throws -- must REJECT ─
#
# Negative compile test.  The checker enforces nothrow via effective-
# can-throw analysis: an impl whose body actually throws cannot
# satisfy a nothrow interface contract.  Verified 2026-05-17:
# checker emits `E-AUTO-3b328370` ("function ... is declared nothrow
# but may throw").
#
# Pinned so the checker rule can't silently regress -- if the impl
# became accepted, the codegen `if not iface_can_throw and
# impl_can_throw` assertion in `_emit_iface_method_thunk` would also
# fire and refuse to emit unsafe IR (defense in depth).
#
# Note: the SURFACE declaration mismatch (impl declared without
# `nothrow` syntactically, but body proves nothrow) is intentionally
# accepted by the checker -- a stylistic concern, not a soundness
# bug, and codegen consults the EFFECTIVE bit on `FnInfo` which
# normalizes correctly.  V9 covers the actual unsafe case only.

_V9_NOTHROW_IFACE_THROWING_IMPL = """\
module v9_unsound;

import std.core as core;
import std.core.arc as arc;

pub error MyError { tag: Int }

pub interface Greeter { fn greet(self: &Self) nothrow -> Int }
struct G { value: Int }
implement Greeter for G {
\tpub fn greet(self: &G) -> Int {
\t\tif self.value < 0 { throw MyError(tag = 1); }
\t\treturn self.value;
\t}
}

pub fn main() nothrow -> Int {
\tval g: arc.Arc<G> = arc.arc(G(value = 42));
\tval gw: arc.Arc<Greeter> = g.as_interface<type Greeter>();
\treturn gw.get().greet();
}
"""


def test_v9_nothrow_iface_with_throwing_impl_rejected_at_compile(tmp_path: Path) -> None:
	"""An impl whose body actually throws cannot satisfy a nothrow
	interface contract.  Checker must reject at compile time;
	specifically expects diagnostic
	`function ... is declared nothrow but may throw` from
	`E-AUTO-3b328370`.

	If the checker ever stops rejecting this, the codegen
	contract-failure assertion in `_emit_iface_method_thunk`
	(the `not iface_can_throw and impl_can_throw` branch) would
	fire as a backstop -- but the diagnostic is the
	primary defense, and the codegen assertion would surface as
	a confusing AssertionError instead of a user-facing error."""
	cc_rc, cc_err, run_err, run_rc = _compile_and_run(
		tmp_path, "v9_unsound", _V9_NOTHROW_IFACE_THROWING_IMPL,
	)
	assert cc_rc != 0, (
		f"V9 compile UNEXPECTEDLY SUCCEEDED -- the checker is no "
		f"longer rejecting impls that may throw against a nothrow "
		f"interface contract.  This is a soundness regression: a "
		f"throwing impl behind a nothrow interface declaration "
		f"means dispatch sites won't catch the exception (no try "
		f"around `gw.get().greet()` because the contract says it "
		f"can't throw) and the program will abort on the first "
		f"thrown error.  Restore the nothrow-violation check in "
		f"the checker."
	)
	assert "is declared nothrow but may throw" in cc_err or "E-AUTO-3b328370" in cc_err, (
		f"V9 compile failed as expected, but with an unexpected "
		f"diagnostic shape.  Looking for "
		f"'is declared nothrow but may throw' or E-AUTO-3b328370.\n"
		f"Got:\n{cc_err[-1500:]}"
	)
