# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 1a Frozen milestone — package-mode coverage.

Companion to `test_frozen_substrate.py` (which exercises Frozen in
fresh-source-compile mode).  This file pins the same auto-derive
behavior in package-loading mode: a user struct with all-Frozen
fields, exported from a signed package and imported by a consumer,
must auto-derive Frozen on the consumer side.

This guards against the prover shortcut breaking under package
boundary type-resolution paths (the same shape that broke 0.31.36's
collision-check assertion under package-mode stdlib recompilation).
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


# Library: an exported struct with all-Frozen fields.  Consumer-side
# auto-derive must classify this as Frozen.
LIB_SOURCE = """\
module frozenlib;

export { Config };

pub struct Config {
\tpub name: String,
\tpub port: Int,
\tpub enabled: Bool
}
"""


# Consumer: imports frozenlib, asserts Config is Frozen via a
# bound-bearing helper.  Compile success means the prover's
# structural shortcut works in package-loaded mode.
CONSUMER_SOURCE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import frozenlib as lib;

use trait shareable.Frozen;

fn assert_frozen<T>() nothrow -> Void require T is shareable.Frozen { }

fn main() nothrow -> Int {
\tassert_frozen<type lib.Config>();
\treturn 0;
}
"""


def _b64(data: bytes) -> str:
	import base64
	return base64.b64encode(data).decode("ascii")


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
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	pkg_path = lib_dir / f"{package_id}.dmp"
	rc = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc",
			"--dev",
			"-M", str(lib_dir),
			"--stdlib-root", str(stdlib_root()),
			*[str(p) for p in src_files],
			"--package-id", package_id,
			"--package-version", package_version,
			"--package-target", "drift-dev",
			"--emit-package", str(pkg_path),
			"--json",
		],
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	assert rc.returncode == 0, f"lib '{package_id}' build failed:\n{rc.stdout}\n---\n{rc.stderr[:1000]}"

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

	trust = {
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {namespace_glob: [kid]},
		"revoked": [],
	}
	dest_trust_path.write_text(json.dumps(trust, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	dest_dir = dest_pkg_root / package_id / package_version
	dest_dir.mkdir(parents=True, exist_ok=True)
	shutil.copy2(str(pkg_path), str(dest_dir / f"{package_id}.dmp"))
	shutil.copy2(str(sig_path), str(dest_dir / f"{package_id}.sig"))


@pytest.fixture(scope="module")
def _built_frozenlib(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	"""Build + sign the frozenlib package."""
	base = tmp_path_factory.mktemp("frozen_pkg")
	lib_dir = base / "lib"
	lib_dir.mkdir(parents=True, exist_ok=True)
	(lib_dir / "frozenlib.drift").write_text(LIB_SOURCE, encoding="utf-8")

	pkg_root = base / "pkg_root"
	trust_path = base / "trust.json"
	_publish_signed_pkg(
		lib_dir,
		src_files=[lib_dir / "frozenlib.drift"],
		package_id="frozenlib",
		package_version="1.0.0",
		namespace_glob="frozenlib.*",
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
	dep: str = "frozenlib@1.0.0",
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
		"--test-build-only",
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


# ── Package-mode regression ───────────────────────────────────────


def test_frozen_auto_derive_through_package_boundary(
	_built_frozenlib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""User struct `Config` with all-Frozen fields (String, Int, Bool)
	is exported from a signed package; the consumer imports it and
	calls `assert_frozen<type lib.Config>()`.  Consumer-side compile
	must succeed: the prover's Frozen structural shortcut must work
	when the struct's TypeKey was reconstructed from a `.dmp` package
	rather than parsed from in-tree source.

	This pins that the substrate's structural derivation is
	package-mode-correct, not just fresh-source-correct."""
	pkg_root, trust_path = _built_frozenlib
	exit_code, msgs = _compile_consumer(
		CONSUMER_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
	)
	if exit_code != 0:
		pytest.fail(
			f"package-imported user struct with all-Frozen fields "
			f"must auto-derive Frozen on consumer side; exit_code="
			f"{exit_code} diagnostics:\n" + "\n".join(msgs)
		)


# ── Spoof-resistance ──────────────────────────────────────────────


# Consumer that declares its own module under a fake `std.*` name and
# attempts to write `implement std.core.shareable.Frozen for Evil { }`
# for a non-Frozen type.  The trust gate must reject this even though
# the module id starts with `std.`.
SPOOF_SOURCE = """\
module std.evil;

import std.core as core;
import std.core.shareable as shareable;
import std.concurrent as conc;

pub struct Evil {
\tpub m: conc.Mutex<Int>
}

implement shareable.Frozen for Evil { }

fn main() nothrow -> Int { return 0; }
"""


def test_user_implement_frozen_spoofed_std_module_is_rejected(
	tmp_path: Path,
) -> None:
	"""A third-party source file that declares `module std.evil;` and
	writes `implement std.core.shareable.Frozen for Evil { }` MUST be
	rejected even though the module id starts with `std.`.  The trust
	gate is package-based (path-vetted via `--stdlib-root`), not
	module-name-based: the spoofing file lives outside `--stdlib-root`,
	so its host package id (`local_pkg`) is not `"std"`, while the
	`Frozen` trait's package id IS `"std"`.  Mismatch → reject.

	If this test ever starts passing, the trust gate has regressed to
	a name-only check and any third-party can claim Frozen for any
	type by spelling their module under `std.*`."""
	src = tmp_path / "spoof.drift"
	src.write_text(SPOOF_SOURCE, encoding="utf-8")
	out = tmp_path / "a.out"

	rc = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc",
			"--target-word-bits", "64",
			"--stdlib-root", str(stdlib_root()),
			str(src),
			"-o", str(out),
			"--json",
			"--test-build-only",
		],
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	if not rc.stdout.strip():
		pytest.fail(
			f"spoof compile produced no JSON output (rc={rc.returncode}); "
			f"stderr:\n{rc.stderr[:2000]}"
		)
	result = json.loads(rc.stdout)
	errs = [
		d for d in result.get("diagnostics", [])
		if d.get("severity") == "error"
	]
	rejected = any(
		d.get("code") == "E_FROZEN_USER_IMPL_REJECTED" for d in errs
	)
	assert rejected, (
		"trust gate must reject `implement Frozen` from a spoofed "
		"`module std.evil;` file built outside --stdlib-root.  "
		f"Diagnostics:\n" + "\n".join(d.get("message", "") for d in errs)
	)
