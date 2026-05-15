# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Tests for the provenance sidecar and v2 envelope signing.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from lang.test_support.drift_tmp import session_root
from pathlib import Path

import pytest

from lang.driftc.packages.trust_v0 import TrustStore
from tools.drift_deploy.provenance import (
	CompilerInfo,
	build_provenance,
	build_provenance_bundle,
	compress_provenance_bundle,
	decompress_provenance_bundle,
	load_provenance_bundle,
	parse_compiler_info,
	provenance_sha256,
	write_provenance,
	write_provenance_bundle,
)


# ── parse_compiler_info ──────────────────────────────────────────────


class TestParseCompilerInfo:
	def test_typical_version_string(self) -> None:
		output = "driftc 0.27.92 | abi 6 | git abc1234 | license MIT"
		info = parse_compiler_info(output)
		assert info.version == "0.27.92"
		assert info.abi == 6
		assert info.commit == "abc1234"

	def test_unknown_commit(self) -> None:
		output = "driftc 0.27.92 | abi 6 | license MIT"
		info = parse_compiler_info(output)
		assert info.version == "0.27.92"
		assert info.abi == 6
		assert info.commit == "unknown"

	def test_empty_string(self) -> None:
		info = parse_compiler_info("")
		assert info.version == "unknown"
		assert info.abi == 0
		assert info.commit == "unknown"


# ── build_provenance ─────────────────────────────────────────────────


class TestBuildProvenance:
	def _default_kwargs(self) -> dict:
		return dict(
			artifact_name="web-rest",
			artifact_version="0.2.5",
			artifact_kind="package",
			artifact_sha256="sha256:deadbeef0123456789abcdef0123456789abcdef0123456789abcdef01234567",
			target="drift-dev",
			compiler=CompilerInfo(version="0.27.92", abi=6, commit="abc1234"),
			resolved_deps={
				"web-jwt": {"version": "0.2.5", "sha256": "aabb"},
			},
		)

	def test_deterministic(self) -> None:
		"""Same inputs produce identical bytes (within same second)."""
		kwargs = self._default_kwargs()
		a = build_provenance(**kwargs)
		b = build_provenance(**kwargs)
		assert a == b

	def test_fields_present(self) -> None:
		raw = build_provenance(**self._default_kwargs())
		obj = json.loads(raw)
		assert obj["schema_version"] == 3
		assert obj["artifact_name"] == "web-rest"
		assert obj["artifact_version"] == "0.2.5"
		assert obj["artifact_kind"] == "package"
		assert obj["artifact_sha256"] == "sha256:deadbeef0123456789abcdef0123456789abcdef0123456789abcdef01234567"
		assert obj["target"] == "drift-dev"
		assert obj["compiler_version"] == "0.27.92"
		assert obj["compiler_commit"] == "abc1234"
		assert obj["abi"] == 6
		assert "build_utc" in obj
		assert obj["resolved_deps"]["web-jwt"]["version"] == "0.2.5"

	def test_source_identity_included(self) -> None:
		"""Source identity is embedded when provided."""
		from tools.drift_deploy.provenance import SourceIdentity
		kwargs = self._default_kwargs()
		kwargs["source"] = SourceIdentity(vcs_type="git", branch="main", commit="deadbeef1234567890")
		raw = build_provenance(**kwargs)
		obj = json.loads(raw)
		assert "source" in obj
		assert obj["source"]["vcs_type"] == "git"
		assert obj["source"]["branch"] == "main"
		assert obj["source"]["commit"] == "deadbeef1234567890"

	def test_source_identity_omitted_when_none(self) -> None:
		"""No source block when source is not provided."""
		raw = build_provenance(**self._default_kwargs())
		obj = json.loads(raw)
		assert "source" not in obj

	def test_different_inputs_differ(self) -> None:
		kwargs = self._default_kwargs()
		a = build_provenance(**kwargs)
		kwargs["artifact_name"] = "web-other"
		b = build_provenance(**kwargs)
		assert a != b

	def test_compact_json(self) -> None:
		"""Output must be compact (no whitespace) for determinism."""
		raw = build_provenance(**self._default_kwargs())
		text = raw.decode("utf-8")
		# No indentation.
		assert "\n" not in text
		# No trailing space.
		assert ": " not in text


# ── write_provenance + sha256 ────────────────────────────────────────


class TestWriteProvenance:
	def test_write_and_sha256(self) -> None:
		data = b'{"test": true}'
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = Path(tmpdir) / "prov.json"
			write_provenance(path, data)
			assert path.read_bytes() == data
		import hashlib
		assert provenance_sha256(data) == hashlib.sha256(data).hexdigest()


# ── Envelope v2 ──────────────────────────────────────────────────────


class TestEnvelopeV2:
	def test_build_envelope_v2(self) -> None:
		from lang.drift.envelope import build_envelope_v2
		env = build_envelope_v2(
			package_sha256_hex="aabb",
			author_profile_sha256_hex="ccdd",
			provenance_sha256_hex="eeff",
		)
		text = env.decode("utf-8")
		lines = text.strip().split("\n")
		assert lines[0] == "drift-sig-envelope-v2"
		assert lines[1] == "package-sha256:aabb"
		assert lines[2] == "author-profile-sha256:ccdd"
		assert lines[3] == "provenance-sha256:eeff"

	def test_build_envelope_v2_no_optionals(self) -> None:
		from lang.drift.envelope import build_envelope_v2
		env = build_envelope_v2(package_sha256_hex="aabb")
		text = env.decode("utf-8")
		lines = text.strip().split("\n")
		assert len(lines) == 2
		assert lines[0] == "drift-sig-envelope-v2"
		assert lines[1] == "package-sha256:aabb"


# ── Sign + verify with provenance ───────────────────────────────────


