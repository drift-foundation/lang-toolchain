# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import host_word_bits, sanitizer_timeout
from lang.driftc import driftc


@pytest.fixture(scope="session", autouse=True)
def _inject_target_word_bits_for_tests() -> None:
	"""
	Driver tests default to host word size unless explicitly specified.

	This keeps production code strict about target layout while allowing tests
	to avoid passing --target-word-bits everywhere.
	"""
	driftc._TEST_TARGET_WORD_BITS = host_word_bits()


@pytest.fixture(scope="session")
def pex_scie_base(tmp_path_factory: pytest.TempPathFactory) -> Path:
	"""Shared scie extraction cache for PEX deploy tests.

	The scie launcher extracts its embedded Python interpreter (~440 MB)
	on first invocation.  Sharing the cache across all PEX tests within
	a worker avoids duplicating this extraction per test.
	"""
	return tmp_path_factory.mktemp("pex_scie_base")


@dataclass(frozen=True)
class StdlibPackage:
	"""Signed stdlib .dmp for consumer-path tests."""
	pkg_root: Path      # directory containing std/<version>/std.dmp
	trust_path: Path    # trust store JSON
	version: str        # e.g. "0.27.120"
	stdlib_root: Path   # source stdlib dir (for --stdlib-root passthrough guard)


@pytest.fixture(scope="session")
def stdlib_package(tmp_path_factory: pytest.TempPathFactory) -> StdlibPackage:
	"""Build a v1-signed stdlib package once per session.

	This exercises the same code path as the PEX/deploy pipeline:
	consumers use --package-root + --dep std@VERSION instead of
	--stdlib-root.  The type table state differs from source compilation
	and can expose bugs in has_drop, destructor_fns, and scope drop
	emission that are invisible to --stdlib-root tests.

	v1 trust layout produced here:
	  - `std/<version>/std.dmp` with `source_content_id` stamped into
	    its manifest;
	  - `std/<version>/std.author-claim` -- trust-v1 author claim binding
	    source identity;
	  - `std/<version>/std.cert-claim.<kid>.json` -- trust-v1 cert claim
	    binding artifact bytes + dep_graph + cert_suite;
	  - `trust.json` (v1 shape) trusting the same kid as both
	    author and certifier for `std.*` and `lang.*` (Foundation
	    bootstrap pattern from the audit doc -- the same kid can
	    play both roles for stdlib in dev).
	"""
	from lang.drift.crypto import compute_ed25519_kid
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.versions import DRIFTC_VERSION
	from lang.driftc.packages.source_content_id import compute_artifact_source_content_id

	tmp = tmp_path_factory.mktemp("stdlib_pkg")
	repo_root = Path(__file__).resolve().parents[3]
	stdlib_dir = repo_root / "stdlib"
	version = DRIFTC_VERSION

	# Compute SCI before driftc emits so we can pass it via
	# --source-content-id and have the manifest carry the stamp.
	# Source files are project-root-relative for the SCI computation
	# (matches the canonical layout the deploy pipeline uses).
	sources = sorted(p for p in stdlib_dir.rglob("*.drift"))
	assert sources, "no .drift files found under stdlib/"
	module_paths_rel = sorted(str(p.relative_to(repo_root)) for p in sources)
	sci = compute_artifact_source_content_id(
		kind="package",
		package_id="std",
		version=version,
		module_namespace="std",
		entry_module="std",
		module_paths=module_paths_rel,
		package_deps=[],
		native_deps=[],
		unsafe=False,
		asset_paths=[],
		source_root=repo_root,
	)

	# Build stdlib .dmp with SCI stamped into the manifest.
	dmp_path = tmp / "std.dmp"
	empty_stdlib = tmp / "_empty_stdlib"
	empty_stdlib.mkdir()

	env = dict(os.environ)
	env["PYTHONPATH"] = str(repo_root)
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--dev",
		"--stdlib-root", str(empty_stdlib),
		"-M", "stdlib",
	] + [str(p) for p in sources] + [
		"--package-id", "std",
		"--package-version", version,
		"--package-target", "drift-dev",
		"--source-content-id", sci,
		"--emit-package", str(dmp_path),
		"--test-build-only",
	]
	res = subprocess.run(cmd, env=env, cwd=str(repo_root),
		capture_output=True, text=True, timeout=sanitizer_timeout(120))
	assert res.returncode == 0, f"stdlib package build failed: {res.stderr[:500]}"
	assert dmp_path.exists(), "stdlib .dmp not produced"

	# Generate the Foundation-bootstrap key (same kid plays author and
	# certifier roles for stdlib in dev; production stdlib release
	# would use separate keys via `drift-author publish` +
	# `drift-deploy cert publish`).
	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	priv_seed_raw = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = dmp_path.read_bytes()
	artifact_sha256 = "sha256:" + sha256(pkg_bytes).hexdigest()

	# Standard layout: <root>/std/<version>/std.dmp + v1 sidecars.
	pkg_root = tmp / "lib"
	dest = pkg_root / "std" / version
	dest.mkdir(parents=True)
	import shutil
	shutil.copy2(str(dmp_path), str(dest / "std.dmp"))

	# trust-v1 author claim -- attests source identity for the release.
	from tools.drift_author.author_publish import (
		SignAuthorClaimOptions,
		sign_and_write_author_claim,
	)
	from lang.driftc.packages.author_claim_v1 import make_author_claim_body
	# Author claim binds every module namespace the stdlib package
	# exposes; the verifier checks that each module under load is
	# covered by the claim's `namespaces` list.  Pin both `std.*`
	# (the stdlib's own modules) and `lang.*` (toolchain-shipped
	# helpers also under the stdlib package).
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=make_author_claim_body(
			package_id="std",
			version=version,
			artifact_kind="package",
			namespaces=("std.*", "lang.*"),
			source_content_id=sci,
			required_deps=(),
			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed_raw,
		sidecar_dir=dest,
	))

	# v2 cert claim -- attests artifact bytes + (empty) dep_graph +
	# cert_suite result.  Same kid for the bootstrap.
	from tools.drift_deploy.cert_emit import (
		SignCertClaimOptions,
		sign_and_write_cert_claim,
	)
	from lang.driftc.packages.cert_claim_v1 import (
		CertSuite,
		Toolchain,
		make_cert_claim_body,
	)
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=make_cert_claim_body(
			package_id="std",
			version=version,
			artifact_kind="package",
			artifact_path="std.dmp",
			artifact_sha256=artifact_sha256,
			source_content_id=sci,
			target="drift-dev",
			toolchain=Toolchain(
				driftc_version=version, drift_rt_abi=1, driftc_commit="test",
			),
			dep_graph=(),  # stdlib has no upstream deps
			cert_suite=CertSuite(
				id="drift-deploy/test", version="1.0", result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64),
			),
			run_id="stdlib-test",
			run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=priv_seed_raw,
		sidecar_dir=dest,
	))

	# v1 trust store: same kid trusted as both author and certifier
	# for the namespaces the stdlib covers.
	trust_path = tmp / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {
			"std.*":  {"authors": [kid], "certifiers": [kid]},
			"lang.*": {"authors": [kid], "certifiers": [kid]},
		},
		"revoked": [],
	}))

	return StdlibPackage(
		pkg_root=pkg_root,
		trust_path=trust_path,
		version=version,
		stdlib_root=stdlib_dir,
	)
