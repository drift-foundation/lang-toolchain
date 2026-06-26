# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Unit regression for `tools/drift_deploy/cert_emit.py`.

Coverage:

  - certifier seed loader parses canonical base64, rejects malformed
    inputs (separate from the author seed loader so a regression in
    one role doesn't silently fix the other);
  - `sign_and_write_cert_claim` writes the canonical per-kid sidecar
    filename, round-trips through `load_cert_claim_json`, and the
    signature verifies against the seed's pubkey;
  - overwrite protection refuses to clobber an existing
    `(package_id, kid)` sidecar;
  - independent certifiers (different kids) on the same release
    coexist — separate sidecar files, no collision;
  - `add_cert_signature_to_claim_file` appends a rotation-co-sig to
    an existing sidecar without rewriting the body bytes.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from lang.drift.crypto import (
	compute_ed25519_kid,
	ed25519_sign_from_seed,
	verify_ed25519,
)
from lang.driftc.packages.cert_claim_v1 import (
	CertClaimBody,
	CertSuite,
	DepGraphEntry,
	Toolchain,
	body_signing_bytes,
	load_cert_claim_json,
	make_cert_claim_body,
)
from lang.driftc.packages.sidecar_naming import cert_claim_filename
from tools.drift_deploy.cert_emit import (
	SignCertClaimOptions,
	add_cert_signature_to_claim_file,
	decode_cert_seed32,
	load_cert_seed32,
	sign_and_write_cert_claim,
)


# ── Fixtures ──────────────────────────────────────────────────────


def _seed_a() -> bytes:
	return bytes(range(32))


def _seed_b() -> bytes:
	return bytes((b ^ 0xFF) for b in _seed_a())


def _sample_body(package_id: str = "demo.lib", version: str = "1.0.0") -> CertClaimBody:
	return make_cert_claim_body(
		package_id=package_id,
		version=version,
		artifact_kind="package",
		artifact_path=f"{package_id}.zdmp",
		artifact_sha256="sha256:" + ("c" * 64),
		source_content_id="sha256:" + ("a" * 64),
		target="linux-x86_64",
		toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit=""),
		dep_graph=(
			DepGraphEntry(
				package_id="std",
				version="1.0.0",
				artifact_sha256="sha256:" + ("d" * 64),
				source_content_id="sha256:" + ("e" * 64),
				author_kid=None,
				cert_kid=None,
				dep_kind="direct",
			),
		),
		cert_suite=CertSuite(
			id="anthropic/release-gate",
			version="1.0",
			result="pass",
			result_evidence_sha256="sha256:" + ("f" * 64),
		),
		run_id="run-001",
		run_started_utc="2026-01-01T00:00:00Z",
		evidence_sha256="sha256:" + ("0" * 64),
	)


# ── Certifier seed loader ─────────────────────────────────────────


def test_decode_cert_seed32_accepts_canonical_base64() -> None:
	seed = _seed_a()
	assert decode_cert_seed32(base64.b64encode(seed).decode("ascii")) == seed


def test_decode_cert_seed32_rejects_empty() -> None:
	with pytest.raises(ValueError) as exc:
		decode_cert_seed32("")
	# Diagnostic must say "certifier" (not "author") so a confused
	# user knows which key role failed to load.
	assert "certifier" in str(exc.value)


def test_decode_cert_seed32_rejects_wrong_length() -> None:
	short = base64.b64encode(b"too-short").decode("ascii")
	with pytest.raises(ValueError) as exc:
		decode_cert_seed32(short)
	assert "certifier" in str(exc.value)
	assert "32" in str(exc.value)


def test_load_cert_seed32_from_file(tmp_path: Path) -> None:
	seed = _seed_a()
	p = tmp_path / "cert.seed"
	p.write_text(base64.b64encode(seed).decode("ascii"), encoding="utf-8")
	assert load_cert_seed32(p) == seed


def test_load_cert_seed32_missing_file_raises(tmp_path: Path) -> None:
	with pytest.raises(FileNotFoundError):
		load_cert_seed32(tmp_path / "does-not-exist.seed")


# ── sign_and_write_cert_claim ─────────────────────────────────────


def test_sign_and_write_produces_per_kid_filename(tmp_path: Path) -> None:
	body = _sample_body()
	seed = _seed_a()
	_, pubkey_raw = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	expected_kid = compute_ed25519_kid(pubkey_raw)

	written = sign_and_write_cert_claim(SignCertClaimOptions(
		body=body, seed32=seed, sidecar_dir=tmp_path,
	))
	assert written.name == cert_claim_filename(body.package_id, expected_kid)