class TestSignVerifyWithProvenance:
	@staticmethod
	def _make_key(tmpdir: Path) -> tuple[Path, bytes]:
		"""Create an Ed25519 key seed file. Returns (path, seed)."""
		seed = os.urandom(32)
		key_path = tmpdir / "key.seed"
		key_path.write_text(base64.b64encode(seed).decode() + "\n")
		return key_path, seed

	def test_sign_verify_with_provenance(self) -> None:
		"""Sign with provenance, verify succeeds."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import (
			load_sig_sidecar,
			verify_ed25519,
			_build_envelope_v2,
			sha256_hex,
		)

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, seed = self._make_key(td)

			# Create a fake package.
			pkg_path = td / "test.dmp"
			pkg_bytes = b"fake package bytes for provenance test"
			pkg_path.write_bytes(pkg_bytes)

			# Create a provenance file.
			prov_path = td / "test.provenance.json"
			prov_bytes = b'{"schema_version":2,"test":true}'
			prov_path.write_bytes(prov_bytes)

			# Sign.
			sig_path = td / "test.sig"
			sign_package_v0(SignOptions(
				package_path=pkg_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				provenance_path=prov_path,
			))

			# Load the sidecar.
			sf = load_sig_sidecar(sig_path)
			assert sf.envelope_version == 2
			assert sf.provenance_sha256_hex == sha256_hex(prov_bytes)
			assert sf.package_sha256_hex == sha256_hex(pkg_bytes)

			# Reconstruct the signed message and verify.
			signed_message = _build_envelope_v2(
				package_sha256_hex=sf.package_sha256_hex,
				provenance_sha256_hex=sf.provenance_sha256_hex,
			)
			entry = sf.signatures[0]
			assert entry.pubkey_raw is not None
			ok = verify_ed25519(
				pubkey_raw=entry.pubkey_raw,
				message=signed_message,
				signature_raw=entry.sig_raw,
			)
			assert ok

	def test_tampered_provenance_rejected(self) -> None:
		"""Modify provenance after signing, verification must fail."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import (
			load_sig_sidecar,
			verify_ed25519,
			_build_envelope_v2,
			sha256_hex,
		)

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, _ = self._make_key(td)

			pkg_path = td / "test.dmp"
			pkg_path.write_bytes(b"package bytes")

			prov_path = td / "test.provenance.json"
			prov_path.write_bytes(b'{"schema_version":2,"original":true}')

			sig_path = td / "test.sig"
			sign_package_v0(SignOptions(
				package_path=pkg_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				provenance_path=prov_path,
			))

			sf = load_sig_sidecar(sig_path)

			# Tamper: use different provenance digest.
			tampered_prov_sha = sha256_hex(b'{"schema_version":2,"tampered":true}')
			signed_message = _build_envelope_v2(
				package_sha256_hex=sf.package_sha256_hex,
				provenance_sha256_hex=tampered_prov_sha,
			)
			entry = sf.signatures[0]
			assert entry.pubkey_raw is not None
			ok = verify_ed25519(
				pubkey_raw=entry.pubkey_raw,
				message=signed_message,
				signature_raw=entry.sig_raw,
			)
			assert not ok

	def test_verify_backward_compat_v0(self) -> None:
		"""V0 packages (no profile, no provenance) still verify."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import (
			load_sig_sidecar,
			verify_ed25519,
			sha256_hex,
		)

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, _ = self._make_key(td)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"legacy package bytes"
			pkg_path.write_bytes(pkg_bytes)

			sig_path = td / "test.sig"
			sign_package_v0(SignOptions(
				package_path=pkg_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
			))

			sf = load_sig_sidecar(sig_path)
			assert sf.envelope_version == 0

			# V0: signature covers raw package bytes.
			entry = sf.signatures[0]
			assert entry.pubkey_raw is not None
			ok = verify_ed25519(
				pubkey_raw=entry.pubkey_raw,
				message=pkg_bytes,
				signature_raw=entry.sig_raw,
			)
			assert ok

	def test_verify_backward_compat_v1(self) -> None:
		"""V1 packages (profile, no provenance) still verify."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.drift.author_profile import create_author_profile, write_author_profile
		from lang.drift.crypto import ed25519_public_bytes_raw, sha256_hex
		from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
		from lang.driftc.packages.signature_v0 import (
			load_sig_sidecar,
			verify_ed25519,
			_build_envelope,
		)

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			seed = os.urandom(32)
			key_path = td / "key.seed"
			key_path.write_text(base64.b64encode(seed).decode() + "\n")

			priv = Ed25519PrivateKey.from_private_bytes(seed)
			pub_raw = ed25519_public_bytes_raw(priv.public_key())
			profile = create_author_profile(pubkey_raw=pub_raw, name="Test", namespaces=["test.*"])
			profile_path = td / "test.author-profile"
			write_author_profile(profile, profile_path)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"v1 package bytes"
			pkg_path.write_bytes(pkg_bytes)

			sig_path = td / "test.sig"
			sign_package_v0(SignOptions(
				package_path=pkg_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				author_profile_path=profile_path,
			))

			sf = load_sig_sidecar(sig_path)
			assert sf.envelope_version == 1

			# V1: signature covers envelope with package + profile digests.
			signed_message = _build_envelope(
				package_sha256_hex=sf.package_sha256_hex,
				author_profile_sha256_hex=sf.author_profile_sha256_hex,
			)
			entry = sf.signatures[0]
			assert entry.pubkey_raw is not None
			ok = verify_ed25519(
				pubkey_raw=entry.pubkey_raw,
				message=signed_message,
				signature_raw=entry.sig_raw,
			)
			assert ok


# ── artifact_sha256 ─────────────────────────────────────────────────


class TestProvenanceArtifactSha256:
	def test_provenance_contains_artifact_sha256(self) -> None:
		"""Verify artifact_sha256 field exists and is correct."""
		import hashlib
		artifact_bytes = b"these are the artifact bytes"
		artifact_sha = f"sha256:{hashlib.sha256(artifact_bytes).hexdigest()}"
		raw = build_provenance(
			artifact_name="my-pkg",
			artifact_version="1.0.0",
			artifact_kind="package",
			artifact_sha256=artifact_sha,
			target="drift-dev",
			compiler=CompilerInfo(version="0.27.92", abi=6, commit="abc1234"),
			resolved_deps={},
		)
		obj = json.loads(raw)
		assert obj["artifact_sha256"] == artifact_sha
		assert obj["artifact_sha256"].startswith("sha256:")


# ── Verifier provenance integrity ───────────────────────────────────


