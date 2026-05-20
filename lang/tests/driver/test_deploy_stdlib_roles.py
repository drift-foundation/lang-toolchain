# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Pin the v1 role-tagging contract for `build_and_install_stdlib`.

Two scenarios that must both work end-to-end without any code
path special-casing kid equality:

  1. **same-kid path** (today's compiler-team self-distribution
     scenario): the operator points the certifier key file at
     the SAME seed that Foundation used to sign the author
     claim.  The emitted `core_trust_v1.json` lists ONE key in
     `keys` (collapsed by kid) and both `authors`/`certifiers`
     namespace lists reference that single kid.  `compose_verify`
     accepts the deployed stdlib.

  2. **split-kid path** (the orch-certifier-separate scenario):
     the certifier key file points at a DIFFERENT seed than
     Foundation's author key.  The emitted `core_trust_v1.json`
     lists TWO distinct keys in `keys`, and the namespace
     entries route `authors` -> Foundation kid, `certifiers` ->
     orch kid.  `compose_verify` accepts the deployed stdlib.

These tests exercise the same `build_and_install_stdlib` code
path the production `tools/deploy/deploy.py` uses (no PEX/SCIE
required); they pin the role-tagging invariant at the source.
"""

from __future__ import annotations

import base64
import json
import os
from hashlib import sha256
from pathlib import Path

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from lang.drift.crypto import compute_ed25519_kid
from lang.driftc.packages.author_claim_v1 import AuthorClaimBody
from lang.driftc.packages.sidecar_naming import cert_claim_filename
from lang.driftc.packages.source_content_id import (
	compute_artifact_source_content_id,
)
from lang.driftc.packages.trust_v1 import load_trust_store_json
from lang.driftc.packages.verify_v1 import (
	PackageIdentity,
	verify_package_from_sidecars,
)
from tools.deploy.steps.stdlib import build_and_install_stdlib
from tools.drift_author.author_publish import (
	SignAuthorClaimOptions,
	sign_and_write_author_claim,
)

ROOT = Path(__file__).resolve().parents[3]
STDLIB_DIR = ROOT / "stdlib"
STD_VERSION = "0.0.0-test"


def _generate_seed_file(path: Path) -> tuple[bytes, bytes, str, str]:
	"""Generate an Ed25519 keypair, write the seed to `path` in the
	canonical base64-of-32-bytes format, return
	`(seed, pub_raw, kid, pub_b64)`.
	"""
	priv = Ed25519PrivateKey.generate()
	seed = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	pub_raw = priv.public_key().public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	path.write_text(base64.b64encode(seed).decode("ascii") + "\n", encoding="utf-8")
	return seed, pub_raw, kid, pub_b64


def _pre_publish_author_claim(
	scratch: Path, author_seed: bytes, *, version: str,
) -> tuple[Path, str]:
	"""Foundation-offline-signing stand-in: emits `std.author-claim`
	using the supplied seed.  Returns `(claim_path, sci)` -- the SCI
	is reused for `PackageIdentity.source_content_id` on the verify
	side (it must match the body's SCI, which is what
	`build_and_install_stdlib` stamps into the deployed package
	manifest after validating equality with its own freshly-computed
	SCI).  Mirrors `_pre_publish_stdlib_author_claim` in
	`test_deploy_runtime_readonly.py`.
	"""
	stdlib_files = sorted(STDLIB_DIR.rglob("*.drift"))
	module_paths_rel = sorted(str(p.relative_to(ROOT)) for p in stdlib_files)
	sci = compute_artifact_source_content_id(
		kind="library", package_id="std", version=version,
		module_namespace="std", entry_module="std",
		module_paths=module_paths_rel,
		package_deps=[], native_deps=[], unsafe=False, asset_paths=[],
		target_class="drift-dev", source_root=ROOT,
	)
	sidecar_dir = scratch / "foundation_author_signing"
	sidecar_dir.mkdir(parents=True, exist_ok=True)
	path = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=AuthorClaimBody(
			schema_version=1, package_id="std", version=version,
			namespaces=("std.*", "lang.*", "drift.*"),
			source_content_id=sci, required_deps=(),
			target_class="library", release_utc="2026-05-19T00:00:00Z",
		),
		seed32=author_seed, sidecar_dir=sidecar_dir,
	))
	return path, sci


def _deployed_stdlib_dmp_sha256(dist: Path) -> str:
	dmp = dist / "lib" / "stdlib" / "std.dmp"
	return "sha256:" + sha256(dmp.read_bytes()).hexdigest()


def _verify_deployed_stdlib(
	dist: Path, *, expected_artifact_sha: str, sci: str,
) -> None:
	"""Load core_trust_v1.json + the deployed sidecars and run
	`verify_package_from_sidecars` against the `std` module.  Used by
	both same-kid and split-kid tests to confirm the end-to-end
	role-routed verification accepts the deployed stdlib.

	`sci` is the source-content-id the author claim was signed under;
	it must match what's stamped into the deployed package manifest
	(`build_and_install_stdlib` rejects mismatch upstream, so reuse is
	safe here).  Per guardrail G1, `PackageIdentity.source_content_id`
	is the manifest-stamped value, not a re-derivation.
	"""
	core_trust_path = dist / "lib" / "compiler" / "lang" / "driftc" / "packages" / "core_trust_v1.json"
	trust = load_trust_store_json(core_trust_path)
	result = verify_package_from_sidecars(
		sidecar_dir=dist / "lib" / "stdlib",
		package_identity=PackageIdentity(
			package_id="std",
			version=STD_VERSION,
			source_content_id=sci,
			artifact_sha256=expected_artifact_sha,
		),
		module_id="std",
		trust=trust,
		resolved_closure=[],
	)
	assert result.ok, (
		f"deployed stdlib failed compose_verify against the emitted "
		f"core_trust_v1.json:\n  mode={result.mode}\n  "
		f"author_kid={result.author_kid}\n  "
		f"certifier_kid={result.certifier_kid}\n  reason={result.reason}"
	)


def _build_and_assert_trust_shape(
	tmp_path: Path,
	*,
	author_seed: bytes, author_pubkey_b64: str, author_kid: str,
	cert_seed_path: Path, expected_cert_kid: str,
	expected_same_kid: bool,
) -> Path:
	"""Drive `build_and_install_stdlib`, return the dist root.
	Asserts the emitted `core_trust_v1.json` shape and runs
	`verify_package_from_sidecars`.
	"""
	dist = tmp_path / "dist"
	dist.mkdir(parents=True, exist_ok=True)
	# `build_and_install_stdlib` expects bundle_compiler to have run
	# (so `dist/lib/compiler/lang/driftc/packages/` exists).  For the
	# trust-shape pin we don't actually need the bundled compiler --
	# generate_core_trust_store_v1 will mkdir the path itself.  Skip
	# the heavy PEX/bundle steps.
	stage = tmp_path / "stage"
	stage.mkdir(parents=True, exist_ok=True)

	# Pre-publish the author claim using the author seed.
	author_claim_path, sci = _pre_publish_author_claim(
		tmp_path, author_seed, version=STD_VERSION,
	)

	build_and_install_stdlib(
		ROOT, stage, dist, STD_VERSION,
		stdlib_author_claim_path=author_claim_path,
		stdlib_author_pubkey_b64=author_pubkey_b64,
		certifier_key_path=cert_seed_path,
	)

	# ── Inspect core_trust_v1.json structure ─────────────────────
	core_trust_path = dist / "lib" / "compiler" / "lang" / "driftc" / "packages" / "core_trust_v1.json"
	obj = json.loads(core_trust_path.read_text(encoding="utf-8"))
	assert obj["format"] == "drift-trust"
	assert obj["version"] == 1

	keys = obj["keys"]
	if expected_same_kid:
		# When the operator uses the same physical seed for both
		# roles, the kid-keyed `keys` dict collapses to a single
		# entry while the namespace entries still record the role
		# split.
		assert len(keys) == 1, (
			f"same-kid path: keys should collapse to a single entry, got {list(keys)}"
		)
		assert author_kid in keys
	else:
		# Split-kid path: two distinct keys.
		assert len(keys) == 2, (
			f"split-kid path: keys must have two distinct entries, got {list(keys)}"
		)
		assert author_kid in keys
		assert expected_cert_kid in keys
		assert author_kid != expected_cert_kid

	# Namespace entries always record (authors -> author_kid,
	# certifiers -> cert_kid) regardless of equality.
	for ns in ("std.*", "lang.*", "drift.*"):
		entry = obj["namespaces"][ns]
		assert entry["authors"] == [author_kid], (
			f"namespace {ns!r} authors must be [{author_kid!r}], got {entry['authors']!r}"
		)
		assert entry["certifiers"] == [expected_cert_kid], (
			f"namespace {ns!r} certifiers must be [{expected_cert_kid!r}], "
			f"got {entry['certifiers']!r}"
		)

	# ── End-to-end: load + verify the deployed stdlib ────────────
	# Confirm the cert claim sidecar's filename embeds the cert kid
	# (per O1 / `sidecar_naming.cert_claim_filename`).
	expected_cert_sidecar = (dist / "lib" / "stdlib"
		/ cert_claim_filename("std", expected_cert_kid))
	assert expected_cert_sidecar.is_file(), (
		f"expected cert claim sidecar not found at {expected_cert_sidecar}; "
		f"directory contains: {sorted((dist / 'lib' / 'stdlib').iterdir())}"
	)

	_verify_deployed_stdlib(
		dist, expected_artifact_sha=_deployed_stdlib_dmp_sha256(dist), sci=sci,
	)
	return dist


@pytest.mark.skipif(
	not (STDLIB_DIR / "std").is_dir(),
	reason="stdlib source tree not available",
)
def test_stdlib_deploy_same_kid_path(tmp_path: Path) -> None:
	"""**Same-kid**: certifier key file points at the same seed
	Foundation used to sign the author claim.  The deployed stdlib
	must load through `compose_verify`; `core_trust_v1.json.keys`
	must collapse to a single entry referenced from both role
	lists.  Pins today's compiler-team self-distribution path.
	"""
	# Generate ONE seed; use it for both author signing and as the
	# certifier_key_path argument to build_and_install_stdlib.
	shared_seed_path = tmp_path / "shared.seed"
	shared_seed, _, shared_kid, shared_pub_b64 = _generate_seed_file(shared_seed_path)

	# Set DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256 since
	# _emit_cert_claim_for_artifact requires it (separately documented
	# fail-closed behavior).  Use the no-evidence sentinel + opt-in
	# since this is a unit test for the role-routing path, not a
	# suite-evidence test.
	prev_evidence = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256")
	prev_no_evidence = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE")
	empty_sha = "sha256:" + sha256(b"").hexdigest()
	os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = empty_sha
	os.environ["DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE"] = "1"
	try:
		_build_and_assert_trust_shape(
			tmp_path,
			author_seed=shared_seed,
			author_pubkey_b64=shared_pub_b64,
			author_kid=shared_kid,
			cert_seed_path=shared_seed_path,
			expected_cert_kid=shared_kid,
			expected_same_kid=True,
		)
	finally:
		if prev_evidence is None:
			os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256", None)
		else:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = prev_evidence
		if prev_no_evidence is None:
			os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE", None)
		else:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE"] = prev_no_evidence


@pytest.mark.skipif(
	not (STDLIB_DIR / "std").is_dir(),
	reason="stdlib source tree not available",
)
def test_stdlib_deploy_split_kid_path(tmp_path: Path) -> None:
	"""**Split-kid**: certifier key file points at a DIFFERENT seed
	than Foundation's author key.  The deployed stdlib must load;
	`core_trust_v1.json.keys` must contain TWO distinct entries with
	the namespace role lists routing them independently.  Pins the
	orch-certifier-separate future path.
	"""
	# Foundation author seed -- pre-publishes the author claim.
	author_seed_path = tmp_path / "foundation_author.seed"
	author_seed, _, author_kid, author_pub_b64 = _generate_seed_file(author_seed_path)

	# Orch certifier seed -- DIFFERENT key, used by the deploy step.
	cert_seed_path = tmp_path / "orch_certifier.seed"
	_, _, cert_kid, _ = _generate_seed_file(cert_seed_path)
	assert author_kid != cert_kid, "test setup: keys must differ"

	prev_evidence = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256")
	prev_no_evidence = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE")
	empty_sha = "sha256:" + sha256(b"").hexdigest()
	os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = empty_sha
	os.environ["DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE"] = "1"
	try:
		_build_and_assert_trust_shape(
			tmp_path,
			author_seed=author_seed,
			author_pubkey_b64=author_pub_b64,
			author_kid=author_kid,
			cert_seed_path=cert_seed_path,
			expected_cert_kid=cert_kid,
			expected_same_kid=False,
		)
	finally:
		if prev_evidence is None:
			os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256", None)
		else:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = prev_evidence
		if prev_no_evidence is None:
			os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE", None)
		else:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE"] = prev_no_evidence
