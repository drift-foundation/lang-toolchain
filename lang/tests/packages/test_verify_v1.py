# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for `lang.driftc.packages.verify_v1` — the trust-v1
composition verifier.

Covers the full proof matrix from `work/drift-trust-model-audit/plan.md`
§12 plus the per-gate failure surface introduced by the
composition itself.  Adversarial fixtures: every documented
rejection path has at least one test; the happy path has both
certifier-shortcut and self-verify variants; multi-cert-claim
behavior pins "first matching wins" and aggregated rejection
diagnostics.

Slice 4 part 1 of the trust-v1 implementation.  Part 2 (v0
deletion sweep + caller migration) lands after this is green.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lang.drift.crypto import (
	compute_ed25519_kid,
	ed25519_sign_from_seed,
)
from lang.driftc.packages.author_claim_v1 import (
	make_author_claim_body,
	AuthorClaim,
	AuthorClaimBody,
	RequiredDep,
	dump_author_claim_json,
	make_author_claim,
)
from lang.driftc.packages.cert_claim_v1 import (
	make_cert_claim_body,
	CertClaim,
	CertClaimBody,
	CertSuite,
	DepGraphEntry,
	ResolvedDep,
	Toolchain,
	cert_claim_filename,
	dump_cert_claim_json,
	make_cert_claim,
)
from lang.driftc.packages.trust_v1 import (
	NamespaceRoles,
	TrustStore,
	TrustedKey,
)
from lang.driftc.packages.sidecar_naming import (
	author_claim_filename,
	cert_claim_filename as shared_cert_claim_filename,
	filename_escape_segment,
)
from lang.driftc.packages.verify_v1 import (
	PackageIdentity,
	VerifyResult,
	compose_verify,
	discover_author_claim_path,
	discover_cert_claim_paths,
	load_sidecar_claims,
	verify_package_from_sidecars,
)


# ── Test fixture builders ───────────────────────────────────────────


_PKG_ID = "singular"
_PKG_VERSION = "0.3.0"
_NAMESPACE = "singular.*"
_MODULE = "singular.api"
_TARGET = "drift-linux-x86_64"
_SCI = "sha256:" + ("a" * 64)
_ARTIFACT_SHA = "sha256:" + ("d" * 64)
_EVIDENCE_SHA = "sha256:" + ("e" * 64)
_RUN_EVIDENCE_SHA = "sha256:" + ("f" * 64)


def _seed(label: str) -> bytes:
	return label.encode().ljust(32, b"_")[:32]


def _kid_for_seed(seed: bytes) -> str:
	_sig, pubkey_raw = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return compute_ed25519_kid(pubkey_raw)


def _pubkey_for_seed(seed: bytes) -> bytes:
	_sig, pubkey_raw = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return pubkey_raw


def _trust_with(
	*,
	author_kid: str | None = None,
	author_pubkey: bytes | None = None,
	certifier_kid: str | None = None,
	certifier_pubkey: bytes | None = None,
	namespace: str = _NAMESPACE,
	revoked: frozenset[str] = frozenset(),
) -> TrustStore:
	"""Build a trust store with optional author + certifier kids
	for one namespace."""
	keys: dict[str, TrustedKey] = {}
	if author_kid is not None:
		assert author_pubkey is not None
		keys[author_kid] = TrustedKey(
			algo="ed25519", kid=author_kid, pubkey_raw=author_pubkey, label="",
		)
	if certifier_kid is not None and certifier_kid != author_kid:
		assert certifier_pubkey is not None
		keys[certifier_kid] = TrustedKey(
			algo="ed25519", kid=certifier_kid, pubkey_raw=certifier_pubkey, label="",
		)
	authors = frozenset({author_kid}) if author_kid else frozenset()
	certifiers = frozenset({certifier_kid}) if certifier_kid else frozenset()
	return TrustStore(
		keys_by_kid=keys,
		roles_by_namespace={
			namespace: NamespaceRoles(authors=authors, certifiers=certifiers),
		},
		revoked_kids=revoked,
	)


def _author_body(
	*,
	package_id: str = _PKG_ID,
	version: str = _PKG_VERSION,
	source_content_id: str = _SCI,
	namespaces: tuple[str, ...] = (_NAMESPACE,),
) -> AuthorClaimBody:
	return make_author_claim_body(
		package_id=package_id,
		version=version,
		artifact_kind="package",
		namespaces=namespaces,
		source_content_id=source_content_id,
		required_deps=(),
		release_utc="2026-05-18T12:00:00Z",
	)


def _cert_body(
	*,
	package_id: str = _PKG_ID,
	version: str = _PKG_VERSION,
	artifact_sha256: str = _ARTIFACT_SHA,
	source_content_id: str = _SCI,
	dep_graph: tuple[DepGraphEntry, ...] = (),
	cert_suite_id: str = "drift.foundation/default",
	cert_suite_result: str = "pass",
) -> CertClaimBody:
	return make_cert_claim_body(
		package_id=package_id,
		version=version,
		artifact_kind="package",
		artifact_path=f"{package_id}.zdmp",
		artifact_sha256=artifact_sha256,
		source_content_id=source_content_id,
		target=_TARGET,
		toolchain=Toolchain(driftc_version="0.31.108", drift_rt_abi=14, driftc_commit="abc"),
		dep_graph=dep_graph,
		cert_suite=CertSuite(
			id=cert_suite_id, version="1.0.0",
			result=cert_suite_result,
			result_evidence_sha256=_EVIDENCE_SHA,
		),
		run_id="run-001",
		run_started_utc="2026-05-18T12:00:00Z",
		evidence_sha256=_RUN_EVIDENCE_SHA,
	)


