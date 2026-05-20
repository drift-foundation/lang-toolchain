# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: borrowing a field of a typed catch binder
(`&e.field` where `e` is the binder from `catch <pub_error>(e)`)
must compile cleanly, both same-package and cross-package.

**Background.**  Filed as app-team `compiler-findings.md` #2,
2026-05-17.  Report initially attributed the failure to
cross-package boundary because their working code happened to
use by-value reads same-package and borrow reads cross-package
-- but the real axis is **by-value vs borrow**, not package
boundary.  Both same-package and cross-package fail identically
pre-fix (verified locally 2026-05-17).

Pre-fix shape:

  catch <Err>(e) {
    val t: String = e.tag;          // OK (HField fast-path)
    val u: String = _take(&e.tag);  // FAILED: E-AUTO-69eb9f81
  }

**Root cause** (pre-fix):

The type-checker had TWO field-projection paths on catch
binders:

  * HField direct visit (`type_checker.py:~9020-9100`): handled
    typed-catch-binder projection.  Annotated expression with
    `typed_proj_event_fqn` for HIR->MIR lowering, which routed
    via `_lower_typed_catch_field_proj` (per-access JSON
    decode helper).

  * HPlaceExpr projection-chain walker (`~9141-9180`): had
    special cases for `params` and `context` projections on
    Error-kind types but NO general typed-catch-binder
    field-projection case.  `&e.tag` parses as
    `HBorrow(HPlaceExpr(base=HVar(e), projections=[HPlaceField(tag)]))`
    and walks this path; sees `td.kind == TypeKind.ERROR`,
    falls through to `td.kind is not TypeKind.STRUCT` check,
    emits `field access requires a struct value`.

**Fix** (landed in 0.31.100 / option-(b) materialized model):

Two-part fix per K-review direction (option b, materialize a
parallel native struct local):

  1. Type-checker (`type_checker.py:~9170+`): add typed-catch-binder
     recognition to the HPlaceExpr walker.  When base is a typed
     catch binder and projection is a declared scalar field,
     annotate the HPlaceExpr with `typed_proj_event_fqn` and
     `typed_proj_field_name` (parallel to the HField fast-path's
     annotation).  Non-scalar projections rejected with
     E_TYPED_CATCH_FIELD_UNSUPPORTED_TYPE (same as HField path).

  2. HIR->MIR (`hir_to_mir.py`): new lazy materialization helper
     `_get_or_materialize_typed_catch_storage` that allocates a
     parallel struct local of the `pub error`'s Path-A
     co-registered struct type on first projection, decodes ALL
     declared scalar fields from the envelope JSON once
     (`_typed_params_field_<scalar>` helpers, same as legacy
     per-access path), constructs the struct, and registers it
     for normal scope cleanup at catch-arm exit.  Memoized per
     binding_id via `self._typed_catch_binder_storage`.  HBorrow
     path in `_lower_addr_of_place` (~line 10973+) routes
     through the storage local with `AddrOfField` instead of
     asserting "field place base is not a struct".

**Design choice (b) rationale** (K, 2026-05-17): "A typed catch
binder represents an error event value with fields.  If `e.tag`
works, then `&e.tag` should be a borrow of that field on an
addressable typed value, not a borrow of a temporary copy."
Avoids hidden semantic drift; scales to future mut/ref
semantics; matches user expectation that `catch E(e)` behaves
like a typed value `e: E` inside the arm.

The HField direct-read path remains on the legacy per-access
JSON-decode helper for now; future unification onto the
storage model is a follow-up.  Storage cleanup: independent of
the envelope's params JSON drop; both own separate +1s on
String fields and drop independently.  No double-drop.

**App-team note:** "We're not applying the by-value workaround
in `gateway.drift`.  Keeping the broken `_dup(&e.tag)` line as
a tracer: when the fix lands, that line will compile again."
With this slice landed, gateway integration unblocks.

**Adjacent §B** (`pub type X = inner.Y` re-export of a
`pub error` drops the field schema, surfacing as
`E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA` even on by-value reads) is
a separate bug; not covered here.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _compile_via_subprocess(
	tmp_path: Path,
	module_name: str,
	source: str,
	extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
	"""Compile via subprocess and capture stderr.  No execute --
	this bug fires at type-check, so we only need compile output."""
	src_path = tmp_path / f"{module_name}.drift"
	src_path.write_text(source)
	out_bin = tmp_path / f"{module_name}_bin"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--stdlib-root", str(ROOT / "stdlib"),
		"--entry", f"{module_name}::main",
		str(src_path),
		"-o", str(out_bin),
	]
	if extra_args:
		cmd.extend(extra_args)
	return subprocess.run(
		cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60,
	)