class TestVerifierProvenanceIntegrity:
	@staticmethod
	def _make_key(tmpdir: Path) -> tuple[Path, bytes]:
		seed = os.urandom(32)
		key_path = tmpdir / "key.seed"
		key_path.write_text(base64.b64encode(seed).decode() + "\n")
		return key_path, seed

	@staticmethod
	def _sign(td: Path, key_path: Path, pkg_path: Path, prov_path: Path, *, author_profile_path: Path | None = None) -> Path:
		from lang.drift.sign import SignOptions, sign_package_v0
		sig_path = td / f"{pkg_path.stem}.sig"
		sign_package_v0(SignOptions(
			package_path=pkg_path,
			key_seed_path=key_path,
			key_seed_text=None,
			out_path=sig_path,
			add_signature=False,
			include_pubkey=True,
			provenance_path=prov_path,
			author_profile_path=author_profile_path,
		))
		return sig_path

	@staticmethod
	def _make_trust(td: Path, seed: bytes) -> "TrustStore":
		"""Build a minimal trust store authorizing the given key for test.*."""
		from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
		from lang.drift.crypto import compute_ed25519_kid, ed25519_public_bytes_raw
		from lang.driftc.packages.trust_v0 import TrustedKey
		priv = Ed25519PrivateKey.from_private_bytes(seed)
		pub_raw = ed25519_public_bytes_raw(priv.public_key())
		kid = compute_ed25519_kid(pub_raw)
		tk = TrustedKey(algo="ed25519", kid=kid, pubkey_raw=pub_raw)
		return TrustStore(
			keys_by_kid={kid: tk},
			allowed_kids_by_namespace={"test.*": {kid}},
			revoked_kids=set(),
		)

	def test_verification_rejects_modified_provenance(self) -> None:
		"""Sign a package, modify provenance.json on disk, verification must fail."""
		from lang.driftc.packages.signature_v0 import verify_package_signatures

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, seed = self._make_key(td)
			trust = self._make_trust(td, seed)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"package bytes for provenance integrity test"
			pkg_path.write_bytes(pkg_bytes)

			prov_path = td / "test.provenance.json"
			prov_bytes = b'{"schema_version":2,"original":true}'
			prov_path.write_bytes(prov_bytes)

			self._sign(td, key_path, pkg_path, prov_path)

			# Tamper with provenance on disk.
			prov_path.write_bytes(b'{"schema_version":2,"tampered":true}')

			# Verification must fail because disk provenance != signed digest.
			pkg_manifest = {"modules": [{"module_id": "test.foo"}]}
			with pytest.raises(ValueError, match="provenance sidecar integrity check failed"):
				verify_package_signatures(
					pkg_path=pkg_path,
					pkg_bytes=pkg_bytes,
					pkg_manifest=pkg_manifest,
					trust=trust,
					core_trust=TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set()),
					require_signatures=True,
					allow_unsigned_roots=[],
					provenance_path=prov_path,
				)

	def test_verification_rejects_modified_artifact(self) -> None:
		"""Sign a package, modify the artifact bytes, verification must fail."""
		from lang.driftc.packages.signature_v0 import verify_package_signatures

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, seed = self._make_key(td)
			trust = self._make_trust(td, seed)

			pkg_path = td / "test.dmp"
			original_bytes = b"original artifact bytes"
			pkg_path.write_bytes(original_bytes)

			prov_path = td / "test.provenance.json"
			prov_bytes = b'{"schema_version":2,"test":true}'
			prov_path.write_bytes(prov_bytes)

			self._sign(td, key_path, pkg_path, prov_path)

			# Tamper with artifact bytes.
			tampered_bytes = b"tampered artifact bytes"

			# Verification must fail because pkg sha256 no longer matches .sig.
			pkg_manifest = {"modules": [{"module_id": "test.foo"}]}
			with pytest.raises(ValueError, match="package_sha256 mismatch"):
				verify_package_signatures(
					pkg_path=pkg_path,
					pkg_bytes=tampered_bytes,
					pkg_manifest=pkg_manifest,
					trust=trust,
					core_trust=TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set()),
					require_signatures=True,
					allow_unsigned_roots=[],
					provenance_path=prov_path,
				)

	def test_envelope_uses_real_provenance_digest(self) -> None:
		"""Verify the signed envelope includes provenance digest from actual sidecar."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import load_sig_sidecar, sha256_hex

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, _ = self._make_key(td)

			pkg_path = td / "test.dmp"
			pkg_path.write_bytes(b"pkg bytes")

			prov_path = td / "test.provenance.json"
			prov_bytes = b'{"schema_version":2,"real":"provenance"}'
			prov_path.write_bytes(prov_bytes)

			sig_path = td / "test.sig"
			sign_package_v0(SignOptions(
				package_path=pkg_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				provenance_path=prov_path,
			))

			sf = load_sig_sidecar(sig_path)
			# The signed envelope records the real provenance digest.
			assert sf.provenance_sha256_hex == sha256_hex(prov_bytes)
			assert sf.envelope_version == 2

	def test_app_provenance_is_authenticated(self) -> None:
		"""App provenance is emitted and authenticated with a .sig file."""
		import hashlib
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import load_sig_sidecar, sha256_hex

		app_bytes = b"compiled app binary"
		app_sha = f"sha256:{hashlib.sha256(app_bytes).hexdigest()}"
		prov_bytes = build_provenance(
			artifact_name="my-app",
			artifact_version="1.0.0",
			artifact_kind="app",
			artifact_sha256=app_sha,
			target="drift-dev",
			compiler=CompilerInfo(version="0.27.92", abi=6, commit="abc"),
			resolved_deps={},
		)
		obj = json.loads(prov_bytes)
		assert obj["artifact_kind"] == "app"
		assert obj["artifact_sha256"] == app_sha

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)

			# Write app binary and provenance bundle.
			app_path = td / "my-app"
			app_path.write_bytes(app_bytes)

			prov_obj = json.loads(prov_bytes)
			bundle_raw = build_provenance_bundle(prov_obj, {}, {})
			compressed = compress_provenance_bundle(bundle_raw)
			prov_path = td / "my-app.provenance.zst"
			write_provenance_bundle(prov_path, compressed)

			# Create a signing key.
			seed = os.urandom(32)
			key_path = td / "key.seed"
			key_path.write_text(base64.b64encode(seed).decode() + "\n")

			# Sign the app.
			sig_path = td / "my-app.sig"
			sign_package_v0(SignOptions(
				package_path=app_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				provenance_path=prov_path,
			))

			# .sig exists and covers app binary + provenance.
			assert sig_path.exists()
			sf = load_sig_sidecar(sig_path)
			assert sf.envelope_version == 2
			assert sf.package_sha256_hex == sha256_hex(app_bytes)
			assert sf.provenance_sha256_hex == sha256_hex(compressed)

	def test_verification_rejects_missing_provenance(self) -> None:
		"""If .sig says provenance is in the envelope, the file must exist on disk."""
		from lang.driftc.packages.signature_v0 import verify_package_signatures

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, seed = self._make_key(td)
			trust = self._make_trust(td, seed)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"package bytes for missing provenance test"
			pkg_path.write_bytes(pkg_bytes)

			prov_path = td / "test.provenance.json"
			prov_bytes = b'{"schema_version":2,"present":true}'
			prov_path.write_bytes(prov_bytes)

			self._sign(td, key_path, pkg_path, prov_path)

			# Delete provenance on disk after signing.
			prov_path.unlink()
			assert not prov_path.exists()

			# Verification must fail because the signed envelope requires provenance.
			pkg_manifest = {"modules": [{"module_id": "test.foo"}]}
			with pytest.raises(ValueError, match="provenance sidecar required by signed envelope but not found"):
				verify_package_signatures(
					pkg_path=pkg_path,
					pkg_bytes=pkg_bytes,
					pkg_manifest=pkg_manifest,
					trust=trust,
					core_trust=TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set()),
					require_signatures=True,
					allow_unsigned_roots=[],
					provenance_path=prov_path,
				)

	def test_verification_succeeds_with_matching_provenance(self) -> None:
		"""Provenance present and matching signed digest → verification succeeds."""
		from lang.driftc.packages.signature_v0 import verify_package_signatures

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, seed = self._make_key(td)
			trust = self._make_trust(td, seed)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"package bytes for success test"
			pkg_path.write_bytes(pkg_bytes)

			prov_path = td / "test.provenance.json"
			prov_bytes = b'{"schema_version":2,"correct":true}'
			prov_path.write_bytes(prov_bytes)

			self._sign(td, key_path, pkg_path, prov_path)

			# Verification should succeed — provenance exists and matches.
			pkg_manifest = {"modules": [{"module_id": "test.foo"}]}
			# Should not raise.
			verify_package_signatures(
				pkg_path=pkg_path,
				pkg_bytes=pkg_bytes,
				pkg_manifest=pkg_manifest,
				trust=trust,
				core_trust=TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set()),
				require_signatures=True,
				allow_unsigned_roots=[],
				provenance_path=prov_path,
			)

	def test_verification_succeeds_with_zst_bundle(self) -> None:
		"""Sign with .provenance.zst, verification succeeds."""
		from lang.driftc.packages.signature_v0 import verify_package_signatures

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, seed = self._make_key(td)
			trust = self._make_trust(td, seed)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"package bytes for zst bundle test"
			pkg_path.write_bytes(pkg_bytes)

			# Build a provenance bundle and compress it.
			prov_obj = {"schema_version": 2, "test": True}
			bundle_raw = build_provenance_bundle(prov_obj, {}, {})
			bundle_compressed = compress_provenance_bundle(bundle_raw)
			prov_path = td / "test.provenance.zst"
			write_provenance_bundle(prov_path, bundle_compressed)

			self._sign(td, key_path, pkg_path, prov_path)

			# Verification should succeed with .zst provenance.
			pkg_manifest = {"modules": [{"module_id": "test.foo"}]}
			verify_package_signatures(
				pkg_path=pkg_path,
				pkg_bytes=pkg_bytes,
				pkg_manifest=pkg_manifest,
				trust=trust,
				core_trust=TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set()),
				require_signatures=True,
				allow_unsigned_roots=[],
				provenance_path=prov_path,
			)

	def test_verification_rejects_modified_zst_bundle(self) -> None:
		"""Tampered .provenance.zst → verification fails."""
		from lang.driftc.packages.signature_v0 import verify_package_signatures

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, seed = self._make_key(td)
			trust = self._make_trust(td, seed)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"package bytes for tampered zst test"
			pkg_path.write_bytes(pkg_bytes)

			# Build original bundle.
			prov_obj = {"schema_version": 2, "original": True}
			bundle_raw = build_provenance_bundle(prov_obj, {}, {})
			bundle_compressed = compress_provenance_bundle(bundle_raw)
			prov_path = td / "test.provenance.zst"
			write_provenance_bundle(prov_path, bundle_compressed)

			self._sign(td, key_path, pkg_path, prov_path)

			# Tamper: write different bundle.
			tampered_obj = {"schema_version": 2, "tampered": True}
			tampered_raw = build_provenance_bundle(tampered_obj, {}, {})
			tampered_compressed = compress_provenance_bundle(tampered_raw)
			prov_path.write_bytes(tampered_compressed)

			pkg_manifest = {"modules": [{"module_id": "test.foo"}]}
			with pytest.raises(ValueError, match="provenance sidecar integrity check failed"):
				verify_package_signatures(
					pkg_path=pkg_path,
					pkg_bytes=pkg_bytes,
					pkg_manifest=pkg_manifest,
					trust=trust,
					core_trust=TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set()),
					require_signatures=True,
					allow_unsigned_roots=[],
					provenance_path=prov_path,
				)

	def test_verification_rejects_missing_zst_bundle(self) -> None:
		"""Missing .provenance.zst → verification fails."""
		from lang.driftc.packages.signature_v0 import verify_package_signatures

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, seed = self._make_key(td)
			trust = self._make_trust(td, seed)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"package bytes for missing zst test"
			pkg_path.write_bytes(pkg_bytes)

			prov_obj = {"schema_version": 2, "present": True}
			bundle_raw = build_provenance_bundle(prov_obj, {}, {})
			bundle_compressed = compress_provenance_bundle(bundle_raw)
			prov_path = td / "test.provenance.zst"
			write_provenance_bundle(prov_path, bundle_compressed)

			self._sign(td, key_path, pkg_path, prov_path)

			# Delete provenance on disk.
			prov_path.unlink()

			pkg_manifest = {"modules": [{"module_id": "test.foo"}]}
			with pytest.raises(ValueError, match="provenance sidecar required by signed envelope but not found"):
				verify_package_signatures(
					pkg_path=pkg_path,
					pkg_bytes=pkg_bytes,
					pkg_manifest=pkg_manifest,
					trust=trust,
					core_trust=TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set()),
					require_signatures=True,
					allow_unsigned_roots=[],
					provenance_path=prov_path,
				)


# ── Provenance bundle ───────────────────────────────────────────────


class TestProvenanceBundle:
	def test_bundle_deterministic(self) -> None:
		"""Same inputs produce identical compressed bytes."""
		prov = {"schema_version": 2, "artifact_name": "test", "build_utc": "2026-03-20T00:00:00Z"}
		dep_prov = {"web-jwt": {"schema_version": 2, "artifact_name": "web-jwt"}}
		dep_keys = {"ed25519:abc": {"algo": "ed25519", "kid": "ed25519:abc", "pubkey": "AAAA"}}

		a = compress_provenance_bundle(build_provenance_bundle(prov, dep_prov, dep_keys))
		b = compress_provenance_bundle(build_provenance_bundle(prov, dep_prov, dep_keys))
		assert a == b

	def test_bundle_contains_dep_provenance(self) -> None:
		"""Dependency provenance is embedded in the bundle."""
		prov = {"schema_version": 2, "artifact_name": "test"}
		dep_prov = {
			"web-jwt": {"schema_version": 2, "artifact_name": "web-jwt"},
			"acme.util": {"schema_version": 2, "artifact_name": "acme.util"},
		}
		raw = build_provenance_bundle(prov, dep_prov, {})
		bundle = json.loads(raw)
		assert bundle["format"] == "drift-provenance-bundle"
		assert bundle["version"] == 0
		assert bundle["provenance"]["artifact_name"] == "test"
		assert "web-jwt" in bundle["dep_provenance"]
		assert "acme.util" in bundle["dep_provenance"]

	def test_bundle_contains_dep_keys(self) -> None:
		"""Dependency public keys are embedded in the bundle."""
		prov = {"schema_version": 2, "artifact_name": "test"}
		dep_keys = {
			"ed25519:abc123": {"algo": "ed25519", "kid": "ed25519:abc123", "pubkey": "AAAA"},
		}
		raw = build_provenance_bundle(prov, {}, dep_keys)
		bundle = json.loads(raw)
		assert "ed25519:abc123" in bundle["dep_keys"]
		assert bundle["dep_keys"]["ed25519:abc123"]["pubkey"] == "AAAA"

	def test_bundle_roundtrip(self) -> None:
		"""Compress → write → load → decompress preserves content."""
		prov = {"schema_version": 2, "artifact_name": "roundtrip-test"}
		dep_prov = {"dep-a": {"schema_version": 2, "artifact_name": "dep-a"}}
		dep_keys = {"ed25519:xyz": {"algo": "ed25519", "kid": "ed25519:xyz", "pubkey": "BBBB"}}

		raw = build_provenance_bundle(prov, dep_prov, dep_keys)
		compressed = compress_provenance_bundle(raw)

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = Path(tmpdir) / "test.provenance.zst"
			write_provenance_bundle(path, compressed)

			bundle = load_provenance_bundle(path)
			assert bundle["format"] == "drift-provenance-bundle"
			assert bundle["version"] == 0
			assert bundle["provenance"]["artifact_name"] == "roundtrip-test"
			assert "dep-a" in bundle["dep_provenance"]
			assert "ed25519:xyz" in bundle["dep_keys"]

	def test_published_layout_includes_provenance_zst(self) -> None:
		"""Provenance bundle uses .provenance.zst extension."""
		prov = {"schema_version": 2, "artifact_name": "my-pkg"}
		raw = build_provenance_bundle(prov, {}, {})
		compressed = compress_provenance_bundle(raw)

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = Path(tmpdir) / "my-pkg.provenance.zst"
			write_provenance_bundle(path, compressed)
			assert path.exists()
			assert path.suffix == ".zst"
			assert path.stem == "my-pkg.provenance"

	def test_signed_envelope_includes_bundle_digest(self) -> None:
		"""The .sig provenance_sha256 covers the compressed .zst bytes."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import load_sig_sidecar, sha256_hex

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			seed = os.urandom(32)
			key_path = td / "key.seed"
			key_path.write_text(base64.b64encode(seed).decode() + "\n")

			pkg_path = td / "test.dmp"
			pkg_path.write_bytes(b"pkg bytes for bundle digest test")

			# Build bundle.
			prov = {"schema_version": 2, "artifact_name": "test"}
			raw = build_provenance_bundle(prov, {}, {})
			compressed = compress_provenance_bundle(raw)
			prov_path = td / "test.provenance.zst"
			write_provenance_bundle(prov_path, compressed)

			sig_path = td / "test.sig"
			sign_package_v0(SignOptions(
				package_path=pkg_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				provenance_path=prov_path,
			))

			sf = load_sig_sidecar(sig_path)
			assert sf.envelope_version == 2
			# The digest must cover the compressed bytes (what's on disk).
			assert sf.provenance_sha256_hex == sha256_hex(compressed)

	def test_bundle_empty_deps(self) -> None:
		"""Bundle with no dependencies still has correct structure."""
		prov = {"schema_version": 2, "artifact_name": "standalone"}
		raw = build_provenance_bundle(prov, {}, {})
		bundle = json.loads(raw)
		assert bundle["dep_provenance"] == {}
		assert bundle["dep_keys"] == {}

	def test_verifier_conventional_zst_fallback(self) -> None:
		"""Verifier finds .provenance.zst by convention when no explicit path given."""
		from lang.driftc.packages.signature_v0 import verify_package_signatures

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			seed = os.urandom(32)
			key_path = td / "key.seed"
			key_path.write_text(base64.b64encode(seed).decode() + "\n")

			from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
			from lang.drift.crypto import compute_ed25519_kid, ed25519_public_bytes_raw
			from lang.driftc.packages.trust_v0 import TrustedKey
			priv = Ed25519PrivateKey.from_private_bytes(seed)
			pub_raw = ed25519_public_bytes_raw(priv.public_key())
			kid = compute_ed25519_kid(pub_raw)
			tk = TrustedKey(algo="ed25519", kid=kid, pubkey_raw=pub_raw)
			trust = TrustStore(
				keys_by_kid={kid: tk},
				allowed_kids_by_namespace={"test.*": {kid}},
				revoked_kids=set(),
			)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"package bytes for conventional zst test"
			pkg_path.write_bytes(pkg_bytes)

			# Create .provenance.zst at conventional location.
			prov_obj = {"schema_version": 2, "test": True}
			bundle_raw = build_provenance_bundle(prov_obj, {}, {})
			bundle_compressed = compress_provenance_bundle(bundle_raw)
			zst_path = td / "test.provenance.zst"
			write_provenance_bundle(zst_path, bundle_compressed)

			# Sign pointing to the .zst path.
			from lang.drift.sign import SignOptions, sign_package_v0
			sig_path = td / "test.sig"
			sign_package_v0(SignOptions(
				package_path=pkg_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				provenance_path=zst_path,
			))

			# Verify WITHOUT explicit provenance_path — verifier must find .zst.
			pkg_manifest = {"modules": [{"module_id": "test.foo"}]}
			verify_package_signatures(
				pkg_path=pkg_path,
				pkg_bytes=pkg_bytes,
				pkg_manifest=pkg_manifest,
				trust=trust,
				core_trust=TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set()),
				require_signatures=True,
				allow_unsigned_roots=[],
			)


