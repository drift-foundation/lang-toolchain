# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: transitive dependency resolution on multi-version package roots.

Proven discriminator:
  - shared package root with two versions of a transitive dep (deplib 0.1.0, 0.2.0)
  - top-level package (mylib) declares package_deps = [deplib=0.2.0]
  - only --dep mylib@1.0.0 passed to consumer
  - consumer must resolve deplib@0.2.0 from package_deps, not collide

This test builds two packages (deplib, mylib), stages a multi-version root
with historical deplib versions, and verifies the consumer compiles without
duplicate-module errors.
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

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

# ── deplib: a simple dependency package ──────────────────────────────

DEPLIB_SOURCE = """\
module deplib;

import std.core as core;

export { add_one };

pub fn add_one(x: Int) nothrow -> Int {
\treturn x + 1;
}
"""

# ── mylib: top-level package that depends on deplib ──────────────────

MYLIB_SOURCE = """\
module mylib;

import std.core as core;
import deplib;

export { compute };

pub fn compute(x: Int) nothrow -> Int {
\treturn deplib.add_one(x);
}
"""

# ── consumer: imports mylib ──────────────────────────────────────────

CONSUMER_SOURCE = """\
module consumer;

import std.core as core;
import mylib;

pub fn main() nothrow -> Int {
\tval result = mylib.compute(41);
\tif result != 42 { return 1; }
\treturn 0;
}
"""


def _sign_package(pkg_path: Path, pkg_root: Path, pkg_id: str, version: str,
                  priv_key, pub_raw: bytes, kid: str, pub_b64: str) -> None:
	"""Sign and stage a package into the standard layout."""
	dest = pkg_root / pkg_id / version
	dest.mkdir(parents=True, exist_ok=True)
	shutil.copy2(str(pkg_path), str(dest / f"{pkg_id}.dmp"))
	pkg_bytes = pkg_path.read_bytes()
	(dest / f"{pkg_id}.sig").write_text(json.dumps({
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{sha256(pkg_bytes).hexdigest()}",
		"signatures": [{"algo": "ed25519", "kid": kid,
			"sig": base64.b64encode(priv_key.sign(pkg_bytes)).decode("ascii"),
			"pubkey": pub_b64}],
	}, separators=(",", ":"), sort_keys=True))


def test_transitive_dep_narrows_to_declared_version(tmp_path: Path) -> None:
	"""Consumer with --dep mylib@1.0.0 must resolve deplib from package_deps,
	not collide on multiple deplib versions in the shared root."""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	pkg_root = tmp_path / "shared_root"
	pkg_root.mkdir()

	# ── Build deplib 0.1.0 and 0.2.0 (identical source, different versions) ──
	deplib_dir = tmp_path / "deplib_src"
	deplib_dir.mkdir()
	(deplib_dir / "deplib.drift").write_text(DEPLIB_SOURCE)

	for ver in ("0.1.0", "0.2.0"):
		deplib_dmp = tmp_path / f"deplib_{ver}.dmp"
		res = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc",
			 "-M", str(deplib_dir), str(deplib_dir / "deplib.drift"),
			 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
			 "--package-id", "deplib", "--package-version", ver,
			 "--package-target", "test-target",
			 "--emit-package", str(deplib_dmp), "--test-build-only"],
			cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
		)
		assert res.returncode == 0, f"deplib {ver} build failed: {res.stderr[:300]}"
		_sign_package(deplib_dmp, pkg_root, "deplib", ver, priv, pub_raw, kid, pub_b64)

	# ── Build mylib 1.0.0 (depends on deplib 0.2.0) ──
	mylib_dir = tmp_path / "mylib_src"
	mylib_dir.mkdir()
	(mylib_dir / "mylib.drift").write_text(MYLIB_SOURCE)

	# Trust store needed for mylib build (it consumes deplib)
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"mylib.*": [kid], "deplib.*": [kid]},
		"revoked": [],
	}))

	mylib_dmp = tmp_path / "mylib.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "-M", str(mylib_dir), str(mylib_dir / "mylib.drift"),
		 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
		 "--package-id", "mylib", "--package-version", "1.0.0",
		 "--package-target", "test-target",
		 "--package-dep", "deplib=0.2.0",
		 "--package-root", str(pkg_root), "--dep", "deplib@0.2.0",
		 "--trust-store", str(trust_path),
		 "--emit-package", str(mylib_dmp), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"mylib build failed: {res.stderr[:300]}"
	_sign_package(mylib_dmp, pkg_root, "mylib", "1.0.0", priv, pub_raw, kid, pub_b64)

	# ── Consumer: only --dep mylib@1.0.0, multi-version root has both deplib versions ──
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text(CONSUMER_SOURCE)

	out_bin = tmp_path / "consumer_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 str(consumer_dir / "consumer.drift"),
		 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
		 "--package-root", str(pkg_root),
		 "--dep", "mylib@1.0.0",
		 "--trust-store", str(trust_path),
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, (
		f"consumer compile failed (transitive dep resolution should have "
		f"narrowed deplib to 0.2.0 via mylib's package_deps):\n{res.stderr[:500]}"
	)
	assert out_bin.exists(), "binary not produced"


