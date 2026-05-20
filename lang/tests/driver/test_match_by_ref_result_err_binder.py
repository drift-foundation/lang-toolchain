# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Positive-pin regression for app-team `compiler-findings.md` #3:
`match &core.Result<T, E> { Result::Err(e) => ... }` -- payload
binder `e` must be bound + usable inside the arm.

**Status (2026-05-17):** the bug does NOT reproduce on current
toolchain (0.31.100, post the #2 fix slice).  We could not
construct any failing case matching the app-team's report shape
across single-package, cross-package, and bit-identical
replicas of their example.  Two leading hypotheses:

  1. **Incidentally fixed by #2's slice (most likely).**  The
     typed-catch-binder + cross-package field-projection work
     in 0.31.100 touched the cross-package nominal-projection
     path that match-arm binder field reads also flow through.
     If the bug shape was "cross-package variant binder fails
     field projection", #2's fix would have addressed both.
  2. **Underspecified report.**  The app-team example in the
     report has the binder `e` bound but unused (body is just
     `return "err";`) -- which wouldn't trigger
     `unknown name 'e'`.  Their actual working code in
     `gateway.drift` uses the workaround shape
     `match &x { Some(_) => ... }` (discarding the binder),
     suggesting the original failing site referenced `e` in
     some way the example didn't capture.

This file pins what we tested.  All carriers MUST compile +
run cleanly post-fix.  If any regress, we know the cross-
package binder path was destabilized by a subsequent change
and the original bug shape resurfaced.

If the bug reappears, the app team should send a 100%-
reproducible repro of the exact shape and we'll reopen with
a precise failing-case carrier; today's file would then
become the "what's already working" backstop.

Carriers (all single-file end-to-end + run; cross-package
where structurally relevant, mirroring the app team's
`&core.Result<Int, mariadb.rpc.managed.ManagedError>` shape):

  V1. Same-package `match &Result<Int, MyError>` with Err
      binder + field projection.
  V2. Same shape with NO binder reference (`Err(_)`) --
      app-team's actual workaround pattern.
  V3. Same-package custom variant with one-arity payload
      and multiple-arity payload, by-ref match, binders
      used.
  V4. Cross-package `match &core.Result<Int, pkg.E>` with
      Err binder + field projection -- bit-identical to
      app team's report shape.
  V5. Cross-package by-ref match with binder passed by
      borrow to a fn expecting `&Type` (mirrors how the
      app would chain through `_dup` / `_take_tag` style
      helpers).

Reference: original report
`/home/sl/src/pushcoin/work/singular-gateway/compiler-findings.md`
section #3.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(
	tmp_path: Path,
	module_name: str,
	source: str,
	extra_args: list[str] | None = None,
) -> tuple[int, str, int, str]:
	"""Compile + execute.  Returns (cc_rc, cc_err, run_rc, run_stderr)."""
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
	cc = subprocess.run(
		cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60,
	)
	if cc.returncode != 0 or not out_bin.exists():
		return cc.returncode, cc.stderr, -1, ""
	run = subprocess.run(
		[str(out_bin)], capture_output=True, text=True, timeout=10,
	)
	return cc.returncode, cc.stderr, run.returncode, run.stderr


# ─── V1: same-package Result Err binder + field projection ─────────

_V1_SAME_PKG_RESULT_ERR_BINDER_USED = """\
module v1;

import std.core as core;

pub error MyError { code: Int, tag: String }

fn maybe_fail(should_fail: Bool) nothrow -> core.Result<Int, MyError> {
	if should_fail {
		return core.Result<Int, MyError>::Err(MyError(code = 7, tag = "boom"));
	}
	return core.Result<Int, MyError>::Ok(42);
}

fn classify(r: &core.Result<Int, MyError>) nothrow -> Int {
	match r {
		core.Result::Err(e) => {
			return e.code;
		},
		core.Result::Ok(_) => {
			return 0;
		}
	}
}

pub fn main() nothrow -> Int {
	val r = maybe_fail(true);
	val c = classify(&r);
	if c == 7 { return 0; }
	return 1;
}
"""


def test_v1_same_pkg_result_err_binder_field_projection(tmp_path: Path) -> None:
	"""Same-package `match &core.Result<Int, MyError>` with
	`Err(e) => e.code`.  Pre-#2-fix shape might have failed
	(report unclear); post-#2-fix passes cleanly."""
	cc_rc, cc_err, run_rc, _ = _compile_and_run(tmp_path, "v1", _V1_SAME_PKG_RESULT_ERR_BINDER_USED)
	assert cc_rc == 0, f"V1 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 0, f"V1 run returned {run_rc}, expected 0"


# ─── V2: same shape with binder discarded (app team's workaround) ──

_V2_RESULT_ERR_BINDER_DISCARDED = """\
module v2;

import std.core as core;

pub error MyError { code: Int }

fn maybe_fail(should_fail: Bool) nothrow -> core.Result<Int, MyError> {
	if should_fail {
		return core.Result<Int, MyError>::Err(MyError(code = 7));
	}
	return core.Result<Int, MyError>::Ok(42);
}

fn classify(r: &core.Result<Int, MyError>) nothrow -> Int {
	match r {
		core.Result::Err(_) => { return 99; },
		core.Result::Ok(_) => { return 0; }
	}
}

pub fn main() nothrow -> Int {
	val r = maybe_fail(true);
	return classify(&r);
}
"""