# ─── V1: positive control -- by-value read (works pre-fix, must keep working) ──

_V1_BY_VALUE = """\
module v1_byvalue;

import std.core as core;

pub error LocalError {
	tag: String,
	message: String
}

fn do_throw() -> Void {
	throw LocalError(tag = "boom", message = "stuff");
}

fn run() -> Int {
	try {
		do_throw();
		return 1;
	} catch LocalError(e) {
		val t: String = e.tag;
		val m: String = e.message;
		if t.byte_length() > 0 and m.byte_length() > 0 { return 0; }
		return 2;
	}
}

pub fn main() nothrow -> Int {
	try { return run(); } catch any { return 99; }
}
"""


def test_v1_byvalue_field_read_compiles(tmp_path: Path) -> None:
	"""Positive control: `e.tag` by-value read on a typed catch
	binder must compile cleanly (HField fast-path at
	`type_checker.py:~9020-9100`).  If this regresses, the fix
	broke the working path."""
	res = _compile_via_subprocess(tmp_path, "v1_byvalue", _V1_BY_VALUE)
	assert res.returncode == 0, (
		f"V1 compile failed -- by-value field read on catch binder "
		f"regressed:\n{res.stderr[-1500:]}"
	)


# ─── V2: same-package borrow -- THE BUG (fails pre-fix) ────────────

_V2_SAMEPKG_BORROW = """\
module v2_samepkg;

import std.core as core;

pub error LocalError {
	tag: String,
	message: String
}

fn _take(s: &String) nothrow -> String { return *s; }

fn do_throw() -> Void {
	throw LocalError(tag = "boom", message = "stuff");
}

fn run() -> Int {
	try {
		do_throw();
		return 1;
	} catch LocalError(e) {
		val t: String = _take(&e.tag);
		val m: String = _take(&e.message);
		if t.byte_length() > 0 and m.byte_length() > 0 { return 0; }
		return 2;
	}
}

pub fn main() nothrow -> Int {
	try { return run(); } catch any { return 99; }
}
"""


def test_v2_samepkg_field_borrow_compiles(tmp_path: Path) -> None:
	"""THE BUG (same-package borrow): `&e.tag` on a typed catch
	binder MUST compile cleanly post-fix.  Pre-fix fails with:

	    error: field access requires a struct value [E-AUTO-69eb9f81]
	    error: no matching overload for function '_take' with args [<typeid>]

	at the `&e.tag` site, because the HPlaceExpr walker at
	`type_checker.py:~9141-9180` lacks the typed-catch-binder
	special case that exists in the HField visit path.

	Critical for understanding scope: app-team report
	attributed this to cross-package boundary, but it actually
	fails same-package too (verified locally 2026-05-17).  The
	axis is by-value vs borrow, not package boundary.

	V3 below mirrors the actual app-team cross-package shape
	for full coverage."""
	res = _compile_via_subprocess(tmp_path, "v2_samepkg", _V2_SAMEPKG_BORROW)
	assert "E-AUTO-69eb9f81" not in res.stderr, (
		f"V2: `&e.tag` on a typed catch binder still emits "
		f"E-AUTO-69eb9f81 (`field access requires a struct value`).  "
		f"The HPlaceExpr projection-chain walker at "
		f"`type_checker.py:~9141-9180` is missing the "
		f"typed-catch-binder special case that exists in the "
		f"HField direct-visit path (~lines 9020-9100).  See "
		f"this test file's docstring for the design questions "
		f"around the fix shape (storage model for the borrow).\n\n"
		f"stderr:\n{res.stderr[-1500:]}"
	)
	assert res.returncode == 0, (
		f"V2 compile failed but NOT with the known "
		f"E-AUTO-69eb9f81 shape -- something else is wrong:\n"
		f"{res.stderr[-1500:]}"
	)


# ─── V3: cross-package borrow (app-team's exact shape) ─────────────
#
# Reproduces /tmp/sgw-repro2/consumer/src/app.drift verbatim
# (modulo module names) -- their reply to compiler-findings #2 with
# fresh mariadb-rpc rebuild on 0.31.99.  Same diagnostic shape as
# V2 above; same root cause; included for cross-pkg coverage in
# case the eventual fix touches package metadata propagation and a
# regression on either axis would be material.
#
# Uses two-package setup via subprocess: producer first
# (errpkg with `pub error ManagedError`), then consumer
# (catches `errpkg.inner:ManagedError(e)`, borrows `&e.tag`).