def _identity(
	*,
	package_id: str = _PKG_ID,
	version: str = _PKG_VERSION,
	source_content_id: str = _SCI,
	artifact_sha256: str = _ARTIFACT_SHA,
) -> PackageIdentity:
	return PackageIdentity(
		package_id=package_id,
		version=version,
		source_content_id=source_content_id,
		artifact_sha256=artifact_sha256,
	)


def _happy_setup():
	"""Author kid + certifier kid (distinct) both trusted for the
	package's namespace; matching author claim + cert claim;
	matching package identity stamps.  Returns:
	  (author_claim, cert_claim, trust, identity, author_kid, cert_kid)
	"""
	a_seed = _seed("pushcoin_author")
	c_seed = _seed("pushcoin_cert")
	a_kid = _kid_for_seed(a_seed)
	c_kid = _kid_for_seed(c_seed)
	trust = _trust_with(
		author_kid=a_kid, author_pubkey=_pubkey_for_seed(a_seed),
		certifier_kid=c_kid, certifier_pubkey=_pubkey_for_seed(c_seed),
	)
	author_claim = make_author_claim(_author_body(), a_seed)
	cert_claim = make_cert_claim(_cert_body(), c_seed)
	identity = _identity()
	return author_claim, cert_claim, trust, identity, a_kid, c_kid


# ── Happy paths (proof matrix T1, T2, T13) ─────────────────────────


def test_certifier_shortcut_happy_path() -> None:
	"""Plan §12 T1: author claim + cert claim, both signed by trusted
	kids; SCIs match; artifact_sha matches; cert_suite.result == pass;
	dep_graph empty (no deps to cover).  ACCEPT via certifier-shortcut."""
	ac, cc, trust, identity, a_kid, c_kid = _happy_setup()
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=identity, module_id=_MODULE,
		trust=trust, resolved_closure=[],
	)
	assert res.ok is True
	assert res.mode == "certifier-shortcut"
	assert res.author_kid == a_kid
	assert res.certifier_kid == c_kid


def test_author_as_distributor_same_kid_both_roles() -> None:
	"""Plan §12 T2: same kid trusted in BOTH author and certifier
	role lists.  The kid signs both an author claim and a cert claim.
	No special verifier path -- this is certifier-shortcut with one
	kid in two roles."""
	seed = _seed("solo_publisher")
	kid = _kid_for_seed(seed)
	pubkey = _pubkey_for_seed(seed)
	trust = TrustStore(
		keys_by_kid={kid: TrustedKey(algo="ed25519", kid=kid, pubkey_raw=pubkey, label="")},
		roles_by_namespace={
			_NAMESPACE: NamespaceRoles(authors=frozenset({kid}), certifiers=frozenset({kid})),
		},
		revoked_kids=frozenset(),
	)
	ac = make_author_claim(_author_body(), seed)
	cc = make_cert_claim(_cert_body(), seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
	)
	assert res.ok is True
	assert res.mode == "certifier-shortcut"
	assert res.author_kid == kid
	assert res.certifier_kid == kid


def test_pushcoin_singular_e2e() -> None:
	"""Plan §12 T13: PushCoin author claim for `singular.*`; trusted
	pushcoin_author for `singular.*` AND trusted foundation_* for
	`mariadb.*`; loading singular pulls in a mariadb.rpc dep entry in
	the dep_graph.  ACCEPT; pushcoin kids never appear in mariadb
	verifications because the dep is not the module under verification
	here.  The dep_graph closure check confirms the certifier
	attested mariadb-rpc with the foundation kids."""
	pc_a_seed = _seed("pc_author")
	pc_c_seed = _seed("pc_certifier")
	pc_a_kid = _kid_for_seed(pc_a_seed)
	pc_c_kid = _kid_for_seed(pc_c_seed)
	# Mariadb dep info (the actual cert/author kids are committed in
	# the dep_graph by the pushcoin certifier; they're informational
	# from THIS module's point of view).
	mariadb_dep = DepGraphEntry(
		package_id="mariadb-rpc", version="0.5.0",
		artifact_sha256="sha256:" + ("1" * 64),
		source_content_id="sha256:" + ("2" * 64),
		author_kid="ed25519:foundation_author",
		cert_kid="ed25519:foundation_cert",
		dep_kind="direct",
	)
	trust = _trust_with(
		author_kid=pc_a_kid, author_pubkey=_pubkey_for_seed(pc_a_seed),
		certifier_kid=pc_c_kid, certifier_pubkey=_pubkey_for_seed(pc_c_seed),
		namespace="singular.*",
	)
	ac = make_author_claim(_author_body(namespaces=("singular",)), pc_a_seed)
	cc = make_cert_claim(_cert_body(dep_graph=(mariadb_dep,)), pc_c_seed)
	# Consumer's resolved closure DOES contain the mariadb dep.
	closure = [ResolvedDep(
		package_id="mariadb-rpc", version="0.5.0",
		artifact_sha256="sha256:" + ("1" * 64),
		source_content_id="sha256:" + ("2" * 64),
	)]
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id="singular",
		trust=trust, resolved_closure=closure,
	)
	assert res.ok is True
	assert res.mode == "certifier-shortcut"


