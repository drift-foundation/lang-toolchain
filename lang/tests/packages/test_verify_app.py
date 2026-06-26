# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Step 3: `verify_deployed_app` — the app verify adapter (verify only).

Builds a signed app deploy dir from primitives (binary + author/cert/provenance
sidecars, NO .zdmp), then exercises the three-leg app agreement: author == cert
== provenance artifact_kind == "app"; cert artifact_path names the binary;
sha256(binary) == cert == provenance; SCI agrees across all three (no two-way
fallback); provenance name/version match; signatures verify against the
namespace-derived trust subject.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
import zstandard

from lang.drift.crypto import compute_ed25519_kid, ed25519_sign_from_seed
from lang.driftc.packages.author_claim_v1 import (
	dump_author_claim_json, make_author_claim, make_author_claim_body,
)
from lang.driftc.packages.cert_claim_v1 import (
	CertSuite, Toolchain, cert_claim_filename, dump_cert_claim_json,
	make_cert_claim, make_cert_claim_body,
)
from lang.driftc.packages.verify_deployed_v1 import (
	VerifyPackageOptions, verify_deployed_app,
)

_APP = "uflowsd"
_NS = "microflows.*"
_VERSION = "0.2.0"
_SCI = "sha256:" + hashlib.sha256(b"uflowsd-source-0.2.0").hexdigest()
_SEED = b"verify-app-solo-publisher_______"[:32]
_BINARY = b"\x7fELF fake binary bytes for uflowsd\n"


def _pub(seed: bytes) -> bytes:
	_s, pub = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return pub


def _write_author(d: Path, *, artifact_kind: str = "app", namespaces=(_NS,), sci: str = _SCI) -> None:
	body = make_author_claim_body(
		package_id=_APP, version=_VERSION, artifact_kind=artifact_kind,
		namespaces=tuple(namespaces), source_content_id=sci,
		required_deps=(), release_utc="2026-06-25T00:00:00Z",
	)
	(d / f"{_APP}.author-claim").write_text(dump_author_claim_json(make_author_claim(body, _SEED)))


