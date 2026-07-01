# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: cross-package bare-untyped lambda passed to a
concrete `Callback3` parameter must get its three parameter types
inferred from the package-loaded interface's instantiated type-args.

Same root surface as `test_callback3_bare_lambda_param_inference.py`,
but the callee's signature is loaded from a SIGNED .dmp package — the
shape that surfaced the user's report (web-rest 0.4.0 consumed by
pushcoin.bookkeeper on driftc 0.31.28, 2026-04-29).

In-tree single-source / cross-module Callback3 bare-lambda inference
already works (see `test_callback3_bare_lambda_param_inference.py`).
This file pins the cross-package case independently because the
callable_registry's candidate signature for a package-loaded fn passes
through .dmp serialization + relinking — distinct from the source path.

Carriers:

  X1. Cross-package `run_mw3(Callback3<Req, Ctx, Callback2<Req, Ctx,
      Result<Resp, AppErr>>, Result<Resp, AppErr>>)` called with a
      bare untyped `|req, ctx, next| => ...` lambda whose body
      accesses `req.n` / `ctx.k` and calls `next.call(req, ctx)`.
      Pre-fix this fails with the user-reported diagnostic family:
      "field access requires a struct value", "no matching method
      'call' for receiver Unknown", "lambda can throw but is expected
      to be nothrow for Fn(Req, Ctx, Callback2) nothrow -> Result".

  X2. Same shape but with ref types in the iface args
      (`Callback3<&Req, &mut Ctx, Callback2<&Req, &mut Ctx, ...>,
      ...>`) — the exact web-rest middleware shape.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


LIB_SOURCE = """\
module mwlib;

import std.core as core;

export { Req, Ctx, Resp, AppErr, run_mw3, run_mw3_ref, ok };

pub struct Req { pub n: Int }
pub struct Ctx { pub k: Int }
pub struct Resp { pub status: Int }
pub struct AppErr { pub code: Int }

pub fn ok(s: Int) nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Ok(Resp(status = s));
}

pub fn run_mw3(
\thandler: core.Callback3<Req, Ctx,
\t                        core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>,
\t                        core.Result<Resp, AppErr>>
) nothrow -> Int {
\treturn 0;
}

pub fn run_mw3_ref(
\thandler: core.Callback3<&Req, &mut Ctx,
\t                        core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>,
\t                        core.Result<Resp, AppErr>>
) nothrow -> Int {
\treturn 0;
}
"""


# Multi-module library shape — the actual web-rest layout. Types live
# in sub-modules (mwlib.req, mwlib.ctx, mwlib.resp, mwlib.errs) and the
# top-level `mwlib` module re-exports them and declares the
# Callback3-taking entry point. This is what surfaces the cross-package
# nested-Callback bug per the user's diagnostic on web-rest 0.4.0.

LIB_REQ_SOURCE = """\
module mwlib_multi.req;

export { Req };

pub struct Req { pub n: Int }
"""

LIB_CTX_SOURCE = """\
module mwlib_multi.ctx;

export { Ctx };

pub struct Ctx { pub k: Int }
"""

LIB_RESP_SOURCE = """\
module mwlib_multi.resp;

export { Resp };

pub struct Resp { pub status: Int }
"""

LIB_ERRS_SOURCE = """\
module mwlib_multi.errs;

export { AppErr };

pub struct AppErr { pub code: Int }
"""

LIB_MULTI_TOP_SOURCE = """\
module mwlib_multi;

import std.core as core;
import mwlib_multi.req as req;
import mwlib_multi.ctx as ctx;
import mwlib_multi.resp as resp;
import mwlib_multi.errs as errs;

export { Req, Ctx, Resp, AppErr, run_mw3_ref, ok };

pub type Req = req.Req;
pub type Ctx = ctx.Ctx;
pub type Resp = resp.Resp;
pub type AppErr = errs.AppErr;

pub fn ok(s: Int) nothrow -> core.Result<resp.Resp, errs.AppErr> {
\treturn core.Result::Ok(resp.Resp(status = s));
}

pub fn run_mw3_ref(
\thandler: core.Callback3<&req.Req, &mut ctx.Ctx,
\t                        core.Callback2<&req.Req, &mut ctx.Ctx, core.Result<resp.Resp, errs.AppErr>>,
\t                        core.Result<resp.Resp, errs.AppErr>>
) nothrow -> Int {
\treturn 0;
}
"""


