# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: method whose signature references a cross-package type alias
must resolve in free-function bodies when the alias comes from a loaded package.

Root cause: the parser's type table didn't have package type aliases during
signature resolution, so cross-package alias references (e.g. lib.Request →
lib.inner.Req) created fresh TypeIds instead of resolving through the alias
chain.  This made the method's parameter TypeId differ from the argument TypeId.

Fix: pre-populate the parser's type table with package type aliases before
signature resolution (external_type_aliases parameter).
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


LIB_INNER_SOURCE = """\
module mylib.inner;

import std.core as core;

export { Req };

pub struct Req {
\tpub path: String
}
"""

LIB_SOURCE = """\
module mylib;

import std.core as core;
import mylib.inner as inner;

export { Request, make_request, run_handler };

pub type Request = inner.Req;

pub fn make_request(path: String) nothrow -> Request {
\treturn inner.Req(path = path);
}

pub fn run_handler(handler: core.Callback2<&Request, Int, Int>) nothrow -> Int {
\treturn 0;
}
"""

# Regression: method with cross-package alias param called in a free function.
# This failed before the fix because the alias wasn't resolved during signature
# resolution — the method's param TypeId didn't match the argument TypeId.
CONSUMER_DIRECT_SOURCE = """\
module main;

import std.core as core;
import mylib as lib;

pub struct App {
\tpub name: String
}

implement App {
\tfn greet(self: &App, req: &lib.Request) nothrow -> Int {
\t\treturn 1;
\t}

\tfn ping(self: &App) nothrow -> Int {
\t\treturn 2;
\t}
}

fn call_greet(app: &App, req: &lib.Request) nothrow -> Int {
\treturn app.greet(req);
}

pub fn main() nothrow -> Int {
\tvar app = App(name = "test");
\tval req = lib.make_request("/health");

\tval p = app.ping();
\tif p != 2 { return 90; }

\tval g = call_greet(&app, &req);
\tif g != 1 { return 91; }

\treturn 0;
}
"""

# Regression: explicit callback2(|req, extra| ...) lambda wrapper where the
# outer call site (lib.run_handler) expects Callback2<&lib.Request, Int, Int>.
# The lambda params must be inferred from the expected Callback2 type so the
# method call inside the lambda body has concrete arg types.
CONSUMER_CALLBACK2_LAMBDA_SOURCE = """\
module main;

import std.core as core;
import mylib as lib;

pub struct App {
\tpub name: String
}

implement App {
\tfn greet(self: &App, req: &lib.Request) nothrow -> Int {
\t\treturn 1;
\t}
}

pub fn main() nothrow -> Int {
\tvar app = App(name = "test");

\t// Explicit callback2 wrapper with untyped lambda params.
\t// The compiler must infer req: &lib.Request and extra: Int from
\t// run_handler's expected Callback2<&lib.Request, Int, Int>.
\tval r = lib.run_handler(core.callback2(|req, extra| captures(move app) nothrow => {
\t\treturn app.greet(req);
\t}));

\treturn r;
}
"""


# Regression: stdlib identity must be canonical across source and package
# paths.  The package's serialized type table contains stdlib types (Result,
# Optional, etc.) with package_id="std".  Source-compiled stdlib must also
# use package_id="std" so that match arms like core.Result::Ok(v) on a
# package function's return type resolve correctly.  Without stdlib identity
# normalization, source-compiled stdlib uses package_id="__local__" which
# creates duplicate NominalKeys for the same logical type, causing:
#   "constructor 'Ok' ... does not match the match scrutinee type"
CONSUMER_STDLIB_IDENTITY_SOURCE = """\
module main;

import std.core as core;
import mylib as lib;

// Package function returns core.Result — the Result variant type in its
// return type comes from the package's serialized type table (package_id="std").
// Source-compiled stdlib must also use package_id="std" so that match arms
// like core.Result::Ok(v) resolve correctly against the package return type.
fn try_make(path: String) -> core.Result<lib.Request, String> {
\treturn core.Result::Ok(lib.make_request(path));
}

pub fn main() nothrow -> Int {
\tmatch try try_make("/test") catch { core.Result::Err("failed") } {
\t\tcore.Result::Ok(r) => {
\t\t\tif r.path.byte_length() > 0 { return 0; }
\t\t\treturn 1;
\t\t},
\t\tcore.Result::Err(_) => { return 2; }
\t}
}
"""


def _b64(data: bytes) -> str:
	return __import__("base64").b64encode(data).decode("ascii")