def test_sign_and_write_round_trip_loads(tmp_path: Path) -> None:
	body = _sample_body()
	written = sign_and_write_cert_claim(SignCertClaimOptions(
		body=body, seed32=_seed_a(), sidecar_dir=tmp_path,
	))
	claim = load_cert_claim_json(written.read_text(encoding="utf-8"))
	assert claim.body == body
	assert len(claim.signatures) == 1


def test_sign_and_write_signature_verifies(tmp_path: Path) -> None:
	body = _sample_body()
	seed = _seed_a()
	_, pubkey_raw = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	written = sign_and_write_cert_claim(SignCertClaimOptions(
		body=body, seed32=seed, sidecar_dir=tmp_path,
	))
	claim = load_cert_claim_json(written.read_text(encoding="utf-8"))
	assert verify_ed25519(
		pubkey_raw=pubkey_raw,
		message=body_signing_bytes(claim.body),
		signature_raw=claim.signatures[0].sig_raw,
	)


def test_independent_certifiers_write_separate_files(tmp_path: Path) -> None:
	"""Per O1, two independent certifiers attesting the same release
	write SEPARATE sidecars.  No collision, no append.  The
	verifier later finds both via prefix scan and either may
	satisfy `--require-certifier`."""
	body = _sample_body()
	path_a = sign_and_write_cert_claim(SignCertClaimOptions(
		body=body, seed32=_seed_a(), sidecar_dir=tmp_path,
	))
	path_b = sign_and_write_cert_claim(SignCertClaimOptions(
		body=body, seed32=_seed_b(), sidecar_dir=tmp_path,
	))
	assert path_a != path_b
	assert path_a.is_file() and path_b.is_file()
	# Both sidecars have the same prefix but different kid suffixes.
	from lang.driftc.packages.sidecar_naming import cert_claim_filename_prefix
	prefix = cert_claim_filename_prefix(body.package_id)
	assert path_a.name.startswith(prefix)
	assert path_b.name.startswith(prefix)


def test_sign_and_write_refuses_overwrite_for_same_kid(tmp_path: Path) -> None:
	body = _sample_body()
	seed = _seed_a()
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=body, seed32=seed, sidecar_dir=tmp_path,
	))
	with pytest.raises(FileExistsError) as exc:
		sign_and_write_cert_claim(SignCertClaimOptions(
			body=body, seed32=seed, sidecar_dir=tmp_path,
		))
	# Diagnostic should point the user at the rotation flow.
	assert "rotation" in str(exc.value)


def test_sign_and_write_overwrite_true_replaces(tmp_path: Path) -> None:
	body_v1 = _sample_body(version="1.0.0")
	body_v2 = _sample_body(version="1.0.1")
	seed = _seed_a()
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=body_v1, seed32=seed, sidecar_dir=tmp_path,
	))
	written = sign_and_write_cert_claim(SignCertClaimOptions(
		body=body_v2, seed32=seed, sidecar_dir=tmp_path, overwrite=True,
	))
	claim = load_cert_claim_json(written.read_text(encoding="utf-8"))
	assert claim.body.version == "1.0.1"


# ── add_cert_signature_to_claim_file (rotation co-sign) ────────────


def test_add_signature_appends_without_changing_body(tmp_path: Path) -> None:
	"""Rotation-co-sign: two signatures from different keys under
	the SAME certifier identity sign the same body bytes.  Used for
	regional orch or rolling key rotation."""
	body = _sample_body()
	seed_a = _seed_a()
	seed_b = _seed_b()
	_, pub_a = ed25519_sign_from_seed(priv_seed32=seed_a, message=b"")
	kid_a = compute_ed25519_kid(pub_a)

	sign_and_write_cert_claim(SignCertClaimOptions(
		body=body, seed32=seed_a, sidecar_dir=tmp_path,
	))
	add_cert_signature_to_claim_file(
		sidecar_dir=tmp_path,
		package_id=body.package_id,
		current_certifier_kid=kid_a,
		seed32=seed_b,
	)
	claim = load_cert_claim_json(
		(tmp_path / cert_claim_filename(body.package_id, kid_a)).read_text(encoding="utf-8")
	)
	assert claim.body == body
	assert len(claim.signatures) == 2


def test_add_signature_to_missing_file_raises(tmp_path: Path) -> None:
	with pytest.raises(FileNotFoundError):
		add_cert_signature_to_claim_file(
			sidecar_dir=tmp_path,
			package_id="never.published",
			current_certifier_kid="ed25519:bogus",
			seed32=_seed_a(),
		)
