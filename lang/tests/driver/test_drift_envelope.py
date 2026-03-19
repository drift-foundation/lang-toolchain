# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for signed envelope binding of author profiles to package signatures."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lang.drift.crypto import b64_encode, compute_ed25519_kid, ed25519_public_bytes_raw, sha256_hex
from lang.drift.envelope import build_envelope, build_envelope_from_bytes


def _make_seed_file(tmpdir: str) -> Path:
	key_path = Path(tmpdir) / "test.seed"
	seed = Ed25519PrivateKey.generate().private_bytes_raw()
	key_path.write_text(base64.b64encode(seed).decode("ascii") + "\n")
	return key_path


# ── Envelope construction ────────────────────────────────────────────


class TestEnvelope:
	def test_canonical_form_with_profile(self) -> None:
		env = build_envelope(
			package_sha256_hex="aabb",
			author_profile_sha256_hex="ccdd",
		)
		lines = env.decode("utf-8").strip().split("\n")
		assert lines[0] == "drift-sig-envelope-v1"
		assert lines[1] == "package-sha256:aabb"
		assert lines[2] == "author-profile-sha256:ccdd"
		assert len(lines) == 3

	def test_canonical_form_without_profile(self) -> None:
		env = build_envelope(package_sha256_hex="aabb")
		lines = env.decode("utf-8").strip().split("\n")
		assert lines[0] == "drift-sig-envelope-v1"
		assert lines[1] == "package-sha256:aabb"
		assert len(lines) == 2

	def test_deterministic(self) -> None:
		"""Same inputs always produce same bytes."""
		a = build_envelope(package_sha256_hex="x", author_profile_sha256_hex="y")
		b = build_envelope(package_sha256_hex="x", author_profile_sha256_hex="y")
		assert a == b

	def test_from_bytes_helper(self) -> None:
		pkg = b"package content"
		profile = b"profile content"
		env = build_envelope_from_bytes(package_bytes=pkg, author_profile_bytes=profile)
		expected = build_envelope(
			package_sha256_hex=sha256_hex(pkg),
			author_profile_sha256_hex=sha256_hex(profile),
		)
		assert env == expected


# ── Signing with envelope ────────────────────────────────────────────


class TestSignWithEnvelope:
	def test_sign_with_profile_produces_envelope_v1(self) -> None:
		"""sign_package_v0 with author_profile_path produces envelope_version: 1."""
		from lang.drift.sign import SignOptions, sign_package_v0

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)
			pkg = td / "test.dmp"
			pkg.write_bytes(b"fake package bytes")
			profile = td / ".author-profile"
			profile.write_text('{"test": "profile"}')
			key = _make_seed_file(tmpdir)
			sig_path = td / "test.sig"

			sign_package_v0(SignOptions(
				package_path=pkg,
				key_seed_path=key,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				author_profile_path=profile,
			))

			sc = json.loads(sig_path.read_text())
			assert sc["envelope_version"] == 1
			assert sc["author_profile_sha256"] == f"sha256:{sha256_hex(profile.read_bytes())}"

	def test_sign_without_profile_produces_envelope_v0(self) -> None:
		"""sign_package_v0 without author_profile_path produces legacy sidecar."""
		from lang.drift.sign import SignOptions, sign_package_v0

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)
			pkg = td / "test.dmp"
			pkg.write_bytes(b"fake package bytes")
			key = _make_seed_file(tmpdir)
			sig_path = td / "test.sig"

			sign_package_v0(SignOptions(
				package_path=pkg,
				key_seed_path=key,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
			))

			sc = json.loads(sig_path.read_text())
			assert "envelope_version" not in sc
			assert "author_profile_sha256" not in sc


# ── Verification ─────────────────────────────────────────────────────