def test_transitive_dep_conflict_rejected(tmp_path: Path) -> None:
	"""Two root packages declaring conflicting versions of the same transitive
	dep must produce an explicit error, not silently pick one."""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	pkg_root = tmp_path / "shared_root"
	pkg_root.mkdir()

	# Build deplib 0.1.0 and 0.2.0
	deplib_dir = tmp_path / "deplib_src"
	deplib_dir.mkdir()
	(deplib_dir / "deplib.drift").write_text(DEPLIB_SOURCE)

	for ver in ("0.1.0", "0.2.0"):
		deplib_dmp = tmp_path / f"deplib_{ver}.dmp"
		res = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc",
			 "-M", str(deplib_dir), str(deplib_dir / "deplib.drift"),
			 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
			 "--package-id", "deplib", "--package-version", ver,
			 "--package-target", "test-target",
			 "--emit-package", str(deplib_dmp), "--test-build-only"],
			cwd=ROOT, capture_output=True, text=True, timeout=120,
		)
		assert res.returncode == 0, f"deplib {ver} build failed: {res.stderr[:300]}"
		_sign_package(deplib_dmp, pkg_root, "deplib", ver, priv, pub_raw, kid, pub_b64)

	# Trust store (needed for liba/libb builds that consume deplib)
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"liba.*": [kid], "libb.*": [kid], "deplib.*": [kid]},
		"revoked": [],
	}))

	# Build two top-level packages that declare different deplib versions
	for pkg_name, dep_ver in [("liba", "0.1.0"), ("libb", "0.2.0")]:
		src_dir = tmp_path / f"{pkg_name}_src"
		src_dir.mkdir()
		(src_dir / f"{pkg_name}.drift").write_text(
			f"module {pkg_name};\nimport std.core as core;\nimport deplib;\n"
			f"export {{ go }};\npub fn go() nothrow -> Int {{ return deplib.add_one(1); }}\n"
		)
		dmp = tmp_path / f"{pkg_name}.dmp"
		res = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc",
			 "-M", str(src_dir), str(src_dir / f"{pkg_name}.drift"),
			 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
			 "--package-id", pkg_name, "--package-version", "1.0.0",
			 "--package-target", "test-target",
			 "--package-dep", f"deplib={dep_ver}",
			 "--package-root", str(pkg_root), "--dep", f"deplib@{dep_ver}",
			 "--trust-store", str(trust_path),
			 "--emit-package", str(dmp), "--test-build-only"],
			cwd=ROOT, capture_output=True, text=True, timeout=120,
		)
		assert res.returncode == 0, f"{pkg_name} build failed: {res.stderr[:300]}"
		_sign_package(dmp, pkg_root, pkg_name, "1.0.0", priv, pub_raw, kid, pub_b64)

	# Consumer depends on both liba and libb — conflicting deplib versions
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text(
		"module consumer;\nimport std.core as core;\nimport liba;\nimport libb;\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)

	out_bin = tmp_path / "consumer_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 str(consumer_dir / "consumer.drift"),
		 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
		 "--package-root", str(pkg_root),
		 "--dep", "liba@1.0.0", "--dep", "libb@1.0.0",
		 "--trust-store", str(trust_path),
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode != 0, "should fail with transitive version conflict"
	assert "transitive dependency version conflict" in res.stderr, (
		f"expected transitive conflict error, got:\n{res.stderr[:500]}"
	)
