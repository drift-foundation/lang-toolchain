# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for `lang.driftc.packages.cert_claim_v1`.

Covers: dataclass round-trip, canonical signing bytes determinism,
strict loader (format v1, body schema v2; unknown-key rejection at every nesting level),
JSON round-trip, signature verification, dep_graph closure check
(O3), full composition `verify_cert_claim_for_module` with all gates
(artifact_sha256, source_content_id, cert_suite.result, dep_graph
closure, --require-certifier, --require-cert-suite, role-tagged
trust lookup, revocation), per-certifier filename helper (O1).

Plan reference: `work/drift-trust-model-audit/plan.md` §4, §5,
§11 (O1, O3, O4, O5, O6, O7).  Slice 3 of the trust-v1
implementation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.drift.crypto import (
	b64_encode,
	compute_ed25519_kid,
	ed25519_sign_from_seed,
)
from lang.driftc.packages.cert_claim_v1 import (
	CertClaim,
	CertClaimBody,
	CertClaimVerifyResult,
	CertSignature,
	CertSuite,
	DepGraphEntry,
	ResolvedDep,
	Toolchain,
	add_signature,
	body_signing_bytes,
	cert_claim_filename,
	check_dep_graph_covers,
	dump_cert_claim_json,
	find_dep_entry,
	load_cert_claim_json,
	make_cert_claim,
	sign_body,
	verify_cert_claim_for_module,
	verify_cert_claim_signatures,
)
from lang.driftc.packages.trust_v1 import (
	NamespaceRoles,
	TrustStore,
	TrustedKey,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _seed(label: str) -> bytes:
	return label.encode().ljust(32, b"_")[:32]


def _kid_for_seed(seed: bytes) -> str:
	_sig, pubkey_raw = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return compute_ed25519_kid(pubkey_raw)


def _pubkey_for_seed(seed: bytes) -> bytes:
	_sig, pubkey_raw = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return pubkey_raw


def _trust_with_certifier(
	*,
	kid: str,
	pubkey_raw: bytes,
	namespace: str = "singular.*",
) -> TrustStore:
	"""Build a TrustStore with one certifier-role kid for one namespace."""
	return TrustStore(
		keys_by_kid={kid: TrustedKey(algo="ed25519", kid=kid, pubkey_raw=pubkey_raw, label="")},
		roles_by_namespace={
			namespace: NamespaceRoles(
				authors=frozenset(),
				certifiers=frozenset({kid}),
			),
		},
		revoked_kids=frozenset(),
	)


def _toolchain() -> Toolchain:
	return Toolchain(
		driftc_version="0.31.108",
		drift_rt_abi=14,
		driftc_commit="abc123",
	)


def _cert_suite(*, result: str = "pass", suite_id: str = "drift.foundation/default") -> CertSuite:
	return CertSuite(
		id=suite_id,
		version="1.0.0",
		result=result,
		result_evidence_sha256="sha256:" + ("e" * 64),
	)


def _example_body(
	*,
	package_id: str = "singular",
	version: str = "0.3.0",
	artifact_kind: str = "package",
	artifact_path: str = "singular.zdmp",
	artifact_sha256: str = "sha256:" + ("d" * 64),
	source_content_id: str = "sha256:" + ("a" * 64),
	target: str = "drift-linux-x86_64",
	dep_graph: tuple[DepGraphEntry, ...] = (),
	cert_suite: CertSuite | None = None,
	run_id: str = "run-001",
	run_started_utc: str = "2026-05-18T12:00:00Z",
	evidence_sha256: str = "sha256:" + ("f" * 64),
) -> CertClaimBody:
	return CertClaimBody(
		schema_version=2,
		package_id=package_id,
		version=version,
		artifact_kind=artifact_kind,
		artifact_path=artifact_path,
		artifact_sha256=artifact_sha256,
		source_content_id=source_content_id,
		target=target,
		toolchain=_toolchain(),
		dep_graph=dep_graph,
		cert_suite=cert_suite or _cert_suite(),
		run_id=run_id,
		run_started_utc=run_started_utc,
		evidence_sha256=evidence_sha256,
	)


def _dep_entry(
	*,
	package_id: str = "mariadb-rpc",
	version: str = "0.5.0",
	artifact_sha256: str = "sha256:" + ("1" * 64),
	source_content_id: str = "sha256:" + ("2" * 64),
	author_kid: str | None = "ed25519:foundation_author",
	cert_kid: str | None = "ed25519:foundation_cert",
	dep_kind: str = "direct",
) -> DepGraphEntry:
	return DepGraphEntry(
		package_id=package_id,
		version=version,
		artifact_sha256=artifact_sha256,
		source_content_id=source_content_id,
		author_kid=author_kid,
		cert_kid=cert_kid,
		dep_kind=dep_kind,
	)


def _resolved_dep(
	*,
	package_id: str = "mariadb-rpc",
	version: str = "0.5.0",
	artifact_sha256: str = "sha256:" + ("1" * 64),
	source_content_id: str = "sha256:" + ("2" * 64),
) -> ResolvedDep:
	return ResolvedDep(
		package_id=package_id,
		version=version,
		artifact_sha256=artifact_sha256,
		source_content_id=source_content_id,
	)


# ── Canonical signing bytes ────────────────────────────────────────


def test_signing_bytes_deterministic() -> None:
	assert body_signing_bytes(_example_body()) == body_signing_bytes(_example_body())


def test_signing_bytes_change_with_artifact_sha() -> None:
	b1 = _example_body(artifact_sha256="sha256:" + ("d" * 64))
	b2 = _example_body(artifact_sha256="sha256:" + ("e" * 64))
	assert body_signing_bytes(b1) != body_signing_bytes(b2)


def test_signing_bytes_change_with_sci() -> None:
	b1 = _example_body(source_content_id="sha256:" + ("a" * 64))
	b2 = _example_body(source_content_id="sha256:" + ("b" * 64))
	assert body_signing_bytes(b1) != body_signing_bytes(b2)


def test_signing_bytes_independent_of_dep_graph_order() -> None:
	"""dep_graph sort key is (package_id, version); reorder must not
	change signing bytes."""
	e1 = _dep_entry(package_id="alpha", version="1.0.0")
	e2 = _dep_entry(package_id="beta", version="2.0.0")
	b1 = _example_body(dep_graph=(e1, e2))
	b2 = _example_body(dep_graph=(e2, e1))
	assert body_signing_bytes(b1) == body_signing_bytes(b2)


def test_signing_bytes_change_with_dep_artifact_sha() -> None:
	"""Changing any dep's artifact_sha256 changes the signature.
	O3 invariant: 'A certifier claim is only meaningful if changing
	any package in the resolved graph changes what the certifier
	signed.'"""
	e_v1 = _dep_entry(artifact_sha256="sha256:" + ("1" * 64))
	e_v2 = _dep_entry(artifact_sha256="sha256:" + ("9" * 64))
	b1 = _example_body(dep_graph=(e_v1,))
	b2 = _example_body(dep_graph=(e_v2,))
	assert body_signing_bytes(b1) != body_signing_bytes(b2)


def test_signing_bytes_change_with_cert_suite_id() -> None:
	"""O4: cert_suite.id changes the signed bytes (so a release-gate
	cert and a smoke-only cert are distinguishable)."""
	b1 = _example_body(cert_suite=_cert_suite(suite_id="drift.foundation/default"))
	b2 = _example_body(cert_suite=_cert_suite(suite_id="pushcoin/internal-stage"))
	assert body_signing_bytes(b1) != body_signing_bytes(b2)


def test_emit_rejects_duplicate_dep_graph_entries() -> None:
	"""Defense-in-depth: a hand-built dataclass with duplicate
	(package_id, version) tuples in dep_graph is rejected at
	canonicalization time."""
	e1 = _dep_entry(package_id="x", version="1.0.0", artifact_sha256="sha256:" + ("1" * 64))
	e2 = _dep_entry(package_id="x", version="1.0.0", artifact_sha256="sha256:" + ("9" * 64))
	body = _example_body(dep_graph=(e1, e2))
	with pytest.raises(ValueError, match="duplicate entry"):
		body_signing_bytes(body)


# ── artifact_kind + artifact_path (v2 body) ────────────────────────


def test_signing_bytes_change_with_artifact_kind() -> None:
	assert body_signing_bytes(_example_body(artifact_kind="package")) \
		!= body_signing_bytes(_example_body(artifact_kind="app"))


def test_signing_bytes_change_with_artifact_path() -> None:
	assert body_signing_bytes(_example_body(artifact_path="a.zdmp")) \
		!= body_signing_bytes(_example_body(artifact_path="b"))


def test_artifact_kind_path_round_trip() -> None:
	seed = _seed("certifier")
	claim = make_cert_claim(_example_body(artifact_kind="app", artifact_path="uflowsd"), seed)
	rt = load_cert_claim_json(dump_cert_claim_json(claim))
	assert rt.body.artifact_kind == "app"
	assert rt.body.artifact_path == "uflowsd"
	assert rt.body.schema_version == 2


def test_reject_missing_artifact_kind() -> None:
	body = _valid_body_dict()
	del body["artifact_kind"]
	with pytest.raises(ValueError, match="artifact_kind"):
		load_cert_claim_json(_wrap(body=body, signatures=[_valid_sig_record()]))


def test_reject_missing_artifact_path() -> None:
	body = _valid_body_dict()
	del body["artifact_path"]
	with pytest.raises(ValueError, match="artifact_path"):
		load_cert_claim_json(_wrap(body=body, signatures=[_valid_sig_record()]))


def test_reject_legacy_library_artifact_kind() -> None:
	body = _valid_body_dict()
	body["artifact_kind"] = "library"
	with pytest.raises(ValueError, match="artifact_kind"):
		load_cert_claim_json(_wrap(body=body, signatures=[_valid_sig_record()]))


def test_reject_unsafe_artifact_path() -> None:
	body = _valid_body_dict()
	body["artifact_path"] = "../escape"
	with pytest.raises(ValueError, match="artifact_path"):
		load_cert_claim_json(_wrap(body=body, signatures=[_valid_sig_record()]))


@pytest.mark.parametrize("bad_path", ["./uflowsd", "uflowsd/", "a\\b", "sub/../x"])
def test_reject_non_canonical_artifact_path(bad_path: str) -> None:
	"""A signed locator must have exactly one spelling — non-canonical forms
	are rejected, not silently normalized (the raw string is what's signed)."""
	body = _valid_body_dict()
	body["artifact_path"] = bad_path
	with pytest.raises(ValueError, match="artifact_path"):
		load_cert_claim_json(_wrap(body=body, signatures=[_valid_sig_record()]))


@pytest.mark.parametrize("good_path", ["uflowsd", "x.zdmp", "assets/singular/db/0001.sql"])
def test_accepts_canonical_artifact_path(good_path: str) -> None:
	seed = _seed("certifier")
	claim = make_cert_claim(_example_body(artifact_path=good_path), seed)
	rt = load_cert_claim_json(dump_cert_claim_json(claim))
	assert rt.body.artifact_path == good_path


# ── Sign + make + add_signature ────────────────────────────────────


def test_sign_body_produces_kid_matching_seed() -> None:
	seed = _seed("certifier")
	sig = sign_body(_example_body(), seed)
	assert sig.algo == "ed25519"
	assert sig.kid == _kid_for_seed(seed)
	assert len(sig.sig_raw) == 64


def test_make_cert_claim_has_one_signature() -> None:
	seed = _seed("certifier")
	claim = make_cert_claim(_example_body(), seed)
	assert len(claim.signatures) == 1


def test_add_signature_co_signs_same_body() -> None:
	"""Key rotation / multi-region orch under one certifier identity:
	add a second signature without changing the body."""
	seed_a = _seed("primary")
	seed_b = _seed("rotated")
	c1 = make_cert_claim(_example_body(), seed_a)
	c2 = add_signature(c1, seed_b)
	assert c1.body is c2.body
	assert len(c2.signatures) == 2


# ── JSON round-trip ────────────────────────────────────────────────


def test_dump_then_load_round_trip() -> None:
	seed = _seed("certifier")
	dep = _dep_entry()
	body = _example_body(dep_graph=(dep,))
	claim = make_cert_claim(body, seed)
	text = dump_cert_claim_json(claim)
	reloaded = load_cert_claim_json(text)
	assert reloaded.body == claim.body
	assert len(reloaded.signatures) == 1
	assert reloaded.signatures[0].sig_raw == claim.signatures[0].sig_raw


def test_dump_is_deterministic() -> None:
	seed = _seed("certifier")
	claim = make_cert_claim(_example_body(), seed)
	assert dump_cert_claim_json(claim) == dump_cert_claim_json(claim)


def test_load_round_trip_recovers_signing_bytes() -> None:
	"""Reload must produce the same body bytes the signer signed
	over.  Signature contract survives serialization."""
	seed = _seed("certifier")
	claim = make_cert_claim(_example_body(), seed)
	text = dump_cert_claim_json(claim)
	reloaded = load_cert_claim_json(text)
	assert body_signing_bytes(reloaded.body) == body_signing_bytes(claim.body)


# ── Strict loader (format v1, body schema v2): unknown-key rejection ──


def _valid_body_dict() -> dict:
	return {
		"schema_version": 2,
		"package_id": "x",
		"version": "0.1.0",
		"artifact_kind": "package",
		"artifact_path": "x.zdmp",
		"artifact_sha256": "sha256:" + ("d" * 64),
		"source_content_id": "sha256:" + ("a" * 64),
		"target": "drift-linux-x86_64",
		"toolchain": {
			"driftc_version": "0.31.108",
			"drift_rt_abi": 14,
			"driftc_commit": "abc123",
		},
		"dep_graph": [],
		"cert_suite": {
			"id": "drift.foundation/default",
			"version": "1.0.0",
			"result": "pass",
			"result_evidence_sha256": "sha256:" + ("e" * 64),
		},
		"run_id": "run-001",
		"run_started_utc": "2026-05-18T00:00:00Z",
		"evidence_sha256": "sha256:" + ("f" * 64),
	}


def _valid_sig_record() -> dict:
	return {"algo": "ed25519", "kid": "ed25519:k", "sig": b64_encode(b"\x00" * 64)}


def _wrap(*, body=None, signatures=None, format="drift-cert-claim", version=1) -> str:
	return json.dumps({
		"format": format,
		"version": version,
		"body": body if body is not None else _valid_body_dict(),
		"signatures": signatures if signatures is not None else [_valid_sig_record()],
	})


def test_strict_v1_rejects_unknown_envelope_key() -> None:
	envelope = {
		"format": "drift-cert-claim",
		"version": 1,
		"body": _valid_body_dict(),
		"signatures": [_valid_sig_record()],
		"smuggled": "value",
	}
	with pytest.raises(ValueError, match="unknown field"):
		load_cert_claim_json(json.dumps(envelope))


def test_strict_v1_rejects_unknown_body_key() -> None:
	body = _valid_body_dict()
	body["future_v2_field"] = "x"
	with pytest.raises(ValueError, match="unknown field.*future_v2_field"):
		load_cert_claim_json(_wrap(body=body))


def test_strict_v1_rejects_unknown_toolchain_key() -> None:
	body = _valid_body_dict()
	body["toolchain"]["uname"] = "linux"
	with pytest.raises(ValueError, match="unknown field.*uname"):
		load_cert_claim_json(_wrap(body=body))


def test_strict_v1_rejects_unknown_dep_graph_entry_key() -> None:
	body = _valid_body_dict()
	body["dep_graph"] = [{
		"package_id": "x",
		"version": "1.0.0",
		"artifact_sha256": "sha256:" + ("1" * 64),
		"source_content_id": "sha256:" + ("2" * 64),
		"author_kid": None,
		"cert_kid": None,
		"dep_kind": "direct",
		"build_tag": "smuggled",
	}]
	with pytest.raises(ValueError, match="unknown field.*build_tag"):
		load_cert_claim_json(_wrap(body=body))


def test_strict_v1_rejects_unknown_cert_suite_key() -> None:
	body = _valid_body_dict()
	body["cert_suite"]["passed_at"] = "2026-05-18"
	with pytest.raises(ValueError, match="unknown field.*passed_at"):
		load_cert_claim_json(_wrap(body=body))


def test_strict_v1_rejects_unknown_signature_key() -> None:
	sig = _valid_sig_record()
	sig["expires_at"] = "2027-01-01"
	with pytest.raises(ValueError, match="unknown field.*expires_at"):
		load_cert_claim_json(_wrap(signatures=[sig]))


# ── Strict loader: shape/value rejections ──────────────────────────


def test_reject_wrong_format() -> None:
	with pytest.raises(ValueError, match="unsupported cert claim format"):
		load_cert_claim_json(_wrap(format="not-drift-cert-claim"))


def test_reject_wrong_version() -> None:
	with pytest.raises(ValueError, match="unsupported cert claim version"):
		load_cert_claim_json(_wrap(version=0))


def test_reject_v2_envelope() -> None:
	with pytest.raises(ValueError, match="unsupported cert claim version"):
		load_cert_claim_json(_wrap(version=2))


def test_reject_empty_signatures() -> None:
	with pytest.raises(ValueError, match="at least one signature"):
		load_cert_claim_json(_wrap(signatures=[]))


def test_reject_invalid_cert_suite_result() -> None:
	"""result must be 'pass' or 'fail' (loader); any other value is
	a malformed claim."""
	body = _valid_body_dict()
	body["cert_suite"]["result"] = "maybe"
	with pytest.raises(ValueError, match="cert_suite.result"):
		load_cert_claim_json(_wrap(body=body))


def test_load_accepts_fail_result() -> None:
	"""A result=='fail' cert claim is well-formed (loader accepts);
	the verifier rejects at composition time per gate 4."""
	body = _valid_body_dict()
	body["cert_suite"]["result"] = "fail"
	claim = load_cert_claim_json(_wrap(body=body))
	assert claim.body.cert_suite.result == "fail"


def test_reject_bad_dep_kind() -> None:
	body = _valid_body_dict()
	body["dep_graph"] = [{
		"package_id": "x",
		"version": "1.0.0",
		"artifact_sha256": "sha256:" + ("1" * 64),
		"source_content_id": "sha256:" + ("2" * 64),
		"author_kid": None,
		"cert_kid": None,
		"dep_kind": "indirect",  # not in v1
	}]
	with pytest.raises(ValueError, match="dep_kind"):
		load_cert_claim_json(_wrap(body=body))


def test_reject_duplicate_dep_graph_at_load() -> None:
	body = _valid_body_dict()
	body["dep_graph"] = [
		{
			"package_id": "x",
			"version": "1.0.0",
			"artifact_sha256": "sha256:" + ("1" * 64),
			"source_content_id": "sha256:" + ("2" * 64),
			"author_kid": None,
			"cert_kid": None,
			"dep_kind": "direct",
		},
		{
			"package_id": "x",
			"version": "1.0.0",   # same as above
			"artifact_sha256": "sha256:" + ("9" * 64),
			"source_content_id": "sha256:" + ("8" * 64),
			"author_kid": None,
			"cert_kid": None,
			"dep_kind": "transitive",
		},
	]
	with pytest.raises(ValueError, match="duplicate"):
		load_cert_claim_json(_wrap(body=body))


def test_reject_bad_sci_shape_artifact() -> None:
	body = _valid_body_dict()
	body["artifact_sha256"] = "not-a-sha"
	with pytest.raises(ValueError, match="artifact_sha256"):
		load_cert_claim_json(_wrap(body=body))


def test_reject_non_ed25519_signature_algo() -> None:
	sig = _valid_sig_record()
	sig["algo"] = "rsa"
	with pytest.raises(ValueError, match="ed25519"):
		load_cert_claim_json(_wrap(signatures=[sig]))


def test_reject_wrong_sig_length() -> None:
	sig = {"algo": "ed25519", "kid": "ed25519:k", "sig": b64_encode(b"\x00" * 32)}
	with pytest.raises(ValueError, match="64 bytes"):
		load_cert_claim_json(_wrap(signatures=[sig]))


def test_drift_rt_abi_must_be_integer() -> None:
	body = _valid_body_dict()
	body["toolchain"]["drift_rt_abi"] = "14"  # string, not int
	with pytest.raises(ValueError, match="drift_rt_abi"):
		load_cert_claim_json(_wrap(body=body))


# ── Signature verification (low-level) ─────────────────────────────


def test_verify_signatures_returns_trusted_kids() -> None:
	seed = _seed("certifier")
	claim = make_cert_claim(_example_body(), seed)
	kid = _kid_for_seed(seed)
	trust = _trust_with_certifier(kid=kid, pubkey_raw=_pubkey_for_seed(seed))
	assert verify_cert_claim_signatures(claim, trust) == {kid}


def test_verify_signatures_skips_unknown_signer() -> None:
	seed_known = _seed("known")
	seed_unknown = _seed("unknown")
	claim = make_cert_claim(_example_body(), seed_known)
	claim = add_signature(claim, seed_unknown)
	trust = _trust_with_certifier(
		kid=_kid_for_seed(seed_known),
		pubkey_raw=_pubkey_for_seed(seed_known),
	)
	verified = verify_cert_claim_signatures(claim, trust)
	assert verified == {_kid_for_seed(seed_known)}


def test_verify_signatures_rejects_tampered_body() -> None:
	seed = _seed("certifier")
	original = make_cert_claim(_example_body(), seed)
	tampered_body = _example_body(artifact_sha256="sha256:" + ("c" * 64))
	tampered = CertClaim(body=tampered_body, signatures=original.signatures)
	trust = _trust_with_certifier(
		kid=_kid_for_seed(seed),
		pubkey_raw=_pubkey_for_seed(seed),
	)
	assert verify_cert_claim_signatures(tampered, trust) == set()


# ── dep_graph closure check (O3) ──────────────────────────────────


def test_find_dep_entry_present() -> None:
	dep = _dep_entry(package_id="foo", version="1.0.0")
	body = _example_body(dep_graph=(dep,))
	claim = CertClaim(body=body, signatures=())
	assert find_dep_entry(claim, package_id="foo", version="1.0.0") == dep


def test_find_dep_entry_absent() -> None:
	body = _example_body(dep_graph=())
	claim = CertClaim(body=body, signatures=())
	assert find_dep_entry(claim, package_id="missing", version="1.0.0") is None


def test_dep_graph_covers_empty_closure() -> None:
	body = _example_body(dep_graph=())
	claim = CertClaim(body=body, signatures=())
	assert check_dep_graph_covers(claim, []) is None


def test_dep_graph_covers_full_closure() -> None:
	dep = _dep_entry()
	body = _example_body(dep_graph=(dep,))
	claim = CertClaim(body=body, signatures=())
	assert check_dep_graph_covers(claim, [_resolved_dep()]) is None


def test_dep_graph_missing_entry_rejected() -> None:
	"""Consumer loads a dep the certifier did not attest."""
	body = _example_body(dep_graph=())
	claim = CertClaim(body=body, signatures=())
	err = check_dep_graph_covers(claim, [_resolved_dep(package_id="orphan")])
	assert err is not None
	assert "missing entry" in err
	assert "orphan" in err


def test_dep_graph_artifact_mismatch_rejected() -> None:
	"""Consumer's dep artifact_sha256 differs from cert claim's entry."""
	dep = _dep_entry(artifact_sha256="sha256:" + ("1" * 64))
	body = _example_body(dep_graph=(dep,))
	claim = CertClaim(body=body, signatures=())
	err = check_dep_graph_covers(claim, [
		_resolved_dep(artifact_sha256="sha256:" + ("9" * 64))
	])
	assert err is not None
	assert "artifact_sha256 mismatch" in err


def test_dep_graph_sci_mismatch_rejected() -> None:
	dep = _dep_entry(source_content_id="sha256:" + ("2" * 64))
	body = _example_body(dep_graph=(dep,))
	claim = CertClaim(body=body, signatures=())
	err = check_dep_graph_covers(claim, [
		_resolved_dep(source_content_id="sha256:" + ("9" * 64))
	])
	assert err is not None
	assert "source_content_id mismatch" in err


def test_dep_graph_extra_entries_allowed() -> None:
	"""Certifier may have attested a LARGER graph than this consumer
	loaded (e.g. dev-only deps).  Only the consumer's actual closure
	must be covered; extras in the cert claim are fine."""
	dep_consumed = _dep_entry(package_id="mariadb-rpc", version="0.5.0")
	dep_extra = _dep_entry(package_id="dev-tool", version="0.1.0",
	                       artifact_sha256="sha256:" + ("3" * 64),
	                       source_content_id="sha256:" + ("4" * 64))
	body = _example_body(dep_graph=(dep_consumed, dep_extra))
	claim = CertClaim(body=body, signatures=())
	# Consumer loaded only mariadb-rpc.
	assert check_dep_graph_covers(claim, [_resolved_dep()]) is None


# ── Full composition: verify_cert_claim_for_module ────────────────


def _full_setup(*, seed_label: str = "fdn_cert", namespace: str = "singular.*"):
	"""Convenience: build a claim, trust, and resolved closure that
	all line up for the happy path."""
	seed = _seed(seed_label)
	dep = _dep_entry()
	body = _example_body(dep_graph=(dep,))
	claim = make_cert_claim(body, seed)
	trust = _trust_with_certifier(
		kid=_kid_for_seed(seed),
		pubkey_raw=_pubkey_for_seed(seed),
		namespace=namespace,
	)
	closure = [_resolved_dep()]
	return claim, trust, closure


def test_verify_for_module_happy_path() -> None:
	claim, trust, closure = _full_setup()
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=closure,
	)
	assert res.ok is True
	assert res.accepted_kid is not None
	assert res.reason == ""


