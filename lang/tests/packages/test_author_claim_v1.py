# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for `lang.driftc.packages.author_claim_v1`.

Covers: dataclass round-trip, canonical signing bytes determinism,
multi-signature composition, strict loader (format v1, body schema v2), JSON round-trip,
signature verification, full namespace+role composition
(`verify_author_claim_for_module`), and failure modes (untrusted
kid, sci mismatch, namespace mismatch, mangled body, etc.).

Plan reference: `work/drift-trust-model-audit/plan.md` §3, §5.
Slice 2 of the trust-v1 implementation.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from lang.drift.crypto import (
	b64_decode,
	b64_encode,
	compute_ed25519_kid,
	ed25519_sign_from_seed,
)
from lang.driftc.packages.author_claim_v1 import (
	AuthorClaim,
	AuthorClaimBody,
	AuthorClaimVerifyResult,
	AuthorSignature,
	RequiredDep,
	_namespace_covers,
	add_signature,
	body_signing_bytes,
	dump_author_claim_json,
	load_author_claim_json,
	make_author_claim,
	sign_body,
	verify_author_claim_for_module,
	verify_author_claim_signatures,
)
from lang.driftc.packages.trust_v1 import (
	NamespaceRoles,
	TrustStore,
	TrustedKey,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _seed(label: str) -> bytes:
	"""Deterministic 32-byte ed25519 private seed from a label."""
	return label.encode().ljust(32, b"_")[:32]


def _kid_for_seed(seed: bytes) -> str:
	_sig, pubkey_raw = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return compute_ed25519_kid(pubkey_raw)


def _pubkey_for_seed(seed: bytes) -> bytes:
	_sig, pubkey_raw = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return pubkey_raw


def _trust_with_author(
	*,
	kid: str,
	pubkey_raw: bytes,
	namespace: str = "singular.*",
) -> TrustStore:
	"""Build a TrustStore with one author-role kid for one namespace."""
	return TrustStore(
		keys_by_kid={kid: TrustedKey(algo="ed25519", kid=kid, pubkey_raw=pubkey_raw, label="")},
		roles_by_namespace={
			namespace: NamespaceRoles(
				authors=frozenset({kid}),
				certifiers=frozenset(),
			),
		},
		revoked_kids=frozenset(),
	)


def _example_body(
	*,
	package_id: str = "singular",
	version: str = "0.3.0",
	artifact_kind: str = "package",
	namespaces: tuple[str, ...] = ("singular", "singular.*"),
	source_content_id: str = "sha256:" + ("a" * 64),
	required_deps: tuple[RequiredDep, ...] = (
		RequiredDep(name="mariadb-rpc", version_range="^0.5.0"),
	),
	release_utc: str = "2026-05-18T12:00:00Z",
) -> AuthorClaimBody:
	return AuthorClaimBody(
		schema_version=2,
		package_id=package_id,
		version=version,
		artifact_kind=artifact_kind,
		namespaces=namespaces,
		source_content_id=source_content_id,
		required_deps=required_deps,
		release_utc=release_utc,
	)


# ── Canonical signing bytes ────────────────────────────────────────


def test_signing_bytes_are_deterministic() -> None:
	"""Two equivalent bodies produce identical signing bytes."""
	b1 = _example_body()
	b2 = _example_body()
	assert body_signing_bytes(b1) == body_signing_bytes(b2)


def test_signing_bytes_independent_of_namespace_order() -> None:
	"""Namespaces sort before signing — reorder must not change the bytes."""
	b1 = _example_body(namespaces=("singular", "singular.*"))
	b2 = _example_body(namespaces=("singular.*", "singular"))
	assert body_signing_bytes(b1) == body_signing_bytes(b2)


def test_signing_bytes_independent_of_required_deps_order() -> None:
	"""required_deps sort by name before signing."""
	deps1 = (
		RequiredDep(name="alpha", version_range="^1.0.0"),
		RequiredDep(name="beta", version_range="^2.0.0"),
	)
	deps2 = (
		RequiredDep(name="beta", version_range="^2.0.0"),
		RequiredDep(name="alpha", version_range="^1.0.0"),
	)
	b1 = _example_body(required_deps=deps1)
	b2 = _example_body(required_deps=deps2)
	assert body_signing_bytes(b1) == body_signing_bytes(b2)


def test_signing_bytes_change_with_sci() -> None:
	"""Any change to source_content_id changes the signing bytes."""
	b1 = _example_body(source_content_id="sha256:" + ("a" * 64))
	b2 = _example_body(source_content_id="sha256:" + ("b" * 64))
	assert body_signing_bytes(b1) != body_signing_bytes(b2)


def test_signing_bytes_change_with_package_id() -> None:
	b1 = _example_body(package_id="singular")
	b2 = _example_body(package_id="other")
	assert body_signing_bytes(b1) != body_signing_bytes(b2)


# ── Sign + make + add_signature ────────────────────────────────────


def test_sign_body_produces_kid_matching_seed() -> None:
	"""The signature's kid is derived from the seed's pubkey."""
	seed = _seed("author")
	body = _example_body()
	sig = sign_body(body, seed)
	assert sig.algo == "ed25519"
	assert sig.kid == _kid_for_seed(seed)
	assert len(sig.sig_raw) == 64


def test_make_author_claim_has_one_signature() -> None:
	seed = _seed("author")
	claim = make_author_claim(_example_body(), seed)
	assert len(claim.signatures) == 1
	assert claim.signatures[0].kid == _kid_for_seed(seed)


def test_add_signature_co_signs_same_body() -> None:
	"""Two authors co-signing.  Body bytes stay the same; signature
	count grows by 1."""
	seed_a = _seed("author_a")
	seed_b = _seed("author_b")
	claim1 = make_author_claim(_example_body(), seed_a)
	claim2 = add_signature(claim1, seed_b)
	assert claim1.body is claim2.body  # same body (frozen dataclass)
	assert len(claim2.signatures) == 2
	kids = {s.kid for s in claim2.signatures}
	assert kids == {_kid_for_seed(seed_a), _kid_for_seed(seed_b)}


# ── JSON round-trip ────────────────────────────────────────────────


def test_dump_then_load_round_trip() -> None:
	seed = _seed("author")
	claim = make_author_claim(_example_body(), seed)
	text = dump_author_claim_json(claim)
	reloaded = load_author_claim_json(text)
	assert reloaded.body == claim.body
	assert len(reloaded.signatures) == 1
	assert reloaded.signatures[0].kid == claim.signatures[0].kid
	assert reloaded.signatures[0].sig_raw == claim.signatures[0].sig_raw


def test_dump_is_deterministic() -> None:
	"""Two dumps of the same claim produce identical text."""
	seed = _seed("author")
	claim = make_author_claim(_example_body(), seed)
	out1 = dump_author_claim_json(claim)
	out2 = dump_author_claim_json(claim)
	assert out1 == out2


def test_dump_signatures_sorted_by_kid() -> None:
	"""Multi-signature output is deterministic in signature order."""
	seed_z = _seed("z_author")  # would sort last
	seed_a = _seed("a_author")
	claim1 = make_author_claim(_example_body(), seed_z)
	claim2 = add_signature(claim1, seed_a)
	out = dump_author_claim_json(claim2)
	# The "a_author" kid should appear before "z_author" in the JSON.
	kid_a = _kid_for_seed(seed_a)
	kid_z = _kid_for_seed(seed_z)
	idx_a = out.index(kid_a)
	idx_z = out.index(kid_z)
	assert idx_a < idx_z


def test_load_round_trip_recovers_signing_bytes_for_reverify() -> None:
	"""Loading a dumped claim and recomputing body_signing_bytes
	produces the SAME bytes the signer used.  This is the load-side
	half of the signature contract: a reader can reverify the
	signature without re-marshaling through dataclasses."""
	seed = _seed("author")
	claim = make_author_claim(_example_body(), seed)
	text = dump_author_claim_json(claim)
	reloaded = load_author_claim_json(text)
	assert body_signing_bytes(reloaded.body) == body_signing_bytes(claim.body)


# ── artifact_kind (v2 body) ────────────────────────────────────────


def test_signing_bytes_change_with_artifact_kind() -> None:
	"""`artifact_kind` is part of signed source identity (canonical body)."""
	b_pkg = _example_body(artifact_kind="package")
	b_app = _example_body(artifact_kind="app")
	assert body_signing_bytes(b_pkg) != body_signing_bytes(b_app)


def test_artifact_kind_round_trips() -> None:
	seed = _seed("author")
	claim = make_author_claim(_example_body(artifact_kind="app"), seed)
	rt = load_author_claim_json(dump_author_claim_json(claim))
	assert rt.body.artifact_kind == "app"
	assert rt.body.schema_version == 2


def test_reject_missing_artifact_kind() -> None:
	"""A v2 body without artifact_kind is malformed."""
	body_dict = {
		"schema_version": 2,
		"package_id": "x",
		"version": "0.1.0",
		"namespaces": ["x"],
		"source_content_id": "sha256:" + ("a" * 64),
		"required_deps": [],
		"release_utc": "2026-05-18T00:00:00Z",
	}
	sigs = [{"algo": "ed25519", "kid": "ed25519:k", "sig": b64_encode(b"\x00" * 64)}]
	with pytest.raises(ValueError, match="artifact_kind"):
		load_author_claim_json(_wrap_envelope(body=body_dict, signatures=sigs))


def test_reject_library_artifact_kind() -> None:
	"""Signed claims accept only `package`/`app`; `library` is rejected."""
	body_dict = {
		"schema_version": 2, "artifact_kind": "library",
		"package_id": "x", "version": "0.1.0", "namespaces": ["x"],
		"source_content_id": "sha256:" + ("a" * 64),
		"required_deps": [], "release_utc": "2026-05-18T00:00:00Z",
	}
	sigs = [{"algo": "ed25519", "kid": "ed25519:k", "sig": b64_encode(b"\x00" * 64)}]
	with pytest.raises(ValueError, match="artifact_kind"):
		load_author_claim_json(_wrap_envelope(body=body_dict, signatures=sigs))


# ── Strict loader (format v1, body schema v2) ──────────────────────


def _wrap_envelope(*, format: str = "drift-author-claim", version: int = 1, body=None, signatures=None) -> str:
	return json.dumps({
		"format": format,
		"version": version,
		"body": body if body is not None else {},
		"signatures": signatures if signatures is not None else [],
	})


def test_reject_wrong_format_tag() -> None:
	with pytest.raises(ValueError, match="unsupported author claim format"):
		load_author_claim_json(_wrap_envelope(format="not-drift-author-claim"))


def test_reject_wrong_version() -> None:
	with pytest.raises(ValueError, match="unsupported author claim version"):
		load_author_claim_json(_wrap_envelope(version=0))


def test_reject_empty_signatures() -> None:
	"""An author claim with no signatures is malformed."""
	seed = _seed("author")
	body_dict = {
		"schema_version": 2, "artifact_kind": "package",
		"package_id": "x",
		"version": "0.1.0",
		"namespaces": ["x"],
		"source_content_id": "sha256:" + ("a" * 64),
		"required_deps": [],
		"release_utc": "2026-05-18T00:00:00Z",
	}
	text = _wrap_envelope(body=body_dict, signatures=[])
	with pytest.raises(ValueError, match="at least one signature"):
		load_author_claim_json(text)


def test_reject_bad_body_schema_version() -> None:
	body_dict = {
		"schema_version": 0,  # wrong
		"package_id": "x",
		"version": "0.1.0",
		"namespaces": ["x"],
		"source_content_id": "sha256:" + ("a" * 64),
		"required_deps": [],
		"release_utc": "2026-05-18T00:00:00Z",
	}
	sigs = [{"algo": "ed25519", "kid": "ed25519:k", "sig": b64_encode(b"\x00" * 64)}]
	text = _wrap_envelope(body=body_dict, signatures=sigs)
	with pytest.raises(ValueError, match="body schema_version"):
		load_author_claim_json(text)


def test_reject_bad_sci_shape() -> None:
	body_dict = {
		"schema_version": 2, "artifact_kind": "package",
		"package_id": "x",
		"version": "0.1.0",
		"namespaces": ["x"],
		"source_content_id": "not-prefixed-hex",
		"required_deps": [],
		"release_utc": "2026-05-18T00:00:00Z",
	}
	sigs = [{"algo": "ed25519", "kid": "ed25519:k", "sig": b64_encode(b"\x00" * 64)}]
	text = _wrap_envelope(body=body_dict, signatures=sigs)
	with pytest.raises(ValueError, match="source_content_id"):
		load_author_claim_json(text)


def test_reject_empty_namespaces() -> None:
	body_dict = {
		"schema_version": 2, "artifact_kind": "package",
		"package_id": "x",
		"version": "0.1.0",
		"namespaces": [],
		"source_content_id": "sha256:" + ("a" * 64),
		"required_deps": [],
		"release_utc": "2026-05-18T00:00:00Z",
	}
	sigs = [{"algo": "ed25519", "kid": "ed25519:k", "sig": b64_encode(b"\x00" * 64)}]
	text = _wrap_envelope(body=body_dict, signatures=sigs)
	with pytest.raises(ValueError, match="namespaces"):
		load_author_claim_json(text)


def test_reject_wrong_sig_length() -> None:
	"""Ed25519 signatures are exactly 64 bytes; otherwise reject."""
	body_dict = {
		"schema_version": 2, "artifact_kind": "package",
		"package_id": "x",
		"version": "0.1.0",
		"namespaces": ["x"],
		"source_content_id": "sha256:" + ("a" * 64),
		"required_deps": [],
		"release_utc": "2026-05-18T00:00:00Z",
	}
	sigs = [{"algo": "ed25519", "kid": "ed25519:k", "sig": b64_encode(b"\x00" * 32)}]
	text = _wrap_envelope(body=body_dict, signatures=sigs)
	with pytest.raises(ValueError, match="64 bytes"):
		load_author_claim_json(text)


def test_reject_non_ed25519_algo() -> None:
	body_dict = {
		"schema_version": 2, "artifact_kind": "package",
		"package_id": "x",
		"version": "0.1.0",
		"namespaces": ["x"],
		"source_content_id": "sha256:" + ("a" * 64),
		"required_deps": [],
		"release_utc": "2026-05-18T00:00:00Z",
	}
	sigs = [{"algo": "rsa", "kid": "rsa:k", "sig": b64_encode(b"\x00" * 64)}]
	text = _wrap_envelope(body=body_dict, signatures=sigs)
	with pytest.raises(ValueError, match="ed25519"):
		load_author_claim_json(text)


def test_reject_required_deps_with_missing_version_range() -> None:
	body_dict = {
		"schema_version": 2, "artifact_kind": "package",
		"package_id": "x",
		"version": "0.1.0",
		"namespaces": ["x"],
		"source_content_id": "sha256:" + ("a" * 64),
		"required_deps": [{"name": "foo"}],   # version_range missing
		"release_utc": "2026-05-18T00:00:00Z",
	}
	sigs = [{"algo": "ed25519", "kid": "ed25519:k", "sig": b64_encode(b"\x00" * 64)}]
	text = _wrap_envelope(body=body_dict, signatures=sigs)
	with pytest.raises(ValueError, match="version_range"):
		load_author_claim_json(text)


# ── Signature verification (low-level) ─────────────────────────────


def test_verify_author_claim_signatures_returns_trusted_kids() -> None:
	seed = _seed("author")
	claim = make_author_claim(_example_body(), seed)
	kid = _kid_for_seed(seed)
	trust = _trust_with_author(kid=kid, pubkey_raw=_pubkey_for_seed(seed))
	verified = verify_author_claim_signatures(claim, trust)
	assert verified == {kid}


def test_verify_author_claim_signatures_skips_unknown_signer() -> None:
	"""A signature by a kid that's not in `trust.keys_by_kid` is
	silently skipped (the consumer simply doesn't know that kid).
	The function returns only the kids that BOTH verified AND have
	a trust-store entry."""
	seed_known = _seed("known")
	seed_unknown = _seed("unknown")
	claim = make_author_claim(_example_body(), seed_known)
	claim = add_signature(claim, seed_unknown)
	# Trust only the "known" kid.
	trust = _trust_with_author(
		kid=_kid_for_seed(seed_known),
		pubkey_raw=_pubkey_for_seed(seed_known),
	)
	verified = verify_author_claim_signatures(claim, trust)
	assert verified == {_kid_for_seed(seed_known)}


def test_verify_author_claim_signatures_rejects_tampered_body() -> None:
	"""If body bytes changed since signing, the signature does not
	verify against the new bytes -> empty result."""
	seed = _seed("author")
	original = make_author_claim(_example_body(), seed)
	# Construct a tampered claim: same signature, different SCI in body.
	tampered_body = _example_body(source_content_id="sha256:" + ("c" * 64))
	tampered = AuthorClaim(body=tampered_body, signatures=original.signatures)
	trust = _trust_with_author(
		kid=_kid_for_seed(seed),
		pubkey_raw=_pubkey_for_seed(seed),
	)
	verified = verify_author_claim_signatures(tampered, trust)
	assert verified == set()


def test_verify_author_claim_signatures_with_corrupt_sig_bytes() -> None:
	"""A signature whose raw bytes do not match the body fails
	verification (corrupted in transit, malicious replacement, etc.)."""
	seed = _seed("author")
	claim = make_author_claim(_example_body(), seed)
	# Replace the signature bytes with garbage.
	bad_sig = AuthorSignature(
		algo="ed25519",
		kid=claim.signatures[0].kid,
		sig_raw=b"\x00" * 64,
	)
	bad_claim = AuthorClaim(body=claim.body, signatures=(bad_sig,))
	trust = _trust_with_author(
		kid=_kid_for_seed(seed),
		pubkey_raw=_pubkey_for_seed(seed),
	)
	verified = verify_author_claim_signatures(bad_claim, trust)
	assert verified == set()


# ── Namespace coverage helper ──────────────────────────────────────


def test_namespace_covers_exact() -> None:
	assert _namespace_covers("singular", "singular") is True
	assert _namespace_covers("singular", "singular.api") is False
	assert _namespace_covers("singular", "other") is False


def test_namespace_covers_prefix() -> None:
	assert _namespace_covers("singular.*", "singular") is True
	assert _namespace_covers("singular.*", "singular.api") is True
	assert _namespace_covers("singular.*", "singularx") is False
	assert _namespace_covers("singular.*", "other.api") is False


# ── verify_author_claim_for_module — composition ──────────────────


def test_verify_for_module_happy_path() -> None:
	seed = _seed("pushcoin_author")
	claim = make_author_claim(_example_body(), seed)
	kid = _kid_for_seed(seed)
	trust = _trust_with_author(kid=kid, pubkey_raw=_pubkey_for_seed(seed), namespace="singular.*")
	result = verify_author_claim_for_module(claim, trust, "singular.api", expected_package_id="singular", expected_version="0.3.0")
	assert result.ok is True
	assert result.accepted_kid == kid
	assert result.reason == ""


def test_verify_for_module_rejects_namespace_mismatch() -> None:
	"""Claim covers `singular.*`; verifier asks about `other.foo` →
	namespace coverage fails before signature even matters."""
	seed = _seed("pushcoin_author")
	claim = make_author_claim(_example_body(namespaces=("singular.*",)), seed)
	trust = _trust_with_author(
		kid=_kid_for_seed(seed),
		pubkey_raw=_pubkey_for_seed(seed),
		namespace="singular.*",
	)
	result = verify_author_claim_for_module(claim, trust, "other.foo", expected_package_id="singular", expected_version="0.3.0")
	assert result.ok is False
	assert "does not cover module" in result.reason


def test_verify_for_module_rejects_untrusted_author_kid() -> None:
	"""Signer's kid is not in `trust.allowed_authors_for_module(M)` →
	rejected with a clear diagnostic."""
	seed = _seed("interloper")
	claim = make_author_claim(_example_body(), seed)
	# Trust store has DIFFERENT kid for singular.*.
	trusted_seed = _seed("trusted_author")
	trusted_kid = _kid_for_seed(trusted_seed)
	trust = _trust_with_author(
		kid=trusted_kid,
		pubkey_raw=_pubkey_for_seed(trusted_seed),
		namespace="singular.*",
	)
	result = verify_author_claim_for_module(claim, trust, "singular.api", expected_package_id="singular", expected_version="0.3.0")
	assert result.ok is False
	# Crypto verification fails because the trust store doesn't have
	# the interloper's pubkey.  The early "no signature verifies"
	# branch fires before the role-allowlist check.
	assert "no signature on author claim verifies" in result.reason


def test_verify_for_module_rejects_signer_with_wrong_role() -> None:
	"""Signer's kid is in the trust store and signature verifies, but
	the kid is registered in the CERTIFIER role (not author) →
	rejected.  Important pin: role-tagging is enforced; a certifier
	kid cannot stand in for an author kid."""
	seed = _seed("cert_only_kid")
	claim = make_author_claim(_example_body(), seed)
	kid = _kid_for_seed(seed)
	# Trust store knows the kid but only authorizes it as a certifier
	# for singular.*.  authors list is empty.
	trust = TrustStore(
		keys_by_kid={kid: TrustedKey(algo="ed25519", kid=kid, pubkey_raw=_pubkey_for_seed(seed), label="")},
		roles_by_namespace={
			"singular.*": NamespaceRoles(
				authors=frozenset(),
				certifiers=frozenset({kid}),
			),
		},
		revoked_kids=frozenset(),
	)
	result = verify_author_claim_for_module(claim, trust, "singular.api", expected_package_id="singular", expected_version="0.3.0")
	assert result.ok is False
	# Trust authorizes no author-role kids for the namespace.
	assert "no author-role kids" in result.reason


def test_verify_for_module_rejects_revoked_author() -> None:
	seed = _seed("author")
	claim = make_author_claim(_example_body(), seed)
	kid = _kid_for_seed(seed)
	# Trust authorizes kid for singular.* BUT revokes it.
	trust = TrustStore(
		keys_by_kid={kid: TrustedKey(algo="ed25519", kid=kid, pubkey_raw=_pubkey_for_seed(seed), label="")},
		roles_by_namespace={
			"singular.*": NamespaceRoles(
				authors=frozenset({kid}),
				certifiers=frozenset(),
			),
		},
		revoked_kids=frozenset({kid}),
	)
	result = verify_author_claim_for_module(claim, trust, "singular.api", expected_package_id="singular", expected_version="0.3.0")
	assert result.ok is False
	# Revocation: the diagnostic names the revoked kid explicitly so a
	# user reading the error can correlate it with the `drift trust
	# revoke` call that produced this state.
	assert "revoked kid(s)" in result.reason
	assert kid in result.reason


def test_verify_for_module_multi_signature_first_trusted_wins() -> None:
	"""Two signatures: one trusted, one untrusted.  Verifier accepts
	via the trusted one (per O5: any one signature in the array
	suffices)."""
	trusted_seed = _seed("trusted_author")
	untrusted_seed = _seed("untrusted_kid")
	claim = make_author_claim(_example_body(), trusted_seed)
	claim = add_signature(claim, untrusted_seed)
	# Trust ONLY the trusted seed's kid.
	trust = _trust_with_author(
		kid=_kid_for_seed(trusted_seed),
		pubkey_raw=_pubkey_for_seed(trusted_seed),
		namespace="singular.*",
	)
	result = verify_author_claim_for_module(claim, trust, "singular.api", expected_package_id="singular", expected_version="0.3.0")
	assert result.ok is True
	assert result.accepted_kid == _kid_for_seed(trusted_seed)


def test_verify_for_module_exact_namespace_match() -> None:
	"""Body claims namespace `singular` (exact, no `.*`); verifier
	queries `singular` exactly."""
	seed = _seed("a")
	claim = make_author_claim(
		_example_body(namespaces=("singular",)),
		seed,
	)
	trust = _trust_with_author(
		kid=_kid_for_seed(seed),
		pubkey_raw=_pubkey_for_seed(seed),
		namespace="singular",
	)
	result = verify_author_claim_for_module(claim, trust, "singular", expected_package_id="singular", expected_version="0.3.0")
	assert result.ok is True


# ── Strict-v1: unknown-key rejection (security pin) ───────────────


def _valid_body_dict() -> dict:
	"""A valid body dict — base for unknown-key injection tests."""
	return {
		"schema_version": 2, "artifact_kind": "package",
		"package_id": "x",
		"version": "0.1.0",
		"namespaces": ["x"],
		"source_content_id": "sha256:" + ("a" * 64),
		"required_deps": [],
		"release_utc": "2026-05-18T00:00:00Z",
	}


def _valid_sig_record() -> dict:
	return {"algo": "ed25519", "kid": "ed25519:k", "sig": b64_encode(b"\x00" * 64)}


def test_strict_v1_rejects_artifact_sha256_in_author_body() -> None:
	"""HIGH SECURITY PIN (O6): author claims must never bind artifact
	bytes.  An attacker who appends `body.artifact_sha256` to a valid
	author-claim JSON would, under permissive parsing, see the field
	silently dropped, signing-bytes recomputed from only the schema
	fields, and the signature still verify — making the appended field
	deceptively present in the on-disk JSON.  The strict loader rejects
	the unknown field outright."""
	body = _valid_body_dict()
	body["artifact_sha256"] = "sha256:" + ("c" * 64)
	text = _wrap_envelope(body=body, signatures=[_valid_sig_record()])
	with pytest.raises(ValueError, match="unknown field.*artifact_sha256"):
		load_author_claim_json(text)


def test_strict_v1_rejects_unknown_field_in_body() -> None:
	"""General case: any unknown body field is rejected."""
	body = _valid_body_dict()
	body["future_v2_field"] = "not yet"
	text = _wrap_envelope(body=body, signatures=[_valid_sig_record()])
	with pytest.raises(ValueError, match="unknown field"):
		load_author_claim_json(text)


def test_strict_v1_rejects_target_class_in_author_body() -> None:
	"""SPEC PIN (2026-05-20): author claim must NOT bind target / build
	environment.  Target lives on the certifier's claim
	(`cert_claim.body.target`), so one author claim covers the same
	source release across multiple build targets.

	The loader previously accepted `body.target_class` as a v1 field.
	Under the spec correction it must be rejected as an unknown key
	(otherwise a stale claim signed under the old schema could load
	silently and confuse role-split audits).
	"""
	body = _valid_body_dict()
	body["target_class"] = "library"
	text = _wrap_envelope(body=body, signatures=[_valid_sig_record()])
	with pytest.raises(ValueError, match="unknown field.*target_class"):
		load_author_claim_json(text)


def test_strict_v1_rejects_unknown_field_in_envelope() -> None:
	"""Unknown fields at the envelope level (sibling to format/version/
	body/signatures) are rejected."""
	body = _valid_body_dict()
	envelope = {
		"format": "drift-author-claim",
		"version": 1,
		"body": body,
		"signatures": [_valid_sig_record()],
		"extra_envelope_metadata": "smuggled",
	}
	with pytest.raises(ValueError, match="unknown field"):
		load_author_claim_json(json.dumps(envelope))


def test_strict_v1_rejects_unknown_field_in_signature_record() -> None:
	"""Unknown fields inside a signature record are rejected.  Prevents
	attaching extra unsigned policy hints (role, expiry, etc.) inside
	a signed-looking signature entry."""
	body = _valid_body_dict()
	sig = _valid_sig_record()
	sig["role"] = "certifier"  # NOT a v1 field — must reject
	text = _wrap_envelope(body=body, signatures=[sig])
	with pytest.raises(ValueError, match="unknown field.*role"):
		load_author_claim_json(text)


def test_strict_v1_rejects_unknown_field_in_required_deps_entry() -> None:
	"""Unknown fields inside a single required_deps entry are rejected."""
	body = _valid_body_dict()
	body["required_deps"] = [
		{"name": "foo", "version_range": "^1.0", "checksum": "sha256:abc"}
	]
	text = _wrap_envelope(body=body, signatures=[_valid_sig_record()])
	with pytest.raises(ValueError, match="unknown field.*checksum"):
		load_author_claim_json(text)


def test_strict_v1_rejects_multiple_unknown_fields_listed() -> None:
	"""When multiple unknown keys are present the diagnostic lists all of them."""
	body = _valid_body_dict()
	body["a_extra"] = 1
	body["b_extra"] = 2
	text = _wrap_envelope(body=body, signatures=[_valid_sig_record()])
	with pytest.raises(ValueError) as excinfo:
		load_author_claim_json(text)
	msg = str(excinfo.value)
	assert "a_extra" in msg and "b_extra" in msg


# ── Strict-v1: duplicate-dep-name rejection ────────────────────────


def test_load_rejects_duplicate_required_dep_names() -> None:
	"""A release claim with two `required_deps` entries sharing a name
	is ambiguous (which range does the author authorize?) and would
	make the canonical signing bytes order-dependent at the sort
	tie-break.  Reject."""
	body = _valid_body_dict()
	body["required_deps"] = [
		{"name": "foo", "version_range": "^1.0.0"},
		{"name": "foo", "version_range": "^2.0.0"},
	]
	text = _wrap_envelope(body=body, signatures=[_valid_sig_record()])
	with pytest.raises(ValueError, match="duplicate dep name"):
		load_author_claim_json(text)


def test_make_author_claim_rejects_duplicate_dep_names_at_emit() -> None:
	"""Defense-in-depth: a hand-built dataclass with duplicate
	`RequiredDep` names is rejected at canonicalization time (not
	just load).  Otherwise canonical bytes would silently depend on
	stable-sort tie-break order."""
	dup_deps = (
		RequiredDep(name="foo", version_range="^1.0.0"),
		RequiredDep(name="foo", version_range="^2.0.0"),
	)
	body = _example_body(required_deps=dup_deps)
	with pytest.raises(ValueError, match="duplicate dep name"):
		body_signing_bytes(body)


# ── Envelope format-version rejection (envelope `version` stays 1) ──


def test_rejects_unknown_envelope_version() -> None:
	"""The envelope `format`/`version` axis is independent of the body
	schema: the envelope `version` is still 1 (`drift-author-claim` v1)
	even though the BODY schema is now v2.  An envelope with `version: 2`
	is rejected — only envelope version 1 exists."""
	body = _valid_body_dict()
	envelope = {
		"format": "drift-author-claim",
		"version": 2,
		"body": body,
		"signatures": [_valid_sig_record()],
	}
	with pytest.raises(ValueError, match="unsupported author claim version"):
		load_author_claim_json(json.dumps(envelope))


# ── Package identity pinning (HIGH security pin) ──────────────────


def test_verify_rejects_package_id_mismatch() -> None:
	"""HIGH SECURITY PIN: an author claim for package 'evil' must not
	pass verification when the caller expects package 'singular',
	even if other gates align.  Without this gate a replay/substitution
	attack could pass off an unrelated package's claim for the
	target module."""
	seed = _seed("pushcoin_author")
	# Claim is for package "evil".
	claim = make_author_claim(_example_body(package_id="evil"), seed)
	kid = _kid_for_seed(seed)
	trust = _trust_with_author(kid=kid, pubkey_raw=_pubkey_for_seed(seed))
	# Caller expects "singular" → reject.
	result = verify_author_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id="singular", expected_version="0.3.0",
	)
	assert result.ok is False
	assert "package_id" in result.reason
	assert "evil" in result.reason


