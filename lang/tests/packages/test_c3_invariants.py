# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
End-to-end invariant tests for trust-v1 Slice 4 Part C (deploy
cutover).  Pins the four contracts K specified:

  1. External deps are NOT signed/authorized by the deploy signer.
     The cert claim's dep_graph carries each external dep's OWN
     author_kid + cert_kid (from the resolved lock), not the orch
     certifier's kid.  Defends against an orch operator silently
     re-attesting an upstream dep they don't control.

  2. Cert claims carry the FULL TRANSITIVE dep_graph.  When the
     consumer's resolved closure contains a transitive dep, the
     parent's cert claim must cover it via `check_dep_graph_covers`
     (O3).  A cert claim missing a transitive entry is rejected.

  3. `--require-certifier` / `--require-cert-suite` work through
     deploy output (O7 / O4).  The cert claim produced by the
     deploy pipeline must verify when those flags are pinned to
     the deploy's actual kid + suite id, and must reject when
     pinned to a different value.

  4. PushCoin/MariaDB and drift-web/net-tls shapes (sibling
     co-artifact + external dep) flow through the new model
     without staged_trust overlays.  The cert claim emitted for
     the app honestly attests both the sibling and the external
     dep.

Implementation notes: these tests exercise the real production
emit path (`drift_deploy.py::_emit_cert_claim_for_artifact`) with
hand-built `ResolvedDep` inputs and `staged_pkg_root` sidecars,
then load the resulting cert-claim file and run it through the
real `verify_v1.compose_verify`.  No subprocess or fixture
regeneration required, but the path under test is the same code
production deploys use.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from lang.drift.crypto import (
	compute_ed25519_kid,
	ed25519_sign_from_seed,
)
from lang.driftc.packages.author_claim_v1 import (
	AuthorClaimBody,
	RequiredDep,
	make_author_claim_body,
)
from lang.driftc.packages.cert_claim_v1 import (
	CertClaimBody,
	CertSuite,
	DepGraphEntry,
	ResolvedDep as CertResolvedDep,
	Toolchain,
	load_cert_claim_json,
	make_cert_claim_body,
)
from lang.driftc.packages.sidecar_naming import (
	author_claim_filename,
	cert_claim_filename,
	cert_claim_filename_prefix,
)
from lang.driftc.packages.trust_v1 import (
	NamespaceRoles,
	TrustStore,
	TrustedKey,
)
from lang.driftc.packages.verify_v1 import (
	PackageIdentity,
	compose_verify,
)
from tools.drift_author.author_publish import (
	SignAuthorClaimOptions,
	sign_and_write_author_claim,
)
from tools.drift_deploy.cert_emit import (
	SignCertClaimOptions,
	sign_and_write_cert_claim,
)
from tools.drift_deploy.drift_deploy import (
	CertSuiteOptions,
	_emit_cert_claim_for_artifact,
)
from tools.drift_deploy.provenance import CompilerInfo
from tools.drift_deploy.resolver import ResolvedDep as LockResolvedDep


# ── Helpers ───────────────────────────────────────────────────────


def _seed(byte: int) -> bytes:
	"""Deterministic seed: every byte is `byte`.  Different tests
	use different `byte` values to produce distinct kids."""
	return bytes([byte] * 32)


def _kid_for(seed: bytes) -> str:
	_, pub = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return compute_ed25519_kid(pub)


def _seed_file(tmp_path: Path, name: str, seed: bytes) -> Path:
	p = tmp_path / name
	p.write_text(base64.b64encode(seed).decode("ascii"), encoding="utf-8")
	return p


def _trust_store_for(
	*,
	authors_by_ns: dict[str, set[str]],
	certifiers_by_ns: dict[str, set[str]],
	keys: dict[str, bytes],
) -> TrustStore:
	"""Build a TrustStore with the given role mappings."""
	tk: dict[str, TrustedKey] = {}
	for kid, pubkey in keys.items():
		tk[kid] = TrustedKey(algo="ed25519", kid=kid, pubkey_raw=pubkey, label="test")
	all_ns = set(authors_by_ns) | set(certifiers_by_ns)
	roles: dict[str, NamespaceRoles] = {}
	for ns in all_ns:
		roles[ns] = NamespaceRoles(
			authors=frozenset(authors_by_ns.get(ns, set())),
			certifiers=frozenset(certifiers_by_ns.get(ns, set())),
		)
	return TrustStore(
		keys_by_kid=tk,
		roles_by_namespace=roles,
		revoked_kids=frozenset(),
	)


def _publish_dep_sidecars(
	staged_pkg_root: Path,
	*,
	pkg_id: str,
	version: str,
	sci: str,
	artifact_sha: str,
	author_seed: bytes,
	cert_seed: bytes,
	target: str = "linux-x86_64",
) -> tuple[str, str]:
	"""Stamp an external/co-artifact dep into `staged_pkg_root` with
	BOTH a trust-v1 author claim and a trust-v1 cert claim, mimicking what a
	previous deploy would have produced for this dep.  Returns
	`(author_kid, cert_kid)`.
	"""
	dep_dir = staged_pkg_root / pkg_id / version
	dep_dir.mkdir(parents=True, exist_ok=True)
	author_body = make_author_claim_body(
		package_id=pkg_id,
		version=version,
		artifact_kind="package",
		namespaces=(pkg_id,),
		source_content_id=sci,
		required_deps=(),
		release_utc="2026-05-19T00:00:00Z",
	)
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=author_body, seed32=author_seed, sidecar_dir=dep_dir,
	))
	cert_body = make_cert_claim_body(
		package_id=pkg_id,
		version=version,
		artifact_kind="package",
		artifact_path=f"{pkg_id}.zdmp",
		artifact_sha256=artifact_sha,
		source_content_id=sci,
		target=target,
		toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="dep"),
		dep_graph=(),
		cert_suite=CertSuite(
			id="upstream/release", version="1.0", result="pass",
			result_evidence_sha256="sha256:" + ("f" * 64),
		),
		run_id=f"run-{pkg_id}",
		run_started_utc="2026-05-19T00:00:00Z",
		evidence_sha256="sha256:" + ("0" * 64),
	)
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=cert_body, seed32=cert_seed, sidecar_dir=dep_dir,
	))
	return _kid_for(author_seed), _kid_for(cert_seed)


@dataclass(frozen=True)
class _Artifact:
	"""Subset of the real `Artifact` dataclass used by the test."""
	name: str
	version: str
	module_namespace: str
	package_deps: tuple

	@property
	def kind(self) -> str:
		# Canonical v2 kind: this fixture stages a `.dmp` package CONTAINER
		# (importable), not a runnable app binary — so "package", not "app".
		return "package"


