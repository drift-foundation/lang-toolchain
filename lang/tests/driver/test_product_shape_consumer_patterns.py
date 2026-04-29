# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Standing product-shape consumer fleet — cert-relevant smoke.

These fixtures encode real downstream usage patterns that have repeatedly
surfaced compiler issues only after app teams hit them in integration.
The goal is to keep this suite the *first* place these patterns get
exercised, not the last.

Coverage axes:

  Patterns
    P1. Callback3 middleware shape: &Req, &mut Ctx, nested Callback2,
        Result return — the shape web-rest 0.4.0 `add_middleware`
        introduced and the bookkeeper app first hit.
    P2. Captures mix: `captures(move next, copy idx, share app_arc)` —
        the exact form web-rest's onion-fold middleware composer uses
        (see `web-rest/src/app.drift:455`).
    P3. Direct `core.Result::Ok(...)` / `core.Result::Err(...)` ctor
        construction inside inferred lambda bodies — the auto-try
        boundary case.
    P4. Explicit nothrow contract: `val x: core.Result<...> = ...`
        type-annotated bindings opt out of auto-try. The shape app
        teams reach for to keep a nothrow lambda nothrow.
    P5. Field / method access through inferred lambda params:
        `req.method.clone()`, `next.call(req, ctx)` — pin that
        downstream method dispatch resolves once the param's type is
        inferred from a CallbackN parameter.

  Modes
    M-A. Single-module source.
    M-B. Multi-module same-package source.
    M-C. Published-library `.dmp` package + separate consumer.
    M-D. Signed stdlib package-consumer (`--package-root` + `--dep
         std@VERSION`).
    M-E. `--emit-package` producer flow — build a library package and
         confirm it emits a valid signed `.dmp` consumers can load.

Not every cell is filled; we cover what's load-bearing without
over-stuffing.  Each test compiles a small, self-contained fixture
and asserts a clean compile (no error diagnostics).  Cert-relevant —
runs in the normal driver gate, not report-only.

History: created 2026-04-29 alongside the 0.31.30 fix that suppressed
the bookkeeper bare-lambda diagnostic cascade.  See also:
- `test_callback3_bare_lambda_param_inference.py` (P1-P7) — single-
  source breadth coverage including the cascade pin.