def _write_provenance(d: Path, *, binary_sha: str, sci: str = _SCI,
		extra: dict | None = None, omit_sci: bool = False) -> str:
	prov = {
		"schema_version": 4, "artifact_name": _APP, "artifact_version": _VERSION,
		"artifact_kind": "app", "artifact_sha256": binary_sha, "source_content_id": sci,
	}
	if omit_sci:
		prov["schema_version"] = 3
		del prov["source_content_id"]
	if extra:
		prov.update(extra)
	bundle = {"format": "drift-provenance-bundle", "version": 0,
		"provenance": prov, "dep_provenance": {}, "dep_keys": {}}
	comp = zstandard.ZstdCompressor(level=3).compress(
		json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8"))
	(d / "provenance.zst").write_bytes(comp)
	return "sha256:" + hashlib.sha256(comp).hexdigest()


def _write_cert(d: Path, *, binary_sha: str, evidence_sha: str, sci: str = _SCI,
		artifact_kind: str = "app", artifact_path: str = _APP) -> None:
	kid = compute_ed25519_kid(_pub(_SEED))
	body = make_cert_claim_body(
		package_id=_APP, version=_VERSION, artifact_kind=artifact_kind,
		artifact_path=artifact_path, artifact_sha256=binary_sha, source_content_id=sci,
		target="drift-linux-x86_64",
		toolchain=Toolchain(driftc_version="0.33.56", drift_rt_abi=18, driftc_commit="t"),
		dep_graph=(),
		cert_suite=CertSuite(id="drift.foundation/default", version="1.0.0",
			result="pass", result_evidence_sha256="sha256:" + "e" * 64),
		run_id="run-001", run_started_utc="2026-06-25T00:00:00Z", evidence_sha256=evidence_sha,
	)
	(d / cert_claim_filename(_APP, kid)).write_text(dump_cert_claim_json(make_cert_claim(body, _SEED)))


def _build_app_dir(tmp_path: Path, *,
		author_kind: str = "app", cert_kind: str = "app", cert_path: str = _APP,
		prov_extra: dict | None = None, prov_omit_sci: bool = False,
		binary: bytes = _BINARY, namespaces=(_NS,)) -> Path:
	d = tmp_path / "app" / _APP / _VERSION
	d.mkdir(parents=True, exist_ok=True)
	(d / _APP).write_bytes(binary)
	binary_sha = "sha256:" + hashlib.sha256(binary).hexdigest()
	(d / f"{_APP}.author-pubkey.b64").write_text(base64.b64encode(_pub(_SEED)).decode("ascii"))
	_write_author(d, artifact_kind=author_kind, namespaces=namespaces)
	evidence_sha = _write_provenance(d, binary_sha=binary_sha, extra=prov_extra, omit_sci=prov_omit_sci)
	_write_cert(d, binary_sha=binary_sha, evidence_sha=evidence_sha,
		artifact_kind=cert_kind, artifact_path=cert_path)
	return d


def _verify(d: Path, **kw):
	return verify_deployed_app(VerifyPackageOptions(package_dir=d, allow_bundled_pubkey=True, **kw))


# ── Positive ───────────────────────────────────────────────────────


def test_app_happy_path(tmp_path: Path) -> None:
	report = _verify(_build_app_dir(tmp_path))
	assert report["ok"] is True, report
	assert report["package_id"] == _APP and report["version"] == _VERSION
	assert report["artifact_kind"] == "app"
	assert report["provenance_ok"] is True


# ── Negatives: three-leg disagreement ──────────────────────────────


def test_author_kind_not_app_fails(tmp_path: Path) -> None:
	report = _verify(_build_app_dir(tmp_path, author_kind="package"))
	assert report["ok"] is False
	assert any(e["code"] == "artifact-kind-mismatch" for e in report["errors"]), report


def test_cert_kind_not_app_fails(tmp_path: Path) -> None:
	report = _verify(_build_app_dir(tmp_path, cert_kind="package"))
	assert report["ok"] is False
	assert any(e["code"] == "artifact-kind-mismatch" for e in report["errors"]), report


def test_cert_path_not_binary_fails(tmp_path: Path) -> None:
	# cert names a file that is not the deployed binary.
	report = _verify(_build_app_dir(tmp_path, cert_path="not-the-binary"))
	assert report["ok"] is False
	codes = {e["code"] for e in report["errors"]}
	assert "artifact-missing" in codes or "artifact-path-mismatch" in codes, report


def test_binary_tamper_fails(tmp_path: Path) -> None:
	d = _build_app_dir(tmp_path)
	(d / _APP).write_bytes(_BINARY + b"TAMPERED")  # hash no longer matches cert/provenance
	report = _verify(d)
	assert report["ok"] is False
	codes = {e["code"] for e in report["errors"]}
	assert "verify-failed" in codes or "artifact-sha-mismatch" in codes, report


def test_provenance_kind_mismatch_fails(tmp_path: Path) -> None:
	report = _verify(_build_app_dir(tmp_path, prov_extra={"artifact_kind": "package"}))
	assert report["ok"] is False
	assert any(e["code"] == "provenance-kind-mismatch" for e in report["errors"]), report


def test_provenance_sci_missing_fails_no_fallback(tmp_path: Path) -> None:
	report = _verify(_build_app_dir(tmp_path, prov_omit_sci=True))
	assert report["ok"] is False
	assert any(e["code"] == "provenance-sci-invalid" for e in report["errors"]), report


def test_provenance_name_mismatch_fails(tmp_path: Path) -> None:
	report = _verify(_build_app_dir(tmp_path, prov_extra={"artifact_name": "other-app"}))
	assert report["ok"] is False
	assert any(e["code"] == "provenance-name-mismatch" for e in report["errors"]), report


def test_provenance_schema_version_3_with_sci_fails(tmp_path: Path) -> None:
	"""v4 clean break: a schema_version 3 app bundle (even with SCI) is rejected."""
	report = _verify(_build_app_dir(tmp_path, prov_extra={"schema_version": 3}))
	assert report["ok"] is False
	assert any(e["code"] == "provenance-schema-version" for e in report["errors"]), report


def test_app_binary_symlink_rejected(tmp_path: Path) -> None:
	"""The verified locator must be a regular non-symlink file (so orchestration
	runs the exact bytes verified, not symlink-reached bytes)."""
	d = _build_app_dir(tmp_path)
	target = tmp_path / "real_uflowsd"
	target.write_bytes(_BINARY)
	(d / _APP).unlink()
	try:
		(d / _APP).symlink_to(target)
	except (OSError, NotImplementedError):
		pytest.skip("symlinks not supported on this platform")
	report = _verify(d)
	assert report["ok"] is False
	assert any(e["code"] == "artifact-symlink" for e in report["errors"]), report


# ── Negatives: trust / layout ──────────────────────────────────────


def test_untrusted_namespace_fails(tmp_path: Path) -> None:
	"""A trust store that does not grant the app's namespace fails verify."""
	d = _build_app_dir(tmp_path)
	trust = tmp_path / "trust.json"
	trust.write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {compute_ed25519_kid(_pub(_SEED)): {
			"algo": "ed25519", "pubkey": base64.b64encode(_pub(_SEED)).decode("ascii")}},
		"namespaces": {"other.*": {"authors": [compute_ed25519_kid(_pub(_SEED))],
			"certifiers": [compute_ed25519_kid(_pub(_SEED))]}},
		"revoked": [],
	}))
	report = verify_deployed_app(VerifyPackageOptions(package_dir=d, trust_store_path=trust))
	assert report["ok"] is False
	assert any(e["code"] == "verify-failed" for e in report["errors"]), report


