# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""CLI regression for `drift unpack`.

Builds a known-good *deployed package directory* whose `.dmp` carries
declared assets as content-addressed blobs (the producer shape `drift
deploy` now emits), signs the author/cert/provenance sidecars with a known
seed (reusing the same primitives as `test_trust_verify_package_cli.py`),
then exercises `drift unpack`:

  - happy path: verifies and materializes each asset to `<dest>/<path>`
    with byte-exact content;
  - fail-closed on a tampered asset blob (artifact_sha256 no longer
    matches the cert claim → verify fails → nothing written);
  - `--dest` must not already exist (usage error, exit 2, nothing written);
  - no trust source is a usage error, not a silent self-trust.

The whole point of the feature: assets travel INSIDE the verified
container and are only materialized through this verify-gated command.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path

import pytest
import zstandard

from lang.drift.cli import main
from lang.drift.crypto import (
	compute_ed25519_kid,
	ed25519_sign_from_seed,
)
from lang.driftc.packages.author_claim_v1 import (
	make_author_claim_body,
	AuthorClaimBody,
	dump_author_claim_json,
	make_author_claim,
)
from lang.driftc.packages.cert_claim_v1 import (
	make_cert_claim_body,
	CertClaimBody,
	CertSuite,
	Toolchain,
	cert_claim_filename,
	dump_cert_claim_json,
	make_cert_claim,
)
from lang.driftc.packages.dmir_pkg_v0 import (
	BLOB_TYPE_ASSET,
	canonical_json_bytes,
	sha256_hex,
	write_dmir_pkg_v0,
)
from lang.driftc.packages.zdmp import compress_to_zdmp

_PKG = "singular"
_MODULE = "singular.api"
_NS = "singular.*"
_VERSION = "0.5.0"
_SCI = "sha256:" + hashlib.sha256(b"singular-source-0.5.0").hexdigest()
_SEED = b"unpack-cli-solo-publisher_______"[:32]
_EMPTY_EVIDENCE = "sha256:" + hashlib.sha256(b"").hexdigest()

# Two declared assets under the author's `assets/...` prefix → the
# consumer materializes them at `<dest>/assets/singular/db/...`.
_ASSETS: dict[str, bytes] = {
	"assets/singular/db/0001_init.sql": b"CREATE TABLE t(id INT);\n",
	"assets/singular/db/0002_add.sql": b"ALTER TABLE t ADD COLUMN name TEXT;\n",
}


def _pubkey_raw(seed: bytes) -> bytes:
	_sig, pub = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return pub


def _emit_dmp_bytes(tmp_path: Path, *, assets: dict[str, bytes], tamper_asset: bool = False) -> bytes:
	"""Build a DMIR-PKG v0 container with asset blobs; return raw bytes.

	`tamper_asset=True` rewrites one asset's bytes WITHOUT updating its
	manifest/TOC sha — used to prove the container loader rejects it; for
	the verify-level tamper we instead change bytes and re-stamp (so the
	container loads but artifact_sha256 diverges from the cert claim)."""
	iface_obj = {"exports": {}}
	payload_obj = {"dmir": "stub"}
	iface_bytes = canonical_json_bytes(iface_obj)
	payload_bytes = canonical_json_bytes(payload_obj)
	iface_sha = sha256_hex(iface_bytes)
	payload_sha = sha256_hex(payload_bytes)

	blobs = {iface_sha: iface_bytes, payload_sha: payload_bytes}
	blob_types = {iface_sha: 2, payload_sha: 1}
	blob_names = {iface_sha: f"iface:{_MODULE}", payload_sha: f"dmir:{_MODULE}"}
	manifest_blobs = {
		f"sha256:{iface_sha}": {"type": "exports", "length": len(iface_bytes)},
		f"sha256:{payload_sha}": {"type": "dmir", "length": len(payload_bytes)},
	}
	manifest_assets = []
	for path, content in sorted(assets.items()):
		c = content + (b"TAMPERED" if tamper_asset else b"")
		sha = sha256_hex(c)
		blobs[sha] = c
		blob_types[sha] = BLOB_TYPE_ASSET
		blob_names[sha] = f"asset:{path}"
		manifest_blobs[f"sha256:{sha}"] = {"type": "asset", "length": len(c)}
		manifest_assets.append({"path": path, "blob": f"sha256:{sha}", "len": len(c)})

	raw_dir = tmp_path / "_raw"
	raw_dir.mkdir(parents=True, exist_ok=True)
	dmp_path = raw_dir / f"{_PKG}.dmp"
	write_dmir_pkg_v0(
		dmp_path,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
			"package_id": _PKG,
			"package_version": _VERSION,
			"source_content_id": _SCI,
			"target": "drift-linux-x86_64",
			"unsigned": True,
			"unstable_format": True,
			"payload_kind": "provisional-dmir",
			"payload_version": 0,
			"modules": [
				{
					"module_id": _MODULE,
					"exports": {},
					"interface_blob": f"sha256:{iface_sha}",
					"payload_blob": f"sha256:{payload_sha}",
				}
			],
			"blobs": manifest_blobs,
			"assets": manifest_assets,
		},
		blobs=blobs,
		blob_types=blob_types,
		blob_names=blob_names,
	)
	return dmp_path.read_bytes()