# ── Author profile on-disk verification ─────────────────────────────


class TestAuthorProfileIntegrity:
	"""Verify the on-disk author profile is checked against the signed digest."""

	@staticmethod
	def _make_key(tmpdir: Path) -> tuple[Path, bytes]:
		seed = os.urandom(32)
		key_path = tmpdir / "key.seed"
		key_path.write_text(base64.b64encode(seed).decode() + "\n")
		return key_path, seed

	@staticmethod
	def _make_trust(td: Path, seed: bytes) -> "TrustStore":
		from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
		from lang.drift.crypto import compute_ed25519_kid, ed25519_public_bytes_raw
		from lang.driftc.packages.trust_v0 import TrustedKey
		priv = Ed25519PrivateKey.from_private_bytes(seed)
		pub_raw = ed25519_public_bytes_raw(priv.public_key())
		kid = compute_ed25519_kid(pub_raw)
		tk = TrustedKey(algo="ed25519", kid=kid, pubkey_raw=pub_raw)
		return TrustStore(
			keys_by_kid={kid: tk},
			allowed_kids_by_namespace={"test.*": {kid}},
			revoked_kids=set(),
		)

	@staticmethod
	def _make_profile(td: Path) -> Path:
		"""Create a minimal author-profile JSON file."""
		profile_path = td / "test.author-profile"
		profile_path.write_text(
			json.dumps({
				"format": "author-profile",
				"version": 0,
				"key": {"algo": "ed25519", "kid": "ed25519:test", "pubkey": "AAAA"},
				"publisher": {"name": "test", "org": "test", "email": "t@t", "url": ""},
				"namespaces": ["test.*"],
			}),
			encoding="utf-8",
		)
		return profile_path

	def test_verification_rejects_missing_author_profile(self) -> None:
		"""If .sig says author-profile is in the envelope, the file must exist."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import verify_package_signatures

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, seed = self._make_key(td)
			trust = self._make_trust(td, seed)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"package for missing author profile test"
			pkg_path.write_bytes(pkg_bytes)

			profile_path = self._make_profile(td)

			# Sign with both provenance and author profile.
			prov_path = td / "test.provenance.zst"
			from tools.drift_deploy.provenance import build_provenance_bundle, compress_provenance_bundle, write_provenance_bundle
			bundle_raw = build_provenance_bundle({"schema_version": 2, "test": True}, {}, {})
			write_provenance_bundle(prov_path, compress_provenance_bundle(bundle_raw))

			sig_path = td / "test.sig"
			sign_package_v0(SignOptions(
				package_path=pkg_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				provenance_path=prov_path,
				author_profile_path=profile_path,
			))

			# Delete author profile after signing.
			profile_path.unlink()

			pkg_manifest = {"modules": [{"module_id": "test.foo"}]}
			with pytest.raises(ValueError, match="author profile required by signed envelope but not found"):
				verify_package_signatures(
					pkg_path=pkg_path,
					pkg_bytes=pkg_bytes,
					pkg_manifest=pkg_manifest,
					trust=trust,
					core_trust=TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set()),
					require_signatures=True,
					allow_unsigned_roots=[],
					provenance_path=prov_path,
					author_profile_path=profile_path,
				)

	def test_verification_rejects_modified_author_profile(self) -> None:
		"""Modified author-profile after signing → verification fails."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import verify_package_signatures

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, seed = self._make_key(td)
			trust = self._make_trust(td, seed)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"package for modified author profile test"
			pkg_path.write_bytes(pkg_bytes)

			profile_path = self._make_profile(td)

			prov_path = td / "test.provenance.zst"
			from tools.drift_deploy.provenance import build_provenance_bundle, compress_provenance_bundle, write_provenance_bundle
			bundle_raw = build_provenance_bundle({"schema_version": 2, "test": True}, {}, {})
			write_provenance_bundle(prov_path, compress_provenance_bundle(bundle_raw))

			sig_path = td / "test.sig"
			sign_package_v0(SignOptions(
				package_path=pkg_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				provenance_path=prov_path,
				author_profile_path=profile_path,
			))

			# Tamper with author profile.
			profile_path.write_text('{"format":"author-profile","version":0,"tampered":true}', encoding="utf-8")

			pkg_manifest = {"modules": [{"module_id": "test.foo"}]}
			with pytest.raises(ValueError, match="author profile integrity check failed"):
				verify_package_signatures(
					pkg_path=pkg_path,
					pkg_bytes=pkg_bytes,
					pkg_manifest=pkg_manifest,
					trust=trust,
					core_trust=TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set()),
					require_signatures=True,
					allow_unsigned_roots=[],
					provenance_path=prov_path,
					author_profile_path=profile_path,
				)

	def test_verification_succeeds_with_matching_profile(self) -> None:
		"""Author profile present and matching → verification succeeds."""
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import verify_package_signatures

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			key_path, seed = self._make_key(td)
			trust = self._make_trust(td, seed)

			pkg_path = td / "test.dmp"
			pkg_bytes = b"package for matching profile test"
			pkg_path.write_bytes(pkg_bytes)

			profile_path = self._make_profile(td)

			prov_path = td / "test.provenance.zst"
			from tools.drift_deploy.provenance import build_provenance_bundle, compress_provenance_bundle, write_provenance_bundle
			bundle_raw = build_provenance_bundle({"schema_version": 2, "test": True}, {}, {})
			write_provenance_bundle(prov_path, compress_provenance_bundle(bundle_raw))

			sig_path = td / "test.sig"
			sign_package_v0(SignOptions(
				package_path=pkg_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				provenance_path=prov_path,
				author_profile_path=profile_path,
			))

			pkg_manifest = {"modules": [{"module_id": "test.foo"}]}
			# Should not raise.
			verify_package_signatures(
				pkg_path=pkg_path,
				pkg_bytes=pkg_bytes,
				pkg_manifest=pkg_manifest,
				trust=trust,
				core_trust=TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set()),
				require_signatures=True,
				allow_unsigned_roots=[],
				provenance_path=prov_path,
				author_profile_path=profile_path,
			)


