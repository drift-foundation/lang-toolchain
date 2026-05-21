# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
End-to-end regression for `drift trust bootstrap` + `drift trust check`.

These cover the project-trust preflight UX the cert team asked for:

  drift trust bootstrap --manifest drift/manifest.json
      → setup / repair drift/trust.json from on-disk author claims
        + companion pubkey files
  drift trust check     --manifest drift/manifest.json
      → read-only preflight before `drift deploy`

The two failures the team wanted these to catch:
  - net-tls: missing drift/trust.json despite a valid author claim
  - mariadb: stale author claim after a manifest version bump
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from lang.drift.cli import main as drift_cli_main
from lang.drift.trust import (
	TrustBootstrapOptions,
	TrustCheckOptions,
	_namespace_is_reserved,
	_range_covers,
	bootstrap_trust_from_manifest,
	check_trust_for_manifest,
	plan_trust_bootstrap,
)


def _seed_b64() -> str:
	# Deterministic seed for reproducible kids inside the suite.
	return base64.b64encode(bytes(range(32))).decode("ascii")


def _write_seed(path: Path, seed_b64: str) -> Path:
	path.write_text(seed_b64 + "\n", encoding="utf-8")
	return path


def _layout(tmp_path: Path, *, artifact_name: str = "net-tls") -> tuple[Path, Path]:
	"""Build a minimal `<repo>/drift/manifest.json` + sources.

	Returns `(manifest_dir, manifest_path)`.
	"""
	project_root = tmp_path / "myrepo"
	drift = project_root / "drift"
	drift.mkdir(parents=True)
	(project_root / "src").mkdir()
	(project_root / "src" / "lib.drift").write_text(
		f"module {artifact_name.replace('-', '_')};\n", encoding="utf-8",
	)
	(project_root / "assets").mkdir()
	(project_root / "assets" / "ca.pem").write_text("(stub)\n", encoding="utf-8")
	mf = {
		"schema_version": 2,
		"project": {"name": artifact_name, "license": "MIT"},
		"artifacts": [{
			"kind": "library", "name": artifact_name, "version": "0.5.0",
			"description": "demo",
			"entry_module": "src/lib.drift",
			"modules": ["src/lib.drift"],
			"assets": ["assets/ca.pem"],
			"native_deps": [{"lib": ":libssl.so.3"}],
			"package_deps": [{"name": "std", "version": "0"}],
		}],
	}
	(drift / "manifest.json").write_text(json.dumps(mf), encoding="utf-8")
	return drift, drift / "manifest.json"


def _publish(manifest_path: Path, seed: Path, *, artifact: str | None = None,
		overwrite: bool = False) -> None:
	"""Run drift-author publish to mint claim + pubkey companion."""
	from tools.drift_author.cli import main as author_cli_main
	argv = [
		"publish",
		"--manifest", str(manifest_path),
		"--key-file", str(seed),
	]
	if artifact:
		argv.extend(["--artifact", artifact])
	if overwrite:
		argv.append("--overwrite")
	rc = author_cli_main(argv)
	assert rc == 0, f"drift-author publish failed: rc={rc}"


# ── publish-time pubkey companion ──────────────────────────────────


def test_publish_writes_author_pubkey_companion(tmp_path: Path) -> None:
	"""`drift-author publish` MUST emit `<pkg>.author-pubkey.b64`
	next to the claim.  `drift trust bootstrap` depends on this
	companion to derive the trust store from the on-disk sidecars
	alone.
	"""
	drift, mf = _layout(tmp_path)
	seed = _write_seed(tmp_path / "k.seed", _seed_b64())
	_publish(mf, seed)
	companion = drift / "net-tls.author-pubkey.b64"
	assert companion.is_file(), (
		f"`drift-author publish` did not write the pubkey companion at "
		f"{companion}; downstream `drift trust bootstrap` cannot work."
	)
	# File must contain a base64-decoded 32-byte pubkey on a single line.
	pub_b64 = companion.read_text(encoding="utf-8").strip()
	pub_raw = base64.b64decode(pub_b64.encode("ascii"))
	assert len(pub_raw) == 32, (
		f"pubkey companion must hold a 32-byte Ed25519 pubkey; got {len(pub_raw)} bytes"
	)


# ── bootstrap ──────────────────────────────────────────────────────


