# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for author profiles and consumer trust (drift init / drift trust)."""
from __future__ import annotations

import base64
import json
import os
import tempfile
from lang.test_support.drift_tmp import session_root
from pathlib import Path

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lang.drift.crypto import b64_encode, compute_ed25519_kid, ed25519_public_bytes_raw
from lang.drift.author_profile import (
	AuthorProfile,
	apply_author_profile_to_trust_store,
	create_author_profile,
	load_author_profile,
	write_author_profile,
)


def _make_key() -> tuple[bytes, bytes, str]:
	"""Generate an Ed25519 key. Returns (seed32, pubkey_raw32, kid)."""
	priv = Ed25519PrivateKey.generate()
	pub_raw = ed25519_public_bytes_raw(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	seed = priv.private_bytes_raw()
	return seed, pub_raw, kid


def _make_seed_file(tmpdir: str) -> Path:
	"""Create a valid key seed file. Returns the path."""
	key_path = Path(tmpdir) / "test.seed"
	seed = Ed25519PrivateKey.generate().private_bytes_raw()
	key_path.write_text(base64.b64encode(seed).decode("ascii") + "\n")
	return key_path


# ── Profile creation ─────────────────────────────────────────────────


class TestCreateProfile:
	def test_create_from_pubkey(self) -> None:
		_seed, pub_raw, kid = _make_key()
		profile = create_author_profile(
			pubkey_raw=pub_raw,
			name="Alice Park",
			org="Acme Labs",
			email="alice@acme.dev",
			url="https://acme.dev",
			namespaces=["acme.*"],
		)
		assert profile.kid == kid
		assert profile.pubkey_b64 == b64_encode(pub_raw)
		assert profile.name == "Alice Park"
		assert profile.org == "Acme Labs"
		assert profile.namespaces == ["acme.*"]

	def test_kid_matches_compiler(self) -> None:
		"""KID in profile matches what the compiler verifier produces."""
		from lang.drift.crypto import compute_ed25519_kid as compiler_kid
		_seed, pub_raw, _kid = _make_key()
		profile = create_author_profile(pubkey_raw=pub_raw, name="Test", namespaces=["test.*"])
		assert profile.kid == compiler_kid(pub_raw)

	def test_accepts_org_only(self) -> None:
		_seed, pub_raw, kid = _make_key()
		profile = create_author_profile(pubkey_raw=pub_raw, name="", org="Acme Labs", namespaces=["a.*"])
		assert profile.org == "Acme Labs"
		assert profile.name == ""

	def test_accepts_name_only(self) -> None:
		_seed, pub_raw, kid = _make_key()
		profile = create_author_profile(pubkey_raw=pub_raw, name="Alice", namespaces=["a.*"])
		assert profile.name == "Alice"
		assert profile.org == ""

	def test_rejects_both_empty(self) -> None:
		_seed, pub_raw, _kid = _make_key()
		with pytest.raises(ValueError, match="at least one of name or org"):
			create_author_profile(pubkey_raw=pub_raw, name="", org="", namespaces=["a.*"])

	def test_rejects_empty_namespaces(self) -> None:
		_seed, pub_raw, _kid = _make_key()
		with pytest.raises(ValueError, match="at least one namespace"):
			create_author_profile(pubkey_raw=pub_raw, name="Test", namespaces=[])

	def test_rejects_bad_pubkey_length(self) -> None:
		with pytest.raises(ValueError, match="32 bytes"):
			create_author_profile(pubkey_raw=b"tooshort", name="Test", namespaces=["a.*"])


# ── Profile serialization ────────────────────────────────────────────


class TestProfileRoundtrip:
	def test_write_and_load(self) -> None:
		_seed, pub_raw, kid = _make_key()
		profile = create_author_profile(
			pubkey_raw=pub_raw,
			name="Alice",
			org="Acme",
			email="a@b.c",
			url="https://x.y",
			namespaces=["acme.*", "acme.crypto"],
		)
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = Path(tmpdir) / "test.author-profile"
			write_author_profile(profile, path)
			loaded = load_author_profile(path)
		assert loaded.kid == profile.kid
		assert loaded.pubkey_b64 == profile.pubkey_b64
		assert loaded.name == "Alice"
		assert loaded.org == "Acme"
		assert loaded.email == "a@b.c"
		assert loaded.url == "https://x.y"
		assert loaded.namespaces == ["acme.*", "acme.crypto"]

	def test_rejects_wrong_format(self) -> None:
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = Path(tmpdir) / "bad.author-profile"
			path.write_text(json.dumps({"format": "wrong", "version": 0}))
			with pytest.raises(ValueError, match="not an author profile"):
				load_author_profile(path)

	def test_rejects_bad_version(self) -> None:
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = Path(tmpdir) / "bad.author-profile"
			path.write_text(json.dumps({"format": "author-profile", "version": 99}))
			with pytest.raises(ValueError, match="unsupported.*version"):
				load_author_profile(path)

	def test_rejects_bad_pubkey(self) -> None:
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = Path(tmpdir) / "bad.author-profile"
			path.write_text(json.dumps({
				"format": "author-profile", "version": 0,
				"key": {"algo": "ed25519", "kid": "ed25519:x", "pubkey": "dG9vc2hvcnQ="},
				"publisher": {"name": "Test"},
				"namespaces": ["test.*"],
			}))
			with pytest.raises(ValueError, match="32 bytes"):
				load_author_profile(path)

	def test_rejects_kid_mismatch(self) -> None:
		_seed, pub_raw, _kid = _make_key()
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = Path(tmpdir) / "bad.author-profile"
			path.write_text(json.dumps({
				"format": "author-profile", "version": 0,
				"key": {"algo": "ed25519", "kid": "ed25519:AAAA", "pubkey": b64_encode(pub_raw)},
				"publisher": {"name": "Test"},
				"namespaces": ["test.*"],
			}))
			with pytest.raises(ValueError, match="kid does not match"):
				load_author_profile(path)

	def test_rejects_empty_namespaces(self) -> None:
		_seed, pub_raw, kid = _make_key()
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = Path(tmpdir) / "bad.author-profile"
			path.write_text(json.dumps({
				"format": "author-profile", "version": 0,
				"key": {"algo": "ed25519", "kid": kid, "pubkey": b64_encode(pub_raw)},
				"publisher": {"name": "Test"},
				"namespaces": [],
			}))
			with pytest.raises(ValueError, match="non-empty"):
				load_author_profile(path)

	def test_rejects_both_name_and_org_empty(self) -> None:
		_seed, pub_raw, kid = _make_key()
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = Path(tmpdir) / "bad.author-profile"
			path.write_text(json.dumps({
				"format": "author-profile", "version": 0,
				"key": {"algo": "ed25519", "kid": kid, "pubkey": b64_encode(pub_raw)},
				"publisher": {"name": "", "org": ""},
				"namespaces": ["test.*"],
			}))
			with pytest.raises(ValueError, match="at least one"):
				load_author_profile(path)

	def test_loads_org_only_profile(self) -> None:
		_seed, pub_raw, kid = _make_key()
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = Path(tmpdir) / "org.author-profile"
			path.write_text(json.dumps({
				"format": "author-profile", "version": 0,
				"key": {"algo": "ed25519", "kid": kid, "pubkey": b64_encode(pub_raw)},
				"publisher": {"name": "", "org": "The Drift Foundation"},
				"namespaces": ["drift.*"],
			}))
			profile = load_author_profile(path)
			assert profile.name == ""
			assert profile.org == "The Drift Foundation"


# ── Trust store application ──────────────────────────────────────────


class TestApplyProfile:
	def test_applies_to_empty_store(self) -> None:
		_seed, pub_raw, kid = _make_key()
		profile = create_author_profile(pubkey_raw=pub_raw, name="Alice", namespaces=["acme.*"])
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			trust_path = Path(tmpdir) / "drift" / "trust.json"
			report = apply_author_profile_to_trust_store(profile, trust_path)
			assert report["kid"] == kid
			assert report["namespaces_added"] == ["acme.*"]
			assert report["already_trusted"] == []
			store = json.loads(trust_path.read_text())
			assert kid in store["keys"]
			assert kid in store["namespaces"]["acme.*"]

	def test_idempotent(self) -> None:
		_seed, pub_raw, kid = _make_key()
		profile = create_author_profile(pubkey_raw=pub_raw, name="Alice", namespaces=["acme.*"])
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			trust_path = Path(tmpdir) / "drift" / "trust.json"
			apply_author_profile_to_trust_store(profile, trust_path)
			report = apply_author_profile_to_trust_store(profile, trust_path)
			assert report["namespaces_added"] == []
			assert report["already_trusted"] == ["acme.*"]

	def test_adds_new_namespaces(self) -> None:
		_seed, pub_raw, kid = _make_key()
		p1 = create_author_profile(pubkey_raw=pub_raw, name="Alice", namespaces=["acme.*"])
		p2 = create_author_profile(pubkey_raw=pub_raw, name="Alice", namespaces=["acme.*", "acme.crypto.*"])
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			trust_path = Path(tmpdir) / "drift" / "trust.json"
			apply_author_profile_to_trust_store(p1, trust_path)
			report = apply_author_profile_to_trust_store(p2, trust_path)
			assert report["namespaces_added"] == ["acme.crypto.*"]
			assert report["already_trusted"] == ["acme.*"]

	def test_creates_store_if_missing(self) -> None:
		_seed, pub_raw, _kid = _make_key()
		profile = create_author_profile(pubkey_raw=pub_raw, name="Alice", namespaces=["acme.*"])
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			trust_path = Path(tmpdir) / "new" / "deep" / "trust.json"
			assert not trust_path.exists()
			apply_author_profile_to_trust_store(profile, trust_path)
			assert trust_path.exists()


# ── drift init CLI (non-interactive) ─────────────────────────────────


class TestInitCLI:
	def test_init_noninteractive_roundtrip(self) -> None:
		"""drift init --yes produces a valid .author-profile."""
		from lang.drift.cli import main as drift_main
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			key_path = _make_seed_file(tmpdir)
			out_path = Path(tmpdir) / "test.author-profile"
			rc = drift_main([
				"init",
				"--key", str(key_path),
				"--name", "Test Publisher",
				"--org", "TestOrg",
				"--namespace", "test.*",
				"--out", str(out_path),
				"--yes",
			])
			assert rc == 0
			assert out_path.exists()
			profile = load_author_profile(out_path)
			assert profile.name == "Test Publisher"
			assert profile.org == "TestOrg"
			assert profile.namespaces == ["test.*"]

	def test_init_org_only(self) -> None:
		"""drift init --org without --name produces valid org-only profile."""
		from lang.drift.cli import main as drift_main
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			key_path = _make_seed_file(tmpdir)
			out_path = Path(tmpdir) / "org.author-profile"
			rc = drift_main([
				"init",
				"--key", str(key_path),
				"--org", "The Drift Foundation",
				"--namespace", "drift.*",
				"--out", str(out_path),
				"--yes",
			])
			assert rc == 0
			profile = load_author_profile(out_path)
			assert profile.name == ""
			assert profile.org == "The Drift Foundation"

	def test_init_neither_name_nor_org_fails(self) -> None:
		"""drift init without --name and --org fails."""
		from lang.drift.cli import main as drift_main
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			key_path = _make_seed_file(tmpdir)
			out_path = Path(tmpdir) / "bad.author-profile"
			rc = drift_main([
				"init",
				"--key", str(key_path),
				"--namespace", "test.*",
				"--out", str(out_path),
				"--yes",
			])
			assert rc == 1
			assert not out_path.exists()

	def test_init_generates_key_when_missing(self) -> None:
		"""drift init --yes without existing key auto-generates one."""
		from lang.drift.cli import main as drift_main
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			keys_dir = Path(tmpdir) / "keys"
			out_path = Path(tmpdir) / "test.author-profile"
			old_env = os.environ.get("DRIFT_SIGN_KEY_FILE")
			try:
				os.environ.pop("DRIFT_SIGN_KEY_FILE", None)
				# Patch default keys dir to tmpdir.
				import lang.drift.cli as cli_mod
				orig_fn = cli_mod._default_keys_dir
				cli_mod._default_keys_dir = lambda: keys_dir
				rc = drift_main([
					"init",
					"--name", "AutoKey",
					"--namespace", "auto.*",
					"--out", str(out_path),
					"--yes",
				])
				cli_mod._default_keys_dir = orig_fn
			finally:
				if old_env is not None:
					os.environ["DRIFT_SIGN_KEY_FILE"] = old_env
			assert rc == 0
			assert out_path.exists()
			assert (keys_dir / "default.seed").exists()


# ── drift trust <profile> CLI ────────────────────────────────────────


class TestTrustProfileCLI:
	def test_trust_profile_noninteractive(self) -> None:
		"""drift trust <profile> --yes updates the trust store."""
		from lang.drift.cli import main as drift_main
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			seed = Ed25519PrivateKey.generate().private_bytes_raw()
			priv = Ed25519PrivateKey.from_private_bytes(seed)
			pub_raw = ed25519_public_bytes_raw(priv.public_key())
			profile = create_author_profile(pubkey_raw=pub_raw, name="Test", namespaces=["test.*"])
			profile_path = Path(tmpdir) / "test.author-profile"
			write_author_profile(profile, profile_path)

			trust_path = Path(tmpdir) / "drift" / "trust.json"
			rc = drift_main([
				"trust", str(profile_path),
				"--trust-store", str(trust_path),
				"--yes",
			])
			assert rc == 0
			assert trust_path.exists()
			store = json.loads(trust_path.read_text())
			assert profile.kid in store["keys"]
			assert profile.kid in store["namespaces"]["test.*"]

	def test_trust_nonexistent_profile_errors(self) -> None:
		from lang.drift.cli import main as drift_main
		rc = drift_main(["trust", "nonexistent.author-profile", "--yes"])
		assert rc == 1

	def test_trust_subcommand_not_confused_with_profile(self) -> None:
		"""drift trust list should NOT be treated as a profile file."""
		from lang.drift.cli import main as drift_main
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			trust_path = Path(tmpdir) / "drift" / "trust.json"
			rc = drift_main(["trust", "list", "--trust-store", str(trust_path)])
			assert rc == 0


# ── Namespace prompt wording ──────────────────────────────────────────


class TestNamespaceWording:
	def test_prompt_says_module_namespace_not_package(self) -> None:
		"""Namespace prompt must reference Drift module namespaces, not package ids."""
		import inspect
		from lang.drift.cli import _init_interactive
		source = inspect.getsource(_init_interactive)
		assert "module namespace" in source.lower(), (
			"drift init prompt must use 'module namespace' wording "
			"(trust namespaces follow imported module names, not package ids)"
		)
		assert "package namespace" not in source.lower(), (
			"drift init prompt must not say 'package namespace' "
			"(misleading when package id differs from module namespace, e.g. net-tls vs net_tls)"
		)


# ── Key resolution precedence ────────────────────────────────────────


class TestKeyResolution:
	def test_cli_key_overrides_env(self) -> None:
		from lang.drift.cli import _resolve_signing_key_path
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			cli_key = Path(tmpdir) / "cli.seed"
			env_key = Path(tmpdir) / "env.seed"
			cli_key.write_text("x")
			env_key.write_text("x")
			old = os.environ.get("DRIFT_SIGN_KEY_FILE")
			try:
				os.environ["DRIFT_SIGN_KEY_FILE"] = str(env_key)
				result = _resolve_signing_key_path(cli_key)
				assert result == cli_key
			finally:
				if old is None:
					os.environ.pop("DRIFT_SIGN_KEY_FILE", None)
				else:
					os.environ["DRIFT_SIGN_KEY_FILE"] = old

	def test_env_used_when_no_cli_key(self) -> None:
		from lang.drift.cli import _resolve_signing_key_path
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			env_key = Path(tmpdir) / "env.seed"
			env_key.write_text("x")
			old = os.environ.get("DRIFT_SIGN_KEY_FILE")
			try:
				os.environ["DRIFT_SIGN_KEY_FILE"] = str(env_key)
				result = _resolve_signing_key_path(None)
				assert result == env_key
			finally:
				if old is None:
					os.environ.pop("DRIFT_SIGN_KEY_FILE", None)
				else:
					os.environ["DRIFT_SIGN_KEY_FILE"] = old

	def test_none_when_no_key_available(self) -> None:
		from lang.drift.cli import _resolve_signing_key_path
		old = os.environ.get("DRIFT_SIGN_KEY_FILE")
		try:
			os.environ.pop("DRIFT_SIGN_KEY_FILE", None)
			result = _resolve_signing_key_path(None)
			assert result is None
		finally:
			if old is not None:
				os.environ["DRIFT_SIGN_KEY_FILE"] = old


# ── Overwrite protection ─────────────────────────────────────────────


class TestOverwriteProtection:
	def test_fresh_output_succeeds(self) -> None:
		from lang.drift.cli import main as drift_main
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			key_path = _make_seed_file(tmpdir)
			out_path = Path(tmpdir) / "new.author-profile"
			assert not out_path.exists()
			rc = drift_main([
				"init",
				"--key", str(key_path),
				"--name", "Test",
				"--namespace", "test.*",
				"--out", str(out_path),
				"--yes",
			])
			assert rc == 0
			assert out_path.exists()

	def test_noninteractive_rejects_overwrite_without_yes(self) -> None:
		from lang.drift.cli import _init_noninteractive
		import argparse
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			key_path = _make_seed_file(tmpdir)
			out_path = Path(tmpdir) / "existing.author-profile"
			out_path.write_text('{"existing": true}')
			original_content = out_path.read_text()
			args = argparse.Namespace(
				key=key_path, name="Test", org="Org", email="", url="",
				namespace=["test.*"], out=out_path, yes=False,
			)
			rc = _init_noninteractive(args)
			assert rc == 1
			assert out_path.read_text() == original_content

	def test_noninteractive_yes_permits_overwrite(self) -> None:
		from lang.drift.cli import main as drift_main
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			key_path = _make_seed_file(tmpdir)
			out_path = Path(tmpdir) / "existing.author-profile"
			out_path.write_text('{"old": true}')
			rc = drift_main([
				"init",
				"--key", str(key_path),
				"--name", "Replaced",
				"--namespace", "replaced.*",
				"--out", str(out_path),
				"--yes",
			])
			assert rc == 0
			profile = load_author_profile(out_path)
			assert profile.name == "Replaced"

	def test_no_partial_file_on_refusal(self) -> None:
		from lang.drift.cli import _init_noninteractive
		import argparse
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			key_path = _make_seed_file(tmpdir)
			out_path = Path(tmpdir) / "existing.author-profile"
			original = '{"format": "author-profile", "version": 0, "preserved": true}\n'
			out_path.write_text(original)
			args = argparse.Namespace(
				key=key_path, name="Test", org="", email="", url="",
				namespace=["test.*"], out=out_path, yes=False,
			)
			rc = _init_noninteractive(args)
			assert rc == 1
			assert out_path.read_text() == original