# ── Self-verify path (T10, T11) ────────────────────────────────────


def test_self_verify_happy_path() -> None:
	"""T10: consumer rebuilt; recomputed SCI matches author claim's.
	No cert claim needed."""
	a_seed = _seed("pushcoin_author")
	a_kid = _kid_for_seed(a_seed)
	trust = _trust_with(author_kid=a_kid, author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
	)
	assert res.ok is True
	assert res.mode == "self-verify"
	assert res.author_kid == a_kid
	assert res.certifier_kid is None


def test_self_verify_sci_mismatch_rejected() -> None:
	"""T11: consumer rebuilt but rebuilt SCI differs from author's
	attested SCI.  The local source is NOT the source the author
	released."""
	a_seed = _seed("pushcoin_author")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True,
		self_verify_sci="sha256:" + ("9" * 64),  # different from author's SCI
	)
	assert res.ok is False
	assert res.mode == "rejected"
	assert "self-verify SCI mismatch" in res.reason


def test_self_verify_requires_sci_argument() -> None:
	"""self_verify=True without self_verify_sci is a caller bug; the
	verifier reports it clearly."""
	a_seed = _seed("a")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=None,
	)
	assert res.ok is False
	assert "self_verify_sci is None" in res.reason


# ── Author claim failure modes ─────────────────────────────────────


def test_missing_author_claim_rejected() -> None:
	"""An author claim is REQUIRED for every load.  No claim → reject."""
	res = compose_verify(
		author_claim=None, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=_trust_with(), resolved_closure=[],
	)
	assert res.ok is False
	assert "no .author-claim sidecar found" in res.reason


def test_author_claim_sci_mismatches_package_stamp() -> None:
	"""G1 stamp comparison: author claim says SCI=A; package manifest
	stamps SCI=B.  The on-disk package is not the source release the
	author claim describes."""
	a_seed = _seed("a")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(source_content_id="sha256:" + ("a" * 64)), a_seed)
	identity = _identity(source_content_id="sha256:" + ("b" * 64))
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=identity, module_id=_MODULE,
		trust=trust, resolved_closure=[], self_verify=False,
	)
	assert res.ok is False
	assert "does not match package manifest stamp" in res.reason


def test_author_claim_package_id_mismatch() -> None:
	"""Adversarial: an author claim for package 'evil' must not pass
	verification when the caller expects 'singular'."""
	a_seed = _seed("a")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(package_id="evil"), a_seed)
	# package_identity stamps "singular".
	identity = _identity(package_id="singular")
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=identity, module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
	)
	assert res.ok is False
	assert "author claim rejected" in res.reason
	assert "package_id" in res.reason


def test_author_claim_version_mismatch() -> None:
	"""Adversarial: author claim for v0.2.0 with package on disk
	stamped v0.3.0 → reject."""
	a_seed = _seed("a")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(version="0.2.0"), a_seed)
	identity = _identity(version="0.3.0")
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=identity, module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
	)
	assert res.ok is False
	assert "version" in res.reason


def test_author_claim_namespace_mismatch() -> None:
	"""T6: author claims namespace `other.*`; module is `singular.api`."""
	a_seed = _seed("a")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(namespaces=("other.*",)), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id="singular.api",
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
	)
	assert res.ok is False
	assert "does not cover module" in res.reason


def test_author_claim_untrusted_kid() -> None:
	"""T5: author claim signed by a kid the trust store doesn't know."""
	a_seed = _seed("untrusted_author")
	# Trust store grants a DIFFERENT kid for the namespace.
	trusted_seed = _seed("trusted_other")
	trust = _trust_with(
		author_kid=_kid_for_seed(trusted_seed),
		author_pubkey=_pubkey_for_seed(trusted_seed),
	)
	ac = make_author_claim(_author_body(), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
	)
	assert res.ok is False
	assert "no signature on author claim verifies" in res.reason


def test_author_revoked() -> None:
	"""T8: author kid is in trust store but revoked → reject."""
	a_seed = _seed("a")
	a_kid = _kid_for_seed(a_seed)
	trust = _trust_with(
		author_kid=a_kid, author_pubkey=_pubkey_for_seed(a_seed),
		revoked=frozenset({a_kid}),
	)
	ac = make_author_claim(_author_body(), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
	)
	assert res.ok is False
	# Revocation: the diagnostic names the revoked kid explicitly.
	assert "revoked kid(s)" in res.reason
	assert a_kid in res.reason