def test_verify_rejects_version_mismatch() -> None:
	"""HIGH SECURITY PIN: a claim for an older version must not pass
	verification when the caller expects a newer version (downgrade
	attack)."""
	seed = _seed("pushcoin_author")
	# Claim is for v0.2.0; caller expects v0.3.0.
	claim = make_author_claim(_example_body(version="0.2.0"), seed)
	kid = _kid_for_seed(seed)
	trust = _trust_with_author(kid=kid, pubkey_raw=_pubkey_for_seed(seed))
	result = verify_author_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id="singular", expected_version="0.3.0",
	)
	assert result.ok is False
	assert "version" in result.reason
	assert "0.2.0" in result.reason


# ── validate_body_shape (emit-side contract) ───────────────────────


def test_sign_rejects_invalid_schema_version() -> None:
	"""Hand-built dataclass with schema_version != 2 fails at sign
	time (caller cannot smuggle a malformed body past the
	signature)."""
	bad_body = AuthorClaimBody(
		schema_version=0,  # invalid
		package_id="x", version="0.1.0", artifact_kind="package", namespaces=("x",),
		source_content_id="sha256:" + ("a" * 64),
		required_deps=(),
		release_utc="2026-05-18T00:00:00Z",
	)
	with pytest.raises(ValueError, match="schema_version"):
		body_signing_bytes(bad_body)