def _write_sidecars(deployed: Path, raw: bytes) -> None:
	artifact_sha = "sha256:" + hashlib.sha256(raw).hexdigest()
	(deployed / f"{_PKG}.zdmp").write_bytes(compress_to_zdmp(raw))
	# Author claim.
	abody = make_author_claim_body(
		package_id=_PKG, version=_VERSION, artifact_kind="package",
		namespaces=(_NS,), source_content_id=_SCI,
		required_deps=(), release_utc="2026-06-24T12:00:00Z",
	)
	(deployed / f"{_PKG}.author-claim").write_text(
		dump_author_claim_json(make_author_claim(abody, _SEED))
	)
	# Bundled pubkey (self-consistency trust source for the test).
	(deployed / f"{_PKG}.author-pubkey.b64").write_text(
		base64.b64encode(_pubkey_raw(_SEED)).decode("ascii")
	)
	# Provenance bundle (v4: binds artifact_sha256 + source_content_id).
	bundle = {
		"format": "drift-provenance-bundle", "version": 0,
		"provenance": {
			"schema_version": 4, "artifact_name": _PKG, "artifact_version": _VERSION,
			"artifact_kind": "package", "artifact_sha256": artifact_sha,
			"source_content_id": _SCI,
		},
		"dep_provenance": {}, "dep_keys": {},
	}
	compressed = zstandard.ZstdCompressor(level=3).compress(
		json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
	)
	(deployed / "provenance.zst").write_bytes(compressed)
	evidence_sha = "sha256:" + hashlib.sha256(compressed).hexdigest()
	# Cert claim (binds artifact_sha256 + evidence_sha256).
	kid = compute_ed25519_kid(_pubkey_raw(_SEED))
	cbody = make_cert_claim_body(
		package_id=_PKG, version=_VERSION,
		artifact_kind="package", artifact_path=f"{_PKG}.zdmp",
		artifact_sha256=artifact_sha, source_content_id=_SCI,
		target="drift-linux-x86_64",
		toolchain=Toolchain(driftc_version="0.33.55", drift_rt_abi=18, driftc_commit="test"),
		dep_graph=(),
		cert_suite=CertSuite(
			id="drift.foundation/default", version="1.0.0", result="pass",
			result_evidence_sha256="sha256:" + "e" * 64,
		),
		run_id="run-001", run_started_utc="2026-06-24T12:00:00Z",
		evidence_sha256=evidence_sha,
	)
	(deployed / cert_claim_filename(_PKG, kid)).write_text(
		dump_cert_claim_json(make_cert_claim(cbody, _SEED))
	)


def _build_deployed(tmp_path: Path, *, tamper_asset_signed: bool = False) -> Path:
	"""Materialize a signed deployed dir with asset blobs.

	`tamper_asset_signed=True` packs DIFFERENT asset bytes but signs the
	cert/provenance over the ORIGINAL artifact_sha → the container still
	loads but verify must reject (artifact hash mismatch), proving the
	verify gate guards the packed assets."""
	deployed = tmp_path / "lib" / _PKG / _VERSION
	deployed.mkdir(parents=True, exist_ok=True)
	good_raw = _emit_dmp_bytes(tmp_path, assets=_ASSETS)
	if tamper_asset_signed:
		# Sign over the good bytes, but write a tampered .zdmp afterward.
		_write_sidecars(deployed, good_raw)
		bad_raw = _emit_dmp_bytes(tmp_path, assets=_ASSETS, tamper_asset=True)
		(deployed / f"{_PKG}.zdmp").write_bytes(compress_to_zdmp(bad_raw))
	else:
		_write_sidecars(deployed, good_raw)
	return deployed


def _run(*argv: str) -> tuple[int, dict | None]:
	buf = io.StringIO()
	with contextlib.redirect_stdout(buf):
		try:
			rc = main(list(argv))
		except SystemExit as e:  # argparse usage → exit 2
			rc = int(e.code) if e.code is not None else 0
	out = buf.getvalue().strip()
	report = None
	for line in out.splitlines():
		line = line.strip()
		if line.startswith("{"):
			with contextlib.suppress(Exception):
				report = json.loads(line)
	return rc, report


def test_unpack_happy_path_materializes_assets(tmp_path: Path) -> None:
	deployed = _build_deployed(tmp_path)
	dest = tmp_path / "out"
	rc, report = _run("unpack", str(deployed), "--dest", str(dest), "--allow-bundled-pubkey", "--json")
	assert rc == 0, report
	assert report and report["ok"] and report["unpacked"]
	# Each declared asset materialized at <dest>/<path> with exact bytes.
	for path, content in _ASSETS.items():
		f = dest / path
		assert f.is_file(), f"missing {path}"
		assert f.read_bytes() == content
	assert sorted(report["assets"]) == sorted(_ASSETS)