@dataclass(frozen=True)
class _PackageDepRef:
	name: str
	version: str


def _emit_app_cert_claim(
	tmp_path: Path,
	*,
	app_name: str = "app",
	app_version: str = "1.0.0",
	app_sci: str = "sha256:" + ("a" * 64),
	app_artifact_sha: str = "sha256:" + ("b" * 64),
	app_target: str = "linux-x86_64",
	resolved_deps: dict[str, LockResolvedDep],
	direct_dep_ids: set[str] | None = None,
	staged_pkg_root: Path | None = None,
	deploy_cert_seed: bytes | None = None,
	provenance_bytes: bytes | None = None,
	omit_provenance: bool = False,
) -> tuple[Path, str]:
	"""Drive the real `_emit_cert_claim_for_artifact` over a
	synthetic fixture.  Returns `(cert_claim_path, deploy_cert_kid)`.

	A synthetic `.provenance.zst` is written next to the artifact by
	default so `_emit_cert_claim_for_artifact` can bind its sha256
	into `body.evidence_sha256` (the fail-closed contract).  Tests
	exercising the missing-provenance rejection path pass
	`omit_provenance=True`.
	"""
	if deploy_cert_seed is None:
		deploy_cert_seed = _seed(0x01)
	if staged_pkg_root is None:
		staged_pkg_root = tmp_path / "staged_pkg_root"
		staged_pkg_root.mkdir(parents=True, exist_ok=True)
	if direct_dep_ids is None:
		direct_dep_ids = set(resolved_deps.keys())
	artifact_path = tmp_path / "staged_install" / f"{app_name}.dmp"
	artifact_path.parent.mkdir(parents=True, exist_ok=True)
	# The cert claim emit writes the sidecar to artifact_path.parent;
	# a real .dmp doesn't need to exist for the emit path itself.
	artifact_path.write_bytes(b"placeholder dmp bytes")

	cert_key_path = _seed_file(tmp_path, "deploy.cert.seed", deploy_cert_seed)

	if omit_provenance:
		provenance_path = None
	else:
		provenance_path = artifact_path.parent / f"{app_name}.provenance.zst"
		if provenance_bytes is None:
			# Synthetic stand-in for the compressed provenance bundle.
			# Real builds emit a zstd-compressed JSON envelope here;
			# for binding-only tests the bytes content is opaque -- we
			# only care that its sha256 ends up in evidence_sha256.
			provenance_bytes = b"synthetic-provenance-bundle-bytes"
		provenance_path.write_bytes(provenance_bytes)

	# trust-v1 cert claim do not accept a synthetic default in the suite
	# evidence digest.  Tests must supply a real-shape sha256 for the
	# *suite's own* evidence artifact (separate from the provenance
	# bundle pinned via `evidence_sha256`).  Use a stable hash so the
	# emitted cert claim is reproducible across runs.
	import hashlib as _hl
	_suite_evidence = "sha256:" + _hl.sha256(b"test-suite-evidence-marker").hexdigest()
	# Honor env-driven cert-suite identity overrides (`monkeypatch.setenv`
	# in callers) — some tests inject a custom suite id to exercise
	# `--require-cert-suite` policy at verify time.  The id/version/result
	# fields here mirror the env-fallback shape of
	# `_resolve_cert_suite_options`; this test helper is intentionally
	# the env path so the helper itself stays explicit about what it
	# signs.
	cert_suite_options = CertSuiteOptions(
		id=os.environ.get("DRIFT_DEPLOY_CERT_SUITE_ID", "drift-deploy/v1"),
		version=os.environ.get("DRIFT_DEPLOY_CERT_SUITE_VERSION", "1.0"),
		result=os.environ.get("DRIFT_DEPLOY_CERT_SUITE_RESULT", "pass"),
		result_evidence_sha256=_suite_evidence,
		no_evidence_sentinel=False,
	)
	cert_claim_path = _emit_cert_claim_for_artifact(
		artifact_path,
		cert_key=cert_key_path,
		package_id=app_name,
		artifact_kind="package",  # `.dmp` container fixture → package kind
		package_version=app_version,
		target=app_target,
		compiler_info=CompilerInfo(version="0.31.0", abi=1, commit="test"),
		source_content_id=app_sci,
		artifact_sha256=app_artifact_sha,
		resolved_deps=resolved_deps,
		direct_dep_ids=direct_dep_ids,
		staged_pkg_root=staged_pkg_root,
		provenance_path=provenance_path,
		cert_suite_options=cert_suite_options,
	)
	return cert_claim_path, _kid_for(deploy_cert_seed)


# ── Invariant 1: external deps are not signed by the deploy signer ─


def test_external_dep_kids_come_from_lock_not_deploy_signer(tmp_path: Path) -> None:
	"""When the deploy emits a cert claim for `app` whose external
	dep `net.tls` was published by some upstream party, the
	resulting `dep_graph` entry for `net.tls` MUST carry that
	upstream party's kids -- NOT the deploy's own certifier kid.

	Catches a regression where the deploy silently re-attests
	upstream deps by stamping its own kid into the dep_graph: a
	consumer would then trust an upstream that the deploy operator
	never actually got authorization from."""
	# Three distinct kids in play.
	deploy_cert_seed = _seed(0x11)
	upstream_author_seed = _seed(0x22)
	upstream_cert_seed = _seed(0x33)
	upstream_author_kid = _kid_for(upstream_author_seed)
	upstream_cert_kid = _kid_for(upstream_cert_seed)
	deploy_cert_kid = _kid_for(deploy_cert_seed)
	# Sanity: all three are distinct.
	assert len({upstream_author_kid, upstream_cert_kid, deploy_cert_kid}) == 3

	# An external dep already resolved in the consumer's lock.
	# Its kids belong to the upstream maintainer, not the deploy.
	resolved_deps = {
		"net.tls": LockResolvedDep(
			version="0.5.0",
			sha256="sha256:" + ("d" * 64),
			dep_type="direct",
			package_id="net.tls",
			author_key=upstream_cert_kid,            # cert kid (v4 naming)
			source_content_id="sha256:" + ("e" * 64),
			source_attestation_key=upstream_author_kid,  # author kid (v4 naming)
		),
	}

	cert_claim_path, emit_deploy_cert_kid = _emit_app_cert_claim(
		tmp_path,
		resolved_deps=resolved_deps,
		deploy_cert_seed=deploy_cert_seed,
	)
	assert emit_deploy_cert_kid == deploy_cert_kid

	cc = load_cert_claim_json(cert_claim_path.read_text(encoding="utf-8"))
	# The CLAIM is signed by the deploy.
	assert len(cc.signatures) == 1
	assert cc.signatures[0].kid == deploy_cert_kid

	# But the dep_graph carries the UPSTREAM kids, not the deploy's.
	assert len(cc.body.dep_graph) == 1
	dep = cc.body.dep_graph[0]
	assert dep.package_id == "net.tls"
	assert dep.author_kid == upstream_author_kid
	assert dep.cert_kid == upstream_cert_kid
	# Explicit anti-regression: the deploy's kid does NOT appear in
	# the external dep's identity slots.
	assert deploy_cert_kid not in (dep.author_kid, dep.cert_kid)