def test_verify_rejects_artifact_sha_mismatch() -> None:
	claim, trust, closure = _full_setup()
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256="sha256:" + ("9" * 64),  # different
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=closure,
	)
	assert res.ok is False
	assert "artifact_sha256" in res.reason


def test_verify_rejects_sci_mismatch() -> None:
	claim, trust, closure = _full_setup()
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id="sha256:" + ("9" * 64),
		resolved_closure=closure,
	)
	assert res.ok is False
	assert "source_content_id" in res.reason


def test_verify_rejects_failing_cert_suite() -> None:
	"""Gate 4: cert_suite.result != 'pass' is rejected."""
	seed = _seed("cert")
	body = _example_body(cert_suite=_cert_suite(result="fail"))
	claim = make_cert_claim(body, seed)
	trust = _trust_with_certifier(kid=_kid_for_seed(seed), pubkey_raw=_pubkey_for_seed(seed))
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=[],
	)
	assert res.ok is False
	assert "cert_suite.result" in res.reason


def test_verify_rejects_dep_graph_gap() -> None:
	claim, trust, _ = _full_setup()
	# Consumer claims an additional dep the cert didn't attest.
	closure = [_resolved_dep(), _resolved_dep(package_id="orphan", version="0.1.0",
	                                          artifact_sha256="sha256:" + ("9" * 64),
	                                          source_content_id="sha256:" + ("8" * 64))]
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=closure,
	)
	assert res.ok is False
	assert "missing entry" in res.reason