@pytest.fixture(scope="module")
def _built_lib(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	"""Build and sign mylib package once for the module."""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.drift.crypto import compute_ed25519_kid

	base = tmp_path_factory.mktemp("cross_pkg_method")
	lib_dir = base / "lib"
	lib_dir.mkdir(parents=True, exist_ok=True)

	(lib_dir / "inner.drift").write_text(LIB_INNER_SOURCE, encoding="utf-8")
	(lib_dir / "mylib.drift").write_text(LIB_SOURCE, encoding="utf-8")

	# v1 fixture: build with SCI stamp, then sign via shared
	# `sign_v1_pkg_into_root` (writes author + cert sidecars and
	# v1 role-tagged trust JSON, copies all three to the canonical
	# pkg-root layout).
	from lang.tests.driver.pkg_test_helpers import sign_v1_pkg_into_root
	_TEST_SCI = "sha256:" + ("0" * 64)
	pkg_path = base / "mylib.dmp"
	rc = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc",
			"--dev",
			"-M", str(lib_dir),
			"--stdlib-root", str(stdlib_root()),
			str(lib_dir / "mylib.drift"), str(lib_dir / "inner.drift"),
			"--package-id", "mylib",
			"--package-version", "1.0.0",
			"--package-target", "drift-dev",
			"--source-content-id", _TEST_SCI,
			"--emit-package", str(pkg_path),
			"--json",
		],
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	assert rc.returncode == 0, f"lib build failed:\n{rc.stdout}"

	trust_path = base / "trust.json"
	sign_v1_pkg_into_root(
		pkg_path=pkg_path,
		package_id="mylib",
		package_version="1.0.0",
		namespace_glob="mylib.*",
		dest_pkg_root=base / "pkg_root",
		dest_trust_path=trust_path,
	)

	return base / "pkg_root", trust_path


def _compile_consumer(
	source: str,
	*,
	pkg_root: Path,
	trust_path: Path,
	tmp_path: Path,
	link: bool = True,
) -> tuple[int, list[str], Path]:
	"""Compile a consumer source. Returns (exit_code, diag_messages, binary_path)."""
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = tmp_path / "a.out"

	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--package-root", str(pkg_root),
		"--dep", "mylib@1.0.0",
		"--trust-store", str(trust_path),
		str(src),
		"--json",
	]
	if link:
		cmd.extend(["-o", str(out)])

	rc = subprocess.run(
		cmd,
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	result = json.loads(rc.stdout)
	msgs = [d["message"] for d in result.get("diagnostics", [])]
	return result["exit_code"], msgs, out


def test_method_with_cross_package_alias_param(
	_built_lib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""Method with cross-package type alias param resolves in free function body."""
	pkg_root, trust_path = _built_lib
	exit_code, msgs, out = _compile_consumer(
		CONSUMER_DIRECT_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
	)
	if exit_code != 0:
		pytest.fail(f"compile failed:\n" + "\n".join(msgs))
	assert out.exists()
	run = subprocess.run([str(out)], capture_output=True, text=True)
	assert run.returncode == 0, f"binary exited {run.returncode}"


def test_callback2_lambda_with_cross_package_alias_param(
	_built_lib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""Explicit callback2(|req, extra| ...) lambda wrapper where the outer
	call expects Callback2<&lib.Request, Int, Int>.  The lambda params must
	be inferred from the expected Callback type so the method call inside
	the body has concrete cross-package arg types."""
	pkg_root, trust_path = _built_lib
	exit_code, msgs, out = _compile_consumer(
		CONSUMER_CALLBACK2_LAMBDA_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
	)
	if exit_code != 0:
		pytest.fail(f"compile failed:\n" + "\n".join(msgs))
	assert out.exists()


def test_stdlib_identity_across_source_and_package(
	_built_lib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""Stdlib types serialized in a package's type table must have the same
	identity as source-compiled stdlib types.  Without canonical stdlib
	package identity, Result/Optional/String etc. get duplicate NominalKeys
	and match-arm constructors fail to match the scrutinee type."""
	pkg_root, trust_path = _built_lib
	exit_code, msgs, out = _compile_consumer(
		CONSUMER_STDLIB_IDENTITY_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
	)
	if exit_code != 0:
		pytest.fail(f"compile failed:\n" + "\n".join(msgs))
	assert out.exists()
	run = subprocess.run([str(out)], capture_output=True, text=True)
	assert run.returncode == 0, f"binary exited {run.returncode}"