class TestEnvelopeVerification:
	def _sign_and_verify(
		self, *, pkg_bytes: bytes, profile_bytes: bytes | None = None,
	) -> None:
		"""Helper: sign with envelope, verify with compiler verifier."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import (
			load_sig_sidecar,
			verify_ed25519,
		)
		from lang.drift.envelope import build_envelope

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)
			pkg = td / "test.dmp"
			pkg.write_bytes(pkg_bytes)
			key_path = _make_seed_file(tmpdir)
			sig_path = td / "test.sig"

			profile_path = None
			if profile_bytes is not None:
				profile_path = td / ".author-profile"
				profile_path.write_bytes(profile_bytes)

			sign_package_v0(SignOptions(
				package_path=pkg,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				author_profile_path=profile_path,
			))

			# Load sidecar and verify as the compiler would.
			sf = load_sig_sidecar(sig_path)

			if sf.envelope_version >= 1:
				signed_message = build_envelope(
					package_sha256_hex=sf.package_sha256_hex,
					author_profile_sha256_hex=sf.author_profile_sha256_hex,
				)
			else:
				signed_message = pkg_bytes

			# Read pubkey from sidecar for verification.
			entry = sf.signatures[0]
			assert entry.pubkey_raw is not None
			ok = verify_ed25519(
				pubkey_raw=entry.pubkey_raw,
				message=signed_message,
				signature_raw=entry.sig_raw,
			)
			assert ok, "signature verification failed"

	def test_verify_envelope_v1_with_profile(self) -> None:
		self._sign_and_verify(
			pkg_bytes=b"real package content",
			profile_bytes=b'{"name": "test"}',
		)

	def test_verify_envelope_v1_without_profile(self) -> None:
		self._sign_and_verify(pkg_bytes=b"real package content")

	def test_modified_profile_fails_verification(self) -> None:
		"""Changing profile bytes after signing must fail signature verification."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import (
			load_sig_sidecar,
			verify_ed25519,
		)
		from lang.drift.envelope import build_envelope

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)
			pkg = td / "test.dmp"
			pkg.write_bytes(b"package")
			profile = td / ".author-profile"
			profile.write_bytes(b'{"original": true}')
			key = _make_seed_file(tmpdir)
			sig_path = td / "test.sig"

			sign_package_v0(SignOptions(
				package_path=pkg, key_seed_path=key, key_seed_text=None,
				out_path=sig_path, add_signature=False, include_pubkey=True,
				author_profile_path=profile,
			))

			# Tamper with the profile.
			profile.write_bytes(b'{"tampered": true}')

			sf = load_sig_sidecar(sig_path)
			# The sidecar still has the original profile digest.
			# Verification with the original digest should succeed
			# (the signature covers the original envelope).
			original_env = build_envelope(
				package_sha256_hex=sf.package_sha256_hex,
				author_profile_sha256_hex=sf.author_profile_sha256_hex,
			)
			entry = sf.signatures[0]
			ok = verify_ed25519(
				pubkey_raw=entry.pubkey_raw,
				message=original_env,
				signature_raw=entry.sig_raw,
			)
			assert ok, "signature should verify with original envelope"

			# But the tampered profile doesn't match the sidecar digest.
			tampered_sha = sha256_hex(profile.read_bytes())
			assert tampered_sha != sf.author_profile_sha256_hex

	def test_modified_sidecar_digest_fails_signature(self) -> None:
		"""Changing author_profile_sha256 in sidecar fails signature check."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import (
			load_sig_sidecar,
			verify_ed25519,
		)
		from lang.drift.envelope import build_envelope

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)
			pkg = td / "test.dmp"
			pkg.write_bytes(b"package")
			profile = td / ".author-profile"
			profile.write_bytes(b'{"original": true}')
			key = _make_seed_file(tmpdir)
			sig_path = td / "test.sig"

			sign_package_v0(SignOptions(
				package_path=pkg, key_seed_path=key, key_seed_text=None,
				out_path=sig_path, add_signature=False, include_pubkey=True,
				author_profile_path=profile,
			))

			# Tamper with the sidecar's profile digest.
			sc = json.loads(sig_path.read_text())
			sc["author_profile_sha256"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
			sig_path.write_text(json.dumps(sc))

			sf = load_sig_sidecar(sig_path)
			# Reconstruct envelope with the tampered digest.
			tampered_env = build_envelope(
				package_sha256_hex=sf.package_sha256_hex,
				author_profile_sha256_hex=sf.author_profile_sha256_hex,
			)
			entry = sf.signatures[0]
			ok = verify_ed25519(
				pubkey_raw=entry.pubkey_raw,
				message=tampered_env,
				signature_raw=entry.sig_raw,
			)
			assert not ok, "signature must fail with tampered sidecar digest"

	def test_downgrade_v1_to_v0_fails(self) -> None:
		"""Stripping envelope_version from a v1 sidecar must fail verification."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import (
			load_sig_sidecar,
			verify_ed25519,
		)

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)
			pkg = td / "test.dmp"
			pkg_bytes = b"package for downgrade test"
			pkg.write_bytes(pkg_bytes)
			profile = td / ".author-profile"
			profile.write_bytes(b'{"profile": true}')
			key = _make_seed_file(tmpdir)
			sig_path = td / "test.sig"

			sign_package_v0(SignOptions(
				package_path=pkg, key_seed_path=key, key_seed_text=None,
				out_path=sig_path, add_signature=False, include_pubkey=True,
				author_profile_path=profile,
			))

			# Strip envelope metadata to simulate downgrade.
			sc = json.loads(sig_path.read_text())
			sc.pop("envelope_version", None)
			sc.pop("author_profile_sha256", None)
			sig_path.write_text(json.dumps(sc))

			sf = load_sig_sidecar(sig_path)
			assert sf.envelope_version == 0  # downgraded

			# Verify against raw bytes (as legacy v0 would).
			entry = sf.signatures[0]
			ok = verify_ed25519(
				pubkey_raw=entry.pubkey_raw,
				message=pkg_bytes,  # legacy: raw package bytes
				signature_raw=entry.sig_raw,
			)
			# Signature was computed over the envelope, not raw bytes → must fail.
			assert not ok, "downgraded v1→v0 must fail: signature was over envelope, not raw bytes"