def test_verify_rejects_untrusted_certifier_kid() -> None:
	"""Signer kid is not in `allowed_certifiers_for_module`."""
	seed_signer = _seed("untrusted_signer")
	dep = _dep_entry()
	body = _example_body(dep_graph=(dep,))
	claim = make_cert_claim(body, seed_signer)
	# Trust knows a DIFFERENT certifier kid.
	seed_trusted = _seed("trusted_cert")
	trust = _trust_with_certifier(
		kid=_kid_for_seed(seed_trusted),
		pubkey_raw=_pubkey_for_seed(seed_trusted),
	)
	closure = [_resolved_dep()]
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=closure,
	)
	assert res.ok is False
	assert "no signature on cert claim verifies" in res.reason


def test_verify_rejects_signer_with_wrong_role() -> None:
	"""Signer kid is in the trust store and signature verifies, but
	the kid is registered in the AUTHOR role (not certifier)."""
	seed = _seed("author_only_kid")
	dep = _dep_entry()
	body = _example_body(dep_graph=(dep,))
	claim = make_cert_claim(body, seed)
	kid = _kid_for_seed(seed)
	# Trust knows the kid but only in the authors role.
	trust = TrustStore(
		keys_by_kid={kid: TrustedKey(algo="ed25519", kid=kid, pubkey_raw=_pubkey_for_seed(seed), label="")},
		roles_by_namespace={
			"singular.*": NamespaceRoles(
				authors=frozenset({kid}),
				certifiers=frozenset(),
			),
		},
		revoked_kids=frozenset(),
	)
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=[_resolved_dep()],
	)
	assert res.ok is False
	# Trust has no certifier-role kid for this namespace.
	assert "no certifier-role kids" in res.reason


