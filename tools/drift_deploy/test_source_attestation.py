# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for tools.drift_deploy.source_attestation.

Phase A coverage: canonical source_content_id determinism + boundary
conditions, attestation body schema, sign/verify round-trip, sidecar
I/O integrity, and rejection of malformed sidecars.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.drift_deploy.source_attestation import (
	AttestationSignature,
	RequiredDepEntry,
	SourceAttestationBody,
	SourceAttestationSidecar,
	SourceContentInputs,
	canonical_body_bytes,
	compute_source_content_id,
	hash_file,
	read_attestation_sidecar,
	sign_attestation,
	validate_sha256_hex_id,
	verify_attestation,
	write_attestation_sidecar,
	SOURCE_ATTESTATION_BODY_SCHEMA_VERSION,
	SOURCE_ATTESTATION_SIDECAR_FORMAT,
	SOURCE_ATTESTATION_SIDECAR_VERSION,
)


# Pinned 32-byte test seed (deterministic Ed25519 keys → deterministic signatures).
_TEST_SEED = bytes(range(32))


def _basic_inputs(**overrides) -> SourceContentInputs:
	defaults = {
		"kind": "library",
		"package_id": "net-tls",
		"version": "0.4.0",
		"module_namespace": "net.tls",
		"entry_module": "net/tls.drift",
		"modules": [
			("net/tls.drift", "a" * 64),
			("net/tls/handshake.drift", "b" * 64),
		],
		"package_deps": [
			("drift-core", "0.27"),
			("drift-net", "0.4"),
		],
		"native_deps": ["openssl", "z"],
		"unsafe": False,
		"assets": [],
		"target_class": "linux-x86_64",
	}
	defaults.update(overrides)
	return SourceContentInputs(**defaults)


def _basic_body(**overrides) -> SourceAttestationBody:
	defaults = {
		"schema_version": SOURCE_ATTESTATION_BODY_SCHEMA_VERSION,
		"package_id": "net-tls",
		"version": "0.4.0",
		"source_content_id": compute_source_content_id(_basic_inputs()),
		"required_deps": [
			RequiredDepEntry(name="drift-core", version="0.27"),
			RequiredDepEntry(name="drift-net", version="0.4"),
		],
		"target_class": "linux-x86_64",
	}
	defaults.update(overrides)
	return SourceAttestationBody(**defaults)


# ── source_content_id: determinism ──────────────────────────────────


class TestSourceContentIdDeterminism:
	def test_same_inputs_same_id(self) -> None:
		"""The exact same inputs must always yield the exact same id."""
		a = compute_source_content_id(_basic_inputs())
		b = compute_source_content_id(_basic_inputs())
		assert a == b
		assert a.startswith("sha256:")
		assert len(a) == len("sha256:") + 64

	def test_module_order_does_not_matter(self) -> None:
		"""Modules are sorted by path inside the function."""
		a = compute_source_content_id(_basic_inputs(modules=[
			("net/tls.drift", "a" * 64),
			("net/tls/handshake.drift", "b" * 64),
		]))
		b = compute_source_content_id(_basic_inputs(modules=[
			("net/tls/handshake.drift", "b" * 64),
			("net/tls.drift", "a" * 64),
		]))
		assert a == b

	def test_dep_order_does_not_matter(self) -> None:
		a = compute_source_content_id(_basic_inputs(package_deps=[
			("drift-core", "0.27"),
			("drift-net", "0.4"),
		]))
		b = compute_source_content_id(_basic_inputs(package_deps=[
			("drift-net", "0.4"),
			("drift-core", "0.27"),
		]))
		assert a == b

	def test_native_dep_order_does_not_matter(self) -> None:
		a = compute_source_content_id(_basic_inputs(native_deps=["openssl", "z"]))
		b = compute_source_content_id(_basic_inputs(native_deps=["z", "openssl"]))
		assert a == b

	def test_asset_order_does_not_matter(self) -> None:
		a = compute_source_content_id(_basic_inputs(assets=[
			("data/cert.pem", "1" * 64),
			("data/dh.pem", "2" * 64),
		]))
		b = compute_source_content_id(_basic_inputs(assets=[
			("data/dh.pem", "2" * 64),
			("data/cert.pem", "1" * 64),
		]))
		assert a == b


# ── source_content_id: sensitivity ──────────────────────────────────