# ── App provenance bundle (informational, NOT authenticated) ────────


class TestAppProvenanceBundle:
	"""App provenance bundles contain the same content as packages.
	When a signing key is available, apps are signed with the same
	v2 envelope as packages (minus author-profile)."""

	def test_app_bundle_emitted_with_all_fields(self) -> None:
		"""App provenance contains all required provenance fields."""
		import hashlib
		app_bytes = b"compiled drift app binary"
		app_sha = f"sha256:{hashlib.sha256(app_bytes).hexdigest()}"
		prov_bytes = build_provenance(
			artifact_name="my-app",
			artifact_version="1.0.0",
			artifact_kind="app",
			artifact_sha256=app_sha,
			target="drift-dev",
			compiler=CompilerInfo(version="0.27.93", abi=6, commit="abc1234"),
			resolved_deps={"net.tls": {"version": "0.3.8", "sha256": "deadbeef"}},
		)
		obj = json.loads(prov_bytes)
		assert obj["schema_version"] == 3
		assert obj["artifact_name"] == "my-app"
		assert obj["artifact_version"] == "1.0.0"
		assert obj["artifact_kind"] == "app"
		assert obj["artifact_sha256"] == app_sha
		assert obj["compiler_version"] == "0.27.93"
		assert obj["compiler_commit"] == "abc1234"
		assert obj["abi"] == 6
		assert "build_utc" in obj
		assert obj["resolved_deps"]["net.tls"]["version"] == "0.3.8"

	def test_app_bundle_contains_dep_provenance(self) -> None:
		"""App bundle includes dependency provenance when available."""
		import hashlib
		app_sha = f"sha256:{hashlib.sha256(b'app').hexdigest()}"
		prov_bytes = build_provenance(
			artifact_name="my-app",
			artifact_version="1.0.0",
			artifact_kind="app",
			artifact_sha256=app_sha,
			target="drift-dev",
			compiler=CompilerInfo(version="0.27.93", abi=6, commit="abc"),
			resolved_deps={},
		)
		prov_obj = json.loads(prov_bytes)
		dep_prov = {"net.tls": {"schema_version": 2, "artifact_name": "net.tls"}}
		dep_keys = {"ed25519:xyz": {"algo": "ed25519", "kid": "ed25519:xyz", "pubkey": "BBBB"}}
		bundle_raw = build_provenance_bundle(prov_obj, dep_prov, dep_keys)
		bundle = json.loads(bundle_raw)

		assert bundle["format"] == "drift-provenance-bundle"
		assert bundle["provenance"]["artifact_kind"] == "app"
		assert "net.tls" in bundle["dep_provenance"]
		assert "ed25519:xyz" in bundle["dep_keys"]

	def test_app_bundle_is_signed_when_key_available(self) -> None:
		"""App provenance bundle is authenticated — .sig is produced when
		a signing key is available, using the same v2 envelope as packages."""
		import hashlib
		from lang.drift.sign import SignOptions, sign_package_v0
		from lang.driftc.packages.signature_v0 import load_sig_sidecar, sha256_hex

		app_bytes = b"compiled app binary for signing test"
		app_sha = f"sha256:{hashlib.sha256(app_bytes).hexdigest()}"
		prov_bytes = build_provenance(
			artifact_name="my-app",
			artifact_version="1.0.0",
			artifact_kind="app",
			artifact_sha256=app_sha,
			target="drift-dev",
			compiler=CompilerInfo(version="0.27.93", abi=6, commit="abc"),
			resolved_deps={},
		)
		prov_obj = json.loads(prov_bytes)
		bundle_raw = build_provenance_bundle(prov_obj, {}, {})
		compressed = compress_provenance_bundle(bundle_raw)

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)

			# Write app binary.
			app_path = td / "my-app"
			app_path.write_bytes(app_bytes)

			# Write provenance bundle.
			prov_path = td / "my-app.provenance.zst"
			write_provenance_bundle(prov_path, compressed)

			# Create a signing key.
			seed = os.urandom(32)
			key_path = td / "key.seed"
			key_path.write_text(base64.b64encode(seed).decode() + "\n")

			# Sign (same as deploy pipeline does for apps).
			sig_path = td / "my-app.sig"
			sign_package_v0(SignOptions(
				package_path=app_path,
				key_seed_path=key_path,
				key_seed_text=None,
				out_path=sig_path,
				add_signature=False,
				include_pubkey=True,
				provenance_path=prov_path,
			))

			# .sig exists and covers the app binary + provenance.
			assert sig_path.exists()
			sf = load_sig_sidecar(sig_path)
			assert sf.envelope_version == 2
			assert sf.package_sha256_hex == sha256_hex(app_bytes)
			assert sf.provenance_sha256_hex == sha256_hex(compressed)

			# Bundle is readable and contains expected content.
			bundle = load_provenance_bundle(prov_path)
			assert bundle["provenance"]["artifact_kind"] == "app"
			assert bundle["provenance"]["artifact_sha256"].startswith("sha256:")