def test_verify_rejects_revoked_certifier() -> None:
	seed = _seed("cert")
	dep = _dep_entry()
	body = _example_body(dep_graph=(dep,))
	claim = make_cert_claim(body, seed)
	kid = _kid_for_seed(seed)
	trust = TrustStore(
		keys_by_kid={kid: TrustedKey(algo="ed25519", kid=kid, pubkey_raw=_pubkey_for_seed(seed), label="")},
		roles_by_namespace={
			"singular.*": NamespaceRoles(
				authors=frozenset(),
				certifiers=frozenset({kid}),
			),
		},
		revoked_kids=frozenset({kid}),
	)
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=[_resolved_dep()],
	)
	assert res.ok is False
	assert "no certifier-role kids" in res.reason


def test_verify_require_certifier_match() -> None:
	"""O7: --require-certifier flag, happy path."""
	claim, trust, closure = _full_setup()
	signing_kid = claim.signatures[0].kid
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=closure,
		require_certifier=signing_kid,
	)
	assert res.ok is True
	assert res.accepted_kid == signing_kid


def test_verify_require_certifier_mismatch() -> None:
	"""O7: --require-certifier with a different kid is rejected even
	if other trusted certifiers DID sign."""
	claim, trust, closure = _full_setup()
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=closure,
		require_certifier="ed25519:not_the_signing_kid",
	)
	assert res.ok is False
	assert "required certifier kid" in res.reason