class TestBootstrap:
	def test_bootstrap_creates_v1_trust_store_with_grants(self, tmp_path: Path) -> None:
		"""Happy path: bootstrap reads the manifest + claim + pubkey
		companion, derives the kid, and grants `authors` for the
		claim's declared namespace in a fresh v1 trust store.
		"""
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		trust_path = drift / "trust.json"
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf,
			trust_store_path=trust_path,
		))
		ts = json.loads(trust_path.read_text(encoding="utf-8"))
		assert ts.get("format") == "drift-trust"
		assert ts.get("version") == 1
		assert "net_tls.*" in ts["namespaces"]
		ns = ts["namespaces"]["net_tls.*"]
		assert len(ns["authors"]) == 1
		kid = ns["authors"][0]
		assert kid in ts["keys"]
		assert ts["keys"][kid]["algo"] == "ed25519"

	def test_bootstrap_is_idempotent(self, tmp_path: Path) -> None:
		"""Running bootstrap twice must yield byte-identical output:
		grants are merged sorted-unique, not re-appended."""
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		trust_path = drift / "trust.json"
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf, trust_store_path=trust_path,
		))
		first = trust_path.read_text(encoding="utf-8")
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf, trust_store_path=trust_path,
		))
		second = trust_path.read_text(encoding="utf-8")
		assert first == second

	def test_bootstrap_refuses_reserved_namespace(self, tmp_path: Path) -> None:
		"""Project trust stores must not grant std.*/lang.*/drift.* —
		those resolve through core_trust_v1.json on the toolchain side.
		Bootstrap refuses unless --allow-reserved.
		"""
		drift, mf = _layout(tmp_path)
		# Force module_namespace to a reserved value so the author
		# claim's declared namespace becomes `std.*`.
		mf_obj = json.loads(mf.read_text(encoding="utf-8"))
		mf_obj["artifacts"][0]["module_namespace"] = "std"
		mf.write_text(json.dumps(mf_obj), encoding="utf-8")
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		with pytest.raises(ValueError, match="reserved"):
			bootstrap_trust_from_manifest(TrustBootstrapOptions(
				manifest_path=mf,
				trust_store_path=drift / "trust.json",
			))
		# With --allow-reserved (the stdlib release path), it goes through.
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf,
			trust_store_path=drift / "trust.json",
			allow_reserved=True,
		))

	def test_bootstrap_fails_without_pubkey_companion(self, tmp_path: Path) -> None:
		"""If the `<pkg>.author-pubkey.b64` companion is missing
		(e.g. claim minted by an older toolchain), bootstrap fails
		with a diagnostic pointing the operator at `drift-author publish`.
		"""
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		# Remove the companion.
		(drift / "net-tls.author-pubkey.b64").unlink()
		with pytest.raises(ValueError, match=r"author pubkey companion"):
			bootstrap_trust_from_manifest(TrustBootstrapOptions(
				manifest_path=mf,
				trust_store_path=drift / "trust.json",
			))

	def test_bootstrap_rejects_companion_kid_mismatch(self, tmp_path: Path) -> None:
		"""If someone hand-edits the pubkey companion to a different
		pubkey, the derived kid will not match the claim's signer.
		Bootstrap refuses rather than silently grant the wrong kid.
		"""
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		other_pub_b64 = base64.b64encode(bytes((b ^ 0xFF) for b in bytes(range(32)))).decode("ascii")
		(drift / "net-tls.author-pubkey.b64").write_text(other_pub_b64 + "\n", encoding="utf-8")
		with pytest.raises(ValueError, match=r"does not appear in the claim's signatures"):
			bootstrap_trust_from_manifest(TrustBootstrapOptions(
				manifest_path=mf,
				trust_store_path=drift / "trust.json",
			))


# ── check ──────────────────────────────────────────────────────────