def test_zdmp_present_is_usage_error(tmp_path: Path) -> None:
	from lang.driftc.packages.verify_deployed_v1 import VerifyPackageUsageError
	d = _build_app_dir(tmp_path)
	(d / "stray.zdmp").write_bytes(b"x")
	with pytest.raises(VerifyPackageUsageError, match="package directory"):
		_verify(d)


def test_no_trust_source_is_usage_error_before_author_io(tmp_path: Path) -> None:
	"""Invocation validation precedes content IO: no trust source is a usage
	error even when the author claim is missing/malformed (not a verify fail)."""
	from lang.driftc.packages.verify_deployed_v1 import VerifyPackageUsageError
	d = _build_app_dir(tmp_path)
	(d / f"{_APP}.author-claim").unlink()  # remove author claim
	with pytest.raises(VerifyPackageUsageError, match="no trust source"):
		verify_deployed_app(VerifyPackageOptions(package_dir=d))  # no trust flag


def test_verify_app_cli_happy_and_fail(tmp_path: Path) -> None:
	"""End-to-end through the `drift verify-app` CLI (read-only; never execs)."""
	import contextlib
	import io
	from lang.drift.cli import main

	def _cli(d: Path, *flags: str) -> tuple[int, dict | None]:
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			try:
				rc = main(["verify-app", str(d), "--json", *flags])
			except SystemExit as e:
				rc = int(e.code) if e.code is not None else 0
		report = None
		for line in buf.getvalue().splitlines():
			if line.strip().startswith("{"):
				with contextlib.suppress(Exception):
					report = json.loads(line)
		return rc, report

	rc, report = _cli(_build_app_dir(tmp_path), "--allow-bundled-pubkey")
	assert rc == 0 and report and report["ok"] is True

	rc, report = _cli(_build_app_dir(tmp_path / "bad", author_kind="package"), "--allow-bundled-pubkey")
	assert rc == 1 and report and report["ok"] is False

	# No trust source → usage error (exit 2), not a silent self-trust.
	rc, _ = _cli(_build_app_dir(tmp_path / "nt"))
	assert rc == 2


def test_v1_cert_rejected_cleanly(tmp_path: Path) -> None:
	d = _build_app_dir(tmp_path)
	cert = sorted(d.glob("*.cert-claim.*.json"))[0]
	body = json.loads(cert.read_text())
	body["body"]["schema_version"] = 1
	body["body"].pop("artifact_kind", None)
	body["body"].pop("artifact_path", None)
	cert.write_text(json.dumps(body))
	report = _verify(d)
	assert report["ok"] is False
	assert any(e["code"] == "malformed-sidecar" for e in report["errors"]), report