def test_verify_require_cert_suite_match() -> None:
	"""O4: --require-cert-suite, happy path."""
	claim, trust, closure = _full_setup()
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=closure,
		require_cert_suite="drift.foundation/default",
	)
	assert res.ok is True


def test_verify_require_cert_suite_mismatch() -> None:
	"""O4: distinguishing release-gate from smoke-only."""
	seed = _seed("cert")
	body = _example_body(cert_suite=_cert_suite(suite_id="pushcoin/internal-stage"))
	claim = make_cert_claim(body, seed)
	trust = _trust_with_certifier(kid=_kid_for_seed(seed), pubkey_raw=_pubkey_for_seed(seed))
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=[],
		require_cert_suite="drift.foundation/default",   # different from what was signed
	)
	assert res.ok is False
	assert "cert_suite.id" in res.reason


# ── Package identity pinning (HIGH security pin) ──────────────────


def test_verify_rejects_package_id_mismatch() -> None:
	"""HIGH SECURITY PIN: cert claim for package 'evil' must not pass
	verification when the caller expects package 'singular', even if
	artifact_sha, SCI, dep_graph, and certifier role all line up."""
	seed = _seed("cert")
	# Claim is for package "evil".
	body = _example_body(package_id="evil", dep_graph=(_dep_entry(),))
	claim = make_cert_claim(body, seed)
	trust = _trust_with_certifier(kid=_kid_for_seed(seed), pubkey_raw=_pubkey_for_seed(seed))
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id="singular",  # caller expects different package
		expected_version=claim.body.version,
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=[_resolved_dep()],
	)
	assert res.ok is False
	assert "package_id" in res.reason
	assert "evil" in res.reason


