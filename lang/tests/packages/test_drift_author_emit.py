# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Unit regression for `tools/drift_author/`'s author-claim emit.

Coverage:

  - `key_loader` accepts the canonical base64 form, rejects empty
    files, junk base64, and seeds of the wrong length.
  - `sign_and_write_author_claim` produces a sidecar whose contents
    round-trip through `load_author_claim_json` AND whose signature
    verifies against the public key derived from the seed.
  - The on-disk filename matches `sidecar_naming.author_claim_filename`
    exactly (so discovery finds it without renaming).
  - Overwrite protection: a second `sign_and_write_author_claim`
    on the same release fails closed unless `overwrite=True`.
  - `add_signature_to_claim_file` appends a co-author signature
    without rewriting the body bytes (the appended sig signs the
    same canonical message the lead author signed).
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from lang.drift.crypto import (
	b64_decode,
	compute_ed25519_kid,
	ed25519_sign_from_seed,
	verify_ed25519,
)
from lang.driftc.packages.author_claim_v1 import (
	AuthorClaimBody,
	RequiredDep,
	body_signing_bytes,
	load_author_claim_json,
	make_author_claim_body,
)
from lang.driftc.packages.sidecar_naming import author_claim_filename
from tools.drift_author import (
	SignAuthorClaimOptions,
	add_signature_to_claim_file,
	decode_author_seed32,
	load_author_seed32,
	sign_and_write_author_claim,
)


# ── Helpers ───────────────────────────────────────────────────────


def _fresh_seed() -> bytes:
	"""Return a 32-byte deterministic seed for repeatable test sigs.

	`os.urandom(32)` would also work but loses the property that a
	failed test's seed can be inspected from the test source.
	"""
	return bytes(range(32))


def _sample_body(package_id: str = "demo.lib", version: str = "1.0.0") -> AuthorClaimBody:
	return make_author_claim_body(
		package_id=package_id,
		version=version,
		artifact_kind="package",
		namespaces=("demo.lib",),
		source_content_id="sha256:" + ("a" * 64),
		required_deps=(RequiredDep(name="std", version_range="^1"),),
		release_utc="2026-01-01T00:00:00Z",
	)


# ── key_loader ────────────────────────────────────────────────────


def test_decode_author_seed32_accepts_canonical_base64() -> None:
	seed = _fresh_seed()
	text = base64.b64encode(seed).decode("ascii")
	assert decode_author_seed32(text) == seed


def test_decode_author_seed32_strips_whitespace() -> None:
	seed = _fresh_seed()
	text = "  " + base64.b64encode(seed).decode("ascii") + "\n\n"
	assert decode_author_seed32(text) == seed


def test_decode_author_seed32_rejects_empty() -> None:
	with pytest.raises(ValueError) as exc:
		decode_author_seed32("")
	assert "empty" in str(exc.value)


def test_decode_author_seed32_rejects_bad_base64() -> None:
	with pytest.raises(ValueError) as exc:
		decode_author_seed32("not-valid-base64!@#$")
	assert "invalid base64" in str(exc.value)


def test_decode_author_seed32_rejects_wrong_length() -> None:
	short = base64.b64encode(b"too-short").decode("ascii")
	with pytest.raises(ValueError) as exc:
		decode_author_seed32(short)
	assert "32 bytes" in str(exc.value)


def test_load_author_seed32_from_file(tmp_path: Path) -> None:
	seed = _fresh_seed()
	p = tmp_path / "author.seed"
	p.write_text(base64.b64encode(seed).decode("ascii") + "\n", encoding="utf-8")
	assert load_author_seed32(p) == seed


def test_load_author_seed32_missing_file_raises(tmp_path: Path) -> None:
	with pytest.raises(FileNotFoundError):
		load_author_seed32(tmp_path / "does-not-exist.seed")


# ── sign_and_write_author_claim ───────────────────────────────────


def test_sign_and_write_produces_canonical_filename(tmp_path: Path) -> None:
	body = _sample_body()
	written = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=body, seed32=_fresh_seed(), sidecar_dir=tmp_path,
	))
	assert written.name == author_claim_filename(body.package_id)
	assert written.parent == tmp_path


def test_sign_and_write_round_trip_loads(tmp_path: Path) -> None:
	body = _sample_body()
	seed = _fresh_seed()
	written = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=body, seed32=seed, sidecar_dir=tmp_path,
	))
	claim = load_author_claim_json(written.read_text(encoding="utf-8"))
	assert claim.body == body
	assert len(claim.signatures) == 1