# ── App signing ─────────────────────────────────────────────────────


class TestAppSigning:
	"""App artifacts use the real verify_app_signatures verifier — same
	security model as packages (minus author-profile)."""

	@staticmethod
	def _make_key(tmpdir: Path) -> tuple[Path, bytes]:
		seed = os.urandom(32)
		key_path = tmpdir / "key.seed"
		key_path.write_text(base64.b64encode(seed).decode() + "\n")
		return key_path, seed

	@staticmethod
	def _make_trust(td: Path, seed: bytes) -> "TrustStore":
		from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
		from lang.drift.crypto import compute_ed25519_kid, ed25519_public_bytes_raw
		from lang.driftc.packages.trust_v0 import TrustedKey
		priv = Ed25519PrivateKey.from_private_bytes(seed)
		pub_raw = ed25519_public_bytes_raw(priv.public_key())
		kid = compute_ed25519_kid(pub_raw)
		tk = TrustedKey(algo="ed25519", kid=kid, pubkey_raw=pub_raw)
		return TrustStore(
			keys_by_kid={kid: tk},
			allowed_kids_by_namespace={"test.*": {kid}},
			revoked_kids=set(),
		)

	@staticmethod
	def _sign_app(td: Path, key_path: Path, app_path: Path, prov_path: Path) -> Path:
		from lang.drift.sign import SignOptions, sign_package_v0
		sig_path = td / f"{app_path.stem}.sig"
		sign_package_v0(SignOptions(
			package_path=app_path,
			key_seed_path=key_path,
			key_seed_text=None,
			out_path=sig_path,
			add_signature=False,
			include_pubkey=True,
			provenance_path=prov_path,
		))
		return sig_path

	def _setup(self, td: Path) -> tuple[Path, Path, Path, bytes, bytes, "TrustStore"]:
		"""Create app binary, provenance bundle, signing key, trust store."""
		import hashlib
		app_bytes = b"compiled app binary for app signing test"
		app_sha = f"sha256:{hashlib.sha256(app_bytes).hexdigest()}"
		app_path = td / "my-app"
		app_path.write_bytes(app_bytes)

		prov_raw = build_provenance(
			artifact_name="my-app", artifact_version="1.0.0",
			artifact_kind="app", artifact_sha256=app_sha,
			target="drift-dev",
			compiler=CompilerInfo(version="0.27.93", abi=6, commit="abc1234"),
			resolved_deps={},
		)
		prov_obj = json.loads(prov_raw)
		bundle_raw = build_provenance_bundle(prov_obj, {}, {})
		compressed = compress_provenance_bundle(bundle_raw)
		prov_path = td / "my-app.provenance.zst"
		write_provenance_bundle(prov_path, compressed)

		key_path, seed = self._make_key(td)
		trust = self._make_trust(td, seed)
		return app_path, prov_path, key_path, app_bytes, compressed, trust

	def test_app_sig_emitted(self) -> None:
		"""App signing produces .sig file."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			app_path, prov_path, key_path, _, _, _ = self._setup(td)
			sig_path = self._sign_app(td, key_path, app_path, prov_path)
			assert sig_path.exists()

	def test_app_envelope_includes_binary_digest(self) -> None:
		"""App .sig records app binary sha256."""
		from lang.driftc.packages.signature_v0 import load_sig_sidecar, sha256_hex
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			app_path, prov_path, key_path, app_bytes, _, _ = self._setup(td)
			self._sign_app(td, key_path, app_path, prov_path)
			sf = load_sig_sidecar(td / "my-app.sig")
			assert sf.package_sha256_hex == sha256_hex(app_bytes)

	def test_app_envelope_includes_provenance_digest(self) -> None:
		"""App .sig records provenance bundle sha256."""
		from lang.driftc.packages.signature_v0 import load_sig_sidecar, sha256_hex
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			app_path, prov_path, key_path, _, compressed, _ = self._setup(td)
			self._sign_app(td, key_path, app_path, prov_path)
			sf = load_sig_sidecar(td / "my-app.sig")
			assert sf.envelope_version == 2
			assert sf.provenance_sha256_hex == sha256_hex(compressed)

	def test_app_envelope_has_no_author_profile(self) -> None:
		"""App .sig does not include author-profile digest."""
		from lang.driftc.packages.signature_v0 import load_sig_sidecar
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			app_path, prov_path, key_path, _, _, _ = self._setup(td)
			self._sign_app(td, key_path, app_path, prov_path)
			sf = load_sig_sidecar(td / "my-app.sig")
			assert sf.author_profile_sha256_hex is None

	def test_app_verification_succeeds(self) -> None:
		"""Matching binary + matching provenance → verify_app_signatures passes."""
		from lang.driftc.packages.signature_v0 import verify_app_signatures
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			app_path, prov_path, key_path, app_bytes, _, trust = self._setup(td)
			self._sign_app(td, key_path, app_path, prov_path)
			# Should not raise.
			verify_app_signatures(
				app_path=app_path, app_bytes=app_bytes,
				trust=trust, provenance_path=prov_path,
			)

	def test_app_verification_rejects_modified_binary(self) -> None:
		"""Tampered binary → verify_app_signatures fails."""
		from lang.driftc.packages.signature_v0 import verify_app_signatures
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			app_path, prov_path, key_path, _, _, trust = self._setup(td)
			self._sign_app(td, key_path, app_path, prov_path)
			with pytest.raises(ValueError, match="artifact sha256 mismatch"):
				verify_app_signatures(
					app_path=app_path, app_bytes=b"tampered binary",
					trust=trust, provenance_path=prov_path,
				)

	def test_app_verification_rejects_modified_provenance(self) -> None:
		"""Tampered provenance → verify_app_signatures fails."""
		from lang.driftc.packages.signature_v0 import verify_app_signatures
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			app_path, prov_path, key_path, app_bytes, _, trust = self._setup(td)
			self._sign_app(td, key_path, app_path, prov_path)
			# Tamper provenance on disk.
			tampered = compress_provenance_bundle(
				build_provenance_bundle({"schema_version": 2, "tampered": True}, {}, {})
			)
			write_provenance_bundle(prov_path, tampered)
			with pytest.raises(ValueError, match="provenance bundle integrity check failed"):
				verify_app_signatures(
					app_path=app_path, app_bytes=app_bytes,
					trust=trust, provenance_path=prov_path,
				)

	def test_app_verification_rejects_missing_provenance(self) -> None:
		"""Missing provenance → verify_app_signatures fails."""
		from lang.driftc.packages.signature_v0 import verify_app_signatures
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			app_path, prov_path, key_path, app_bytes, _, trust = self._setup(td)
			self._sign_app(td, key_path, app_path, prov_path)
			prov_path.unlink()
			with pytest.raises(ValueError, match="provenance bundle required by signed envelope but not found"):
				verify_app_signatures(
					app_path=app_path, app_bytes=app_bytes,
					trust=trust, provenance_path=prov_path,
				)

	def test_app_verification_rejects_missing_sig(self) -> None:
		"""Missing .sig → verify_app_signatures fails."""
		from lang.driftc.packages.signature_v0 import verify_app_signatures
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			td = Path(tmpdir)
			app_path, prov_path, _, app_bytes, _, trust = self._setup(td)
			# Don't sign.
			with pytest.raises(ValueError, match="missing signature sidecar for app"):
				verify_app_signatures(
					app_path=app_path, app_bytes=app_bytes,
					trust=trust, provenance_path=prov_path,
				)