def test_verify_rejects_version_mismatch() -> None:
	"""HIGH SECURITY PIN: cert claim for version 0.2.0 must not pass
	verification when the caller expects version 0.3.0 (downgrade)."""
	seed = _seed("cert")
	body = _example_body(version="0.2.0", dep_graph=(_dep_entry(),))
	claim = make_cert_claim(body, seed)
	trust = _trust_with_certifier(kid=_kid_for_seed(seed), pubkey_raw=_pubkey_for_seed(seed))
	res = verify_cert_claim_for_module(
		claim, trust, "singular.api",
		expected_package_id=claim.body.package_id,
		expected_version="0.3.0",  # caller expects newer
		artifact_sha256=claim.body.artifact_sha256,
		expected_source_content_id=claim.body.source_content_id,
		resolved_closure=[_resolved_dep()],
	)
	assert res.ok is False
	assert "version" in res.reason
	assert "0.2.0" in res.reason


# ── validate_body_shape (emit-side contract) ───────────────────────


def test_sign_rejects_invalid_cert_suite_result() -> None:
	"""Hand-built dataclass with cert_suite.result='maybe' fails at
	sign time.  The loader rejects 'maybe' at load — the emit side
	now matches."""
	bad_suite = CertSuite(
		id="x", version="1", result="maybe",  # invalid
		result_evidence_sha256="sha256:" + ("e" * 64),
	)
	body = _example_body(cert_suite=bad_suite)
	with pytest.raises(ValueError, match="cert_suite.result"):
		body_signing_bytes(body)