# ── Profile package field ────────────────────────────────────────────


class TestProfilePackageField:
	def test_profile_with_package_field_roundtrip(self) -> None:
		from lang.drift.author_profile import (
			create_author_profile, write_author_profile, load_author_profile,
		)
		from dataclasses import replace

		priv = Ed25519PrivateKey.generate()
		pub_raw = ed25519_public_bytes_raw(priv.public_key())
		profile = create_author_profile(
			pubkey_raw=pub_raw, name="Test", namespaces=["test.*"],
		)
		bound = replace(profile, package="net-tls")

		with tempfile.TemporaryDirectory() as tmpdir:
			path = Path(tmpdir) / ".author-profile"
			write_author_profile(bound, path)
			loaded = load_author_profile(path)
			assert loaded.package == "net-tls"

	def test_profile_without_package_field(self) -> None:
		from lang.drift.author_profile import (
			create_author_profile, write_author_profile, load_author_profile,
		)

		priv = Ed25519PrivateKey.generate()
		pub_raw = ed25519_public_bytes_raw(priv.public_key())
		profile = create_author_profile(
			pubkey_raw=pub_raw, name="Test", namespaces=["test.*"],
		)

		with tempfile.TemporaryDirectory() as tmpdir:
			path = Path(tmpdir) / ".author-profile"
			write_author_profile(profile, path)
			loaded = load_author_profile(path)
			assert loaded.package == ""


# ── Consumer trust flow signature verification ───────────────────────