# X1 — cross-package Callback3, value-typed iface args.
CONSUMER_X1_SOURCE = """\
module main;

import std.core as core;
import mwlib as lib;

pub fn main() nothrow -> Int {
\treturn lib.run_mw3(|req, ctx, next| => {
\t\tval inner = next.call(req, ctx);
\t\treturn lib.ok(req.n + ctx.k);
\t});
}
"""


# X2 — cross-package Callback3, ref-typed iface args (exact web-rest shape).
CONSUMER_X2_SOURCE = """\
module main;

import std.core as core;
import mwlib as lib;

pub fn main() nothrow -> Int {
\treturn lib.run_mw3_ref(|req, ctx, next| => {
\t\tval inner = next.call(req, ctx);
\t\treturn lib.ok(req.n + ctx.k);
\t});
}
"""


def _b64(data: bytes) -> str:
	return __import__("base64").b64encode(data).decode("ascii")


def _publish_signed_pkg(
	lib_dir: Path,
	*,
	src_files: list[Path],
	package_id: str,
	package_version: str,
	namespace_glob: str,
	dest_pkg_root: Path,
	dest_trust_path: Path,
) -> None:
	"""Build + sign a single library package via the shared v1
	publisher; place .dmp + v1 sidecars under
	`<dest_pkg_root>/<package_id>/<package_version>/` and write a
	v1 role-tagged trust file at `dest_trust_path`."""
	from lang.tests.driver.pkg_test_helpers import publish_v1_pkg
	publish_v1_pkg(
		lib_dir=lib_dir,
		src_files=src_files,
		package_id=package_id,
		package_version=package_version,
		namespace_glob=namespace_glob,
		dest_pkg_root=dest_pkg_root,
		dest_trust_path=dest_trust_path,
		stdlib_root_override=stdlib_root(),
	)


