# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Driver-layer regression pins for driftc as an exact package loader.

Contract (post-0.29):
  - Producer `.dmp` publishes `required_deps` as owner-declared ranges
    (`"M"` or `"M.N"`) — the manifest's acceptable-range vocabulary.
  - driftc does NOT auto-expand transitive dependencies from those
    ranges.  It is a strict, exact loader: every transitive dep must
    reach it as an exact `M.N.P` pin via `--dep`.
  - `drift prepare` / `drift build` are responsible for resolving the
    full graph and handing driftc the complete pin list.  The
    resolver-side semantics (pick-highest-in-range, cross-package
    conflict detection, patch float) are exercised at the tooling
    layer in `tools/drift_deploy/test_resolver.py` and
    `test_prepare.py` — this module only pins driftc-layer behavior.

The three scenarios below cover the complete driftc-loader contract:

  1. Negative (missing pin):  consumer passes only the root package
     pin; the package's published `required_deps` name a transitive
     the consumer did not pin → driftc refuses to invent a version
     and tells the user to run `drift prepare`.
  2. Positive (full graph):   consumer passes the root pin AND every
     transitive pin the package's `required_deps` name, where each
     exact pin lies within the declared range → driftc loads and
     compiles cleanly.
  3. Negative (range violation):  consumer passes a transitive pin
     whose exact version falls OUTSIDE the producer's declared range
     → driftc reports a transitive-version-conflict diagnostic.

The pre-0.29 driver test "driftc auto-expands transitive deps from
package roots" was deleted: that behavior is intentionally gone.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

# ── deplib: a trivial transitive package ─────────────────────────────

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
	"""Sign and stage a package into the standard pkg-root layout."""
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


def _stage_pkgroot(tmp_path: Path, stdlib: Path):
	"""Build deplib@0.2.0, deplib@0.3.0, and mylib@1.0.0.

	mylib's published `required_deps` pin `deplib = "0.2"` — the
	owner-declared acceptable range that crosses the package boundary.
	Returns (pkg_root, trust_path, consumer_dir).

	Two deplib versions are staged so callers can exercise both the
	in-range (0.2.0) and out-of-range (0.3.0) pinning paths.
	"""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	pkg_root = tmp_path / "shared_root"
	pkg_root.mkdir()

	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"mylib.*": [kid], "deplib.*": [kid]},
		"revoked": [],
	}))

	# Build both deplib versions (identical source, distinct package
	# versions).  Both land in the pkg-root so downstream scenarios can
	# reference either one via --dep.
	deplib_dir = tmp_path / "deplib_src"
	deplib_dir.mkdir()
	(deplib_dir / "deplib.drift").write_text(DEPLIB_SOURCE)
	for ver in ("0.2.0", "0.3.0"):
		deplib_dmp = tmp_path / f"deplib_{ver}.dmp"
		res = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc",
			 "-M", str(deplib_dir), str(deplib_dir / "deplib.drift"),
			 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
			 "--package-id", "deplib", "--package-version", ver,
			 "--package-target", "test-target",
			 "--emit-package", str(deplib_dmp), "--test-build-only"],
			cwd=ROOT, capture_output=True, text=True,
			timeout=sanitizer_timeout(120),
		)
		assert res.returncode == 0, (
			f"deplib {ver} build failed: {res.stderr[:300]}"
		)
		_sign_package(deplib_dmp, pkg_root, "deplib", ver, priv, pub_raw, kid, pub_b64)

	# Build mylib@1.0.0 with required_deps = [deplib "0.2"].  The
	# producer build consumes an exact deplib (0.2.0), but the
	# boundary-crossing metadata is the M.N range.
	mylib_dir = tmp_path / "mylib_src"
	mylib_dir.mkdir()
	(mylib_dir / "mylib.drift").write_text(MYLIB_SOURCE)
	mylib_dmp = tmp_path / "mylib.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "-M", str(mylib_dir), str(mylib_dir / "mylib.drift"),
		 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
		 "--package-id", "mylib", "--package-version", "1.0.0",
		 "--package-target", "test-target",
		 "--package-dep", "deplib=0.2",
		 "--package-root", str(pkg_root), "--dep", "deplib@0.2.0",
		 "--trust-store", str(trust_path),
		 "--emit-package", str(mylib_dmp), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"mylib build failed: {res.stderr[:300]}"
	_sign_package(mylib_dmp, pkg_root, "mylib", "1.0.0", priv, pub_raw, kid, pub_b64)

	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text(CONSUMER_SOURCE)
	return pkg_root, trust_path, consumer_dir