def test_v2_result_err_binder_discarded(tmp_path: Path) -> None:
	"""Same as V1 but with `Err(_)` -- the app team's
	actual workaround pattern.  Always worked; pinned so we
	don't break the workaround path."""
	cc_rc, cc_err, run_rc, _ = _compile_and_run(tmp_path, "v2", _V2_RESULT_ERR_BINDER_DISCARDED)
	assert cc_rc == 0, f"V2 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 99, f"V2 run returned {run_rc}, expected 99"


# ─── V3: custom variant by-ref, single + multi-arity binders ───────

_V3_CUSTOM_VARIANT_BY_REF = """\
module v3;

import std.core as core;

pub variant V {
	Empty,
	One(x: Int),
	Two(x: Int, y: Int)
}

fn use_v(v: &V) nothrow -> Int {
	match v {
		V::Empty => { return 0; },
		V::One(x) => { return x; },
		V::Two(x, y) => { return x + y; }
	}
}

pub fn main() nothrow -> Int {
	val v1 = V::One(x = 5);
	val v2 = V::Two(x = 3, y = 4);
	val v3 = V::Empty();
	val r = use_v(&v1) + use_v(&v2) + use_v(&v3);
	if r == 12 { return 0; }
	return 1;
}
"""


def test_v3_custom_variant_by_ref_multi_arity(tmp_path: Path) -> None:
	"""Custom variant by-ref match with one-arity and two-arity
	binders.  All binders used inside arms.  Pins that the
	HIR->MIR by-ref binder lowering handles multi-arity
	payloads correctly."""
	cc_rc, cc_err, run_rc, _ = _compile_and_run(tmp_path, "v3", _V3_CUSTOM_VARIANT_BY_REF)
	assert cc_rc == 0, f"V3 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 0, f"V3 run returned {run_rc}, expected 0"


# ─── V4 + V5 cross-package variants -- use pkg fixture ──────────────


def _build_errpkg(tmp_path: Path) -> tuple[Path, Path]:
	"""Same shape as the typed-catch-binder #2 regression test:
	build + sign an errpkg producer with `pub error MyError`
	and a `Result`-returning function.  Returns (pkg_root,
	trust_path)."""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.drift.crypto import compute_ed25519_kid

	lib_dir = tmp_path / "errpkg_src"
	lib_dir.mkdir()
	(lib_dir / "inner.drift").write_text("""\
module errpkg.inner;
import std.core as core;
export { MyError, open };

pub error MyError {
	code: Int,
	tag: String
}

pub fn open(host: String) nothrow -> core.Result<Int, MyError> {
	if host.byte_length() == 0 {
		return core.Result<Int, MyError>::Err(
			MyError(code = 7, tag = "open-failed")
		);
	}
	return core.Result<Int, MyError>::Ok(42);
}
""")
	(lib_dir / "lib.drift").write_text("""\
module errpkg;
import std.core as core;
import errpkg.inner as inner;
export { open };

pub fn open(host: String) nothrow -> core.Result<Int, inner.MyError> {
	return inner.open(move host);
}
""")

	pkg_root = tmp_path / "pkg_root" / "errpkg" / "0.1.0"
	pkg_root.mkdir(parents=True)
	dmp = pkg_root / "errpkg.dmp"
	res = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc",
			"--dev", "-M", str(lib_dir), "--stdlib-root", str(ROOT / "stdlib"),
			str(lib_dir / "inner.drift"), str(lib_dir / "lib.drift"),
			"--package-id", "errpkg", "--package-version", "0.1.0",
			"--package-target", "drift-dev",
			"--emit-package", str(dmp), "--test-build-only",
		],
		cwd=str(ROOT), capture_output=True, text=True, timeout=60,
	)
	assert res.returncode == 0, f"producer build failed:\n{res.stderr[-1500:]}"

	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes_raw()
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = dmp.read_bytes()
	(dmp.with_suffix(".sig")).write_text(json.dumps({
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{hashlib.sha256(pkg_bytes).hexdigest()}",
		"signatures": [{
			"algo": "ed25519", "kid": kid,
			"sig": base64.b64encode(priv.sign(pkg_bytes)).decode("ascii"),
			"pubkey": pub_b64,
		}],
	}, separators=(",", ":"), sort_keys=True))

	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"errpkg.*": [kid], "std.*": [kid]},
		"revoked": [],
	}, separators=(",", ":"), sort_keys=True))

	return tmp_path / "pkg_root", trust_path