def _build_errpkg(tmp_path: Path) -> tuple[Path, Path]:
	"""Build + sign a producer package mirroring app team's errpkg.
	Returns (pkg_root, trust_path).

	v1 fixture: uses the shared `publish_v1_pkg` helper which
	stamps SCI into the manifest and emits both author + cert
	claim sidecars alongside the .dmp, plus a v1 role-tagged
	trust store -- one semantic publisher instead of the inline
	dmir-pkg-sig + v0 trust pattern.
	"""
	from lang.tests.driver.pkg_test_helpers import publish_v1_pkg

	lib_dir = tmp_path / "errpkg_src"
	lib_dir.mkdir()
	(lib_dir / "inner.drift").write_text("""\
module errpkg.inner;
import std.core as core;
export { ManagedError, open };

pub error ManagedError {
	tag: String,
	message: String
}

pub fn open(host: String) nothrow -> core.Result<Int, ManagedError> {
	if host.byte_length() == 0 {
		return core.Result<Int, ManagedError>::Err(
			ManagedError(tag = "open-failed", message = "empty host")
		);
	}
	return core.Result<Int, ManagedError>::Ok(42);
}
""")
	(lib_dir / "lib.drift").write_text("""\
module errpkg;
import std.core as core;
import errpkg.inner as inner;
export { open };

pub fn open(host: String) nothrow -> core.Result<Int, inner.ManagedError> {
	return inner.open(move host);
}
""")

	pkg_root = tmp_path / "pkg_root"
	trust_path = tmp_path / "trust.json"
	# `std.*` is added to the same trust file alongside `errpkg.*`
	# because the consumer compile loads stdlib too.  publish_v1_pkg
	# writes the errpkg entry, then we merge an std.* entry in for
	# the same kid (Foundation-bootstrap pattern shared by the test
	# suite).
	pub_info = publish_v1_pkg(
		lib_dir=lib_dir,
		src_files=[lib_dir / "inner.drift", lib_dir / "lib.drift"],
		package_id="errpkg",
		package_version="0.1.0",
		namespace_glob="errpkg.*",
		dest_pkg_root=pkg_root,
		dest_trust_path=trust_path,
		stdlib_root_override=ROOT / "stdlib",
	)
	# Merge stdlib namespace coverage in (this test compiles
	# consumer code that imports std.core).
	import json as _json
	trust = _json.loads(trust_path.read_text())
	trust["namespaces"]["std.*"] = {
		"authors": [pub_info["kid"]], "certifiers": [pub_info["kid"]],
	}
	trust_path.write_text(_json.dumps(trust, separators=(",", ":"), sort_keys=True))

	return pkg_root, trust_path


_V3_CROSSPKG_BORROW = """\
module consumer;

import std.core as core;
import errpkg as errpkg;
import errpkg.inner as inner;

fn _take_tag(s: &String) nothrow -> String { return *s; }

pub fn main() nothrow -> Int {
	try {
		val v: Int = errpkg.open("").or_throw();
		return v;
	} catch inner:ManagedError(e) {
		val t_value: String = e.tag;
		val t_borrow: String = _take_tag(&e.tag);
		if t_value.byte_length() > 0 and t_borrow.byte_length() > 0 { return 0; }
		return 3;
	}
}
"""


def test_v3_crosspkg_field_borrow_compiles(tmp_path: Path) -> None:
	"""Cross-package mirror of V2 -- bit-identical to the
	app-team's `/tmp/sgw-repro2/consumer/src/app.drift` from the
	2026-05-17 reply.  Catches `errpkg.inner:ManagedError(e)` and
	borrows `&e.tag` via a function expecting `&String`.

	Same root cause as V2 (HPlaceExpr walker missing the
	typed-catch-binder special case).  Kept as a separate carrier
	so a fix that incidentally fixes same-package but misses
	cross-package metadata still surfaces here."""
	pkg_root, trust_path = _build_errpkg(tmp_path)

	src_dir = tmp_path / "consumer_src"
	src_dir.mkdir()
	src = src_dir / "app.drift"
	src.write_text(_V3_CROSSPKG_BORROW)
	out_bin = tmp_path / "consumer_bin"
	res = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc",
			"--target-word-bits", "64",
			"--stdlib-root", str(ROOT / "stdlib"),
			"--package-root", str(pkg_root),
			"--dep", "errpkg@0.1.0",
			"--trust-store", str(trust_path),
			"--entry", "consumer::main",
			str(src),
			"-o", str(out_bin),
		],
		cwd=str(ROOT), capture_output=True, text=True, timeout=60,
	)

	assert "E-AUTO-69eb9f81" not in res.stderr, (
		f"V3: cross-package `&e.tag` on typed catch binder still "
		f"emits E-AUTO-69eb9f81.  Same shape as V2; root cause is "
		f"the HPlaceExpr walker's missing typed-catch-binder case "
		f"at `type_checker.py:~9141-9180`.\n\n"
		f"stderr:\n{res.stderr[-1500:]}"
	)
	assert res.returncode == 0, (
		f"V3 compile failed but NOT with the known shape:\n"
		f"{res.stderr[-1500:]}"
	)