def test_sign_and_write_signature_verifies(tmp_path: Path) -> None:
	"""The emitted signature must verify against the pubkey derived
	from the seed -- exercising the full encode → sign → load →
	verify pipeline so a regression in canonicalization or signing
	is caught here, not at consumer-side verify time."""
	body = _sample_body()
	seed = _fresh_seed()
	# Pre-compute pubkey + kid for assertion.
	_, pubkey_raw = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	expected_kid = compute_ed25519_kid(pubkey_raw)

	written = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=body, seed32=seed, sidecar_dir=tmp_path,
	))
	claim = load_author_claim_json(written.read_text(encoding="utf-8"))
	sig = claim.signatures[0]
	assert sig.kid == expected_kid
	assert verify_ed25519(
		pubkey_raw=pubkey_raw,
		message=body_signing_bytes(claim.body),
		signature_raw=sig.sig_raw,
	)


def test_sign_and_write_refuses_overwrite_by_default(tmp_path: Path) -> None:
	body = _sample_body()
	seed = _fresh_seed()
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=body, seed32=seed, sidecar_dir=tmp_path,
	))
	with pytest.raises(FileExistsError) as exc:
		sign_and_write_author_claim(SignAuthorClaimOptions(
			body=body, seed32=seed, sidecar_dir=tmp_path,
		))
	# Diagnostic must point the user at the multi-author workflow.
	assert "add_signature_to_claim_file" in str(exc.value)


def test_sign_and_write_overwrite_true_replaces(tmp_path: Path) -> None:
	body_v1 = _sample_body(version="1.0.0")
	body_v2 = _sample_body(version="1.0.1")
	seed = _fresh_seed()
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=body_v1, seed32=seed, sidecar_dir=tmp_path,
	))
	written = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=body_v2, seed32=seed, sidecar_dir=tmp_path, overwrite=True,
	))
	claim = load_author_claim_json(written.read_text(encoding="utf-8"))
	assert claim.body.version == "1.0.1"


def test_sign_and_write_missing_dir_raises(tmp_path: Path) -> None:
	with pytest.raises(FileNotFoundError):
		sign_and_write_author_claim(SignAuthorClaimOptions(
			body=_sample_body(),
			seed32=_fresh_seed(),
			sidecar_dir=tmp_path / "no-such-dir",
		))


# ── add_signature_to_claim_file (multi-author) ─────────────────────


def test_add_signature_appends_without_changing_body(tmp_path: Path) -> None:
	body = _sample_body()
	seed_a = _fresh_seed()
	seed_b = bytes((b ^ 0xFF) for b in seed_a)  # different key
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=body, seed32=seed_a, sidecar_dir=tmp_path,
	))
	add_signature_to_claim_file(
		sidecar_dir=tmp_path,
		package_id=body.package_id,
		seed32=seed_b,
	)
	claim = load_author_claim_json(
		(tmp_path / author_claim_filename(body.package_id)).read_text(encoding="utf-8")
	)
	# Body bytes unchanged: both sigs sign the SAME canonical bytes.
	assert claim.body == body
	assert len(claim.signatures) == 2
	# Sigs come from two different kids.
	kids = {s.kid for s in claim.signatures}
	assert len(kids) == 2


def test_add_signature_to_missing_file_raises(tmp_path: Path) -> None:
	with pytest.raises(FileNotFoundError):
		add_signature_to_claim_file(
			sidecar_dir=tmp_path,
			package_id="never.published",
			seed32=_fresh_seed(),
		)


def test_co_signed_claim_verifies_for_each_signer(tmp_path: Path) -> None:
	"""Anti-regression for the multi-author flow: each sig must
	verify against its own pubkey + the shared body bytes.  A
	regression where `add_signature` accidentally re-canonicalized
	the body (mutating the signed bytes for the appended sig only)
	would surface as one of the two verifies failing."""
	body = _sample_body()
	seed_a = _fresh_seed()
	seed_b = bytes((b ^ 0xFF) for b in seed_a)

	_, pub_a = ed25519_sign_from_seed(priv_seed32=seed_a, message=b"")
	_, pub_b = ed25519_sign_from_seed(priv_seed32=seed_b, message=b"")

	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=body, seed32=seed_a, sidecar_dir=tmp_path,
	))
	add_signature_to_claim_file(
		sidecar_dir=tmp_path,
		package_id=body.package_id,
		seed32=seed_b,
	)
	claim = load_author_claim_json(
		(tmp_path / author_claim_filename(body.package_id)).read_text(encoding="utf-8")
	)
	msg = body_signing_bytes(claim.body)
	# Look up each sig by kid and verify against its matching pubkey.
	pubkeys = {compute_ed25519_kid(pub_a): pub_a, compute_ed25519_kid(pub_b): pub_b}
	for sig in claim.signatures:
		assert verify_ed25519(
			pubkey_raw=pubkeys[sig.kid],
			message=msg,
			signature_raw=sig.sig_raw,
		)