# ── Invariant 2: cert claim covers the full transitive dep_graph ───


def test_full_transitive_dep_graph_covers_consumer_closure(tmp_path: Path) -> None:
	"""When the consumer's resolved closure for `app` contains
	both a direct dep `net.tls` and a transitive dep `acme.crypto`,
	the emitted cert claim's dep_graph must include BOTH (so
	`check_dep_graph_covers` accepts at consumer verify time).
	The transitive dep_graph entry must carry the transitive dep's
	own identity from the lock.

	Beyond inspecting `dep_graph` shape, the test ALSO runs the
	emitted claim through the real `compose_verify` with a closure
	that contains BOTH deps -- this proves the integration boundary
	(the deploy emits exactly what the consumer accepts), not just
	that the serialization round-trips."""
	direct_author = _kid_for(_seed(0x40))
	direct_cert = _kid_for(_seed(0x41))
	trans_author = _kid_for(_seed(0x50))
	trans_cert = _kid_for(_seed(0x51))

	resolved_deps = {
		"net.tls": LockResolvedDep(
			version="0.5.0", sha256="sha256:" + ("d" * 64),
			dep_type="direct", package_id="net.tls",
			author_key=direct_cert,
			source_content_id="sha256:" + ("e" * 64),
			source_attestation_key=direct_author,
		),
		"acme.crypto": LockResolvedDep(
			version="1.2.0", sha256="sha256:" + ("4" * 64),
			dep_type="transitive", package_id="acme.crypto",
			author_key=trans_cert,
			source_content_id="sha256:" + ("5" * 64),
			source_attestation_key=trans_author,
		),
	}

	# Author claim + trust store + identity for the app, prepared so
	# the same emit produces a claim we can put through compose_verify.
	app_sci = "sha256:" + ("a" * 64)
	app_artifact = "sha256:" + ("b" * 64)
	author_seed = _seed(0x42)
	deploy_cert_seed = _seed(0x01)  # default seed used inside the helper
	author_kid = _kid_for(author_seed)
	deploy_cert_kid = _kid_for(deploy_cert_seed)
	_, author_pub = ed25519_sign_from_seed(priv_seed32=author_seed, message=b"")
	_, deploy_cert_pub = ed25519_sign_from_seed(priv_seed32=deploy_cert_seed, message=b"")
	# Pre-publish the author claim in the staged_install dir before
	# the cert emit runs.  This mimics what the real deploy flow
	# does via `_attach_author_claim_to_artifact` before cert sign.
	sidecar_dir = tmp_path / "staged_install"
	sidecar_dir.mkdir(parents=True, exist_ok=True)
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=make_author_claim_body(
			package_id="app", version="1.0.0", artifact_kind="package",
			namespaces=("app",), source_content_id=app_sci,
			required_deps=(
				RequiredDep(name="net.tls", version_range="^0.5"),
			),
			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=author_seed,
		sidecar_dir=sidecar_dir,
	))

	cert_claim_path, _ = _emit_app_cert_claim(
		tmp_path,
		app_sci=app_sci,
		app_artifact_sha=app_artifact,
		resolved_deps=resolved_deps,
		direct_dep_ids={"net.tls"},  # acme.crypto is transitive
		deploy_cert_seed=deploy_cert_seed,
	)
	cc = load_cert_claim_json(cert_claim_path.read_text(encoding="utf-8"))

	# ── Shape assertions (dep_graph carries the right rows) ────
	by_id = {e.package_id: e for e in cc.body.dep_graph}
	assert set(by_id) == {"net.tls", "acme.crypto"}
	assert by_id["net.tls"].dep_kind == "direct"
	assert by_id["acme.crypto"].dep_kind == "transitive"
	assert by_id["net.tls"].source_content_id == "sha256:" + ("e" * 64)
	assert by_id["acme.crypto"].source_content_id == "sha256:" + ("5" * 64)
	assert by_id["acme.crypto"].author_kid == trans_author
	assert by_id["acme.crypto"].cert_kid == trans_cert

	# ── Positive end-to-end: compose_verify must ACCEPT the
	# emitted claim against the consumer's actual resolved closure.
	# If `dep_graph` failed to include `acme.crypto`, this would
	# reject; the symmetric negative test
	# `test_cert_claim_missing_a_transitive_entry_is_rejected_by_verifier`
	# confirms the cover check is the teeth.  Together they pin
	# the integration boundary in both directions.
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	author_claim = load_author_claim_json(
		(sidecar_dir / author_claim_filename("app")).read_text(encoding="utf-8")
	)
	trust = _trust_store_for(
		authors_by_ns={"app": {author_kid}},
		certifiers_by_ns={"app": {deploy_cert_kid}},
		keys={author_kid: author_pub, deploy_cert_kid: deploy_cert_pub},
	)
	consumer_closure = [
		CertResolvedDep(
			package_id="net.tls", version="0.5.0",
			artifact_sha256="sha256:" + ("d" * 64),
			source_content_id="sha256:" + ("e" * 64),
		),
		CertResolvedDep(
			package_id="acme.crypto", version="1.2.0",
			artifact_sha256="sha256:" + ("4" * 64),
			source_content_id="sha256:" + ("5" * 64),
		),
	]
	result = compose_verify(
		author_claim=author_claim,
		cert_claims=[cc],
		package_identity=PackageIdentity(
			package_id="app", version="1.0.0",
			source_content_id=app_sci, artifact_sha256=app_artifact,
		),
		module_id="app",
		trust=trust,
		resolved_closure=consumer_closure,
	)
	assert result.ok, f"verify rejected emitted claim: {result.reason}"
	assert result.mode == "certifier-shortcut"
	assert result.certifier_kid == deploy_cert_kid
	assert result.author_kid == author_kid