class TestCheck:
	def test_check_does_not_require_pubkey_companion(self, tmp_path: Path) -> None:
		"""**Contract pin** (cert-team request): `drift trust check` is
		read-only and must work from the trust store + claim's signer
		kid alone.  The `<pkg>.author-pubkey.b64` companion is a
		bootstrap input, not a check input -- repos that already have
		a trust store from before the companion existed must still
		pass preflight without re-running `drift-author publish`.
		"""
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		# Bootstrap (uses the companion).
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		# Now delete the companion -- the world a pre-companion repo
		# would face.
		(drift / "net-tls.author-pubkey.b64").unlink()
		# `check` must still pass.
		report = check_trust_for_manifest(TrustCheckOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		assert report["ok"], (
			f"`drift trust check` must not require the pubkey companion; "
			f"errors: {report['errors']}"
		)

	def test_check_ok_after_bootstrap(self, tmp_path: Path) -> None:
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		report = check_trust_for_manifest(TrustCheckOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		assert report["ok"], f"errors: {report['errors']}"

	def test_check_flags_missing_trust_store(self, tmp_path: Path) -> None:
		"""The net-tls failure: valid claim, no trust.json.  Preflight
		must catch this before deploy, with a clear stable code.
		"""
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		report = check_trust_for_manifest(TrustCheckOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		assert report["ok"] is False
		assert any(e["code"] == "trust_store_missing" for e in report["errors"])

	def test_check_flags_missing_author_claim(self, tmp_path: Path) -> None:
		drift, mf = _layout(tmp_path)
		# Skip publish; the claim is missing.
		report = check_trust_for_manifest(TrustCheckOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		assert report["ok"] is False
		codes = {e["code"] for e in report["errors"]}
		assert "author_claim_missing" in codes

	def test_check_flags_stale_claim_after_version_bump(self, tmp_path: Path) -> None:
		"""The mariadb failure: manifest version bumped, author claim
		stale.  Preflight must catch this before deploy.
		"""
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		# Bump version in the manifest WITHOUT re-publishing.
		mf_obj = json.loads(mf.read_text(encoding="utf-8"))
		mf_obj["artifacts"][0]["version"] = "0.5.1"
		mf.write_text(json.dumps(mf_obj), encoding="utf-8")
		report = check_trust_for_manifest(TrustCheckOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		assert report["ok"] is False
		codes = {e["code"] for e in report["errors"]}
		# Both shapes should fire — version doesn't match, AND the SCI
		# computed from the new manifest version doesn't match the claim's
		# stamped SCI.
		assert "version_mismatch" in codes
		assert "sci_mismatch" in codes

	def test_check_flags_required_deps_mismatch(self, tmp_path: Path) -> None:
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		# Add a manifest dep that wasn't in the claim.
		mf_obj = json.loads(mf.read_text(encoding="utf-8"))
		mf_obj["artifacts"][0]["package_deps"].append(
			{"name": "drift.crypto", "version": "1"}
		)
		mf.write_text(json.dumps(mf_obj), encoding="utf-8")
		report = check_trust_for_manifest(TrustCheckOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		assert report["ok"] is False
		codes = {e["code"] for e in report["errors"]}
		# manifest version is unchanged, so the only mismatch should be
		# `required_deps_mismatch` (and sci_mismatch — adding a dep also
		# changes the SCI inputs).
		assert "required_deps_mismatch" in codes

	def test_check_rejects_legacy_sig_sidecars(self, tmp_path: Path) -> None:
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		# Drop a pre-v1 sidecar — must be rejected.
		(drift / "stale.sig").write_text("x", encoding="utf-8")
		report = check_trust_for_manifest(TrustCheckOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		assert report["ok"] is False
		assert any(e["code"] == "legacy_sig_present" for e in report["errors"])

	def test_check_rejects_legacy_source_attestation(self, tmp_path: Path) -> None:
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		(drift / "stale.source-attestation").write_text("x", encoding="utf-8")
		report = check_trust_for_manifest(TrustCheckOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		assert report["ok"] is False
		assert any(e["code"] == "legacy_attestation_present" for e in report["errors"])

	def test_check_optional_certifier_kid_pin(self, tmp_path: Path) -> None:
		"""--certifier-kid lets the operator preflight that the deploy
		signer is granted `certifiers` for the artifact namespace.
		Missing grant → certifier_not_trusted error code.
		"""
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		rand_pub = bytes(range(32, 64))
		rand_kid_b64 = base64.b64encode(rand_pub).decode("ascii")
		report = check_trust_for_manifest(TrustCheckOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
			certifier_kid=f"ed25519:{rand_kid_b64}",
		))
		assert report["ok"] is False
		assert any(e["code"] == "certifier_not_trusted" for e in report["errors"])

	def test_check_optional_certifier_kid_satisfied(self, tmp_path: Path) -> None:
		"""When the certifier kid IS granted, --certifier-kid validates."""
		from lang.drift.trust import (
			TrustAddKeyOptions, add_key_to_trust_store,
		)
		drift, mf = _layout(tmp_path)
		seed = _write_seed(tmp_path / "k.seed", _seed_b64())
		_publish(mf, seed)
		bootstrap_trust_from_manifest(TrustBootstrapOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
		))
		# Manually grant a certifier under net_tls.*.
		other_pub = bytes((b ^ 0xFF) for b in bytes(range(32)))
		other_b64 = base64.b64encode(other_pub).decode("ascii")
		from lang.drift.crypto import compute_ed25519_kid as _kid
		other_kid = _kid(other_pub)
		add_key_to_trust_store(TrustAddKeyOptions(
			trust_store_path=drift / "trust.json",
			namespace="net_tls.*",
			pubkey_b64=other_b64,
			kid=None,
			role="certifier",
		))
		report = check_trust_for_manifest(TrustCheckOptions(
			manifest_path=mf, trust_store_path=drift / "trust.json",
			certifier_kid=other_kid,
		))
		assert report["ok"], f"errors: {report['errors']}"


# ── unit-level guards ──────────────────────────────────────────────


def test_namespace_is_reserved_recognises_toolchain_namespaces() -> None:
	for ns in ("std", "lang", "drift", "std.*", "std.io", "lang.compiler",
			"drift.rpc", "drift.*"):
		assert _namespace_is_reserved(ns), f"expected {ns!r} to be reserved"
	for ns in ("net_tls.*", "acme.crypto.*", "myrepo.deep.*", "studio.*"):
		assert not _namespace_is_reserved(ns), f"expected {ns!r} NOT reserved"


def test_range_covers_v2_shapes() -> None:
	# "M" — any M.x.x
	assert _range_covers("0", "0.5.0") is True
	assert _range_covers("0", "0.99.123") is True
	assert _range_covers("1", "0.5.0") is False
	# "M.N" — any M.N.x
	assert _range_covers("0.5", "0.5.0") is True
	assert _range_covers("0.5", "0.5.99") is True
	assert _range_covers("0.5", "0.6.0") is False


# ── CLI wiring (in-process via cli.main) ───────────────────────────


def test_cli_trust_check_returns_nonzero_when_not_ready(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""`drift trust check` exits 1 when the repo is not trust-v1 ready,
	so CI/orch can gate on the return code without parsing JSON."""
	drift, mf = _layout(tmp_path)
	seed = _write_seed(tmp_path / "k.seed", _seed_b64())
	_publish(mf, seed)
	# No trust.json -> not ready.
	rc = drift_cli_main([
		"trust", "check",
		"--manifest", str(mf),
		"--trust-store", str(drift / "trust.json"),
	])
	assert rc == 1
	out = capsys.readouterr().out
	assert "trust_store_missing" in out
	assert "FAIL" in out


def test_cli_trust_check_returns_zero_when_ready(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	drift, mf = _layout(tmp_path)
	seed = _write_seed(tmp_path / "k.seed", _seed_b64())
	_publish(mf, seed)
	# Bootstrap then check.
	assert drift_cli_main([
		"trust", "bootstrap",
		"--manifest", str(mf),
		"--trust-store", str(drift / "trust.json"),
	]) == 0
	capsys.readouterr()  # drain bootstrap output
	rc = drift_cli_main([
		"trust", "check",
		"--manifest", str(mf),
		"--trust-store", str(drift / "trust.json"),
	])
	assert rc == 0
	out = capsys.readouterr().out
	assert "OK" in out


def test_cli_trust_bootstrap_dry_run_does_not_write(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	drift, mf = _layout(tmp_path)
	seed = _write_seed(tmp_path / "k.seed", _seed_b64())
	_publish(mf, seed)
	rc = drift_cli_main([
		"trust", "bootstrap",
		"--manifest", str(mf),
		"--trust-store", str(drift / "trust.json"),
		"--dry-run",
	])
	assert rc == 0
	assert not (drift / "trust.json").exists(), (
		"--dry-run must not write the trust store"
	)
	out = capsys.readouterr().out
	assert "dry-run" in out.lower()