def _compile_consumer(consumer_dir: Path, stdlib: Path, pkg_root: Path,
                      trust_path: Path, out_bin: Path,
                      extra_deps: list[str]) -> subprocess.CompletedProcess:
	"""Run driftc on the consumer with the supplied --dep pins."""
	cmd = [sys.executable, "-m", "lang.driftc.driftc",
	       str(consumer_dir / "consumer.drift"),
	       "--stdlib-root", str(stdlib), "--target-word-bits", "64",
	       "--package-root", str(pkg_root),
	       "--trust-store", str(trust_path),
	       "--entry", "consumer::main",
	       "-o", str(out_bin)]
	for dep in extra_deps:
		cmd.extend(["--dep", dep])
	return subprocess.run(
		cmd, cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)


# ── Driftc exact-loader scenarios ────────────────────────────────────


def test_driftc_rejects_missing_transitive_pin(tmp_path: Path) -> None:
	"""Consumer that pins only the root package but not the transitive
	it pulls in must be rejected with a clear pointer to `drift prepare`.

	driftc is an exact loader; it cannot invent a deplib version from
	mylib's `required_deps` range.  The full transitive graph must
	reach it as exact `--dep` pins."""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	pkg_root, trust_path, consumer_dir = _stage_pkgroot(tmp_path, stdlib)

	res = _compile_consumer(
		consumer_dir, stdlib, pkg_root, trust_path,
		out_bin=tmp_path / "consumer_bin",
		extra_deps=["mylib@1.0.0"],
	)
	assert res.returncode != 0, (
		"consumer compile should have failed — deplib is in mylib's "
		"required_deps but not pinned via --dep, and driftc is an "
		"exact loader"
	)
	assert "no --dep is pinned for it" in res.stderr, (
		f"expected 'no --dep is pinned' diagnostic, got:\n{res.stderr[:500]}"
	)
	assert "drift prepare" in res.stderr, (
		f"diagnostic should point at `drift prepare`, got:\n{res.stderr[:500]}"
	)


def test_driftc_accepts_full_transitive_graph(tmp_path: Path) -> None:
	"""Consumer that pins the root package AND every transitive the
	package's `required_deps` name (each exact version within the
	declared range) compiles cleanly."""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	pkg_root, trust_path, consumer_dir = _stage_pkgroot(tmp_path, stdlib)

	out_bin = tmp_path / "consumer_bin"
	res = _compile_consumer(
		consumer_dir, stdlib, pkg_root, trust_path, out_bin,
		# mylib's published required_deps: deplib = "0.2".
		# 0.2.0 satisfies "0.2".
		extra_deps=["mylib@1.0.0", "deplib@0.2.0"],
	)
	assert res.returncode == 0, (
		f"consumer compile failed with full transitive graph; stderr:\n"
		f"{res.stderr[:500]}"
	)
	assert out_bin.exists(), "binary not produced"


def test_driftc_rejects_pin_outside_required_range(tmp_path: Path) -> None:
	"""Consumer that pins a transitive at an exact version OUTSIDE the
	range the producer published in `required_deps` must be rejected
	with a transitive-version-conflict diagnostic.

	Concretely: mylib's `required_deps` say `deplib = "0.2"`; the
	consumer pins `deplib@0.3.0`.  `0.3.0` is not within `"0.2"`, so
	driftc must refuse to load."""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	pkg_root, trust_path, consumer_dir = _stage_pkgroot(tmp_path, stdlib)

	res = _compile_consumer(
		consumer_dir, stdlib, pkg_root, trust_path,
		out_bin=tmp_path / "consumer_bin",
		extra_deps=["mylib@1.0.0", "deplib@0.3.0"],
	)
	assert res.returncode != 0, (
		"consumer compile should have failed — deplib@0.3.0 does not "
		"satisfy mylib's required_deps range \"0.2\""
	)
	assert "transitive dependency version conflict" in res.stderr, (
		f"expected transitive-conflict diagnostic, got:\n{res.stderr[:500]}"
	)
	assert "deplib" in res.stderr
	# The diagnostic body MUST frame this as "the provided pin does
	# not satisfy the declared range" and MUST NOT imply driftc is
	# resolving a satisfying version from the package roots.  Exact-
	# loader model — any "could not resolve" / "no matching version"
	# wording here would contradict the contract.
	assert "does not satisfy" in res.stderr, (
		f"conflict diagnostic must state that the pin 'does not "
		f"satisfy' the declared range — pinning the exact-loader "
		f"framing.  Got:\n{res.stderr[:500]}"
	)
	assert "exact loader" in res.stderr, (
		f"conflict diagnostic must mention driftc is an exact loader "
		f"so the user does not expect automatic fallback to a "
		f"different version.  Got:\n{res.stderr[:500]}"
	)
	for forbidden in ("could not resolve", "no matching version",
	                  "no version found", "failed to resolve"):
		assert forbidden not in res.stderr, (
			f"conflict diagnostic contains resolver-style wording "
			f"'{forbidden}' — driftc does not resolve.  Got:\n"
			f"{res.stderr[:500]}"
		)