def test_sign_rejects_invalid_dep_kind() -> None:
	bad_dep = DepGraphEntry(
		package_id="x", version="1.0.0",
		artifact_sha256="sha256:" + ("1" * 64),
		source_content_id="sha256:" + ("2" * 64),
		author_kid=None, cert_kid=None,
		dep_kind="indirect",   # not in v1
	)
	body = _example_body(dep_graph=(bad_dep,))
	with pytest.raises(ValueError, match="dep_kind"):
		body_signing_bytes(body)


def test_sign_rejects_bad_artifact_sha_shape() -> None:
	body = _example_body(artifact_sha256="not-a-sha")
	with pytest.raises(ValueError, match="artifact_sha256"):
		body_signing_bytes(body)


def test_sign_rejects_bad_sci_shape() -> None:
	body = _example_body(source_content_id="not-a-sha")
	with pytest.raises(ValueError, match="source_content_id"):
		body_signing_bytes(body)


def test_sign_rejects_non_int_drift_rt_abi() -> None:
	bad_tc = Toolchain(driftc_version="0.31.108", drift_rt_abi="14", driftc_commit="abc")  # type: ignore[arg-type]
	body = CertClaimBody(
		schema_version=2, package_id="x", version="0.1.0",
		artifact_kind="package", artifact_path="x.zdmp",
		artifact_sha256="sha256:" + ("d" * 64),
		source_content_id="sha256:" + ("a" * 64),
		target="t", toolchain=bad_tc, dep_graph=(),
		cert_suite=_cert_suite(),
		run_id="r", run_started_utc="2026-05-18T00:00:00Z",
		evidence_sha256="sha256:" + ("f" * 64),
	)
	with pytest.raises(ValueError, match="drift_rt_abi"):
		body_signing_bytes(body)