@pytest.fixture(scope="module")
def _built_lib(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	"""Build + sign single-module mwlib package."""
	base = tmp_path_factory.mktemp("cross_pkg_cb3")
	lib_dir = base / "lib"
	lib_dir.mkdir(parents=True, exist_ok=True)
	(lib_dir / "mwlib.drift").write_text(LIB_SOURCE, encoding="utf-8")

	pkg_root = base / "pkg_root"
	trust_path = base / "trust.json"
	_publish_signed_pkg(
		lib_dir,
		src_files=[lib_dir / "mwlib.drift"],
		package_id="mwlib",
		package_version="1.0.0",
		namespace_glob="mwlib.*",
		dest_pkg_root=pkg_root,
		dest_trust_path=trust_path,
	)
	return pkg_root, trust_path


@pytest.fixture(scope="module")
def _built_multi_lib(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	"""Build + sign multi-module mwlib_multi package — types live in
	sub-modules and are re-exported via `pub type` aliases through the
	top-level module. This mirrors web-rest's package layout."""
	base = tmp_path_factory.mktemp("cross_pkg_cb3_multi")
	lib_dir = base / "lib"
	lib_dir.mkdir(parents=True, exist_ok=True)
	(lib_dir / "req.drift").write_text(LIB_REQ_SOURCE, encoding="utf-8")
	(lib_dir / "ctx.drift").write_text(LIB_CTX_SOURCE, encoding="utf-8")
	(lib_dir / "resp.drift").write_text(LIB_RESP_SOURCE, encoding="utf-8")
	(lib_dir / "errs.drift").write_text(LIB_ERRS_SOURCE, encoding="utf-8")
	(lib_dir / "mwlib_multi.drift").write_text(LIB_MULTI_TOP_SOURCE, encoding="utf-8")

	pkg_root = base / "pkg_root"
	trust_path = base / "trust.json"
	_publish_signed_pkg(
		lib_dir,
		src_files=[
			lib_dir / "req.drift",
			lib_dir / "ctx.drift",
			lib_dir / "resp.drift",
			lib_dir / "errs.drift",
			lib_dir / "mwlib_multi.drift",
		],
		package_id="mwlib_multi",
		package_version="1.0.0",
		namespace_glob="mwlib_multi.*",
		dest_pkg_root=pkg_root,
		dest_trust_path=trust_path,
	)
	return pkg_root, trust_path


def _compile_consumer(
	source: str,
	*,
	pkg_root: Path,
	trust_path: Path,
	tmp_path: Path,
	dep: str = "mwlib@1.0.0",
) -> tuple[int, list[str]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = tmp_path / "a.out"

	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--package-root", str(pkg_root),
		"--dep", dep,
		"--trust-store", str(trust_path),
		str(src),
		"-o", str(out),
		"--json",
	]
	rc = subprocess.run(
		cmd,
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	if not rc.stdout.strip():
		return rc.returncode, [rc.stderr[:2000]]
	result = json.loads(rc.stdout)
	msgs = [d["message"] for d in result.get("diagnostics", []) if d.get("severity") == "error"]
	return result.get("exit_code", rc.returncode), msgs


def test_x1_cross_package_callback3_bare_lambda(
	_built_lib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""X1 — cross-package `Callback3<Req, Ctx, Callback2<Req, Ctx,
	Result<...>>, Result<...>>` accepts a bare untyped lambda
	`|req, ctx, next| => { val inner = next.call(req, ctx); return ok(req.n + ctx.k); }`.
	The lambda's three params must be inferred from the package-
	loaded interface's instantiated type-args."""
	pkg_root, trust_path = _built_lib
	exit_code, msgs = _compile_consumer(
		CONSUMER_X1_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
	)
	if exit_code != 0:
		pytest.fail(
			f"cross-package Callback3 bare lambda must compile; "
			f"exit_code={exit_code} diagnostics:\n" + "\n".join(msgs)
		)


def test_x2_cross_package_callback3_bare_lambda_ref_args(
	_built_lib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""X2 — cross-package middleware shape with ref types in iface
	args: `Callback3<&Req, &mut Ctx, Callback2<&Req, &mut Ctx,
	Result<...>>, Result<...>>`. This is the exact web-rest 0.4.0
	`add_middleware` shape that the bookkeeper app failed against."""
	pkg_root, trust_path = _built_lib
	exit_code, msgs = _compile_consumer(
		CONSUMER_X2_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
	)
	if exit_code != 0:
		pytest.fail(
			f"cross-package Callback3 bare lambda with ref-typed "
			f"iface args must compile; exit_code={exit_code} "
			f"diagnostics:\n" + "\n".join(msgs)
		)


# X3 — multi-module published library, web-rest layout. The Callback3
# parameter's type-args reference types that live in sibling sub-modules
# of the library package (req.Req, ctx.Ctx, resp.Resp, errs.AppErr).
# This is the load-bearing shape that the user's report against
# web-rest 0.4.0 surfaced — pre-fix the third type-arg `Callback2<...>`
# loses its instance type-args at consumer side and the bare lambda's
# `next` parameter resolves to bare `std.core.Callback2` (no args).
CONSUMER_X3_SOURCE = """\
module main;

import std.core as core;
import mwlib_multi as lib;

pub fn main() nothrow -> Int {
\treturn lib.run_mw3_ref(|req, ctx, next| => {
\t\tval inner = next.call(req, ctx);
\t\treturn lib.ok(req.n + ctx.k);
\t});
}
"""


def test_x3_multi_module_cross_package_callback3_bare_lambda(
	_built_multi_lib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""X3 — LANGUAGE_BUG carrier (2026-04-29). Multi-module library
	package whose Callback3 type-args reference types from sibling
	sub-modules. Reproduces the user's report against web-rest 0.4.0
	on driftc 0.31.28: the third Callback3 type-arg (a nested
	Callback2 instance) loses its type-args at consumer side, the
	bare lambda's third param resolves to bare `std.core.Callback2`
	with no args, and `next.call(req, ctx)` fails with `no matching
	method 'call' for receiver Unknown`."""
	pkg_root, trust_path = _built_multi_lib
	exit_code, msgs = _compile_consumer(
		CONSUMER_X3_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
		dep="mwlib_multi@1.0.0",
	)
	if exit_code != 0:
		pytest.fail(
			f"multi-module cross-package Callback3 bare lambda must "
			f"compile; exit_code={exit_code} diagnostics:\n" + "\n".join(msgs)
		)


# X4 — Bug A package-consumer regression (LANGUAGE_BUG, 0.31.38).
#
# Bookkeeper shape: a `throws` outer function calls `lib.run_mw3(...)` with
# a bare nothrow Callback3 lambda whose body does `val r = next.call(req,
# ctx); match &r { Ok(_) => ..., Err(_) => ... }`.  Pre-fix
# (`type_checker.py:_check_function_body`'s `fn_declared_throws` leaking
# across the lambda boundary), the throws outer's auto-try context fires
# inside the nothrow lambda body, eager-unwraps `r` from
# `Result<Resp, AppErr>` to `Resp`, then `match &r` rejects with "match
# scrutinee must be a variant type" → the cascade observed in the bookkeeper
# / web-rest 0.4.1 report on driftc 0.31.37 (2026-04-30).
#
# This test pins the package-consumer shape directly.  The narrow
# single-source regression for the same bug lives in
# `test_auto_try_lambda_boundary.py` — that file isolates the fix; this
# file pins that the fix closes the bookkeeper-shape report end-to-end.
CONSUMER_X4_SOURCE = """\
module main;

import std.core as core;
import mwlib as lib;

// Throws outer fn — pre-fix, this throws-ness leaked into the nothrow
// Callback3 lambda body's auto-try context.
fn install() throws -> Int {
\treturn lib.run_mw3(|req, ctx, next| => {
\t\tval r = next.call(req, ctx);
\t\tval n = match &r {
\t\t\tcore.Result::Ok(_) => { 1 },
\t\t\tcore.Result::Err(_) => { 0 }
\t\t};
\t\treturn lib.ok(req.n + ctx.k + n);
\t});
}

pub fn main() nothrow -> Int { return 0; }
"""


def test_x4_throws_outer_with_nothrow_callback3_lambda_match_on_result(
	_built_lib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""X4 — Bug A package-consumer regression (LANGUAGE_BUG, 0.31.38).
	Throws outer fn × nothrow Callback3 lambda × `match &r { Ok, Err }`
	on a Result returned by the inner Callback2's `.call(...)`.  Mirrors
	the exact bookkeeper / web-rest 0.4.1 middleware shape.

	Pre-fix: cascade of "match scrutinee must be a variant type",
	"unknown name 'r'", "lambda can throw but is expected to be nothrow",
	"callback3 expects a function value", "no matching overload for
	function 'run_mw3'".  Post-fix: clean compile (the fix
	save/restores `fn_declared_throws` and `try_block_depth` across the
	lambda body type-check at `type_checker.py:6266+`)."""
	pkg_root, trust_path = _built_lib
	exit_code, msgs = _compile_consumer(
		CONSUMER_X4_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
	)
	if exit_code != 0:
		pytest.fail(
			f"throws-outer × nothrow-Callback3-lambda × match-on-Result "
			f"must compile post-fix; exit_code={exit_code} diagnostics:\n"
			+ "\n".join(msgs)
		)