- `test_callback3_cross_package_bare_lambda.py` (X1-X3) — narrow
  cross-package bare-lambda regression set.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _b64(data: bytes) -> str:
	return base64.b64encode(data).decode("ascii")


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile_source(
	tmp_path: Path,
	capsys: pytest.CaptureFixture[str],
	files: dict[str, str],
) -> tuple[int, list[str]]:
	"""Compile one or more source files using `--stdlib-root` (source mode)."""
	src_dir = tmp_path / "src"
	src_dir.mkdir(parents=True, exist_ok=True)
	src_paths: list[str] = []
	for rel, content in files.items():
		p = src_dir / rel
		p.parent.mkdir(parents=True, exist_ok=True)
		p.write_text(content, encoding="utf-8")
		src_paths.append(str(p))
	argv = ["--stdlib-root", "stdlib", "--test-build-only", *src_paths]
	rc, payload = _run_driftc_json(argv, capsys)
	errs = [d.get("message", "") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


def _emit_signed_package(
	*,
	tmp_path: Path,
	package_id: str,
	package_version: str,
	namespace_glob: str,
	module_dir_name: str,
	files: dict[str, str],
	dest_pkg_root: Path,
	dest_trust_path: Path,
) -> Path:
	"""Build + sign a library package; place .dmp + .sig under
	`<dest_pkg_root>/<package_id>/<package_version>/`. Writes (or merges
	into) a trust file at `dest_trust_path`. Returns the dmp path."""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	lib_dir = tmp_path / module_dir_name
	lib_dir.mkdir(parents=True, exist_ok=True)
	src_files: list[str] = []
	for rel, content in files.items():
		p = lib_dir / rel
		p.parent.mkdir(parents=True, exist_ok=True)
		p.write_text(content, encoding="utf-8")
		src_files.append(str(p))

	pkg_path = lib_dir / f"{package_id}.dmp"
	rc = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc",
			"--dev",
			"-M", str(lib_dir),
			"--stdlib-root", str(stdlib_root()),
			*src_files,
			"--package-id", package_id,
			"--package-version", package_version,
			"--package-target", "drift-dev",
			"--emit-package", str(pkg_path),
			"--json",
		],
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	assert rc.returncode == 0, (
		f"--emit-package for {package_id}@{package_version} failed:\n{rc.stdout}\n---\n{rc.stderr[:1500]}"
	)
	assert pkg_path.exists(), f"{pkg_path} not produced"

	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes_raw()
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	sidecar = {
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{sha256(pkg_bytes).hexdigest()}",
		"signatures": [{"algo": "ed25519", "kid": kid, "sig": _b64(sig_raw), "pubkey": pub_b64}],
	}
	sig_path = pkg_path.with_suffix(".sig")
	sig_path.write_text(json.dumps(sidecar, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	# Merge into existing trust file if present, else write fresh.
	if dest_trust_path.exists():
		trust = json.loads(dest_trust_path.read_text())
	else:
		trust = {"format": "drift-trust", "version": 0, "keys": {}, "namespaces": {}, "revoked": []}
	trust["keys"][kid] = {"algo": "ed25519", "pubkey": pub_b64}
	trust["namespaces"].setdefault(namespace_glob, []).append(kid)
	dest_trust_path.write_text(json.dumps(trust, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	dest_dir = dest_pkg_root / package_id / package_version
	dest_dir.mkdir(parents=True, exist_ok=True)
	shutil.copy2(str(pkg_path), str(dest_dir / f"{package_id}.dmp"))
	shutil.copy2(str(sig_path), str(dest_dir / f"{package_id}.sig"))
	return pkg_path


def _compile_against_pkg(
	*,
	tmp_path: Path,
	pkg_root: Path,
	trust_path: Path,
	deps: list[str],
	source: str,
) -> tuple[int, list[str]]:
	"""Compile a single-file consumer against signed library package(s)."""
	tmp_path.mkdir(parents=True, exist_ok=True)
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = tmp_path / "a.out"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--package-root", str(pkg_root),
	]
	for d in deps:
		cmd.extend(["--dep", d])
	cmd.extend([
		"--trust-store", str(trust_path),
		str(src),
		"-o", str(out),
		"--json",
	])
	rc = subprocess.run(
		cmd, capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	if not rc.stdout.strip():
		return rc.returncode, [rc.stderr[:2000]]
	result = json.loads(rc.stdout)
	errs = [d["message"] for d in result.get("diagnostics", []) if d.get("severity") == "error"]
	return result.get("exit_code", rc.returncode), errs


def _assert_clean(rc: int, errs: list[str], scenario: str) -> None:
	assert rc == 0 and not errs, (
		f"{scenario} must compile clean; got rc={rc} diagnostics:\n  - "
		+ "\n  - ".join(errs)
	)


# ────────────────────────────────────────────────────────────────────
# Section A — Single-module source (Mode M-A)
# ────────────────────────────────────────────────────────────────────


# Comprehensive single-module fixture covering P1 (Callback3 middleware),
# P5 (field/method access through inferred params), and P3 (Result ctor
# inside lambda).  The lambda explicitly opts out of auto-try via
# annotated bindings (P4) so the body stays nothrow as the Callback3
# contract demands.
_SOURCE_MW_FULL = """
module main;

import std.core as core;

struct Req { pub method: String, pub path: String }
struct Ctx { pub idx: Int }
struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn ok_resp(status: Int) nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Ok(Resp(status = status));
}

fn err_resp(code: Int) nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Err(AppErr(code = code));
}

fn register_mw(slot: &mut Array<core.Callback3<&Req, &mut Ctx,
    core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>,
    core.Result<Resp, AppErr>>>,
    cb: core.Callback3<&Req, &mut Ctx,
    core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>,
    core.Result<Resp, AppErr>>) nothrow -> Void {
\tslot.push(move cb);
\treturn core.void_value();
}

fn main() nothrow -> Int {
\tvar slot: Array<core.Callback3<&Req, &mut Ctx,
\t    core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>,
\t    core.Result<Resp, AppErr>>> = [];
\tregister_mw(&mut slot, |req, ctx, next| => {
\t\t// P5: field + method access through inferred params.
\t\tval m: String = req.method.clone();
\t\tval p: String = req.path.clone();
\t\tval tag: String = m + " " + p;
\t\tctx.idx = ctx.idx + 1;
\t\t// P3 + P4: Result-typed binding opts out of auto-try.
\t\tval inner: core.Result<Resp, AppErr> = next.call(req, ctx);
\t\treturn move inner;
\t});
\treturn 0;
}
"""


def test_mw_callback3_source_single_module(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""P1 + P3 + P4 + P5 in M-A. The middleware lambda binds `inner`
	with an explicit `core.Result<Resp, AppErr>` type to opt out of
	auto-try, so the body stays nothrow as required by the Callback3
	contract."""
	rc, errs = _compile_source(tmp_path, capsys, {"main.drift": _SOURCE_MW_FULL})
	_assert_clean(rc, errs, "P1+P3+P4+P5 / M-A")


# Captures-mix shape from web-rest's onion-fold composer
# (web-rest/src/app.drift:455). Outer Callback2 captures
# `move next, copy mw_idx, share app_arc`.
_SOURCE_CAPTURES_MIX = """
module main;

import std.core as core;
import std.concurrent as conc;

struct App { pub name: String }
struct Req { pub n: Int }
struct Ctx { pub k: Int }
struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn run(handler: core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>) nothrow -> Int {
\treturn 0;
}

fn build_chain(app_arc: conc.Arc<App>, mw_idx: Int,
\t           next: core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>) nothrow -> Int {
\tval composed = core.callback2(|r: &Req, c: &mut Ctx| captures(move next, copy mw_idx, share app_arc) => {
\t\t// Inferred bindings would auto-try; explicit Result annotation opts out.
\t\tval inner: core.Result<Resp, AppErr> = next.call(r, c);
\t\tval _idx_seen: Int = mw_idx;
\t\tval _app_seen = app_arc.get();
\t\treturn move inner;
\t});
\treturn run(move composed);
}

fn main() nothrow -> Int {
\tval a = conc.arc<type App>(App(name = "test"));
\t// Typed binding gives `core.callback2(...)` the full Callback2
\t// instantiation so the lambda's `Result::Ok(Resp(...))` body has a
\t// concrete Err arm to infer against. Without the binding type, the
\t// Err type-arg of Result cannot be deduced from `Ok(...)` alone —
\t// a known design constraint of the current inferencer, not a bug.
\t// This keeps the fixture representative: app code that builds a
\t// callback in two stages has to spell the Callback type at the
\t// upper boundary anyway.
\tval base: core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>> =
\t    core.callback2(|r: &Req, c: &mut Ctx| nothrow => {
\t\treturn core.Result::Ok(Resp(status = r.n + c.k));
\t});
\treturn build_chain(move a, 0, move base);
}
"""


def test_captures_mix_source_single_module(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""P2 in M-A. Outer lambda captures `move next, copy mw_idx, share
	app_arc` — the exact mix web-rest's onion-fold composer uses."""
	rc, errs = _compile_source(tmp_path, capsys, {"main.drift": _SOURCE_CAPTURES_MIX})
	_assert_clean(rc, errs, "P2 / M-A")


# Direct Result::Ok / Result::Err inside an inferred lambda body. The
# lambda's expected type is a nothrow Callback2 that returns
# Result<Resp, AppErr>, so the Ok / Err ctors construct that Result
# directly with no auto-try involved.
_SOURCE_RESULT_CTOR_IN_LAMBDA = """
module main;

import std.core as core;

struct Req { pub n: Int }
struct Ctx { pub k: Int }
struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn register(slot: &mut Array<core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>>,
\t        cb: core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>) nothrow -> Void {
\tslot.push(move cb);
\treturn core.void_value();
}

fn main() nothrow -> Int {
\tvar slot: Array<core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>> = [];
\tregister(&mut slot, |req, ctx| => {
\t\tif req.n < 0 {
\t\t\treturn core.Result::Err(AppErr(code = 400));
\t\t}
\t\treturn core.Result::Ok(Resp(status = req.n + ctx.k));
\t});
\treturn 0;
}
"""


def test_result_ok_err_in_inferred_lambda_source(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""P3 in M-A. Bare lambda body constructs Result::Ok / Result::Err
	directly; the body's nothrow inference must accept ctor
	construction (variant ctors are not throws)."""
	rc, errs = _compile_source(tmp_path, capsys, {"main.drift": _SOURCE_RESULT_CTOR_IN_LAMBDA})
	_assert_clean(rc, errs, "P3 / M-A")


# Explicit-nothrow-contract pattern: the lambda body would auto-try
# without the Result annotation; with the annotation, no or_throw is
# injected, so the body stays nothrow as the Callback contract demands.
# This is the user-facing fix path for the bookkeeper / web-rest
# middleware report.
_SOURCE_EXPLICIT_NOTHROW = """
module main;

import std.core as core;

struct Req { pub n: Int }
struct Ctx { pub k: Int }
struct Resp { pub status: Int }
struct AppErr { pub code: Int }

// Nothrow producer.  Default function-throws-mode is throws, so a bare
// `-> core.Result<...>` here would make every caller's lambda may-throw.
// Pin nothrow at the source so this fixture exercises the auto-try
// opt-out idiom (annotated Result binding), not the throws contract.
fn maybe(n: Int) nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Ok(Resp(status = n));
}

fn run(handler: core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>) nothrow -> Int {
\treturn 0;
}

fn main() nothrow -> Int {
\t// Bare `val r = maybe(...);` would auto-try and inject or_throw.
\t// Explicit Result annotation opts out and keeps the lambda nothrow.
\tval h = core.callback2(|req: &Req, ctx: &mut Ctx| nothrow => {
\t\tval r: core.Result<Resp, AppErr> = maybe(req.n + ctx.k);
\t\treturn move r;
\t});
\treturn run(move h);
}
"""


def test_explicit_nothrow_contract_no_auto_try_source(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""P4 in M-A. Pin the user-facing fix path: explicit
	`val r: core.Result<...> = ...` annotation opts out of auto-try
	so a nothrow lambda body stays nothrow."""
	rc, errs = _compile_source(tmp_path, capsys, {"main.drift": _SOURCE_EXPLICIT_NOTHROW})
	_assert_clean(rc, errs, "P4 / M-A")


# ────────────────────────────────────────────────────────────────────
# Section B — Multi-module same-package source (Mode M-B)
# ────────────────────────────────────────────────────────────────────


# Multi-module same-package shape: types live in sibling modules of the
# same project, brought together at the call site. Mirrors the
# minimal-but-realistic web app split (request, context, response, etc.).
_MM_REQ = """
module myapp.req;

export { Req };

pub struct Req { pub method: String, pub path: String }
"""

_MM_CTX = """
module myapp.ctx;

export { Ctx };

pub struct Ctx { pub idx: Int }
"""

_MM_TYPES = """
module myapp.types;

export { Resp, AppErr };

pub struct Resp { pub status: Int }
pub struct AppErr { pub code: Int }
"""

_MM_API = """
module myapp.api;

import std.core as core;
import myapp.req as req;
import myapp.ctx as ctx;
import myapp.types as types;

export { register_mw, ok_resp };

pub fn ok_resp(s: Int) nothrow -> core.Result<types.Resp, types.AppErr> {
\treturn core.Result::Ok(types.Resp(status = s));
}

pub fn register_mw(slot: &mut Array<core.Callback3<&req.Req, &mut ctx.Ctx,
\t              core.Callback2<&req.Req, &mut ctx.Ctx, core.Result<types.Resp, types.AppErr>>,
\t              core.Result<types.Resp, types.AppErr>>>,
\t              cb: core.Callback3<&req.Req, &mut ctx.Ctx,
\t              core.Callback2<&req.Req, &mut ctx.Ctx, core.Result<types.Resp, types.AppErr>>,
\t              core.Result<types.Resp, types.AppErr>>) nothrow -> Void {
\tslot.push(move cb);
\treturn core.void_value();
}
"""

_MM_MAIN = """
module main;

import std.core as core;
import myapp.req as req;
import myapp.ctx as ctx;
import myapp.types as types;
import myapp.api as api;

fn main() nothrow -> Int {
\tvar slot: Array<core.Callback3<&req.Req, &mut ctx.Ctx,
\t    core.Callback2<&req.Req, &mut ctx.Ctx, core.Result<types.Resp, types.AppErr>>,
\t    core.Result<types.Resp, types.AppErr>>> = [];
\tapi.register_mw(&mut slot, |r, c, next| => {
\t\tval m: String = r.method.clone();
\t\tc.idx = c.idx + 1;
\t\tval inner: core.Result<types.Resp, types.AppErr> = next.call(r, c);
\t\treturn move inner;
\t});
\treturn 0;
}
"""


def test_mw_callback3_multi_module_source(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""P1 + P5 in M-B. Types live in `myapp.req` / `myapp.ctx` /
	`myapp.types`; the API + middleware registration live in
	`myapp.api`; the consumer is a separate `main` module. The
	middleware lambda's params must be inferred from the Callback3
	type-args sourced from sibling modules."""
	rc, errs = _compile_source(tmp_path, capsys, {
		"req.drift": _MM_REQ,
		"ctx.drift": _MM_CTX,
		"types.drift": _MM_TYPES,
		"api.drift": _MM_API,
		"main.drift": _MM_MAIN,
	})
	_assert_clean(rc, errs, "P1+P5 / M-B")


# ────────────────────────────────────────────────────────────────────
# Section C — Published-library package + separate consumer (Mode M-C)
# ────────────────────────────────────────────────────────────────────


# Library matches the multi-module same-package shape, then is
# published as a signed `.dmp`. The consumer compiles against the
# package, never seeing library source.
_LIB_PKG_REQ = """
module mwpkg.req;
export { Req };
pub struct Req { pub method: String, pub path: String }
"""

_LIB_PKG_CTX = """
module mwpkg.ctx;
export { Ctx };
pub struct Ctx { pub idx: Int }
"""

_LIB_PKG_TYPES = """
module mwpkg.types;
export { Resp, AppErr };
pub struct Resp { pub status: Int }
pub struct AppErr { pub code: Int }
"""

_LIB_PKG_API = """
module mwpkg;

import std.core as core;
import mwpkg.req as req;
import mwpkg.ctx as ctx;
import mwpkg.types as types;

export { Req, Ctx, Resp, AppErr, register_mw, ok_resp };

pub type Req = req.Req;
pub type Ctx = ctx.Ctx;
pub type Resp = types.Resp;
pub type AppErr = types.AppErr;

pub fn ok_resp(s: Int) nothrow -> core.Result<types.Resp, types.AppErr> {
\treturn core.Result::Ok(types.Resp(status = s));
}

pub fn register_mw(slot: &mut Array<core.Callback3<&req.Req, &mut ctx.Ctx,
\t              core.Callback2<&req.Req, &mut ctx.Ctx, core.Result<types.Resp, types.AppErr>>,
\t              core.Result<types.Resp, types.AppErr>>>,
\t              cb: core.Callback3<&req.Req, &mut ctx.Ctx,
\t              core.Callback2<&req.Req, &mut ctx.Ctx, core.Result<types.Resp, types.AppErr>>,
\t              core.Result<types.Resp, types.AppErr>>) nothrow -> Void {
\tslot.push(move cb);
\treturn core.void_value();
}
"""


@pytest.fixture(scope="module")
def _published_mwpkg(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	"""Build + sign mwpkg once for the module."""
	base = tmp_path_factory.mktemp("mwpkg_published")
	pkg_root = base / "pkg_root"
	trust_path = base / "trust.json"
	_emit_signed_package(
		tmp_path=base,
		package_id="mwpkg",
		package_version="1.0.0",
		namespace_glob="mwpkg.*",
		module_dir_name="lib",
		files={
			"req.drift": _LIB_PKG_REQ,
			"ctx.drift": _LIB_PKG_CTX,
			"types.drift": _LIB_PKG_TYPES,
			"mwpkg.drift": _LIB_PKG_API,
		},
		dest_pkg_root=pkg_root,
		dest_trust_path=trust_path,
	)
	return pkg_root, trust_path


_CONSUMER_MW_FROM_PKG = """
module main;

import std.core as core;
import mwpkg as lib;

fn main() nothrow -> Int {
\tvar slot: Array<core.Callback3<&lib.Req, &mut lib.Ctx,
\t    core.Callback2<&lib.Req, &mut lib.Ctx, core.Result<lib.Resp, lib.AppErr>>,
\t    core.Result<lib.Resp, lib.AppErr>>> = [];
\tlib.register_mw(&mut slot, |req, ctx, next| => {
\t\tval m: String = req.method.clone();
\t\tctx.idx = ctx.idx + 1;
\t\tval inner: core.Result<lib.Resp, lib.AppErr> = next.call(req, ctx);
\t\treturn move inner;
\t});
\treturn 0;
}
"""


def test_mw_callback3_published_library_consumer(
	_published_mwpkg: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""P1 + P5 in M-C. Library is a multi-module signed `.dmp`; consumer
	is a single source file. The consumer's bare lambda must get its
	params inferred from the library's exported Callback3 type."""
	pkg_root, trust_path = _published_mwpkg
	rc, errs = _compile_against_pkg(
		tmp_path=tmp_path, pkg_root=pkg_root, trust_path=trust_path,
		deps=["mwpkg@1.0.0"], source=_CONSUMER_MW_FROM_PKG,
	)
	_assert_clean(rc, errs, "P1+P5 / M-C")


_CONSUMER_CAPTURES_MIX_FROM_PKG = """
module main;

import std.core as core;
import std.concurrent as conc;
import mwpkg as lib;

struct AppState { pub name: String }

fn build(app_arc: conc.Arc<AppState>, mw_idx: Int,
\t      next: core.Callback2<&lib.Req, &mut lib.Ctx, core.Result<lib.Resp, lib.AppErr>>) nothrow ->
\t      core.Callback2<&lib.Req, &mut lib.Ctx, core.Result<lib.Resp, lib.AppErr>> {
\treturn core.callback2(|r: &lib.Req, c: &mut lib.Ctx| captures(move next, copy mw_idx, share app_arc) => {
\t\tval inner: core.Result<lib.Resp, lib.AppErr> = next.call(r, c);
\t\tval _i: Int = mw_idx;
\t\tval _a = app_arc.get();
\t\treturn move inner;
\t});
}

fn main() nothrow -> Int {
\tval a = conc.arc<type AppState>(AppState(name = "x"));
\tval base = core.callback2(|r: &lib.Req, c: &mut lib.Ctx| nothrow => {
\t\treturn lib.ok_resp(r.method.byte_length() + c.idx);
\t});
\tval _composed = build(move a, 0, move base);
\treturn 0;
}
"""


def test_captures_mix_published_library_consumer(
	_published_mwpkg: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""P2 + P5 in M-C. The captures-mix lambda lives in a consumer that
	imports types from a signed library package. Pins that
	`captures(move next, copy idx, share arc)` resolves correctly when
	the lambda's expected Callback type is from a cross-package
	signature."""
	pkg_root, trust_path = _published_mwpkg
	rc, errs = _compile_against_pkg(
		tmp_path=tmp_path, pkg_root=pkg_root, trust_path=trust_path,
		deps=["mwpkg@1.0.0"], source=_CONSUMER_CAPTURES_MIX_FROM_PKG,
	)
	_assert_clean(rc, errs, "P2+P5 / M-C")


# ────────────────────────────────────────────────────────────────────
# Section D — Signed stdlib package-consumer (Mode M-D)
# ────────────────────────────────────────────────────────────────────


_STDLIB_CONSUMER_CB3 = """\
module main;

import std.core as core;

struct Req { pub method: String }
struct Ctx { pub idx: Int }
struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn register(slot: &mut Array<core.Callback3<&Req, &mut Ctx,
\t        core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>,
\t        core.Result<Resp, AppErr>>>,
\t        cb: core.Callback3<&Req, &mut Ctx,
\t        core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>,
\t        core.Result<Resp, AppErr>>) nothrow -> Void {
\tslot.push(move cb);
\treturn core.void_value();
}

fn main() nothrow -> Int {
\tvar slot: Array<core.Callback3<&Req, &mut Ctx,
\t    core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>,
\t    core.Result<Resp, AppErr>>> = [];
\tregister(&mut slot, |req, ctx, next| => {
\t\tval m: String = req.method.clone();
\t\tctx.idx = ctx.idx + 1;
\t\tval inner: core.Result<Resp, AppErr> = next.call(req, ctx);
\t\treturn move inner;
\t});
\treturn 0;
}
"""


def test_callback3_through_signed_stdlib(
	stdlib_package, tmp_path: Path,
) -> None:
	"""P1 + P5 in M-D. Stdlib loaded as a signed package (not via
	`--stdlib-root`). Pins that the Callback3 / Callback2 / Result
	types reach the consumer correctly when stdlib itself comes from
	a `.dmp` — the deploy-pipeline path."""
	src = tmp_path / "main.drift"
	src.write_text(_STDLIB_CONSUMER_CB3, encoding="utf-8")
	out = tmp_path / "a.out"
	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir(exist_ok=True)
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(src),
		"--stdlib-root", str(empty_stdlib),
		"--target-word-bits", "64",
		"--package-root", str(stdlib_package.pkg_root),
		"--dep", f"std@{stdlib_package.version}",
		"--trust-store", str(stdlib_package.trust_path),
		"--dev", "--dev-core-trust-store", str(stdlib_package.trust_path),
		"--entry", "main::main",
		"-o", str(out),
		"--json",
	]
	rc = subprocess.run(
		cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=180,
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	if not rc.stdout.strip():
		errs = [rc.stderr[:2000]]
		exit_code = rc.returncode
	else:
		result = json.loads(rc.stdout)
		errs = [d["message"] for d in result.get("diagnostics", []) if d.get("severity") == "error"]
		exit_code = result.get("exit_code", rc.returncode)
	_assert_clean(exit_code, errs, "P1+P5 / M-D")


# ────────────────────────────────────────────────────────────────────
# Section E — `--emit-package` producer flow (Mode M-E)
# ────────────────────────────────────────────────────────────────────


def test_mw_callback3_emit_package_producer(tmp_path: Path) -> None:
	"""Mode M-E. Producer flow: build the multi-module middleware
	library as a signed `.dmp` package and confirm the producer
	emits a valid signed payload that downstream consumers can load.
	Pins that signatures involving `Callback3<&T, &mut U, Callback2<...>,
	Result<...>>` survive package serialization without the producer
	rejecting them."""
	pkg_root = tmp_path / "pkg_root"
	trust_path = tmp_path / "trust.json"
	dmp = _emit_signed_package(
		tmp_path=tmp_path,
		package_id="mwprod",
		package_version="0.1.0",
		namespace_glob="mwprod.*",
		module_dir_name="lib",
		files={
			"req.drift": _LIB_PKG_REQ.replace("mwpkg", "mwprod"),
			"ctx.drift": _LIB_PKG_CTX.replace("mwpkg", "mwprod"),
			"types.drift": _LIB_PKG_TYPES.replace("mwpkg", "mwprod"),
			"mwprod.drift": _LIB_PKG_API.replace("mwpkg", "mwprod"),
		},
		dest_pkg_root=pkg_root,
		dest_trust_path=trust_path,
	)
	assert dmp.exists() and dmp.stat().st_size > 0, "produced .dmp must be non-empty"
	# Round-trip: the produced package must be loadable by a fresh
	# consumer compile. This catches producer / consumer drift that an
	# emit-only test would miss.
	consumer_src = _CONSUMER_MW_FROM_PKG.replace("mwpkg", "mwprod")
	rc, errs = _compile_against_pkg(
		tmp_path=tmp_path / "consume",
		pkg_root=pkg_root,
		trust_path=trust_path,
		deps=["mwprod@0.1.0"],
		source=consumer_src,
	)
	_assert_clean(rc, errs, "P1 / M-E (producer + round-trip consume)")