def test_cert_claim_missing_a_transitive_entry_is_rejected_by_verifier() -> None:
	"""If a cert claim's dep_graph is INCOMPLETE (the deploy
	signed a graph that omits a real transitive dep), the
	consumer's `check_dep_graph_covers` -- via `compose_verify` --
	must reject.  Validates that the slice-3 cover check is the
	teeth behind invariant 2."""
	# Build a cert claim whose body's dep_graph only contains
	# `net.tls` -- but the consumer's closure also has `acme.crypto`.
	author_seed = _seed(0x60)
	cert_seed = _seed(0x61)
	author_kid = _kid_for(author_seed)
	cert_kid = _kid_for(cert_seed)
	_, author_pub = ed25519_sign_from_seed(priv_seed32=author_seed, message=b"")
	_, cert_pub = ed25519_sign_from_seed(priv_seed32=cert_seed, message=b"")

	app_sci = "sha256:" + ("a" * 64)
	app_artifact = "sha256:" + ("b" * 64)
	dep_graph = (
		DepGraphEntry(
			package_id="net.tls", version="0.5.0",
			artifact_sha256="sha256:" + ("d" * 64),
			source_content_id="sha256:" + ("e" * 64),
			author_kid="ed25519:net-author",
			cert_kid="ed25519:net-cert",
			dep_kind="direct",
		),
		# acme.crypto INTENTIONALLY OMITTED -- the cert claim
		# attests an incomplete graph.
	)
	from lang.driftc.packages.cert_claim_v1 import make_cert_claim
	from lang.driftc.packages.author_claim_v1 import make_author_claim
	cert_body = make_cert_claim_body(
		package_id="app", version="1.0.0",
		artifact_kind="package", artifact_path="app.zdmp",
		artifact_sha256=app_artifact, source_content_id=app_sci,
		target="linux-x86_64",
		toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit=""),
		dep_graph=dep_graph,
		cert_suite=CertSuite(
			id="drift-deploy/v1", version="1.0", result="pass",
			result_evidence_sha256="sha256:" + ("f" * 64),
		),
		run_id="run-x", run_started_utc="2026-05-19T00:00:00Z",
		evidence_sha256="sha256:" + ("0" * 64),
	)
	cert_claim = make_cert_claim(cert_body, cert_seed)
	author_body = make_author_claim_body(
		package_id="app", version="1.0.0", artifact_kind="package",
		namespaces=("app",), source_content_id=app_sci,
		required_deps=(), 		release_utc="2026-05-19T00:00:00Z",
	)
	author_claim = make_author_claim(author_body, author_seed)

	# Consumer's actual resolved closure has BOTH deps -- the cert
	# claim above omits acme.crypto, so the cover check must fail.
	consumer_closure = [
		CertResolvedDep(
			package_id="net.tls", version="0.5.0",
			artifact_sha256="sha256:" + ("d" * 64),
			source_content_id="sha256:" + ("e" * 64),
		),
		CertResolvedDep(
			package_id="acme.crypto", version="1.2.0",
			artifact_sha256="sha256:" + ("4" * 64),
			source_content_id="sha256:" + ("5" * 64),
		),
	]
	trust = _trust_store_for(
		authors_by_ns={"app": {author_kid}},
		certifiers_by_ns={"app": {cert_kid}},
		keys={author_kid: author_pub, cert_kid: cert_pub},
	)
	result = compose_verify(
		author_claim=author_claim,
		cert_claims=[cert_claim],
		package_identity=PackageIdentity(
			package_id="app", version="1.0.0",
			source_content_id=app_sci, artifact_sha256=app_artifact,
		),
		module_id="app",
		trust=trust,
		resolved_closure=consumer_closure,
	)
	assert not result.ok
	assert "dep_graph" in result.reason or "acme.crypto" in result.reason


# ── Invariant 3: --require-certifier / --require-cert-suite work ───


def _emit_and_verify_app(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	*,
	cert_suite_id: str = "drift-deploy/v1",
	require_certifier: str | None = None,
	require_cert_suite: str | None = None,
):
	"""Emit a v1 app cert claim + author claim, then run
	`compose_verify` with the policy flags under test.  Returns
	(VerifyResult, deploy_cert_kid).

	`monkeypatch` is used for env-var mutation so the test does not
	erase a `DRIFT_DEPLOY_CERT_SUITE_ID` value that may legitimately
	be set in the surrounding test environment (CI, dev shell, etc.).
	"""
	# Author lives in user-space; emit via the same code path as the
	# real release flow.
	author_seed = _seed(0x71)
	deploy_cert_seed = _seed(0x72)
	author_kid = _kid_for(author_seed)
	deploy_cert_kid = _kid_for(deploy_cert_seed)
	_, author_pub = ed25519_sign_from_seed(priv_seed32=author_seed, message=b"")
	_, deploy_cert_pub = ed25519_sign_from_seed(priv_seed32=deploy_cert_seed, message=b"")

	app_sci = "sha256:" + ("a" * 64)
	app_artifact = "sha256:" + ("b" * 64)

	# Pre-publish the author claim in the staged install dir so the
	# verify path can load both claims.
	sidecar_dir = tmp_path / "staged_install"
	sidecar_dir.mkdir(parents=True, exist_ok=True)
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=make_author_claim_body(
			package_id="app", version="1.0.0", artifact_kind="package",
			namespaces=("app",), source_content_id=app_sci,
			required_deps=(), 			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=author_seed,
		sidecar_dir=sidecar_dir,
	))

	# Cert suite id flows in via env var so we can test
	# `--require-cert-suite` against a deploy-side default.
	# `monkeypatch.setenv` auto-restores the prior value (set or
	# unset) when the test exits.
	monkeypatch.setenv("DRIFT_DEPLOY_CERT_SUITE_ID", cert_suite_id)
	cert_claim_path, _ = _emit_app_cert_claim(
		tmp_path,
		app_sci=app_sci,
		app_artifact_sha=app_artifact,
		resolved_deps={},
		direct_dep_ids=set(),
		deploy_cert_seed=deploy_cert_seed,
	)

	cert_claim = load_cert_claim_json(cert_claim_path.read_text(encoding="utf-8"))
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	author_claim = load_author_claim_json(
		(sidecar_dir / author_claim_filename("app")).read_text(encoding="utf-8")
	)

	trust = _trust_store_for(
		authors_by_ns={"app": {author_kid}},
		certifiers_by_ns={"app": {deploy_cert_kid}},
		keys={author_kid: author_pub, deploy_cert_kid: deploy_cert_pub},
	)
	result = compose_verify(
		author_claim=author_claim,
		cert_claims=[cert_claim],
		package_identity=PackageIdentity(
			package_id="app", version="1.0.0",
			source_content_id=app_sci, artifact_sha256=app_artifact,
		),
		module_id="app",
		trust=trust,
		resolved_closure=[],
		require_certifier=require_certifier,
		require_cert_suite=require_cert_suite,
	)
	return result, deploy_cert_kid