def test_author_claim_signer_with_wrong_role() -> None:
	"""Signer's kid is in trust store but registered as CERTIFIER not
	AUTHOR.  Role-tagging enforced: cert kid cannot stand in for
	author."""
	seed = _seed("cert_only_kid")
	kid = _kid_for_seed(seed)
	trust = TrustStore(
		keys_by_kid={kid: TrustedKey(algo="ed25519", kid=kid, pubkey_raw=_pubkey_for_seed(seed), label="")},
		roles_by_namespace={
			_NAMESPACE: NamespaceRoles(
				authors=frozenset(),
				certifiers=frozenset({kid}),
			),
		},
		revoked_kids=frozenset(),
	)
	ac = make_author_claim(_author_body(), seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
	)
	assert res.ok is False
	assert "no author-role kids" in res.reason


# ── Cert claim failure modes (T3, T4, T5/9 cert-side, T7, T17–T20) ──


def test_missing_cert_claim_rejected() -> None:
	"""T4: author claim trusted, but no cert claim AND not in
	self-verify mode → reject (no acceptance path)."""
	a_seed = _seed("a")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=False,
	)
	assert res.ok is False
	assert "no .cert-claim sidecar found" in res.reason


def test_cert_claim_sci_mismatch_with_author_claim() -> None:
	"""T3: author claim says SCI=A; cert claim says SCI=B.  The two
	claims describe different source releases."""
	a_seed = _seed("a")
	c_seed = _seed("c")
	trust = _trust_with(
		author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed),
		certifier_kid=_kid_for_seed(c_seed), certifier_pubkey=_pubkey_for_seed(c_seed),
	)
	ac = make_author_claim(_author_body(source_content_id=_SCI), a_seed)
	# Cert claim has a DIFFERENT SCI.
	cc = make_cert_claim(_cert_body(source_content_id="sha256:" + ("9" * 64)), c_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
	)
	assert res.ok is False
	assert "source_content_id" in res.reason


def test_cert_claim_artifact_sha_mismatch() -> None:
	"""T7: cert claim's artifact_sha256 disagrees with the on-disk
	package's hash."""
	a_seed = _seed("a")
	c_seed = _seed("c")
	trust = _trust_with(
		author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed),
		certifier_kid=_kid_for_seed(c_seed), certifier_pubkey=_pubkey_for_seed(c_seed),
	)
	ac = make_author_claim(_author_body(), a_seed)
	# cert claim's artifact hash = "d"*64; package_identity says "9"*64
	cc = make_cert_claim(_cert_body(artifact_sha256="sha256:" + ("d" * 64)), c_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(artifact_sha256="sha256:" + ("9" * 64)),
		module_id=_MODULE, trust=trust, resolved_closure=[],
	)
	assert res.ok is False
	assert "artifact_sha256" in res.reason


def test_cert_claim_failing_suite_rejected() -> None:
	"""T20: cert_suite.result == 'fail' is well-formed at load but
	rejected at verify."""
	a_seed = _seed("a")
	c_seed = _seed("c")
	trust = _trust_with(
		author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed),
		certifier_kid=_kid_for_seed(c_seed), certifier_pubkey=_pubkey_for_seed(c_seed),
	)
	ac = make_author_claim(_author_body(), a_seed)
	cc = make_cert_claim(_cert_body(cert_suite_result="fail"), c_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
	)
	assert res.ok is False
	assert "cert_suite.result" in res.reason


def test_cert_claim_dep_graph_gap() -> None:
	"""T17: consumer's resolved closure includes a dep the cert
	claim's dep_graph does not attest."""
	a_seed = _seed("a")
	c_seed = _seed("c")
	trust = _trust_with(
		author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed),
		certifier_kid=_kid_for_seed(c_seed), certifier_pubkey=_pubkey_for_seed(c_seed),
	)
	ac = make_author_claim(_author_body(), a_seed)
	# Cert claim attests NO deps.
	cc = make_cert_claim(_cert_body(dep_graph=()), c_seed)
	# Consumer loaded a dep.
	closure = [ResolvedDep(
		package_id="orphan", version="0.1.0",
		artifact_sha256="sha256:" + ("9" * 64),
		source_content_id="sha256:" + ("8" * 64),
	)]
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=closure,
	)
	assert res.ok is False
	assert "missing entry" in res.reason


def test_cert_claim_dep_graph_artifact_mismatch() -> None:
	"""T18: cert claim has an entry for the consumer's dep but the
	dep's artifact_sha256 differs."""
	a_seed = _seed("a")
	c_seed = _seed("c")
	trust = _trust_with(
		author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed),
		certifier_kid=_kid_for_seed(c_seed), certifier_pubkey=_pubkey_for_seed(c_seed),
	)
	ac = make_author_claim(_author_body(), a_seed)
	cc_dep = DepGraphEntry(
		package_id="dep", version="1.0.0",
		artifact_sha256="sha256:" + ("1" * 64),
		source_content_id="sha256:" + ("2" * 64),
		author_kid=None, cert_kid=None, dep_kind="direct",
	)
	cc = make_cert_claim(_cert_body(dep_graph=(cc_dep,)), c_seed)
	consumer_dep = ResolvedDep(
		package_id="dep", version="1.0.0",
		artifact_sha256="sha256:" + ("9" * 64),   # different
		source_content_id="sha256:" + ("2" * 64),
	)
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[consumer_dep],
	)
	assert res.ok is False
	assert "artifact_sha256 mismatch" in res.reason