class TestSourceContentIdSensitivity:
	def test_module_byte_change_changes_id(self) -> None:
		a = compute_source_content_id(_basic_inputs())
		b = compute_source_content_id(_basic_inputs(modules=[
			("net/tls.drift", "a" * 64),
			("net/tls/handshake.drift", "c" * 64),  # changed
		]))
		assert a != b

	def test_module_added_changes_id(self) -> None:
		a = compute_source_content_id(_basic_inputs())
		b = compute_source_content_id(_basic_inputs(modules=[
			("net/tls.drift", "a" * 64),
			("net/tls/handshake.drift", "b" * 64),
			("net/tls/extra.drift", "d" * 64),
		]))
		assert a != b

	def test_dep_range_change_changes_id(self) -> None:
		a = compute_source_content_id(_basic_inputs())
		b = compute_source_content_id(_basic_inputs(package_deps=[
			("drift-core", "0.28"),  # range bumped
			("drift-net", "0.4"),
		]))
		assert a != b

	def test_kind_change_changes_id(self) -> None:
		a = compute_source_content_id(_basic_inputs(kind="library"))
		b = compute_source_content_id(_basic_inputs(kind="app"))
		assert a != b

	def test_unsafe_change_changes_id(self) -> None:
		a = compute_source_content_id(_basic_inputs(unsafe=False))
		b = compute_source_content_id(_basic_inputs(unsafe=True))
		assert a != b

	def test_target_class_change_changes_id(self) -> None:
		"""Cross-target rebuild substitution must be visible in id."""
		a = compute_source_content_id(_basic_inputs(target_class="linux-x86_64"))
		b = compute_source_content_id(_basic_inputs(target_class="linux-aarch64"))
		assert a != b

	def test_module_namespace_change_changes_id(self) -> None:
		a = compute_source_content_id(_basic_inputs())
		b = compute_source_content_id(_basic_inputs(module_namespace="net.tls.alt"))
		assert a != b

	def test_entry_module_change_changes_id(self) -> None:
		a = compute_source_content_id(_basic_inputs())
		b = compute_source_content_id(_basic_inputs(entry_module="net/tls/main.drift"))
		assert a != b

	def test_asset_byte_change_changes_id(self) -> None:
		a = compute_source_content_id(_basic_inputs(assets=[("data/cert.pem", "1" * 64)]))
		b = compute_source_content_id(_basic_inputs(assets=[("data/cert.pem", "9" * 64)]))
		assert a != b

	def test_version_change_changes_id(self) -> None:
		a = compute_source_content_id(_basic_inputs(version="0.4.0"))
		b = compute_source_content_id(_basic_inputs(version="0.4.1"))
		assert a != b


# ── canonical paths ─────────────────────────────────────────────────


class TestCanonicalPaths:
	def test_backslash_normalised(self) -> None:
		a = compute_source_content_id(_basic_inputs(modules=[
			("net/tls.drift", "a" * 64),
			("net/tls/handshake.drift", "b" * 64),
		]))
		b = compute_source_content_id(_basic_inputs(modules=[
			("net\\tls.drift", "a" * 64),
			("net\\tls\\handshake.drift", "b" * 64),
		]))
		assert a == b

	def test_leading_dot_slash_normalised(self) -> None:
		a = compute_source_content_id(_basic_inputs(modules=[
			("net/tls.drift", "a" * 64),
			("net/tls/handshake.drift", "b" * 64),
		]))
		b = compute_source_content_id(_basic_inputs(modules=[
			("./net/tls.drift", "a" * 64),
			("./net/tls/handshake.drift", "b" * 64),
		]))
		assert a == b

	def test_absolute_path_rejected(self) -> None:
		with pytest.raises(ValueError, match="absolute"):
			compute_source_content_id(_basic_inputs(modules=[
				("/tmp/escape.drift", "a" * 64),
			]))

	def test_dotdot_rejected(self) -> None:
		with pytest.raises(ValueError, match=r"'\.\.', or empty"):
			compute_source_content_id(_basic_inputs(modules=[
				("net/../escape.drift", "a" * 64),
			]))

	def test_empty_path_rejected(self) -> None:
		with pytest.raises(ValueError):
			compute_source_content_id(_basic_inputs(modules=[
				("", "a" * 64),
			]))

	def test_entry_module_absolute_rejected(self) -> None:
		with pytest.raises(ValueError, match="absolute"):
			compute_source_content_id(_basic_inputs(entry_module="/etc/passwd"))

	def test_asset_dotdot_rejected(self) -> None:
		with pytest.raises(ValueError):
			compute_source_content_id(_basic_inputs(assets=[
				("../escape.pem", "a" * 64),
			]))