def test_unpack_consumer_flow_layout(tmp_path: Path) -> None:
	"""The `mariachi --schema-template "$t/assets/singular/db"` directory exists."""
	deployed = _build_deployed(tmp_path)
	dest = tmp_path / "out"
	rc, _ = _run("unpack", str(deployed), "--dest", str(dest), "--allow-bundled-pubkey", "--json")
	assert rc == 0
	schema_dir = dest / "assets" / "singular" / "db"
	assert schema_dir.is_dir()
	assert {p.name for p in schema_dir.glob("*.sql")} == {"0001_init.sql", "0002_add.sql"}


def test_unpack_tampered_asset_fails_closed(tmp_path: Path) -> None:
	deployed = _build_deployed(tmp_path, tamper_asset_signed=True)
	dest = tmp_path / "out"
	rc, report = _run("unpack", str(deployed), "--dest", str(dest), "--allow-bundled-pubkey", "--json")
	assert rc == 1, report
	assert report and report["ok"] is False and report["unpacked"] is False
	# Fail-closed: NOTHING written.
	assert not dest.exists()


def test_unpack_dest_must_not_exist(tmp_path: Path) -> None:
	deployed = _build_deployed(tmp_path)
	dest = tmp_path / "out"
	dest.mkdir()
	(dest / "preexisting.txt").write_text("keep me")
	rc, _ = _run("unpack", str(deployed), "--dest", str(dest), "--allow-bundled-pubkey", "--json")
	assert rc == 2  # usage error
	# The pre-existing tree is untouched.
	assert (dest / "preexisting.txt").read_text() == "keep me"
	assert not (dest / "assets").exists()


def test_unpack_artifact_swapped_after_verify_fails_closed(tmp_path: Path, monkeypatch) -> None:
	"""TOCTOU: if the .zdmp is swapped between verify and extract, extraction
	recomputes the artifact hash and refuses to materialize unverified bytes."""
	from tools.drift_deploy import drift_unpack

	deployed = _build_deployed(tmp_path)
	dest = tmp_path / "out"

	# Verification reports OK for the ORIGINAL artifact_sha256...
	good_raw = _emit_dmp_bytes(tmp_path, assets=_ASSETS)
	good_sha = "sha256:" + hashlib.sha256(good_raw).hexdigest()

	def _fake_verify(opts):
		# Simulate a successful verify of the good bytes, THEN an attacker
		# swaps the on-disk .zdmp before extraction reads it.
		swapped = _emit_dmp_bytes(tmp_path, assets={"assets/evil.sql": b"DROP TABLE users;\n"})
		(opts.package_dir / f"{_PKG}.zdmp").write_bytes(compress_to_zdmp(swapped))
		return {
			"ok": True, "package_id": _PKG, "version": _VERSION,
			"artifact_sha256": good_sha, "trust_source": "test",
			"modules": [], "errors": [], "warnings": [], "provenance_ok": True,
		}

	monkeypatch.setattr(drift_unpack, "verify_deployed_package", _fake_verify)
	rc, report = _run("unpack", str(deployed), "--dest", str(dest), "--allow-bundled-pubkey", "--json")
	assert rc == 1, report
	assert report and report["unpacked"] is False
	assert not dest.exists()  # the swapped (unverified) bytes were NOT written


def test_unpack_corrupt_artifact_after_verify_clean_failure(tmp_path: Path, monkeypatch) -> None:
	"""A post-verify corrupt artifact must yield a clean failure (JSON + exit 1),
	not a traceback — extraction-side decompress/load errors are wrapped."""
	from tools.drift_deploy import drift_unpack

	deployed = _build_deployed(tmp_path)
	dest = tmp_path / "out"
	good_raw = _emit_dmp_bytes(tmp_path, assets=_ASSETS)
	good_sha = "sha256:" + hashlib.sha256(good_raw).hexdigest()

	def _fake_verify(opts):
		# Corrupt the .zdmp with non-zstd garbage after a "successful" verify.
		(opts.package_dir / f"{_PKG}.zdmp").write_bytes(b"not a zstd frame at all")
		return {
			"ok": True, "package_id": _PKG, "version": _VERSION,
			"artifact_sha256": good_sha, "trust_source": "test",
			"modules": [], "errors": [], "warnings": [], "provenance_ok": True,
		}

	monkeypatch.setattr(drift_unpack, "verify_deployed_package", _fake_verify)
	rc, report = _run("unpack", str(deployed), "--dest", str(dest), "--allow-bundled-pubkey", "--json")
	assert rc == 1, report  # clean failure, not a traceback
	assert report and report["unpacked"] is False and "error" in report
	assert not dest.exists()


def test_unpack_no_trust_source_is_usage_error(tmp_path: Path) -> None:
	deployed = _build_deployed(tmp_path)
	dest = tmp_path / "out"
	rc, _ = _run("unpack", str(deployed), "--dest", str(dest), "--json")
	assert rc == 2  # no silent self-trust
	assert not dest.exists()