def test_cert_claim_untrusted_certifier() -> None:
	"""Cert claim signed by an unknown kid → reject."""
	a_seed = _seed("a")
	# Trust knows the author kid but NOT a certifier kid.
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	# Some random certifier signs the cert claim.
	c_seed = _seed("untrusted_cert")
	ac = make_author_claim(_author_body(), a_seed)
	cc = make_cert_claim(_cert_body(), c_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
	)
	assert res.ok is False
	# The composition diagnostic should mention either certifier
	# signature or trust-set failure.
	assert "cert claim" in res.reason.lower()


def test_cert_revoked() -> None:
	"""T11 (cert side): certifier kid is in trust store but revoked."""
	a_seed = _seed("a")
	c_seed = _seed("c")
	c_kid = _kid_for_seed(c_seed)
	trust = _trust_with(
		author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed),
		certifier_kid=c_kid, certifier_pubkey=_pubkey_for_seed(c_seed),
		revoked=frozenset({c_kid}),
	)
	ac = make_author_claim(_author_body(), a_seed)
	cc = make_cert_claim(_cert_body(), c_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
	)
	assert res.ok is False
	assert "no certifier-role kids" in res.reason


# ── Policy flags: --require-certifier, --require-cert-suite ───────


def test_require_certifier_mismatch() -> None:
	"""T15: --require-certifier set to a kid that didn't sign."""
	ac, cc, trust, identity, _, _ = _happy_setup()
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=identity, module_id=_MODULE,
		trust=trust, resolved_closure=[],
		require_certifier="ed25519:not_signing_this",
	)
	assert res.ok is False
	assert "required certifier" in res.reason


def test_require_cert_suite_mismatch() -> None:
	"""T16: --require-cert-suite drift.foundation/default but cert
	signed by a different suite."""
	a_seed = _seed("a")
	c_seed = _seed("c")
	trust = _trust_with(
		author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed),
		certifier_kid=_kid_for_seed(c_seed), certifier_pubkey=_pubkey_for_seed(c_seed),
	)
	ac = make_author_claim(_author_body(), a_seed)
	cc = make_cert_claim(_cert_body(cert_suite_id="pushcoin/internal-stage"), c_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		require_cert_suite="drift.foundation/default",
	)
	assert res.ok is False
	assert "cert_suite.id" in res.reason


# ── Multi-cert behavior ────────────────────────────────────────────


def test_multi_cert_first_matching_wins() -> None:
	"""T9 / T12: multiple cert claims; one trusted, one not.  The
	trusted one accepts; verifier short-circuits."""
	a_seed = _seed("a")
	c_seed = _seed("trusted_cert")
	other_seed = _seed("untrusted_other")
	trust = _trust_with(
		author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed),
		certifier_kid=_kid_for_seed(c_seed), certifier_pubkey=_pubkey_for_seed(c_seed),
	)
	ac = make_author_claim(_author_body(), a_seed)
	cc_trusted = make_cert_claim(_cert_body(), c_seed)
	cc_untrusted = make_cert_claim(_cert_body(), other_seed)
	# Untrusted appears FIRST in the list -- verifier iterates,
	# falls past it, then accepts the trusted one.
	res = compose_verify(
		author_claim=ac, cert_claims=[cc_untrusted, cc_trusted],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
	)
	assert res.ok is True
	assert res.certifier_kid == _kid_for_seed(c_seed)


def test_multi_cert_all_fail_aggregated_diagnostic() -> None:
	"""When NO cert claim accepts, the diagnostic names each
	attempted claim and its failure reason."""
	a_seed = _seed("a")
	c1_seed = _seed("cert_1")
	c2_seed = _seed("cert_2")
	# Trust knows neither certifier.
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(), a_seed)
	cc1 = make_cert_claim(_cert_body(), c1_seed)
	cc2 = make_cert_claim(_cert_body(), c2_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[cc1, cc2],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
	)
	assert res.ok is False
	# Both kids should appear in the aggregated diagnostic so the
	# operator can see which kids were tried.
	assert _kid_for_seed(c1_seed) in res.reason
	assert _kid_for_seed(c2_seed) in res.reason


# ── G1 diagnostic shape (T25, T26) ─────────────────────────────────


def test_normal_mode_diagnostic_does_not_claim_source_identity_proof() -> None:
	"""T25: on the happy certifier-shortcut path, `result.mode` is
	'certifier-shortcut' (NOT something implying SCI was
	independently recomputed)."""
	ac, cc, trust, identity, _, _ = _happy_setup()
	res = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=identity, module_id=_MODULE,
		trust=trust, resolved_closure=[],
	)
	assert res.ok is True
	assert res.mode == "certifier-shortcut"


def test_self_verify_diagnostic_says_self_verify() -> None:
	"""T26: on the self-verify path, `result.mode` says so explicitly."""
	a_seed = _seed("a")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
	)
	assert res.ok is True
	assert res.mode == "self-verify"


# ── Sidecar discovery + load_sidecar_claims + verify_package_from_sidecars ──