class TestTrustFlowSignatureVerification:
	"""Verify drift trust actually checks the Ed25519 signature, not just the digest."""

	def test_forged_sidecar_and_profile_rejected(self) -> None:
		"""Attacker forges both profile and sidecar with matching digest → must be rejected."""
		from lang.drift.cli import _trust_profile_flow
		from lang.drift.author_profile import create_author_profile, write_author_profile
		from dataclasses import replace

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)

			# Create a "legitimate" signed package + profile.
			priv = Ed25519PrivateKey.generate()
			pub_raw = ed25519_public_bytes_raw(priv.public_key())
			profile = create_author_profile(pubkey_raw=pub_raw, name="Legit", namespaces=["legit.*"])
			bound = replace(profile, package="test.pkg")
			profile_path = td / ".author-profile"
			write_author_profile(bound, profile_path)

			# Forge a sidecar with the correct profile digest but a garbage signature.
			profile_sha = sha256_hex(profile_path.read_bytes())
			forged_sidecar = {
				"format": "dmir-pkg-sig",
				"version": 0,
				"package_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
				"envelope_version": 1,
				"author_profile_sha256": f"sha256:{profile_sha}",
				"signatures": [{
					"algo": "ed25519",
					"kid": "ed25519:fake",
					"sig": b64_encode(b"\x00" * 64),
					"pubkey": b64_encode(pub_raw),
				}],
			}
			sig_path = td / "test.pkg.sig"
			sig_path.write_text(json.dumps(forged_sidecar))

			# Trust flow must reject: signature doesn't verify over the envelope.
			trust_store = td / "drift" / "trust.json"
			rc = _trust_profile_flow(str(profile_path), ["--trust-store", str(trust_store), "--yes"])
			assert rc == 1, "forged sidecar with matching digest must be rejected"

	def test_legitimate_bound_profile_accepted(self) -> None:
		"""Properly signed package + bound profile → trust flow succeeds."""
		from lang.drift.cli import _trust_profile_flow
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.drift.author_profile import create_author_profile, write_author_profile
		from dataclasses import replace

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)

			# Create a key + profile.
			seed = Ed25519PrivateKey.generate().private_bytes_raw()
			key_path = td / "key.seed"
			key_path.write_text(base64.b64encode(seed).decode() + "\n")
			priv = Ed25519PrivateKey.from_private_bytes(seed)
			pub_raw = ed25519_public_bytes_raw(priv.public_key())

			profile = create_author_profile(pubkey_raw=pub_raw, name="Real", namespaces=["real.*"])
			bound = replace(profile, package="test.pkg")
			profile_path = td / ".author-profile"
			write_author_profile(bound, profile_path)

			# Create + sign a package with the profile.
			pkg = td / "test.pkg.dmp"
			pkg.write_bytes(b"real package content")
			sign_package_v0(SignOptions(
				package_path=pkg,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=td / "test.pkg.sig",
				add_signature=False,
				include_pubkey=True,
				author_profile_path=profile_path,
			))

			# Trust flow should succeed — package bytes on disk match.
			trust_store = td / "drift" / "trust.json"
			rc = _trust_profile_flow(str(profile_path), ["--trust-store", str(trust_store), "--yes"])
			assert rc == 0, "legitimately signed bound profile must be accepted"

	def test_tampered_package_bytes_rejected(self) -> None:
		"""Package artifact on disk has different bytes than what was signed → must be rejected."""
		from lang.drift.cli import _trust_profile_flow
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.drift.author_profile import create_author_profile, write_author_profile
		from dataclasses import replace

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)

			seed = Ed25519PrivateKey.generate().private_bytes_raw()
			key_path = td / "key.seed"
			key_path.write_text(base64.b64encode(seed).decode() + "\n")
			priv = Ed25519PrivateKey.from_private_bytes(seed)
			pub_raw = ed25519_public_bytes_raw(priv.public_key())

			profile = create_author_profile(pubkey_raw=pub_raw, name="Real", namespaces=["real.*"])
			bound = replace(profile, package="test.pkg")
			profile_path = td / ".author-profile"
			write_author_profile(bound, profile_path)

			# Create + sign a package with the profile.
			pkg = td / "test.pkg.dmp"
			pkg.write_bytes(b"real package content")
			sign_package_v0(SignOptions(
				package_path=pkg,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=td / "test.pkg.sig",
				add_signature=False,
				include_pubkey=True,
				author_profile_path=profile_path,
			))

			# Tamper with the package bytes after signing.
			pkg.write_bytes(b"TAMPERED package content")

			trust_store = td / "drift" / "trust.json"
			rc = _trust_profile_flow(str(profile_path), ["--trust-store", str(trust_store), "--yes"])
			assert rc == 1, "tampered package bytes must be rejected"

	def test_missing_package_artifact_signature_only(self) -> None:
		"""Signed sidecar exists but package artifact is absent → signature-only, not bound."""
		from lang.drift.cli import _trust_profile_flow
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.drift.author_profile import create_author_profile, write_author_profile
		from dataclasses import replace

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)

			seed = Ed25519PrivateKey.generate().private_bytes_raw()
			key_path = td / "key.seed"
			key_path.write_text(base64.b64encode(seed).decode() + "\n")
			priv = Ed25519PrivateKey.from_private_bytes(seed)
			pub_raw = ed25519_public_bytes_raw(priv.public_key())

			profile = create_author_profile(pubkey_raw=pub_raw, name="Real", namespaces=["real.*"])
			bound = replace(profile, package="test.pkg")
			profile_path = td / ".author-profile"
			write_author_profile(bound, profile_path)

			# Create + sign, then remove the package artifact.
			pkg = td / "test.pkg.dmp"
			pkg.write_bytes(b"real package content")
			sign_package_v0(SignOptions(
				package_path=pkg,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=td / "test.pkg.sig",
				add_signature=False,
				include_pubkey=True,
				author_profile_path=profile_path,
			))
			pkg.unlink()  # remove the package artifact

			# Trust flow should still succeed (signature is valid),
			# but binding status should be "signature-only".
			trust_store = td / "drift" / "trust.json"
			rc = _trust_profile_flow(str(profile_path), ["--trust-store", str(trust_store), "--yes"])
			assert rc == 0, "missing package artifact should not be a hard error"

	def test_different_signer_key_rejected(self) -> None:
		"""Profile describes key B, but package was signed by key A → must be rejected.

		The envelope/digest binding is otherwise completely valid — the profile
		bytes are included in the signed envelope — but the signing key is not
		the one the profile declares.  The trust flow must reject this because
		"bound" should mean "the profile's own key signed this package."
		"""
		from lang.drift.cli import _trust_profile_flow
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.drift.author_profile import create_author_profile, write_author_profile
		from dataclasses import replace

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)

			# Key A — the actual signer.
			seed_a = Ed25519PrivateKey.generate().private_bytes_raw()
			key_a_path = td / "key_a.seed"
			key_a_path.write_text(base64.b64encode(seed_a).decode() + "\n")

			# Key B — the key described in the profile.
			priv_b = Ed25519PrivateKey.generate()
			pub_b_raw = ed25519_public_bytes_raw(priv_b.public_key())

			# Profile declares key B.
			profile = create_author_profile(pubkey_raw=pub_b_raw, name="Author B", namespaces=["test.*"])
			bound = replace(profile, package="test.pkg")
			profile_path = td / ".author-profile"
			write_author_profile(bound, profile_path)

			# Sign the package with key A (not key B).
			pkg = td / "test.pkg.dmp"
			pkg.write_bytes(b"package content")
			sign_package_v0(SignOptions(
				package_path=pkg,
				key_seed_path=key_a_path,
				key_seed_text=None,
				out_path=td / "test.pkg.sig",
				add_signature=False,
				include_pubkey=True,
				author_profile_path=profile_path,
			))

			# Envelope is valid, digest binding is valid, but signer != profile key.
			trust_store = td / "drift" / "trust.json"
			rc = _trust_profile_flow(str(profile_path), ["--trust-store", str(trust_store), "--yes"])
			assert rc == 1, "signer key != profile key must be rejected"