# ─── V4: repeated borrow + two-field borrow ────────────────────────
#
# Per K-review 2026-05-17: ensure the storage materialization
# is reused across multiple borrows on the same binder, and that
# different fields project independently.  If the materialization
# is per-projection (not lazily memoized), we'd see double-decode
# or stale storage; if the field index lookup is wrong, the
# second-field borrow would surface a layout/index issue here
# rather than at runtime corruption.

_V4_REPEATED_AND_TWO_FIELD = """\
module v4_repeated;

import std.core as core;

pub error LocalError {
	tag: String,
	message: String
}

fn _take(s: &String) nothrow -> String { return *s; }

fn do_throw() -> Void {
	throw LocalError(tag = "boom", message = "stuff");
}

fn run() -> Int {
	try {
		do_throw();
		return 1;
	} catch LocalError(e) {
		// Repeated borrow of the same field -- storage must be
		// reused (single materialization), not re-decoded.
		val a: String = _take(&e.tag);
		val b: String = _take(&e.tag);
		// Borrow of a second field -- different field index on the
		// same storage struct.
		val c: String = _take(&e.message);
		if a.byte_length() > 0
				and b.byte_length() > 0
				and c.byte_length() > 0
				and a.byte_length() == b.byte_length() {
			return 0;
		}
		return 2;
	}
}

pub fn main() nothrow -> Int {
	try { return run(); } catch any { return 99; }
}
"""


def test_v4_repeated_and_two_field_borrow(tmp_path: Path) -> None:
	"""Same binder, repeated `&e.tag` borrows + `&e.message`
	borrow of a second field.  Verifies the storage local is
	memoized per binding_id (single decode) and that field index
	lookup is correct for the second field.

	Compile + run + assert exit 0.  Pre-fix would fire
	`E-AUTO-69eb9f81` on the first `&e.tag` line.  Post-fix all
	three borrows succeed; runtime returns 0 (all three borrow
	values are non-empty and the two `&e.tag` borrows produce
	values of equal length, sanity-checking they came from the
	same storage)."""
	res = _compile_via_subprocess(tmp_path, "v4_repeated", _V4_REPEATED_AND_TWO_FIELD)
	assert res.returncode == 0, (
		f"V4 compile failed:\n{res.stderr[-1500:]}"
	)

	# Run the binary (V1-V3 only checked compile; V4 also runs).
	out_bin = tmp_path / "v4_repeated_bin"
	assert out_bin.exists(), "V4 binary not produced"
	run = subprocess.run(
		[str(out_bin)], capture_output=True, text=True, timeout=10,
	)
	assert run.returncode == 0, (
		f"V4 binary exited {run.returncode}, expected 0.  Three "
		f"borrows on the typed catch binder didn't all succeed -- "
		f"likely a storage-memoization bug (re-decoded into a fresh "
		f"struct on the second borrow) or a field-index lookup bug "
		f"(second field projected through wrong slot).\n"
		f"stderr: {run.stderr[-500:]}"
	)


# ─── V5: mixed-schema rejection ────────────────────────────────────
#
# K-review 2026-05-17 (HIGH): when the pub_error schema contains
# any non-scalar field (nested error, struct, variant, array,
# optional, etc.), the borrow path's materialization would have
# to either decode the non-scalar (no helper exists today) or
# zero-init it.  Zero-init + later scope-drop would invoke the
# field type's destructor on garbage memory -- silently UB.
#
# Fix: reject at type-check with E_TYPED_CATCH_BORROW_MIXED_SCHEMA.
# By-value reads through HField direct-visit are UNAFFECTED (they
# decode per-access and never build a sibling struct), so mixed
# schemas still work for value reads -- only the borrow path is
# gated.
#
# Long-term: implement non-scalar typed-payload materialization
# (decode helpers for nested error / struct / etc.); the
# diagnostic message points at this as the workaround path.