def _write_sidecars(tmp_path: Path, author_claim: AuthorClaim, cert_claims: list[CertClaim], pkg_id: str = _PKG_ID) -> None:
	"""Materialize sidecars to disk in the canonical filenames."""
	(tmp_path / f"{pkg_id}.author-claim").write_text(dump_author_claim_json(author_claim))
	for cc in cert_claims:
		# Use first signature's kid as the filename disambiguator.
		kid = cc.signatures[0].kid
		fn = cert_claim_filename(pkg_id, kid)
		(tmp_path / fn).write_text(dump_cert_claim_json(cc))


def test_discover_author_claim_path_found(tmp_path: Path) -> None:
	a_seed = _seed("a")
	ac = make_author_claim(_author_body(), a_seed)
	_write_sidecars(tmp_path, ac, [])
	p = discover_author_claim_path(tmp_path, package_id=_PKG_ID)
	assert p is not None
	assert p.name == f"{_PKG_ID}.author-claim"


def test_discover_author_claim_path_absent(tmp_path: Path) -> None:
	p = discover_author_claim_path(tmp_path, package_id="other")
	assert p is None


def test_discover_cert_claim_paths_finds_all(tmp_path: Path) -> None:
	a_seed = _seed("a")
	c1_seed = _seed("c1")
	c2_seed = _seed("c2")
	ac = make_author_claim(_author_body(), a_seed)
	cc1 = make_cert_claim(_cert_body(), c1_seed)
	cc2 = make_cert_claim(_cert_body(), c2_seed)
	_write_sidecars(tmp_path, ac, [cc1, cc2])
	paths = discover_cert_claim_paths(tmp_path, package_id=_PKG_ID)
	assert len(paths) == 2
	# Deterministic sort order.
	assert paths == sorted(paths)


def test_load_sidecar_claims_round_trip(tmp_path: Path) -> None:
	a_seed = _seed("a")
	c_seed = _seed("c")
	ac = make_author_claim(_author_body(), a_seed)
	cc = make_cert_claim(_cert_body(), c_seed)
	_write_sidecars(tmp_path, ac, [cc])
	loaded_ac, loaded_ccs = load_sidecar_claims(tmp_path, package_id=_PKG_ID)
	assert loaded_ac is not None
	assert loaded_ac.body == ac.body
	assert len(loaded_ccs) == 1
	assert loaded_ccs[0].body == cc.body


def test_verify_package_from_sidecars_e2e(tmp_path: Path) -> None:
	"""Sign → write sidecars to disk → verify via the high-level
	wrapper.  End-to-end shape pin: the slice-4 verifier composition
	works when wired to real filesystem sidecars."""
	ac, cc, trust, identity, a_kid, c_kid = _happy_setup()
	_write_sidecars(tmp_path, ac, [cc])
	res = verify_package_from_sidecars(
		sidecar_dir=tmp_path,
		package_identity=identity, module_id=_MODULE,
		trust=trust, resolved_closure=[],
	)
	assert res.ok is True
	assert res.mode == "certifier-shortcut"
	assert res.author_kid == a_kid
	assert res.certifier_kid == c_kid


def test_verify_package_from_sidecars_no_author_claim(tmp_path: Path) -> None:
	"""Higher-level wrapper rejects when no author claim exists."""
	res = verify_package_from_sidecars(
		sidecar_dir=tmp_path,
		package_identity=_identity(), module_id=_MODULE,
		trust=_trust_with(), resolved_closure=[],
	)
	assert res.ok is False
	assert "no .author-claim sidecar" in res.reason


def test_corrupt_author_claim_sidecar_raises(tmp_path: Path) -> None:
	"""Strict-v1 loader fails closed on corrupted sidecars; the
	higher-level wrapper propagates the ValueError rather than
	silently accepting via 'no claim found'."""
	(tmp_path / f"{_PKG_ID}.author-claim").write_text("{not valid json")
	with pytest.raises(Exception):
		verify_package_from_sidecars(
			sidecar_dir=tmp_path,
			package_identity=_identity(), module_id=_MODULE,
			trust=_trust_with(), resolved_closure=[],
		)


# ── Adversarial: smuggled artifact_sha256 in author claim body ────


def test_author_body_artifact_sha256_injection_rejected_at_load(tmp_path: Path) -> None:
	"""Adversarial T28-like: an author claim JSON on disk with an
	injected `body.artifact_sha256` key is rejected by the strict-v1
	loader.  Compose-verify never even sees the malformed claim."""
	a_seed = _seed("a")
	ac = make_author_claim(_author_body(), a_seed)
	# Build a corrupted JSON by hand: append `body.artifact_sha256`.
	# The loader must reject it before composition runs.
	good_text = dump_author_claim_json(ac)
	# Inject the field; use a shape the underlying JSON parser will
	# accept but the strict-v1 loader will reject as an unknown key.
	corrupted_obj = __import__("json").loads(good_text)
	corrupted_obj["body"]["artifact_sha256"] = "sha256:" + ("c" * 64)
	(tmp_path / f"{_PKG_ID}.author-claim").write_text(
		__import__("json").dumps(corrupted_obj)
	)
	with pytest.raises(ValueError, match="unknown field.*artifact_sha256"):
		verify_package_from_sidecars(
			sidecar_dir=tmp_path,
			package_identity=_identity(), module_id=_MODULE,
			trust=_trust_with(), resolved_closure=[],
		)