def test_sign_rejects_empty_package_id() -> None:
	bad_body = AuthorClaimBody(
		schema_version=2, artifact_kind="package", package_id="",  # empty
		version="0.1.0", namespaces=("x",),
		source_content_id="sha256:" + ("a" * 64),
		required_deps=(),
		release_utc="2026-05-18T00:00:00Z",
	)
	with pytest.raises(ValueError, match="package_id"):
		body_signing_bytes(bad_body)


def test_sign_rejects_bad_sci_shape() -> None:
	bad_body = AuthorClaimBody(
		schema_version=2, artifact_kind="package", package_id="x", version="0.1.0",
		namespaces=("x",),
		source_content_id="not-a-sha",   # malformed
		required_deps=(),
		release_utc="2026-05-18T00:00:00Z",
	)
	with pytest.raises(ValueError, match="source_content_id"):
		body_signing_bytes(bad_body)


def test_sign_rejects_empty_namespaces() -> None:
	bad_body = AuthorClaimBody(
		schema_version=2, artifact_kind="package", package_id="x", version="0.1.0",
		namespaces=(),   # empty
		source_content_id="sha256:" + ("a" * 64),
		required_deps=(),
		release_utc="2026-05-18T00:00:00Z",
	)
	with pytest.raises(ValueError, match="namespaces"):
		body_signing_bytes(bad_body)