_V5_MIXED_SCHEMA_BORROW_REJECTED = """\
module v5_mixed;

import std.core as core;

pub error InnerError {
	code: Int
}

pub error Outer {
	tag: String,
	cause: InnerError
}

fn _take(s: &String) nothrow -> String { return *s; }

fn do_throw() -> Void {
	throw Outer(
		tag = "boom",
		cause = InnerError(code = 7)
	);
}

fn run() -> Int {
	try {
		do_throw();
		return 1;
	} catch Outer(e) {
		val t: String = _take(&e.tag);
		if t.byte_length() > 0 { return 0; }
		return 2;
	}
}

pub fn main() nothrow -> Int {
	try { return run(); } catch any { return 99; }
}
"""


def test_v5_mixed_schema_borrow_rejected_at_compile(tmp_path: Path) -> None:
	"""`&e.tag` on a typed catch binder whose schema contains a
	non-scalar field (here: `cause: InnerError`) must be rejected
	at compile-time with E_TYPED_CATCH_BORROW_MIXED_SCHEMA, NOT
	silently materialize a struct with zero-initialized
	non-scalar slots that later get dropped (UB).

	The user is only borrowing the SCALAR sibling `&e.tag`, but
	the materialization model would need to construct the full
	struct value -- including the non-scalar `cause` slot --
	and register it for scope drop.  Zero-init + drop of a
	non-scalar slot would invoke `InnerError`'s destructor on
	garbage memory.

	Post-fix expectation: compile fails with explicit diagnostic
	`E_TYPED_CATCH_BORROW_MIXED_SCHEMA` naming the offending
	non-scalar field.  By-value reads (`val t = e.tag;`) on the
	same schema continue to work through the HField direct-visit
	path -- not tested here, but covered by V1's pattern."""
	res = _compile_via_subprocess(tmp_path, "v5_mixed", _V5_MIXED_SCHEMA_BORROW_REJECTED)
	assert res.returncode != 0, (
		f"V5 compile UNEXPECTEDLY SUCCEEDED.  Borrowing `&e.tag` "
		f"on a typed catch binder whose schema has a non-scalar "
		f"field (`cause: InnerError`) must be rejected.  If "
		f"materialization silently zero-init'd the non-scalar "
		f"slot and registered the struct for drop, the destructor "
		f"would run on garbage memory at scope exit (UB).  See "
		f"`_get_or_materialize_typed_catch_storage` defense-in-"
		f"depth assertion."
	)
	assert "E_TYPED_CATCH_BORROW_MIXED_SCHEMA" in res.stderr, (
		f"V5 compile failed but NOT with the expected "
		f"E_TYPED_CATCH_BORROW_MIXED_SCHEMA diagnostic.  Something "
		f"else is wrong:\n{res.stderr[-1500:]}"
	)


# ─── V6: by-value reads on mixed schema still work ─────────────────
#
# Confirms the rejection in V5 is scoped to the BORROW path; the
# HField direct-read path is unaffected because it decodes
# per-access and never materializes a sibling struct.

_V6_MIXED_SCHEMA_BYVALUE_OK = """\
module v6_mixed_byvalue;

import std.core as core;

pub error InnerError {
	code: Int
}

pub error Outer {
	tag: String,
	cause: InnerError
}

fn do_throw() -> Void {
	throw Outer(
		tag = "boom",
		cause = InnerError(code = 7)
	);
}

fn run() -> Int {
	try {
		do_throw();
		return 1;
	} catch Outer(e) {
		val t: String = e.tag;
		if t.byte_length() > 0 { return 0; }
		return 2;
	}
}

pub fn main() nothrow -> Int {
	try { return run(); } catch any { return 99; }
}
"""


def test_v6_mixed_schema_byvalue_read_still_works(tmp_path: Path) -> None:
	"""By-value read `e.tag` on a typed catch binder with a mixed
	schema (scalar tag + non-scalar cause) must continue to work
	post-fix.  The HField direct-visit path decodes per-access
	via `_typed_params_field_<scalar>` helpers and never builds
	a sibling struct, so non-scalar schema fields don't trip
	the materialization invariant.

	Pin so the V5 rejection doesn't accidentally over-fire on
	the value-read path too."""
	res = _compile_via_subprocess(tmp_path, "v6_mixed_byvalue", _V6_MIXED_SCHEMA_BYVALUE_OK)
	assert res.returncode == 0, (
		f"V6 compile failed -- by-value read on mixed-schema pub_error "
		f"binder regressed:\n{res.stderr[-1500:]}"
	)