def test_sign_rejects_empty_package_id() -> None:
	body = _example_body(package_id="")
	with pytest.raises(ValueError, match="package_id"):
		body_signing_bytes(body)


# ── Per-certifier filename (O1) ────────────────────────────────────


def test_filename_simple_ascii() -> None:
	"""Pure-ASCII kid would be unusual but is allowed."""
	fn = cert_claim_filename("singular", "abcDEF123")
	assert fn == "singular.cert-claim.abcDEF123.json"


def test_filename_url_encodes_unsafe_chars_in_kid() -> None:
	"""ed25519 kids carry `:` and base64 padding `=`; the filename
	URL-encodes them so the on-disk name is portable.  Readers parse
	the canonical kid from the file body, not the filename."""
	fn = cert_claim_filename("singular", "ed25519:abc=")
	# ':' -> %3A; '=' -> %3D.  Letters and digits pass through.
	assert fn == "singular.cert-claim.ed25519%3Aabc%3D.json"


def test_filename_url_encodes_unsafe_chars_in_package_id() -> None:
	"""Package ids may contain characters that need escaping for
	filesystem safety.  Defense-in-depth: encode the package_id too.
	If a package id ever contains `/`, `:`, `..`, or spaces, the
	filename must not encode those characters literally (path
	traversal / portability risk)."""
	# `/` -> %2F.
	fn = cert_claim_filename("a/b", "ed25519:k")
	assert fn == "a%2Fb.cert-claim.ed25519%3Ak.json"
	# `..` -> two literal dots, which ARE safe in the filename
	# character set (dot is in `[A-Za-z0-9._-]`).  But spaces, `:`,
	# and other characters must encode.
	fn2 = cert_claim_filename("pkg with space", "ed25519:k")
	assert "%20" in fn2   # space encoded
	# `:` in package id (would be path-shaped on some filesystems).
	fn3 = cert_claim_filename("scoped:pkg", "ed25519:k")
	assert "%3A" in fn3.split(".cert-claim.")[0]


def test_filename_rejects_empty_package_id() -> None:
	with pytest.raises(ValueError, match="package_id"):
		cert_claim_filename("", "ed25519:abc")


def test_filename_rejects_empty_kid() -> None:
	with pytest.raises(ValueError, match="certifier_kid"):
		cert_claim_filename("pkg", "")


# ── End-to-end via sidecar text ────────────────────────────────────


def test_end_to_end_via_json_sidecar(tmp_path: Path) -> None:
	"""Sign → dump → save → load → verify, end to end."""
	seed = _seed("cert_e2e")
	dep = _dep_entry()
	body = _example_body(dep_graph=(dep,))
	claim = make_cert_claim(body, seed)
	fn = cert_claim_filename(body.package_id, claim.signatures[0].kid)
	sidecar = tmp_path / fn
	sidecar.write_text(dump_cert_claim_json(claim))

	reloaded = load_cert_claim_json(sidecar.read_text())
	trust = _trust_with_certifier(
		kid=_kid_for_seed(seed),
		pubkey_raw=_pubkey_for_seed(seed),
		namespace="singular.*",
	)
	res = verify_cert_claim_for_module(
		reloaded, trust, "singular.api",
		expected_package_id=reloaded.body.package_id,
		expected_version=reloaded.body.version,
		artifact_sha256=reloaded.body.artifact_sha256,
		expected_source_content_id=reloaded.body.source_content_id,
		resolved_closure=[_resolved_dep()],
	)
	assert res.ok is True