_V4_CROSS_PKG_RESULT_ERR_FIELD = """\
module consumer;

import std.core as core;
import errpkg as errpkg;
import errpkg.inner as inner;

fn classify(r: &core.Result<Int, inner.MyError>) nothrow -> Int {
	match r {
		core.Result::Err(e) => { return e.code; },
		core.Result::Ok(_) => { return 0; }
	}
}

pub fn main() nothrow -> Int {
	val r = errpkg.open("");
	val c = classify(&r);
	if c == 7 { return 0; }
	return 1;
}
"""


def test_v4_cross_pkg_result_err_binder_field_projection(tmp_path: Path) -> None:
	"""Bit-identical to the app team's report shape:
	`match &core.Result<Int, pkg.MyError> { Err(e) => e.code }`.
	Cross-package; binder used for field projection."""
	pkg_root, trust_path = _build_errpkg(tmp_path)
	src_dir = tmp_path / "consumer_src"
	src_dir.mkdir()
	src = src_dir / "app.drift"
	src.write_text(_V4_CROSS_PKG_RESULT_ERR_FIELD)
	out_bin = tmp_path / "consumer_bin"
	cc = subprocess.run(
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
	assert cc.returncode == 0, (
		f"V4 cross-pkg compile failed:\n{cc.stderr[-1500:]}"
	)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 0, (
		f"V4 cross-pkg run returned {run.returncode}, expected 0"
	)


_V5_CROSS_PKG_BINDER_PASSED_BY_BORROW = """\
module consumer;

import std.core as core;
import errpkg as errpkg;
import errpkg.inner as inner;

fn _take_err(e: &inner.MyError) nothrow -> Int { return e.code; }

fn classify(r: &core.Result<Int, inner.MyError>) nothrow -> Int {
	match r {
		core.Result::Err(e) => {
			return _take_err(&e);
		},
		core.Result::Ok(_) => { return 0; }
	}
}

pub fn main() nothrow -> Int {
	val r = errpkg.open("");
	val c = classify(&r);
	if c == 7 { return 0; }
	return 1;
}
"""


def test_v5_cross_pkg_binder_passed_by_borrow_to_fn(tmp_path: Path) -> None:
	"""Cross-package, by-ref match, binder `e` borrowed and
	passed to a fn expecting `&inner.MyError`.  Mirrors how
	app code chains through `_dup(&e.field)` / `_take_tag(&e)`
	style helpers.  Combines #2's borrow-projection fix and
	#3's binder-bind path."""
	pkg_root, trust_path = _build_errpkg(tmp_path)
	src_dir = tmp_path / "consumer_src"
	src_dir.mkdir()
	src = src_dir / "app.drift"
	src.write_text(_V5_CROSS_PKG_BINDER_PASSED_BY_BORROW)
	out_bin = tmp_path / "consumer_bin"
	cc = subprocess.run(
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
	assert cc.returncode == 0, (
		f"V5 cross-pkg compile failed:\n{cc.stderr[-1500:]}"
	)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 0, (
		f"V5 cross-pkg run returned {run.returncode}, expected 0"
	)


# ─── V6: borrow OF FIELD on payload binder ──────────────────────────
#
# Added 2026-05-17 per app-team confirmation note on #3.  My
# original V1-V5 missed the specific shape that combines #2's
# borrow-projection path with #3's binder-bind path: borrowing a
# field of the binder (`&e.field`) inside a by-ref match arm.
# App-team verified this shape compiles cleanly on 0.31.100;
# pinned here so a future change to either #2's typed-projection
# storage or #3's binder-bind path won't silently break the
# combined case.


_V6_BORROW_FIELD_ON_PAYLOAD_BINDER = """\
module v6;

import std.core as core;

pub error MyError { code: Int, tag: String }

fn maybe_fail(should_fail: Bool) nothrow -> core.Result<Int, MyError> {
	if should_fail {
		return core.Result<Int, MyError>::Err(MyError(code = 7, tag = "boom"));
	}
	return core.Result<Int, MyError>::Ok(42);
}

fn _take(s: &String) nothrow -> Int { return s.byte_length(); }

fn classify(r: &core.Result<Int, MyError>) nothrow -> Int {
	match r {
		core.Result::Err(e) => {
			return _take(&e.tag);
		},
		core.Result::Ok(_) => { return 0; }
	}
}

pub fn main() nothrow -> Int {
	val r = maybe_fail(true);
	val n = classify(&r);
	if n == 4 { return 0; }
	return 1;
}
"""


def test_v6_borrow_field_on_match_payload_binder(tmp_path: Path) -> None:
	"""Combines #2 (borrow-projection on a binder typed via Path-A
	struct) and #3 (by-ref match arm binder bind path).  Both
	live on the cross-package nominal-projection axis; if either
	regresses, this carrier would surface it earlier than either
	dedicated test.

	App-team note: this shape wasn't in V1-V5; they verified it
	compiles cleanly post-0.31.100 and asked we pin it."""
	cc_rc, cc_err, run_rc, _ = _compile_and_run(tmp_path, "v6", _V6_BORROW_FIELD_ON_PAYLOAD_BINDER)
	assert cc_rc == 0, f"V6 compile failed:\n{cc_err[-1500:]}"
	assert run_rc == 0, f"V6 run returned {run_rc}, expected 0"