def test_require_certifier_matching_kid_accepts(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""When `--require-certifier` is pinned to the deploy's actual
	kid, verify accepts and reports `mode='certifier-shortcut'`
	with `certifier_kid` set to that kid."""
	# Compute the deploy's kid up front (same seed as the helper
	# uses) so we can pin the flag in a single emit pass.
	deploy_kid = _kid_for(_seed(0x72))
	result, emitted_kid = _emit_and_verify_app(
		tmp_path, monkeypatch, require_certifier=deploy_kid,
	)
	assert emitted_kid == deploy_kid
	assert result.ok, f"unexpected: {result.reason}"
	assert result.mode == "certifier-shortcut"
	assert result.certifier_kid == deploy_kid


def test_require_certifier_wrong_kid_rejects(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`--require-certifier ed25519:somebody-else` rejects even
	when everything else verifies.  Pins O7."""
	result, _ = _emit_and_verify_app(
		tmp_path, monkeypatch,
		require_certifier="ed25519:wrong-kid-not-the-deploy",
	)
	assert not result.ok
	assert "require" in result.reason.lower() or "certifier" in result.reason.lower()


def test_require_cert_suite_matching_id_accepts(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`--require-cert-suite <id>` matching the deploy's emitted
	cert_suite.id accepts.  Pins O4."""
	result, _ = _emit_and_verify_app(
		tmp_path, monkeypatch,
		cert_suite_id="anthropic/release-gate",
		require_cert_suite="anthropic/release-gate",
	)
	assert result.ok, f"unexpected: {result.reason}"


def test_require_cert_suite_wrong_id_rejects(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Wrong --require-cert-suite is rejected -- a smoke-only
	signature must not satisfy a release-gate requirement."""
	result, _ = _emit_and_verify_app(
		tmp_path, monkeypatch,
		cert_suite_id="anthropic/smoke",
		require_cert_suite="anthropic/release-gate",
	)
	assert not result.ok
	assert "cert_suite" in result.reason or "suite" in result.reason.lower()


# ── Invariant 4: sibling co-artifact + external dep shape ──────────


def test_mariadb_shape_sibling_plus_external_dep(tmp_path: Path) -> None:
	"""PushCoin/MariaDB / drift-web/net-tls shape: the app depends
	on a sibling library (`mariadb.rpc.managed`) co-deployed in
	this run, which depends on an external library (`net.tls`)
	already published upstream.

	The trust-v1 cert claim for the app must contain dep_graph entries
	for BOTH:
	  - the SIBLING (identity comes from the just-emitted sibling
	    sidecars in staged_pkg_root -- the K HIGH #8 fix);
	  - the EXTERNAL dep (identity comes from the resolved lock).

	No staged_trust overlay is involved -- the consumer's trust
	store + the sibling/external sidecars are sufficient.
	"""
	staged_pkg_root = tmp_path / "staged_pkg_root"

	# Sibling co-artifact: just-emitted sidecars in staged_pkg_root.
	sibling_author_kid, sibling_cert_kid = _publish_dep_sidecars(
		staged_pkg_root,
		pkg_id="mariadb.rpc.managed",
		version="2.0.0",
		sci="sha256:" + ("7" * 64),
		artifact_sha="sha256:" + ("8" * 64),
		author_seed=_seed(0x80),
		cert_seed=_seed(0x81),
	)

	# External dep: identity comes from the consumer's lock.
	external_author_kid = _kid_for(_seed(0x90))
	external_cert_kid = _kid_for(_seed(0x91))

	resolved_deps = {
		"mariadb.rpc.managed": LockResolvedDep(
			version="2.0.0", sha256="",  # co-artifact: empty in lock
			dep_type="co-artifact", package_id="mariadb.rpc.managed",
			author_key="", source_content_id="", source_attestation_key="",
		),
		"net.tls": LockResolvedDep(
			version="0.5.0", sha256="sha256:" + ("d" * 64),
			dep_type="transitive", package_id="net.tls",
			author_key=external_cert_kid,
			source_content_id="sha256:" + ("e" * 64),
			source_attestation_key=external_author_kid,
		),
	}

	# Prepare app author claim + identity + trust before emit so we
	# can put the emitted cert claim through compose_verify
	# end-to-end.  This pins the integration boundary, not just
	# serialization: the deploy emits something the consumer will
	# accept against the same closure.
	app_sci = "sha256:" + ("a" * 64)
	app_artifact = "sha256:" + ("b" * 64)
	author_seed = _seed(0x82)
	deploy_cert_seed = _seed(0x01)  # default seed used inside the helper
	author_kid = _kid_for(author_seed)
	deploy_cert_kid = _kid_for(deploy_cert_seed)
	_, author_pub = ed25519_sign_from_seed(priv_seed32=author_seed, message=b"")
	_, deploy_cert_pub = ed25519_sign_from_seed(priv_seed32=deploy_cert_seed, message=b"")
	sidecar_dir = tmp_path / "staged_install"
	sidecar_dir.mkdir(parents=True, exist_ok=True)
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=make_author_claim_body(
			package_id="app", version="1.0.0", artifact_kind="package",
			namespaces=("app",), source_content_id=app_sci,
			required_deps=(
				RequiredDep(name="mariadb.rpc.managed", version_range="^2"),
			),
			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=author_seed,
		sidecar_dir=sidecar_dir,
	))

	cert_claim_path, _ = _emit_app_cert_claim(
		tmp_path,
		app_sci=app_sci,
		app_artifact_sha=app_artifact,
		resolved_deps=resolved_deps,
		direct_dep_ids={"mariadb.rpc.managed"},  # net.tls pulled in transitively
		staged_pkg_root=staged_pkg_root,
		deploy_cert_seed=deploy_cert_seed,
	)
	cc = load_cert_claim_json(cert_claim_path.read_text(encoding="utf-8"))
	by_id = {e.package_id: e for e in cc.body.dep_graph}

	# Both deps present.
	assert set(by_id) == {"mariadb.rpc.managed", "net.tls"}

	# Sibling identity came from the just-emitted sidecars.
	sibling = by_id["mariadb.rpc.managed"]
	assert sibling.source_content_id == "sha256:" + ("7" * 64)
	assert sibling.artifact_sha256 == "sha256:" + ("8" * 64)
	assert sibling.author_kid == sibling_author_kid
	assert sibling.cert_kid == sibling_cert_kid
	assert sibling.dep_kind == "direct"

	# External dep identity came from the lock.
	external = by_id["net.tls"]
	assert external.source_content_id == "sha256:" + ("e" * 64)
	assert external.author_kid == external_author_kid
	assert external.cert_kid == external_cert_kid
	assert external.dep_kind == "transitive"

	# Sanity: the deploy's own kid does NOT appear in either dep's
	# identity slots.  No staged_trust overlay snuck the orch kid
	# into the dep_graph.
	for entry in cc.body.dep_graph:
		assert deploy_cert_kid not in (entry.author_kid, entry.cert_kid), (
			f"deploy kid leaked into dep_graph entry {entry.package_id!r}"
		)

	# ── Positive end-to-end: compose_verify must ACCEPT the
	# emitted claim against a consumer closure that mirrors what a
	# real consumer would resolve at load time (sibling + external
	# transitive).  Pins the integration boundary for the
	# mariadb-shape topology.
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	author_claim = load_author_claim_json(
		(sidecar_dir / author_claim_filename("app")).read_text(encoding="utf-8")
	)
	trust = _trust_store_for(
		authors_by_ns={"app": {author_kid}},
		certifiers_by_ns={"app": {deploy_cert_kid}},
		keys={author_kid: author_pub, deploy_cert_kid: deploy_cert_pub},
	)
	consumer_closure = [
		CertResolvedDep(
			package_id="mariadb.rpc.managed", version="2.0.0",
			artifact_sha256="sha256:" + ("8" * 64),
			source_content_id="sha256:" + ("7" * 64),
		),
		CertResolvedDep(
			package_id="net.tls", version="0.5.0",
			artifact_sha256="sha256:" + ("d" * 64),
			source_content_id="sha256:" + ("e" * 64),
		),
	]
	result = compose_verify(
		author_claim=author_claim,
		cert_claims=[cc],
		package_identity=PackageIdentity(
			package_id="app", version="1.0.0",
			source_content_id=app_sci, artifact_sha256=app_artifact,
		),
		module_id="app",
		trust=trust,
		resolved_closure=consumer_closure,
	)
	assert result.ok, f"verify rejected mariadb-shape claim: {result.reason}"
	assert result.mode == "certifier-shortcut"
	assert result.certifier_kid == deploy_cert_kid
	assert result.author_kid == author_kid


def test_co_artifact_missing_sidecar_fails_closed(tmp_path: Path) -> None:
	"""Negative control for invariant 4: if the sibling artifact's
	sidecars are NOT present in staged_pkg_root (topo-sort regression
	or failed sibling build), the cert emit must fail closed --
	NOT silently drop the sibling from the dep_graph.  Pins the
	HIGH #8 fix end-to-end."""
	from tools.drift_deploy.drift_deploy import DeployError

	staged_pkg_root = tmp_path / "staged_pkg_root"
	staged_pkg_root.mkdir(parents=True, exist_ok=True)
	# `mariadb.rpc.managed` is a co-artifact but NO sidecars at all.

	resolved_deps = {
		"mariadb.rpc.managed": LockResolvedDep(
			version="2.0.0", sha256="",
			dep_type="co-artifact", package_id="mariadb.rpc.managed",
			author_key="", source_content_id="", source_attestation_key="",
		),
	}

	with pytest.raises(DeployError) as exc:
		_emit_app_cert_claim(
			tmp_path,
			resolved_deps=resolved_deps,
			direct_dep_ids={"mariadb.rpc.managed"},
			staged_pkg_root=staged_pkg_root,
		)
	msg = str(exc.value)
	assert "co-artifact" in msg
	assert "mariadb.rpc.managed" in msg
	# The diagnostic must say WHY we refuse to sign, not just that
	# we did.
	assert "closure cover" in msg or "topological" in msg or "failed" in msg


# ── Invariant: provenance bundle is cryptographically bound by cert ───


def test_evidence_sha256_pins_provenance_bundle_bytes(tmp_path: Path) -> None:
	"""The cert claim's `body.evidence_sha256` MUST equal
	sha256(`<pkg>.provenance.zst` on-disk bytes).

	Without this binding, a hostile mirror could swap the unsigned
	provenance bundle and an operator inspecting it would read
	attacker-chosen evidence under the certifier's name.  The cert
	claim is the only signed artifact, so the only honest path is
	for it to pin the provenance bundle's bytes.
	"""
	import hashlib
	bundle_bytes = b"deploy-evidence-bundle-v1-payload-bytes"
	resolved_deps = {
		"net.tls": LockResolvedDep(
			version="0.5.0",
			sha256="sha256:" + ("d" * 64),
			dep_type="direct",
			package_id="net.tls",
			author_key="ed25519:" + ("c" * 22),
			source_content_id="sha256:" + ("e" * 64),
			source_attestation_key="ed25519:" + ("a" * 22),
		),
	}
	cert_claim_path, _ = _emit_app_cert_claim(
		tmp_path,
		resolved_deps=resolved_deps,
		provenance_bytes=bundle_bytes,
	)
	cc = load_cert_claim_json(cert_claim_path.read_text(encoding="utf-8"))
	expected = "sha256:" + hashlib.sha256(bundle_bytes).hexdigest()
	assert cc.body.evidence_sha256 == expected, (
		f"cert claim must pin provenance bundle bytes via evidence_sha256:\n"
		f"  expected: {expected}\n"
		f"  got:      {cc.body.evidence_sha256}"
	)


def test_evidence_sha256_changes_when_provenance_bytes_change(tmp_path: Path) -> None:
	"""Different provenance bundle bytes MUST produce a different
	`evidence_sha256` on the cert claim.  A hostile mirror that
	substitutes the bundle without rewriting the cert claim must
	fail an inspector's sha256 comparison.
	"""
	resolved_deps = {
		"net.tls": LockResolvedDep(
			version="0.5.0",
			sha256="sha256:" + ("d" * 64),
			dep_type="direct",
			package_id="net.tls",
			author_key="ed25519:" + ("c" * 22),
			source_content_id="sha256:" + ("e" * 64),
			source_attestation_key="ed25519:" + ("a" * 22),
		),
	}
	cc_a_path, _ = _emit_app_cert_claim(
		tmp_path / "run-a",
		resolved_deps=resolved_deps,
		provenance_bytes=b"bundle-A",
	)
	cc_b_path, _ = _emit_app_cert_claim(
		tmp_path / "run-b",
		resolved_deps=resolved_deps,
		provenance_bytes=b"bundle-B-differs",
	)
	cc_a = load_cert_claim_json(cc_a_path.read_text(encoding="utf-8"))
	cc_b = load_cert_claim_json(cc_b_path.read_text(encoding="utf-8"))
	assert cc_a.body.evidence_sha256 != cc_b.body.evidence_sha256, (
		"provenance binding leaked: different bundle bytes produced "
		"the same evidence_sha256"
	)


def test_missing_provenance_bundle_fails_closed(tmp_path: Path) -> None:
	"""When the deploy does not produce a provenance bundle, cert
	claim emission MUST fail rather than fall back to a sentinel
	digest.  The cert suite asserts that the certifier observed
	evidence; an empty / synthetic sentinel would let the cert
	claim attest evidence that doesn't exist on disk."""
	from tools.drift_deploy.drift_deploy import DeployError
	resolved_deps = {
		"net.tls": LockResolvedDep(
			version="0.5.0",
			sha256="sha256:" + ("d" * 64),
			dep_type="direct",
			package_id="net.tls",
			author_key="ed25519:" + ("c" * 22),
			source_content_id="sha256:" + ("e" * 64),
			source_attestation_key="ed25519:" + ("a" * 22),
		),
	}
	with pytest.raises(DeployError, match="provenance bundle is missing"):
		_emit_app_cert_claim(
			tmp_path,
			resolved_deps=resolved_deps,
			omit_provenance=True,
		)


# ── Cert-suite options resolver ───────────────────────────────────
#
# These tests target `_resolve_cert_suite_options(args)` directly --
# the choke point where CLI flags > env vars > hard-error resolution
# happens.  They replace earlier tests that drove the env-var
# fallback through `_emit_cert_claim_for_artifact`; the contract is
# unchanged (missing evidence -> DeployError; empty-sentinel without
# opt-in -> DeployError; empty-sentinel with opt-in -> accepted)
# but the resolver function is the right place to pin it now that
# the resolution happens once up front in `_run_impl` rather than
# inside the per-artifact emitter.


def _fake_deploy_args(**overrides) -> Any:
	"""Build a synthetic `argparse.Namespace`-ish object for the
	cert-suite resolver.  Defaults to all-None CLI flags; tests
	override individual fields to exercise specific paths.
	"""
	import types
	defaults = dict(
		cert_suite_id=None,
		cert_suite_version=None,
		cert_suite_result=None,
		cert_suite_evidence_sha256=None,
		cert_suite_no_evidence=False,
	)
	defaults.update(overrides)
	return types.SimpleNamespace(**defaults)


def test_resolver_missing_suite_evidence_fails_closed() -> None:
	"""Neither CLI flag nor env var: hard DeployError.  v1 does not
	accept a synthetic default in a signed body.
	"""
	from tools.drift_deploy.drift_deploy import (
		DeployError, _resolve_cert_suite_options,
	)
	prev = os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256", None)
	try:
		with pytest.raises(DeployError, match=r"cert.*evidence|--cert-suite-evidence-sha256"):
			_resolve_cert_suite_options(_fake_deploy_args())
	finally:
		if prev is not None:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = prev


def test_resolver_env_empty_sentinel_requires_explicit_opt_in() -> None:
	"""Env empty-sha WITHOUT `DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1`
	is a misconfiguration -- refuse rather than silently sign a
	zero-hash evidence digest.  (CLI users pass
	`--cert-suite-no-evidence` instead; the legacy env-pair shape
	is only honored when no CLI flag is present.)
	"""
	import hashlib as _hl
	from tools.drift_deploy.drift_deploy import (
		DeployError, _resolve_cert_suite_options,
	)
	empty_sha = "sha256:" + _hl.sha256(b"").hexdigest()
	prev_evidence = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256")
	prev_opt_in = os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE", None)
	os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = empty_sha
	try:
		with pytest.raises(DeployError, match="DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE"):
			_resolve_cert_suite_options(_fake_deploy_args())
	finally:
		if prev_evidence is None:
			os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256", None)
		else:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = prev_evidence
		if prev_opt_in is not None:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE"] = prev_opt_in


def test_resolver_env_empty_sentinel_with_opt_in_accepted() -> None:
	"""Env empty-sha + `DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1` -> the
	resolved options carry the empty-bytes hash and the
	`no_evidence_sentinel` flag (the emitter then prints the visible
	stderr warning).
	"""
	import hashlib as _hl
	from tools.drift_deploy.drift_deploy import _resolve_cert_suite_options
	empty_sha = "sha256:" + _hl.sha256(b"").hexdigest()
	prev_evidence = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256")
	prev_opt_in = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE")
	os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = empty_sha
	os.environ["DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE"] = "1"
	try:
		opts = _resolve_cert_suite_options(_fake_deploy_args())
		assert opts.result_evidence_sha256 == empty_sha
		assert opts.no_evidence_sentinel is True
	finally:
		if prev_evidence is None:
			os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256", None)
		else:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = prev_evidence
		if prev_opt_in is None:
			os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE", None)
		else:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE"] = prev_opt_in


def test_resolver_cli_evidence_sha256_wins_over_env() -> None:
	"""When both `--cert-suite-evidence-sha256` and the env var are
	set, the CLI flag wins.  Signed certifier metadata belongs in
	the deploy command, not in ambient shell state.
	"""
	import hashlib as _hl
	from tools.drift_deploy.drift_deploy import _resolve_cert_suite_options
	cli_sha = "sha256:" + _hl.sha256(b"cli-evidence").hexdigest()
	env_sha = "sha256:" + _hl.sha256(b"env-evidence").hexdigest()
	prev_env = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256")
	os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = env_sha
	try:
		opts = _resolve_cert_suite_options(_fake_deploy_args(
			cert_suite_evidence_sha256=cli_sha,
		))
		assert opts.result_evidence_sha256 == cli_sha, (
			f"CLI flag must win over env; got {opts.result_evidence_sha256!r}"
		)
		assert opts.no_evidence_sentinel is False
	finally:
		if prev_env is None:
			os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256", None)
		else:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = prev_env


def test_resolver_cli_no_evidence_is_self_contained_opt_in() -> None:
	"""`--cert-suite-no-evidence` IS the explicit opt-in.  Unlike the
	legacy env-pair shape, the CLI form does NOT require a separate
	`DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1` to fire.
	"""
	import hashlib as _hl
	from tools.drift_deploy.drift_deploy import _resolve_cert_suite_options
	empty_sha = "sha256:" + _hl.sha256(b"").hexdigest()
	# Ensure NO env var is set so we can verify the CLI flag stands alone.
	prev_evidence = os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256", None)
	prev_opt_in = os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE", None)
	try:
		opts = _resolve_cert_suite_options(_fake_deploy_args(
			cert_suite_no_evidence=True,
		))
		assert opts.result_evidence_sha256 == empty_sha
		assert opts.no_evidence_sentinel is True
	finally:
		if prev_evidence is not None:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = prev_evidence
		if prev_opt_in is not None:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE"] = prev_opt_in


def test_resolver_cli_id_version_result_override_env() -> None:
	"""CLI metadata flags (--cert-suite-id, --cert-suite-version,
	--cert-suite-result) override the matching env vars and the
	hardcoded defaults.
	"""
	import hashlib as _hl
	from tools.drift_deploy.drift_deploy import _resolve_cert_suite_options
	cli_sha = "sha256:" + _hl.sha256(b"x").hexdigest()
	prev = {
		k: os.environ.get(k)
		for k in (
			"DRIFT_DEPLOY_CERT_SUITE_ID",
			"DRIFT_DEPLOY_CERT_SUITE_VERSION",
			"DRIFT_DEPLOY_CERT_SUITE_RESULT",
		)
	}
	os.environ["DRIFT_DEPLOY_CERT_SUITE_ID"] = "env-id"
	os.environ["DRIFT_DEPLOY_CERT_SUITE_VERSION"] = "9.9"
	os.environ["DRIFT_DEPLOY_CERT_SUITE_RESULT"] = "fail"
	try:
		opts = _resolve_cert_suite_options(_fake_deploy_args(
			cert_suite_id="cli-id",
			cert_suite_version="2.0",
			cert_suite_result="pass",
			cert_suite_evidence_sha256=cli_sha,
		))
		assert opts.id == "cli-id"
		assert opts.version == "2.0"
		assert opts.result == "pass"
	finally:
		for k, v in prev.items():
			if v is None:
				os.environ.pop(k, None)
			else:
				os.environ[k] = v


def test_resolver_argparse_enforces_mutual_exclusivity() -> None:
	"""Argparse rejects passing both --cert-suite-evidence-sha256 AND
	--cert-suite-no-evidence on the same command line (the resolver
	itself never sees a colliding pair).
	"""
	from tools.drift_deploy.drift_deploy import build_arg_parser
	parser = build_arg_parser()
	with pytest.raises(SystemExit):
		parser.parse_args([
			"--dest", "/tmp/x",  # drift-tmp-root-audit: allow negative-test arg, never written
			"--cert-suite-evidence-sha256", "sha256:" + ("a" * 64),
			"--cert-suite-no-evidence",
		])


# ── Value validation (fail fast before any artifact build) ───────


def test_resolver_argparse_rejects_invalid_result_choice() -> None:
	"""Argparse enforces choices=('pass','fail') on --cert-suite-result.
	A typo / arbitrary string is rejected at parse time."""
	from tools.drift_deploy.drift_deploy import build_arg_parser
	parser = build_arg_parser()
	with pytest.raises(SystemExit):
		parser.parse_args([
			"--dest", "/tmp/x",  # drift-tmp-root-audit: allow negative-test arg, never written
			"--cert-suite-result", "passsss",
			"--cert-suite-evidence-sha256", "sha256:" + ("a" * 64),
		])


def test_resolver_rejects_invalid_env_result_value() -> None:
	"""Argparse only guards CLI; the env-fallback path can still inject
	an arbitrary string into `DRIFT_DEPLOY_CERT_SUITE_RESULT`.  The
	resolver MUST reject those before signing.
	"""
	from tools.drift_deploy.drift_deploy import (
		DeployError, _resolve_cert_suite_options,
	)
	import hashlib as _hl
	prev_evidence = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256")
	prev_result = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_RESULT")
	os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = (
		"sha256:" + _hl.sha256(b"e").hexdigest()
	)
	os.environ["DRIFT_DEPLOY_CERT_SUITE_RESULT"] = "PASS"  # wrong case
	try:
		with pytest.raises(DeployError, match=r"cert suite result.*not valid|`pass`.*`fail`"):
			_resolve_cert_suite_options(_fake_deploy_args())
	finally:
		if prev_evidence is None:
			os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256", None)
		else:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = prev_evidence
		if prev_result is None:
			os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_RESULT", None)
		else:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_RESULT"] = prev_result


def test_resolver_rejects_malformed_cli_evidence_sha256() -> None:
	"""Malformed `--cert-suite-evidence-sha256` (wrong shape, missing
	prefix, non-hex, wrong length) must be caught at deploy startup,
	not buried inside cert claim body assembly.  The diagnostic must
	name the CLI flag so the operator sees where the bad value came
	from.
	"""
	from tools.drift_deploy.drift_deploy import (
		DeployError, _resolve_cert_suite_options,
	)
	for bad in (
		"not-a-sha",                       # no prefix
		"sha256:abc",                      # too short
		"md5:" + ("a" * 64),               # wrong algo
		"sha256:" + ("g" * 64),            # non-hex chars
		"sha256:" + ("A" * 64),            # uppercase rejected
	):
		with pytest.raises(DeployError, match=r"--cert-suite-evidence-sha256.*malformed"):
			_resolve_cert_suite_options(_fake_deploy_args(
				cert_suite_evidence_sha256=bad,
			))


def test_resolver_rejects_malformed_env_evidence_sha256() -> None:
	"""Same fail-fast contract for the env-fallback path: a
	malformed `$DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256` is rejected
	before signing, and the diagnostic names the env var so the
	operator sees the source.
	"""
	from tools.drift_deploy.drift_deploy import (
		DeployError, _resolve_cert_suite_options,
	)
	prev = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256")
	try:
		os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = "not-a-sha"
		with pytest.raises(
			DeployError,
			match=r"DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256.*malformed",
		):
			_resolve_cert_suite_options(_fake_deploy_args())
	finally:
		if prev is None:
			os.environ.pop("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256", None)
		else:
			os.environ["DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256"] = prev