# ── hash_file ───────────────────────────────────────────────────────


class TestHashFile:
	def test_known_content(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "x.drift"
			p.write_bytes(b"hello\n")
			# Pre-computed: sha256(b"hello\n")
			assert hash_file(p) == "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"

	def test_chunked_read(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "big.drift"
			# Write 200KB to force chunked reads.
			p.write_bytes(b"x" * 200_000)
			h = hash_file(p)
			assert len(h) == 64


# ── sign / verify round-trip ────────────────────────────────────────


class TestSignVerify:
	def test_sign_then_verify_passes(self) -> None:
		body = _basic_body()
		sidecar = sign_attestation(body, signing_key_seed=_TEST_SEED)
		# Self-verify (no expected kid).
		verify_attestation(sidecar)

	def test_sign_then_verify_with_expected_kid_passes(self) -> None:
		body = _basic_body()
		sidecar = sign_attestation(body, signing_key_seed=_TEST_SEED)
		expected_kid = sidecar.signatures[0].kid
		verify_attestation(sidecar, expected_signer_kid=expected_kid)

	def test_verify_with_wrong_expected_kid_fails(self) -> None:
		body = _basic_body()
		sidecar = sign_attestation(body, signing_key_seed=_TEST_SEED)
		with pytest.raises(ValueError, match="no signature from expected signer"):
			verify_attestation(sidecar, expected_signer_kid="ed25519:bogus")

	def test_signature_is_deterministic(self) -> None:
		"""Ed25519 is deterministic; same body + same seed → identical signature.
		This is load-bearing for reproducibility — two builds of the same
		source by the same author produce byte-identical attestations."""
		a = sign_attestation(_basic_body(), signing_key_seed=_TEST_SEED)
		b = sign_attestation(_basic_body(), signing_key_seed=_TEST_SEED)
		assert a.signatures[0].sig_raw == b.signatures[0].sig_raw
		assert a.body_sha256_hex == b.body_sha256_hex

	def test_different_key_different_kid(self) -> None:
		a = sign_attestation(_basic_body(), signing_key_seed=_TEST_SEED)
		other_seed = bytes([0xFF] * 32)
		b = sign_attestation(_basic_body(), signing_key_seed=other_seed)
		assert a.signatures[0].kid != b.signatures[0].kid

	def test_sign_invalid_seed_size_rejected(self) -> None:
		with pytest.raises(ValueError, match="32 bytes"):
			sign_attestation(_basic_body(), signing_key_seed=b"\x00" * 16)


# ── sidecar I/O ─────────────────────────────────────────────────────


class TestSidecarIO:
	def test_round_trip(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "net-tls.source-attestation"
			body = _basic_body()
			original = sign_attestation(body, signing_key_seed=_TEST_SEED)
			write_attestation_sidecar(p, original)
			loaded = read_attestation_sidecar(p)
			assert loaded.body == original.body
			assert loaded.body_sha256_hex == original.body_sha256_hex
			assert len(loaded.signatures) == 1
			assert loaded.signatures[0].kid == original.signatures[0].kid
			assert loaded.signatures[0].sig_raw == original.signatures[0].sig_raw
			# Loaded sidecar still verifies.
			verify_attestation(loaded, expected_signer_kid=loaded.signatures[0].kid)

	def test_sidecar_is_canonical_on_disk(self) -> None:
		"""On-disk bytes are canonical JSON so two runs produce identical files."""
		with tempfile.TemporaryDirectory() as tmpdir:
			p1 = Path(tmpdir) / "a.source-attestation"
			p2 = Path(tmpdir) / "b.source-attestation"
			s1 = sign_attestation(_basic_body(), signing_key_seed=_TEST_SEED)
			s2 = sign_attestation(_basic_body(), signing_key_seed=_TEST_SEED)
			write_attestation_sidecar(p1, s1)
			write_attestation_sidecar(p2, s2)
			assert p1.read_bytes() == p2.read_bytes()

	def test_tampered_body_rejected(self) -> None:
		"""Hand-edit the body field — body_sha256 self-check fires before signature verify."""
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "x.source-attestation"
			sidecar = sign_attestation(_basic_body(), signing_key_seed=_TEST_SEED)
			write_attestation_sidecar(p, sidecar)
			obj = json.loads(p.read_text(encoding="utf-8"))
			obj["body"]["version"] = "9.9.9"  # edit body
			p.write_text(json.dumps(obj), encoding="utf-8")
			with pytest.raises(ValueError, match="body_sha256 does not match"):
				read_attestation_sidecar(p)

	def test_tampered_body_sha256_then_verify_fails(self) -> None:
		"""Edit body AND body_sha256 to keep self-check happy → signature verify fails."""
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "x.source-attestation"
			sidecar = sign_attestation(_basic_body(), signing_key_seed=_TEST_SEED)
			write_attestation_sidecar(p, sidecar)
			obj = json.loads(p.read_text(encoding="utf-8"))
			obj["body"]["version"] = "9.9.9"
			# Recompute body_sha256 to bypass the self-check.
			import hashlib as _hl
			fake_canonical = json.dumps(obj["body"], sort_keys=True, separators=(",", ":")).encode("utf-8")
			obj["body_sha256"] = "sha256:" + _hl.sha256(fake_canonical).hexdigest()
			p.write_text(json.dumps(obj), encoding="utf-8")
			loaded = read_attestation_sidecar(p)  # passes self-check
			with pytest.raises(ValueError, match="signature verification failed"):
				verify_attestation(loaded)

	def test_kid_pubkey_mismatch_rejected(self) -> None:
		"""Mismatched kid vs pubkey caught at load time."""
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "x.source-attestation"
			sidecar = sign_attestation(_basic_body(), signing_key_seed=_TEST_SEED)
			write_attestation_sidecar(p, sidecar)
			obj = json.loads(p.read_text(encoding="utf-8"))
			obj["signatures"][0]["kid"] = "ed25519:bogus_does_not_match_pubkey"
			p.write_text(json.dumps(obj), encoding="utf-8")
			with pytest.raises(ValueError, match="kid does not match pubkey"):
				read_attestation_sidecar(p)

	def test_unknown_format_rejected(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "x.source-attestation"
			p.write_text(json.dumps({
				"format": "drift-something-else",
				"version": 0,
				"body": {},
				"body_sha256": "sha256:00",
				"signatures": [],
			}), encoding="utf-8")
			with pytest.raises(ValueError, match="unexpected source attestation format"):
				read_attestation_sidecar(p)

	def test_unknown_version_rejected(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "x.source-attestation"
			p.write_text(json.dumps({
				"format": SOURCE_ATTESTATION_SIDECAR_FORMAT,
				"version": 99,
				"body": {},
				"body_sha256": "sha256:00",
				"signatures": [],
			}), encoding="utf-8")
			with pytest.raises(ValueError, match="unsupported source attestation sidecar version"):
				read_attestation_sidecar(p)

	def test_invalid_json_rejected(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "x.source-attestation"
			p.write_text("not json", encoding="utf-8")
			with pytest.raises(ValueError, match="invalid JSON"):
				read_attestation_sidecar(p)


# ── strict sha256 id validator ──────────────────────────────────────


class TestStrictShaValidator:
	"""Pin the trust-boundary validator: any sha256:<hex> id touching a
	signed surface must match exactly `sha256:<64 lowercase hex>`."""

	def test_canonical_form_accepted(self) -> None:
		validate_sha256_hex_id("sha256:" + "a" * 64, field="x")

	def test_uppercase_hex_rejected(self) -> None:
		with pytest.raises(ValueError, match="sha256:<64 lowercase hex>"):
			validate_sha256_hex_id("sha256:" + "A" * 64, field="x")

	def test_short_hex_rejected(self) -> None:
		with pytest.raises(ValueError, match="sha256:<64 lowercase hex>"):
			validate_sha256_hex_id("sha256:" + "a" * 63, field="x")

	def test_long_hex_rejected(self) -> None:
		with pytest.raises(ValueError, match="sha256:<64 lowercase hex>"):
			validate_sha256_hex_id("sha256:" + "a" * 65, field="x")

	def test_non_hex_rejected(self) -> None:
		with pytest.raises(ValueError, match="sha256:<64 lowercase hex>"):
			validate_sha256_hex_id("sha256:" + "z" * 64, field="x")

	def test_prefix_only_rejected(self) -> None:
		with pytest.raises(ValueError, match="sha256:<64 lowercase hex>"):
			validate_sha256_hex_id("sha256:", field="x")

	def test_trailing_whitespace_rejected(self) -> None:
		with pytest.raises(ValueError, match="sha256:<64 lowercase hex>"):
			validate_sha256_hex_id("sha256:" + "a" * 64 + "\n", field="x")

	def test_non_string_rejected(self) -> None:
		with pytest.raises(ValueError, match="must be a string"):
			validate_sha256_hex_id(123, field="x")

	def test_compute_rejects_uppercase_module_hash(self) -> None:
		"""Per-module content hashes feed canonical signed bytes; a
		malformed hash must not become signed input."""
		with pytest.raises(ValueError, match="64 lowercase hex"):
			compute_source_content_id(_basic_inputs(modules=[
				("net/tls.drift", "A" * 64),  # uppercase
			]))

	def test_compute_rejects_short_module_hash(self) -> None:
		with pytest.raises(ValueError, match="64 lowercase hex"):
			compute_source_content_id(_basic_inputs(modules=[
				("net/tls.drift", "a" * 63),
			]))

	def test_compute_rejects_uppercase_asset_hash(self) -> None:
		with pytest.raises(ValueError, match="64 lowercase hex"):
			compute_source_content_id(_basic_inputs(assets=[
				("data/cert.pem", "A" * 64),
			]))

	def test_sign_rejects_malformed_body_source_content_id(self) -> None:
		"""Programmatic callers can't sign a body with a malformed id."""
		body = SourceAttestationBody(
			schema_version=1,
			package_id="p",
			version="1.0.0",
			source_content_id="sha256:" + "Z" * 64,  # uppercase, non-hex
			required_deps=[],
			target_class="linux-x86_64",
		)
		with pytest.raises(ValueError, match="sha256:<64 lowercase hex>"):
			sign_attestation(body, signing_key_seed=_TEST_SEED)

	def test_load_rejects_malformed_body_source_content_id(self) -> None:
		"""On-disk body with uppercase id is rejected at load."""
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "x.source-attestation"
			# Build a sidecar manually with an uppercase id to bypass sign-time check.
			body_obj = {
				"schema_version": 1,
				"package_id": "p",
				"version": "1.0.0",
				"source_content_id": "sha256:" + "A" * 64,
				"required_deps": [],
				"target_class": "linux-x86_64",
			}
			import hashlib as _hl
			canon = json.dumps(body_obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
			body_sha = _hl.sha256(canon).hexdigest()
			p.write_text(json.dumps({
				"format": SOURCE_ATTESTATION_SIDECAR_FORMAT,
				"version": SOURCE_ATTESTATION_SIDECAR_VERSION,
				"envelope": "drift-src-attestation-v1",
				"body": body_obj,
				"body_sha256": "sha256:" + body_sha,
				"signatures": [],  # never reached; we fail in body validation
			}), encoding="utf-8")
			with pytest.raises(ValueError, match="sha256:<64 lowercase hex>"):
				read_attestation_sidecar(p)


# ── canonical body bytes ────────────────────────────────────────────


class TestCanonicalBody:
	def test_body_canonical_is_sorted(self) -> None:
		"""Two bodies with deps in different declaration order canonicalise the same."""
		a = SourceAttestationBody(
			schema_version=1,
			package_id="p",
			version="1.0.0",
			source_content_id="sha256:" + "0" * 64,
			required_deps=[
				RequiredDepEntry(name="b-dep", version="1"),
				RequiredDepEntry(name="a-dep", version="1"),
			],
			target_class="linux-x86_64",
		)
		b = SourceAttestationBody(
			schema_version=1,
			package_id="p",
			version="1.0.0",
			source_content_id="sha256:" + "0" * 64,
			required_deps=[
				RequiredDepEntry(name="a-dep", version="1"),
				RequiredDepEntry(name="b-dep", version="1"),
			],
			target_class="linux-x86_64",
		)
		assert canonical_body_bytes(a) == canonical_body_bytes(b)
