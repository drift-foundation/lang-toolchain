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


_TEST_SCI = "sha256:" + ("0" * 64)


def _sign_package(pkg_path: Path, pkg_root: Path, pkg_id: str, version: str,
                  priv_seed: bytes, pub_raw: bytes, kid: str, pub_b64: str,
                  dep_graph_entries: tuple = ()) -> None:
	"""Sign and stage a package into the standard pkg-root layout.

	v1 fixture: emits author + cert claim sidecars alongside the
	.dmp.  `dep_graph_entries` is `tuple[(pkg_id, version), ...]`
	naming each dep the cert claim should attest; the helper builds
	`DepGraphEntry` rows from the SAME sentinel kid + SCI used by
	the bootstrap (every fixture in this file shares one key).  An
	empty tuple means a leaf package.

	The consumer-side closure cover check (O3) rejects loads whose
	resolved closure contains a dep that's not in dep_graph -- so
	tests that publish a dependent package MUST populate this.
	"""
	from lang.driftc.packages.author_claim_v1 import make_author_claim_body, AuthorClaimBody
	from lang.driftc.packages.cert_claim_v1 import (
	make_cert_claim_body,
		CertClaimBody, CertSuite, DepGraphEntry, Toolchain,
	)
	from tools.drift_author.author_publish import (
		SignAuthorClaimOptions, sign_and_write_author_claim,
	)
	from tools.drift_deploy.cert_emit import (
		SignCertClaimOptions, sign_and_write_cert_claim,
	)

	dest = pkg_root / pkg_id / version
	dest.mkdir(parents=True, exist_ok=True)
	shutil.copy2(str(pkg_path), str(dest / f"{pkg_id}.dmp"))
	pkg_bytes = pkg_path.read_bytes()

	# Build dep_graph rows from the bootstrap kid (every fixture in
	# this file shares one key, so every dep's cert/author kid is
	# the same `kid`).  artifact_sha256 must match the actual
	# dep's `.dmp` bytes in pkg_root, otherwise the consumer's
	# cover check rejects on hash mismatch.
	dep_graph: list[DepGraphEntry] = []
	for dep_id, dep_ver in dep_graph_entries:
		dep_dmp = pkg_root / dep_id / dep_ver / f"{dep_id}.dmp"
		dep_bytes = dep_dmp.read_bytes()
		dep_graph.append(DepGraphEntry(
			package_id=dep_id, version=dep_ver,
			artifact_sha256="sha256:" + sha256(dep_bytes).hexdigest(),
			source_content_id=_TEST_SCI,
			author_kid=kid, cert_kid=kid,
			dep_kind="direct",
		))

	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=make_author_claim_body(
			artifact_kind="package", package_id=pkg_id, version=version,
			namespaces=(f"{pkg_id}.*",),
			source_content_id=_TEST_SCI,
			required_deps=(), 			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed,
		sidecar_dir=dest,
	))
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=make_cert_claim_body(
			artifact_kind="package", artifact_path=f"{pkg_id}.zdmp", package_id=pkg_id, version=version,
			artifact_sha256="sha256:" + sha256(pkg_bytes).hexdigest(),
			source_content_id=_TEST_SCI, target="test-target",
			toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="test"),
			dep_graph=tuple(dep_graph),
			cert_suite=CertSuite(id="drift-deploy/test", version="1.0",
				result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64)),
			run_id=f"test-{pkg_id}",
			run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=priv_seed,
		sidecar_dir=dest,
	))


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
	from lang.drift.crypto import compute_ed25519_kid

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	priv_seed = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	pkg_root = tmp_path / "shared_root"
	pkg_root.mkdir()

	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {
			"mylib.*": {"authors": [kid], "certifiers": [kid]},
			"deplib.*": {"authors": [kid], "certifiers": [kid]},
		},
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
			 "--source-content-id", _TEST_SCI,
			 "--emit-package", str(deplib_dmp), "--test-build-only"],
			cwd=ROOT, capture_output=True, text=True,
			timeout=sanitizer_timeout(120),
		)
		assert res.returncode == 0, (
			f"deplib {ver} build failed: {res.stderr[:300]}"
		)
		_sign_package(deplib_dmp, pkg_root, "deplib", ver, priv_seed, pub_raw, kid, pub_b64)

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
		 "--source-content-id", _TEST_SCI,
		 "--package-dep", "deplib=0.2",
		 "--package-root", str(pkg_root), "--dep", "deplib@0.2.0",
		 "--trust-store", str(trust_path),
		 "--emit-package", str(mylib_dmp), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"mylib build failed: {res.stderr[:300]}"
	# mylib depends on deplib@0.2.0 -- its cert claim must attest
	# that exact dep so the consumer-side closure cover (O3) accepts.
	_sign_package(mylib_dmp, pkg_root, "mylib", "1.0.0", priv_seed, pub_raw, kid, pub_b64,
		dep_graph_entries=(("deplib", "0.2.0"),))

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
	# v1's closure walker fires first (the cert-claim cover check
	# needs the full closure to be resolvable before it can run),
	# producing a different but equivalent diagnostic:
	# "declared required_deps entry 'deplib' has no --dep pin".
	# Same contract: the user gets pointed at `drift prepare` to
	# pin the missing transitive.
	assert "has no --dep pin" in res.stderr, (
		f"expected 'has no --dep pin' diagnostic, got:\n{res.stderr[:500]}"
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
	# In v1, the rejection fires via the cert-claim closure cover
	# check (O3): mylib's cert claim attested deplib@0.2.0 but the
	# consumer's closure contains deplib@0.3.0, so the cert's
	# `dep_graph` is missing the entry for 0.3.0.  Same contract
	# as the v0 "transitive dependency version conflict" diagnostic
	# (the consumer's pin doesn't match what was certified), but
	# expressed through the cryptographic gate rather than the
	# version-range checker.
	assert "deplib" in res.stderr and "0.3.0" in res.stderr, (
		f"expected diagnostic naming deplib@0.3.0, got:\n{res.stderr[:500]}"
	)
	assert "dep_graph missing entry" in res.stderr or "does not satisfy" in res.stderr, (
		f"conflict diagnostic must indicate the consumer's pin was "
		f"not in mylib's attested dep_graph.  Got:\n{res.stderr[:500]}"
	)
	# In v1 the diagnostic is phrased around the cert claim's
	# attestation set ("the certifier did not attest it") rather
	# than the v0 "exact loader" framing.  The semantic is the
	# same: driftc refuses to fall back to a different version
	# silently, just through the cryptographic gate.
	assert "exact loader" in res.stderr or "did not attest" in res.stderr or "certifier" in res.stderr, (
		f"conflict diagnostic must indicate driftc refuses to use a "
		f"version different from what was certified.  Got:\n{res.stderr[:500]}"
	)
	for forbidden in ("could not resolve", "no matching version",
	                  "no version found", "failed to resolve"):
		assert forbidden not in res.stderr, (
			f"conflict diagnostic contains resolver-style wording "
			f"'{forbidden}' — driftc does not resolve.  Got:\n"
			f"{res.stderr[:500]}"
		)


def test_sanity_check_runs_after_version_selection(tmp_path: Path) -> None:
	"""K Finding 3 regression: when flat package roots contain
	multiple versions of an allowlisted pkg, driftc's
	`required_deps` sanity check must only look at the
	`--dep`-selected version — NOT every discovered sibling.

	Concrete shape that used to fail:

	- ``deplib@0.1.0`` on disk, with empty ``required_deps``.
	- ``deplib@0.2.0`` on disk, with ``required_deps: [phantom.lib=1.0]``
	  (an extra transitive that the consumer never pins).
	- Consumer compiles with only ``--dep deplib@0.1.0``.

	Version selection picks deplib@0.1.0.  If the sanity pass ran
	BEFORE that selection (pre-fix), it iterated every loaded
	package — including the unselected deplib@0.2.0 — and
	incorrectly reported "no --dep is pinned for 'phantom.lib'"
	against a version that would then be discarded.  Post-fix, the
	sanity pass runs AFTER version selection and only sees the
	pin-selected deplib@0.1.0, whose ``required_deps`` is clean.
	"""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.drift.crypto import compute_ed25519_kid

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	priv_seed = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	pkg_root = tmp_path / "shared_root"
	pkg_root.mkdir()
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"deplib.*": {"authors": [kid], "certifiers": [kid]}},
		"revoked": [],
	}))

	deplib_dir = tmp_path / "deplib_src"
	deplib_dir.mkdir()
	(deplib_dir / "deplib.drift").write_text(DEPLIB_SOURCE)

	# deplib@0.1.0 — clean, no required_deps.
	deplib_010 = tmp_path / "deplib_0.1.0.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "-M", str(deplib_dir), str(deplib_dir / "deplib.drift"),
		 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
		 "--package-id", "deplib", "--package-version", "0.1.0",
		 "--package-target", "test-target",
		 "--source-content-id", _TEST_SCI,
		 "--emit-package", str(deplib_010), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"deplib@0.1.0 build failed: {res.stderr[:300]}"
	_sign_package(deplib_010, pkg_root, "deplib", "0.1.0", priv_seed, pub_raw, kid, pub_b64)

	# deplib@0.2.0 — carries a phantom required_dep that the
	# consumer never pins.  The producer's own emit-only build has
	# no loaded packages, so its own sanity pass trivially passes.
	deplib_020 = tmp_path / "deplib_0.2.0.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "-M", str(deplib_dir), str(deplib_dir / "deplib.drift"),
		 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
		 "--package-id", "deplib", "--package-version", "0.2.0",
		 "--package-target", "test-target",
		 "--source-content-id", _TEST_SCI,
		 "--package-dep", "phantom.lib=1.0",
		 "--emit-package", str(deplib_020), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"deplib@0.2.0 build failed: {res.stderr[:300]}"
	_sign_package(deplib_020, pkg_root, "deplib", "0.2.0", priv_seed, pub_raw, kid, pub_b64)

	# Consumer pins only deplib@0.1.0.  Both versions are in the
	# pkg root, so discover_package_files yields both.  The fix
	# makes driftc version-select first and then sanity-check, so
	# phantom.lib on the unselected 0.2.0 never trips anything.
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text(
		"module consumer;\n"
		"import std.core as core;\n"
		"import deplib;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval result = deplib.add_one(41);\n"
		"\tif result != 42 { return 1; }\n"
		"\treturn 0;\n"
		"}\n"
	)

	out_bin = tmp_path / "consumer_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 str(consumer_dir / "consumer.drift"),
		 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
		 "--package-root", str(pkg_root),
		 "--dep", "deplib@0.1.0",
		 "--trust-store", str(trust_path),
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, (
		f"consumer compile should succeed — deplib@0.1.0 is pinned and "
		f"has no required_deps.  If this fails with a 'no --dep is "
		f"pinned for phantom.lib' diagnostic, the sanity pass regressed "
		f"to iterating unselected duplicate versions.  stderr:\n"
		f"{res.stderr[:500]}"
	)
	# Belt-and-suspenders: if the sanity pass DID leak, this would be
	# the exact diagnostic text we'd see.
	assert "phantom.lib" not in res.stderr