# ── Adversarial: swap one cert claim's package_id (replay) ─────────


def test_cert_claim_for_different_package_rejected(tmp_path: Path) -> None:
	"""Adversarial: attacker places a cert-claim sidecar whose
	package_id is 'evil' (instead of 'singular') in the singular
	sidecar dir, hoping to ride along an author claim.  The cert
	claim's own discovery filename matches the requested package
	prefix only if the attacker can write to the directory.

	Realistic case: the attacker substitutes a malicious cert claim
	in a directory with the right NAME prefix; verifier sees
	body.package_id != expected_package_id and rejects.  This pins
	the HIGH security fix in `verify_cert_claim_for_module`."""
	a_seed = _seed("a")
	c_seed = _seed("c")
	trust = _trust_with(
		author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed),
		certifier_kid=_kid_for_seed(c_seed), certifier_pubkey=_pubkey_for_seed(c_seed),
	)
	ac = make_author_claim(_author_body(), a_seed)
	# Cert claim says package_id="evil" — but its filename matches
	# the singular pattern (planted by attacker).
	evil_cc = make_cert_claim(_cert_body(package_id="evil"), c_seed)
	(tmp_path / f"{_PKG_ID}.author-claim").write_text(dump_author_claim_json(ac))
	kid = evil_cc.signatures[0].kid
	(tmp_path / cert_claim_filename(_PKG_ID, kid)).write_text(dump_cert_claim_json(evil_cc))

	res = verify_package_from_sidecars(
		sidecar_dir=tmp_path,
		package_identity=_identity(),  # expects "singular"
		module_id=_MODULE, trust=trust, resolved_closure=[],
	)
	assert res.ok is False
	# package_id pin catches it.
	assert "package_id" in res.reason


# ── self-verify + policy-flag incompatibility (HIGH pin) ───────────


def test_self_verify_with_require_certifier_rejected() -> None:
	"""HIGH: --require-certifier exists to prove a specific
	certifier path was used.  Combining it with self_verify=True
	is structurally contradictory (self-verify never consults a
	cert claim), and silently letting self-verify accept while
	ignoring the flag would deceive a CI auditor.  Reject at the
	API boundary."""
	a_seed = _seed("a")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
		require_certifier="ed25519:foundation-ci",
	)
	assert res.ok is False
	assert res.mode == "rejected"
	assert "self_verify=True is incompatible with --require-certifier" in res.reason


def test_self_verify_with_require_cert_suite_rejected() -> None:
	"""HIGH: same for --require-cert-suite."""
	a_seed = _seed("a")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
		require_cert_suite="drift.foundation/default",
	)
	assert res.ok is False
	assert res.mode == "rejected"
	assert "self_verify=True is incompatible" in res.reason


def test_self_verify_with_both_policy_flags_rejected() -> None:
	"""HIGH: both flags set together with self_verify=True."""
	a_seed = _seed("a")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
		require_certifier="ed25519:foundation-ci",
		require_cert_suite="drift.foundation/default",
	)
	assert res.ok is False
	assert "self_verify=True is incompatible" in res.reason


def test_self_verify_without_policy_flags_still_works() -> None:
	"""Sanity: self-verify alone (no policy flags) is still a valid
	acceptance path -- the fix only blocks the contradictory
	combination."""
	a_seed = _seed("a")
	trust = _trust_with(author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed))
	ac = make_author_claim(_author_body(), a_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(), module_id=_MODULE,
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
	)
	assert res.ok is True
	assert res.mode == "self-verify"


# ── Sidecar discovery with unsafe package ids (HIGH + MEDIUM pin) ──


def test_discover_author_claim_finds_escaped_filename(tmp_path: Path) -> None:
	"""HIGH/MEDIUM pin: a package id with a `/` is written to disk
	by `author_claim_filename` as `a%2Fb.author-claim`.  Discovery
	must use the SAME escape so it locates the file.  Previously
	discovery searched with the raw id and silently missed
	emit-side-escaped sidecars (split-brain)."""
	a_seed = _seed("a")
	pkg_id = "scope/pkg"
	ac = make_author_claim(_author_body(package_id=pkg_id), a_seed)
	# Write using the canonical filename helper.
	fn = author_claim_filename(pkg_id)
	(tmp_path / fn).write_text(dump_author_claim_json(ac))
	# Discovery must find it using the raw package id.
	p = discover_author_claim_path(tmp_path, package_id=pkg_id)
	assert p is not None
	assert p.name == fn
	assert "%2F" in p.name   # confirms emit-side escape was applied