def test_sign_rejects_empty_dep_version_range() -> None:
	bad_body = AuthorClaimBody(
		schema_version=2, artifact_kind="package", package_id="x", version="0.1.0",
		namespaces=("x",),
		source_content_id="sha256:" + ("a" * 64),
		required_deps=(RequiredDep(name="foo", version_range=""),),
		release_utc="2026-05-18T00:00:00Z",
	)
	with pytest.raises(ValueError, match="version_range"):
		body_signing_bytes(bad_body)


# ── End-to-end via sidecar text ────────────────────────────────────


def test_end_to_end_via_json_sidecar(tmp_path: Path) -> None:
	"""Sign → dump to JSON → save to disk → load from disk → verify.
	The signature contract survives serialization."""
	seed = _seed("author_e2e")
	body = _example_body()
	claim = make_author_claim(body, seed)
	sidecar = tmp_path / "pkg.author-claim"
	sidecar.write_text(dump_author_claim_json(claim))

	reloaded = load_author_claim_json(sidecar.read_text())
	trust = _trust_with_author(
		kid=_kid_for_seed(seed),
		pubkey_raw=_pubkey_for_seed(seed),
		namespace="singular.*",
	)
	result = verify_author_claim_for_module(reloaded, trust, "singular.api", expected_package_id="singular", expected_version="0.3.0")
	assert result.ok is True