def test_discover_cert_claim_finds_escaped_filename(tmp_path: Path) -> None:
	"""HIGH pin: cert claim with unsafe package id is found by
	discovery via the shared prefix helper."""
	a_seed = _seed("a")
	c_seed = _seed("c")
	pkg_id = "scope/pkg"
	cc = make_cert_claim(_cert_body(package_id=pkg_id), c_seed)
	fn = shared_cert_claim_filename(pkg_id, cc.signatures[0].kid)
	(tmp_path / fn).write_text(dump_cert_claim_json(cc))
	# Discovery uses the same escape under the hood.
	paths = discover_cert_claim_paths(tmp_path, package_id=pkg_id)
	assert len(paths) == 1
	assert paths[0].name == fn
	assert "%2F" in paths[0].name


def test_verify_package_from_sidecars_e2e_with_unsafe_package_id(tmp_path: Path) -> None:
	"""HIGH pin (end-to-end): a package whose id contains unsafe
	chars round-trips through emit → write → discover → load →
	verify cleanly under the v1 verifier."""
	a_seed = _seed("a")
	c_seed = _seed("c")
	pkg_id = "scope:pkg"   # `:` is unsafe in filenames on Windows
	trust = _trust_with(
		author_kid=_kid_for_seed(a_seed), author_pubkey=_pubkey_for_seed(a_seed),
		certifier_kid=_kid_for_seed(c_seed), certifier_pubkey=_pubkey_for_seed(c_seed),
	)
	ac = make_author_claim(_author_body(package_id=pkg_id), a_seed)
	cc = make_cert_claim(_cert_body(package_id=pkg_id), c_seed)
	# Write under the canonical (escaped) filenames.
	(tmp_path / author_claim_filename(pkg_id)).write_text(dump_author_claim_json(ac))
	(tmp_path / shared_cert_claim_filename(pkg_id, cc.signatures[0].kid)).write_text(
		dump_cert_claim_json(cc),
	)
	identity = PackageIdentity(
		package_id=pkg_id, version=_PKG_VERSION,
		source_content_id=_SCI, artifact_sha256=_ARTIFACT_SHA,
	)
	res = verify_package_from_sidecars(
		sidecar_dir=tmp_path,
		package_identity=identity, module_id=_MODULE,
		trust=trust, resolved_closure=[],
	)
	assert res.ok is True
	assert res.mode == "certifier-shortcut"


def test_filename_escape_passthrough_for_safe_chars() -> None:
	"""Sanity: pure-ASCII alphanumerics + `._-` survive untouched.
	The escape helper is OFF for the common case."""
	assert filename_escape_segment("singular") == "singular"
	assert filename_escape_segment("pkg-name_v2.3") == "pkg-name_v2.3"


def test_filename_escape_encodes_dangerous_chars() -> None:
	"""Path-traversal, FS-unsafe, and namespace separators are all
	encoded."""
	assert filename_escape_segment("a/b") == "a%2Fb"
	assert filename_escape_segment("a:b") == "a%3Ab"
	assert filename_escape_segment("a b") == "a%20b"
	# `..` is technically two dots and IS safe in the filename
	# character set; if it appears it does NOT get encoded, but the
	# overall name remains `..` (the filename position is per-segment
	# in our naming, so `..` here is a literal package id, not a
	# directory traversal).  Confirm.
	assert filename_escape_segment("..") == ".."


# ── Cross-publisher negative (T14, T23, T24) ───────────────────────


def test_cross_publisher_pushcoin_cannot_substitute_for_mariadb() -> None:
	"""T23: PushCoin author kid is trusted ONLY for singular.*.
	An attempt to verify a singular module's author claim signed by
	the pushcoin kid against the mariadb namespace must fail."""
	pc_seed = _seed("pushcoin_author")
	pc_kid = _kid_for_seed(pc_seed)
	# Trust grants pushcoin for singular.* AND foundation for mariadb.*.
	fdn_seed = _seed("fdn_author")
	fdn_kid = _kid_for_seed(fdn_seed)
	trust = TrustStore(
		keys_by_kid={
			pc_kid: TrustedKey(algo="ed25519", kid=pc_kid, pubkey_raw=_pubkey_for_seed(pc_seed), label=""),
			fdn_kid: TrustedKey(algo="ed25519", kid=fdn_kid, pubkey_raw=_pubkey_for_seed(fdn_seed), label=""),
		},
		roles_by_namespace={
			"singular.*": NamespaceRoles(authors=frozenset({pc_kid}), certifiers=frozenset()),
			"mariadb.*": NamespaceRoles(authors=frozenset({fdn_kid}), certifiers=frozenset()),
		},
		revoked_kids=frozenset(),
	)
	# PushCoin claims namespace `mariadb.*` in their author claim.  The
	# claim is signed by pc_kid.  Verifier asks about a mariadb module
	# → pushcoin kid is NOT in `allowed_authors_for_module(mariadb.rpc)`.
	ac = make_author_claim(_author_body(namespaces=("mariadb.*",)), pc_seed)
	res = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(package_id="evil-pushcoin-pkg"),
		module_id="mariadb.rpc.managed",
		trust=trust, resolved_closure=[],
		self_verify=True, self_verify_sci=_SCI,
	)
	assert res.ok is False
	# Either the package_id pin or the kid-not-trusted-for-this-namespace
	# pin catches it.  Both are valid defenses; we don't pin which one
	# fires first, just that the load is rejected.
	assert res.mode == "rejected"
