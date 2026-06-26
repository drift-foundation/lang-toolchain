# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""CLI regression self-check for `drift trust verify-package`.

Builds a known-good *deployed package directory* from primitives (a real
`.zdmp` + signed author/cert sidecars + author-pubkey companion + a
provenance bundle whose bytes are pinned by the cert claim's signed
`evidence_sha256`), asserts the verb accepts it, then applies one
targeted on-disk mutation per test and asserts the verb fails on the
expected gate.  This is the guard that proves the CLI actually validates
the package/sidecar/provenance set rather than merely parsing it.

Mutations covered:
  - `.zdmp` byte tamper (artifact bytes no longer match the claims)
  - cert-claim `artifact_sha256` tamper
  - author/cert `source_content_id` mismatch
  - wrong `--expect-version`
  - provenance content tamper that PRESERVES the inner artifact_sha256
    field (caught only by the signed evidence_sha256 byte-binding)
  - reserved-namespace module routed to core trust (a bundled /
    synthetic key cannot bless a `lang.*` package)
  - no trust source supplied (usage error, not a silent self-trust)
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path

import zstandard

from lang.drift.cli import main
from lang.drift.crypto import compute_ed25519_kid, ed25519_sign_from_seed
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
	canonical_json_bytes,
	sha256_hex,
	write_dmir_pkg_v0,
)
from lang.driftc.packages.zdmp import compress_to_zdmp


_PKG = "acme.widget"
_MODULE = "acme.widget.api"
_NS = "acme.widget.*"
_VERSION = "0.5.2"
_SCI = "sha256:" + hashlib.sha256(b"acme.widget-source-0.5.2").hexdigest()
_SEED = b"verify-package-solo-publisher___"[:32]
_EMPTY_EVIDENCE = "sha256:" + hashlib.sha256(b"").hexdigest()


def _pubkey_raw(seed: bytes) -> bytes:
	_sig, pub = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return pub


def _emit_dmp_bytes(tmp_path: Path, *, payload_marker: str, pkg_id: str, module_id: str) -> bytes:
	"""Build a minimal-but-valid DMIR-PKG v0 container; return raw bytes.
	`payload_marker` produces content-distinct bytes (different
	artifact_sha256) while keeping pkg_id/version/sci stable."""
	iface_obj = {"exports": {}, "marker": payload_marker}
	payload_obj = {"dmir": "stub", "marker": payload_marker}
	iface_bytes = canonical_json_bytes(iface_obj)
	payload_bytes = canonical_json_bytes(payload_obj)
	iface_sha = sha256_hex(iface_bytes)
	payload_sha = sha256_hex(payload_bytes)
	raw_dir = tmp_path / "_raw"
	raw_dir.mkdir(parents=True, exist_ok=True)
	dmp_path = raw_dir / f"{pkg_id}.dmp"
	write_dmir_pkg_v0(
		dmp_path,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
			"package_id": pkg_id,
			"package_version": _VERSION,
			"source_content_id": _SCI,
			"target": "drift-linux-x86_64",
			"unsigned": True,
			"unstable_format": True,
			"payload_kind": "provisional-dmir",
			"payload_version": 0,
			"modules": [
				{
					"module_id": module_id,
					"exports": {},
					"interface_blob": f"sha256:{iface_sha}",
					"payload_blob": f"sha256:{payload_sha}",
				}
			],
			"blobs": {
				f"sha256:{iface_sha}": {"type": "exports", "length": len(iface_bytes)},
				f"sha256:{payload_sha}": {"type": "dmir", "length": len(payload_bytes)},
			},
		},
		blobs={iface_sha: iface_bytes, payload_sha: payload_bytes},
		blob_types={iface_sha: 2, payload_sha: 1},
		blob_names={iface_sha: f"iface:{module_id}", payload_sha: f"dmir:{module_id}"},
	)
	return dmp_path.read_bytes()


def _write_zdmp(deployed: Path, raw_bytes: bytes, *, pkg_id: str) -> str:
	(deployed / f"{pkg_id}.zdmp").write_bytes(compress_to_zdmp(raw_bytes))
	return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


def _write_author_sidecar(deployed: Path, *, pkg_id: str, namespace: str, sci: str = _SCI,
		artifact_kind: str = "package") -> None:
	body = make_author_claim_body(
		package_id=pkg_id, version=_VERSION, artifact_kind=artifact_kind,
		namespaces=(namespace,), source_content_id=sci,
		required_deps=(), release_utc="2026-05-29T12:00:00Z",
	)
	(deployed / f"{pkg_id}.author-claim").write_text(
		dump_author_claim_json(make_author_claim(body, _SEED))
	)


def _write_cert_sidecar(
	deployed: Path, *, pkg_id: str, artifact_sha: str, evidence_sha: str,
	sci: str = _SCI, no_evidence: bool = False,
	artifact_kind: str = "package", artifact_path: str | None = None,
) -> None:
	kid = compute_ed25519_kid(_pubkey_raw(_SEED))
	body = make_cert_claim_body(
		package_id=pkg_id, version=_VERSION,
		artifact_kind=artifact_kind,
		artifact_path=artifact_path if artifact_path is not None else f"{pkg_id}.zdmp",
		artifact_sha256=artifact_sha, source_content_id=sci,
		target="drift-linux-x86_64",
		toolchain=Toolchain(driftc_version="0.33.9", drift_rt_abi=14, driftc_commit="test"),
		dep_graph=(),
		cert_suite=CertSuite(
			id="drift.foundation/default", version="1.0.0", result="pass",
			# The dev/no-evidence sentinel lives on the SUITE digest; the
			# provenance binding (`evidence_sha256`) is independent.
			result_evidence_sha256=(_EMPTY_EVIDENCE if no_evidence else "sha256:" + "e" * 64),
		),
		run_id="run-001", run_started_utc="2026-05-29T12:00:00Z",
		evidence_sha256=evidence_sha,  # pins the on-disk provenance.zst bytes
	)
	fn = cert_claim_filename(pkg_id, kid)
	(deployed / fn).write_text(dump_cert_claim_json(make_cert_claim(body, _SEED)))


def _write_pubkey(deployed: Path, *, pkg_id: str) -> None:
	pub_b64 = base64.b64encode(_pubkey_raw(_SEED)).decode("ascii")
	(deployed / f"{pkg_id}.author-pubkey.b64").write_text(pub_b64)


def _write_provenance(deployed: Path, *, pkg_id: str, artifact_sha: str, sci: str = _SCI,
		extra: dict | None = None, omit_sci: bool = False) -> str:
	"""Write `provenance.zst`; return its evidence digest
	("sha256:<hex>" of the compressed bytes), i.e. what the cert claim
	must sign. `extra` injects/overrides inner fields; `omit_sci` drops
	source_content_id (simulates a legacy v3 bundle)."""
	prov = {
		"schema_version": 4,
		"artifact_name": pkg_id,
		"artifact_version": _VERSION,
		"artifact_kind": "package",
		"artifact_sha256": artifact_sha,
		"source_content_id": sci,
	}
	if omit_sci:
		prov["schema_version"] = 3
		del prov["source_content_id"]
	if extra:
		prov.update(extra)
	bundle = {
		"format": "drift-provenance-bundle",
		"version": 0,
		"provenance": prov,
		"dep_provenance": {},
		"dep_keys": {},
	}
	raw = json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
	compressed = zstandard.ZstdCompressor(level=3).compress(raw)
	(deployed / "provenance.zst").write_bytes(compressed)
	return "sha256:" + hashlib.sha256(compressed).hexdigest()


def _build_good_dir(
	tmp_path: Path, *, no_evidence: bool = False,
	pkg_id: str = _PKG, module_id: str = _MODULE, namespace: str = _NS,
	prov_extra: dict | None = None, prov_omit_sci: bool = False,
	cert_kind: str = "package", cert_path: str | None = None,
) -> tuple[Path, str, str]:
	"""Materialize a known-good deployed package directory.
	Returns (deployed_dir, artifact_sha256, evidence_sha256).
	The `prov_*`/`cert_*` knobs inject ONE v2/v4 cross-check mismatch while
	keeping signatures + the provenance evidence-binding valid (the cert
	signs sha256(<the mutated bundle bytes>)), so the cross-check — not a
	signature/evidence failure — is what the negative tests exercise."""
	deployed = tmp_path / "lib" / pkg_id / _VERSION
	deployed.mkdir(parents=True, exist_ok=True)
	raw = _emit_dmp_bytes(tmp_path, payload_marker="good", pkg_id=pkg_id, module_id=module_id)
	artifact_sha = _write_zdmp(deployed, raw, pkg_id=pkg_id)
	_write_author_sidecar(deployed, pkg_id=pkg_id, namespace=namespace)
	_write_pubkey(deployed, pkg_id=pkg_id)
	# Provenance first; the cert signs sha256(provenance.zst bytes).
	evidence_sha = _write_provenance(
		deployed, pkg_id=pkg_id, artifact_sha=artifact_sha,
		extra=prov_extra, omit_sci=prov_omit_sci,
	)
	_write_cert_sidecar(
		deployed, pkg_id=pkg_id, artifact_sha=artifact_sha,
		evidence_sha=evidence_sha, no_evidence=no_evidence,
		artifact_kind=cert_kind, artifact_path=cert_path,
	)
	return deployed, artifact_sha, evidence_sha


def _run(deployed: Path, *flags: str) -> tuple[int, dict | None]:
	"""Invoke `drift trust verify-package <dir> --json [flags...]`.
	Returns (exit_code, parsed_report) — report is None for a usage
	error (argparse `p.error` raises SystemExit before printing JSON)."""
	buf = io.StringIO()
	try:
		with contextlib.redirect_stdout(buf):
			rc = main(["trust", "verify-package", str(deployed), "--json", *flags])
	except SystemExit as ex:
		return int(ex.code if ex.code is not None else 0), None
	return rc, json.loads(buf.getvalue())


# ── Happy paths + trust forms ───────────────────────────────────────


def test_verify_package_happy_path_bundled_pubkey(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path)
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 0, report
	assert report["ok"] is True
	assert report["package_id"] == _PKG
	assert report["version"] == _VERSION
	assert report["source_content_id"] == _SCI
	assert report["mode"] == "certifier-shortcut"
	assert report["provenance_ok"] is True
	assert report["no_evidence_sentinel"] is False
	assert report["trust_source"].startswith("bundled-pubkey:")
	assert [m["module_id"] for m in report["modules"]] == [_MODULE]


def test_verify_package_explicit_author_pubkey(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path)
	pub_b64 = (deployed / f"{_PKG}.author-pubkey.b64").read_text().strip()
	rc, report = _run(deployed, "--author-pubkey-b64", pub_b64)
	assert rc == 0, report
	assert report["ok"] is True
	assert report["trust_source"] == "author-pubkey-b64"


def test_json_report_shape(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path)
	_rc, report = _run(deployed, "--allow-bundled-pubkey")
	for key in (
		"ok", "package_dir", "package_id", "version", "source_content_id",
		"artifact_sha256", "trust_source", "mode", "author_kid",
		"certifier_kid", "certifier_kids", "provenance_ok",
		"no_evidence_sentinel", "modules", "warnings", "errors",
	):
		assert key in report, f"missing key {key!r} in JSON report"


# ── Finding 3: no trust source is a usage error, not silent self-trust ──


def test_no_trust_source_is_usage_error(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path)
	rc, report = _run(deployed)  # no trust flag at all
	assert rc == 2
	assert report is None


# ── Mutations: each must flip ok→False on its specific gate ──────────


def test_zdmp_byte_tamper_fails(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path)
	tampered = _emit_dmp_bytes(tmp_path, payload_marker="TAMPERED", pkg_id=_PKG, module_id=_MODULE)
	_write_zdmp(deployed, tampered, pkg_id=_PKG)  # content-distinct artifact
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1
	assert report["ok"] is False
	codes = {e["code"] for e in report["errors"]}
	assert "verify-failed" in codes or "provenance-artifact-mismatch" in codes


def test_cert_artifact_sha_tamper_fails(tmp_path: Path) -> None:
	deployed, _a, evidence_sha = _build_good_dir(tmp_path)
	# Re-sign cert with a wrong artifact_sha but the correct evidence pin.
	_write_cert_sidecar(deployed, pkg_id=_PKG, artifact_sha="sha256:" + "9" * 64, evidence_sha=evidence_sha)
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1
	assert report["ok"] is False
	assert any(e["code"] == "verify-failed" for e in report["errors"]), report
	assert any("artifact_sha256" in (m["reason"] or "") for m in report["modules"])


def test_author_cert_sci_mismatch_fails(tmp_path: Path) -> None:
	deployed, good_sha, evidence_sha = _build_good_dir(tmp_path)
	_write_cert_sidecar(
		deployed, pkg_id=_PKG, artifact_sha=good_sha, evidence_sha=evidence_sha,
		sci="sha256:" + "b" * 64,  # disagrees with author/manifest SCI
	)
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1
	assert report["ok"] is False
	assert any(e["code"] == "verify-failed" for e in report["errors"]), report


def test_wrong_expect_version_fails(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path)
	rc, report = _run(deployed, "--allow-bundled-pubkey", "--expect-version", "9.9.9")
	assert rc == 1
	assert report["ok"] is False
	assert any(e["code"] == "expect-version" for e in report["errors"]), report


def test_provenance_content_tamper_preserving_artifact_sha_fails(tmp_path: Path) -> None:
	"""Finding 1: a hostile mirror swaps the provenance bundle for
	attacker-chosen contents that keep the same inner `artifact_sha256`.
	The signed `evidence_sha256` byte-binding must catch it even though
	the inner-field check would pass."""
	deployed, artifact_sha, _e = _build_good_dir(tmp_path)
	# Rewrite provenance with extra content -> different bytes, SAME inner
	# artifact_sha256.  Returns a new evidence digest we deliberately
	# discard: the cert still pins the ORIGINAL bytes.
	_write_provenance(deployed, pkg_id=_PKG, artifact_sha=artifact_sha, extra={"injected": "by-mirror"})
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1
	assert report["ok"] is False
	assert report["provenance_ok"] is False
	codes = {e["code"] for e in report["errors"]}
	assert "provenance-evidence-mismatch" in codes, report
	# Confirm the inner field is unchanged, i.e. the OLD check would NOT
	# have caught this — the byte-binding is what does.
	assert "provenance-artifact-mismatch" not in codes


# ── Step 2: v2/v4 package cross-check regressions ──────────────────


def test_author_artifact_kind_mismatch_fails(tmp_path: Path) -> None:
	"""A validly-signed author claim with artifact_kind='app' must be caught
	by the author-kind cross-check in verify-package (regression for the
	dead-check bug where discover_author_claim_path was mis-called)."""
	deployed, _a, _e = _build_good_dir(tmp_path)
	_write_author_sidecar(deployed, pkg_id=_PKG, namespace=_NS, artifact_kind="app")
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1 and report["ok"] is False
	assert any(e["code"] == "artifact-kind-mismatch" for e in report["errors"]), report


def test_cert_artifact_kind_mismatch_fails(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path, cert_kind="app")
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1 and report["ok"] is False
	assert any(e["code"] == "artifact-kind-mismatch" for e in report["errors"]), report


def test_cert_artifact_path_mismatch_fails(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path, cert_path="wrong-name.zdmp")
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1 and report["ok"] is False
	assert any(e["code"] == "artifact-path-mismatch" for e in report["errors"]), report


def test_provenance_kind_mismatch_fails(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path, prov_extra={"artifact_kind": "app"})
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1 and report["ok"] is False
	assert any(e["code"] == "provenance-kind-mismatch" for e in report["errors"]), report


def test_provenance_sci_mismatch_fails(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(
		tmp_path, prov_extra={"source_content_id": "sha256:" + "9" * 64})
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1 and report["ok"] is False
	assert any(e["code"] == "provenance-sci-mismatch" for e in report["errors"]), report


def test_provenance_sci_missing_fails_no_fallback(tmp_path: Path) -> None:
	"""No two-way fallback: a legacy v3 bundle (no source_content_id) is a
	HARD verify failure, not a silent skip."""
	deployed, _a, _e = _build_good_dir(tmp_path, prov_omit_sci=True)
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1 and report["ok"] is False
	assert any(e["code"] == "provenance-sci-invalid" for e in report["errors"]), report


def test_provenance_name_mismatch_fails(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path, prov_extra={"artifact_name": "not-the-pkg"})
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1 and report["ok"] is False
	assert any(e["code"] == "provenance-name-mismatch" for e in report["errors"]), report


def test_provenance_version_mismatch_fails(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path, prov_extra={"artifact_version": "9.9.9"})
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1 and report["ok"] is False
	assert any(e["code"] == "provenance-version-mismatch" for e in report["errors"]), report


def test_v1_cert_claim_rejected_cleanly(tmp_path: Path) -> None:
	"""An old v1 cert claim (body schema_version 1, no artifact_kind/path) is
	rejected at load — clean ok=false, not a crash."""
	deployed, _a, _e = _build_good_dir(tmp_path)
	cert_files = sorted(deployed.glob("*.cert-claim.*.json"))
	assert cert_files
	v1 = json.loads(cert_files[0].read_text())
	v1["body"]["schema_version"] = 1
	v1["body"].pop("artifact_kind", None)
	v1["body"].pop("artifact_path", None)
	cert_files[0].write_text(json.dumps(v1))
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1 and report["ok"] is False
	assert any(e["code"] == "malformed-sidecar" for e in report["errors"]), report


def test_reserved_namespace_module_routes_to_core_trust(tmp_path: Path) -> None:
	"""Finding 2: a `lang.*` module must verify against the core trust
	store, so a bundled / synthetic non-Foundation key cannot bless it."""
	deployed, _a, _e = _build_good_dir(
		tmp_path, pkg_id="acme.reserved-probe",
		module_id="lang.fake", namespace="lang.*",
	)
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1
	assert report["ok"] is False
	mod = next(m for m in report["modules"] if m["module_id"] == "lang.fake")
	assert mod["reserved"] is True
	assert mod["ok"] is False
	assert any(e["code"] == "verify-failed" for e in report["errors"]), report


# ── Multi-module: provenance binding must hold for EVERY accepted cert ──


def _emit_multi_module_dmp(tmp_path: Path, *, pkg_id: str, modules: list[str]) -> bytes:
	blobs: dict[str, bytes] = {}
	blob_types: dict[str, int] = {}
	blob_names: dict[str, str] = {}
	mod_entries = []
	blobs_manifest: dict[str, dict] = {}
	for m in modules:
		ib = canonical_json_bytes({"exports": {}, "m": m})
		pb = canonical_json_bytes({"dmir": "stub", "m": m})
		ish = sha256_hex(ib)
		psh = sha256_hex(pb)
		blobs[ish] = ib
		blobs[psh] = pb
		blob_types[ish] = 2
		blob_types[psh] = 1
		blob_names[ish] = f"iface:{m}"
		blob_names[psh] = f"dmir:{m}"
		mod_entries.append({
			"module_id": m, "exports": {},
			"interface_blob": f"sha256:{ish}", "payload_blob": f"sha256:{psh}",
		})
		blobs_manifest[f"sha256:{ish}"] = {"type": "exports", "length": len(ib)}
		blobs_manifest[f"sha256:{psh}"] = {"type": "dmir", "length": len(pb)}
	raw_dir = tmp_path / "_rawmm"
	raw_dir.mkdir(parents=True, exist_ok=True)
	dmp = raw_dir / f"{pkg_id}.dmp"
	write_dmir_pkg_v0(
		dmp,
		manifest_obj={
			"format": "dmir-pkg", "format_version": 0,
			"package_id": pkg_id, "package_version": _VERSION,
			"source_content_id": _SCI, "target": "drift-linux-x86_64",
			"unsigned": True, "unstable_format": True,
			"payload_kind": "provisional-dmir", "payload_version": 0,
			"modules": mod_entries, "blobs": blobs_manifest,
		},
		blobs=blobs, blob_types=blob_types, blob_names=blob_names,
	)
	return dmp.read_bytes()


def _cert_claim_text(*, pkg_id: str, seed: bytes, artifact_sha: str, evidence_sha: str) -> tuple[str, str]:
	"""Return (filename, json_text) for a cert claim signed by `seed`."""
	kid = compute_ed25519_kid(_pubkey_raw(seed))
	body = make_cert_claim_body(
		package_id=pkg_id, version=_VERSION,
		artifact_kind="package", artifact_path=f"{pkg_id}.zdmp",
		artifact_sha256=artifact_sha, source_content_id=_SCI,
		target="drift-linux-x86_64",
		toolchain=Toolchain(driftc_version="0.33.9", drift_rt_abi=14, driftc_commit="t"),
		dep_graph=(),
		cert_suite=CertSuite(id="drift.foundation/default", version="1.0.0",
			result="pass", result_evidence_sha256="sha256:" + "e" * 64),
		run_id="r", run_started_utc="2026-05-29T12:00:00Z",
		evidence_sha256=evidence_sha,
	)
	return cert_claim_filename(pkg_id, kid), dump_cert_claim_json(make_cert_claim(body, seed))


def test_provenance_binding_holds_for_every_accepted_cert(tmp_path: Path) -> None:
	"""A multi-module package can verify different modules through
	different cert claims.  The provenance byte-binding must be checked
	against EVERY accepted cert, not just the last module's.  Here the
	module with a WRONG-evidence cert is NOT last, so the old 'last kid
	only' logic would pass; the fix must fail."""
	pkg = "acme.multi"
	a_seed = b"multi-author____________________"[:32]
	c1_seed = b"multi-cert-one__________________"[:32]   # good evidence, LAST module
	c2_seed = b"multi-cert-two__________________"[:32]   # WRONG evidence, FIRST module
	a_kid = compute_ed25519_kid(_pubkey_raw(a_seed))
	c1_kid = compute_ed25519_kid(_pubkey_raw(c1_seed))
	c2_kid = compute_ed25519_kid(_pubkey_raw(c2_seed))

	deployed = tmp_path / "lib" / pkg / _VERSION
	deployed.mkdir(parents=True, exist_ok=True)
	# Module order: beta.api FIRST (bad cert), alpha.api LAST (good cert).
	raw = _emit_multi_module_dmp(tmp_path, pkg_id=pkg, modules=["beta.api", "alpha.api"])
	artifact_sha = _write_zdmp(deployed, raw, pkg_id=pkg)
	evidence_sha = _write_provenance(deployed, pkg_id=pkg, artifact_sha=artifact_sha)

	# Author claim covers both namespaces, signed by A.
	abody = make_author_claim_body(
		package_id=pkg, version=_VERSION, artifact_kind="package",
		namespaces=("alpha.*", "beta.*"), source_content_id=_SCI,
		required_deps=(), release_utc="2026-05-29T12:00:00Z",
	)
	(deployed / f"{pkg}.author-claim").write_text(
		dump_author_claim_json(make_author_claim(abody, a_seed))
	)
	# cc1 (C1, alpha) pins the REAL provenance; cc2 (C2, beta) pins a wrong digest.
	fn1, txt1 = _cert_claim_text(pkg_id=pkg, seed=c1_seed, artifact_sha=artifact_sha, evidence_sha=evidence_sha)
	fn2, txt2 = _cert_claim_text(pkg_id=pkg, seed=c2_seed, artifact_sha=artifact_sha, evidence_sha="sha256:" + "7" * 64)
	(deployed / fn1).write_text(txt1)
	(deployed / fn2).write_text(txt2)

	# Real trust store: A authors both; C1 certifies alpha.*, C2 certifies beta.*.
	def _k(seed: bytes) -> dict:
		return {"algo": "ed25519", "pubkey": base64.b64encode(_pubkey_raw(seed)).decode("ascii")}
	trust = {
		"format": "drift-trust", "version": 1,
		"keys": {a_kid: _k(a_seed), c1_kid: _k(c1_seed), c2_kid: _k(c2_seed)},
		"namespaces": {
			"alpha.*": {"authors": [a_kid], "certifiers": [c1_kid]},
			"beta.*": {"authors": [a_kid], "certifiers": [c2_kid]},
		},
		"revoked": [],
	}
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps(trust))

	rc, report = _run(deployed, "--trust-store", str(trust_path))
	assert rc == 1, report
	assert report["ok"] is False
	# Both modules verified (so both certs were "accepted"); the failure is
	# purely the provenance binding on the NON-last (beta/C2) cert.
	assert all(m["ok"] for m in report["modules"]), report
	assert set(report["certifier_kids"]) == {c1_kid, c2_kid}, report
	ev = [e for e in report["errors"] if e["code"] == "provenance-evidence-mismatch"]
	assert ev and any(e.get("certifier_kid") == c2_kid for e in ev), report


# ── Sentinel surfacing ──────────────────────────────────────────────


def test_no_evidence_sentinel_surfaced(tmp_path: Path) -> None:
	deployed, _a, _e = _build_good_dir(tmp_path, no_evidence=True)
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 0, report
	assert report["ok"] is True
	assert report["no_evidence_sentinel"] is True
	assert any("no-evidence sentinel" in w for w in report["warnings"]), report


# ── Exit-code contract: invalid PACKAGE contents are verification ───
#    outcomes (exit 1 + ok=false JSON), NOT argparse usage errors
#    (exit 2).  Only command-invocation problems are exit 2.


def test_malformed_artifact_is_verification_failure_not_usage(tmp_path: Path) -> None:
	"""A corrupt `.zdmp` (not even valid zstd) is a property of the
	package, not the invocation: it must produce an ok=false JSON report
	with exit 1, never a bare argparse exit-2 with no body."""
	deployed, _a, _e = _build_good_dir(tmp_path)
	(deployed / f"{_PKG}.zdmp").write_bytes(b"this is not a zstd frame at all")
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1
	assert report is not None, "exit 1 must still emit a JSON report"
	assert report["ok"] is False
	assert any(e["code"] == "malformed-artifact" for e in report["errors"]), report


def test_malformed_author_sidecar_is_verification_failure(tmp_path: Path) -> None:
	"""A present-but-corrupt sidecar makes the strict-v1 loader raise; the
	facade must fold that into an ok=false report (exit 1), not let it
	escape as an argparse usage error."""
	deployed, _a, _e = _build_good_dir(tmp_path)
	(deployed / f"{_PKG}.author-claim").write_text("{ this is not valid json")
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1
	assert report is not None
	assert report["ok"] is False
	assert any(e["code"] == "malformed-sidecar" for e in report["errors"]), report


def test_missing_author_sidecar_is_verification_failure(tmp_path: Path) -> None:
	"""A missing required sidecar is a rejected verification (exit 1), not
	misuse: the verifier reports it as a module failure."""
	deployed, _a, _e = _build_good_dir(tmp_path)
	(deployed / f"{_PKG}.author-claim").unlink()
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1
	assert report is not None
	assert report["ok"] is False
	assert any(e["code"] == "verify-failed" for e in report["errors"]), report


def test_missing_bundled_pubkey_is_verification_failure(tmp_path: Path) -> None:
	"""Under --allow-bundled-pubkey the bundled key is part of the package;
	its absence is package incompleteness (exit 1 + ok=false), not a usage
	error.  (Contrast `test_no_trust_source_is_usage_error`, which IS exit
	2 because no trust source was requested at all.)"""
	deployed, _a, _e = _build_good_dir(tmp_path)
	(deployed / f"{_PKG}.author-pubkey.b64").unlink()
	rc, report = _run(deployed, "--allow-bundled-pubkey")
	assert rc == 1
	assert report is not None
	assert report["ok"] is False
	assert any(e["code"] == "bundled-pubkey-missing" for e in report["errors"]), report


def test_no_trust_source_is_usage_error_even_for_corrupt_package(tmp_path: Path) -> None:
	"""Trust-source presence is an invocation property, validated BEFORE
	reading the artifact: a corrupt package with no trust flag must still
	be a deterministic usage error (exit 2), not a malformed-artifact
	verification failure (exit 1)."""
	deployed, _a, _e = _build_good_dir(tmp_path)
	(deployed / f"{_PKG}.zdmp").write_bytes(b"not a zstd frame")
	rc, report = _run(deployed)  # no trust flag at all
	assert rc == 2
	assert report is None


def test_backstop_error_report_has_full_schema(tmp_path: Path) -> None:
	"""The CLI's unexpected-error backstop must emit the SAME full report
	schema as a normal verification failure, so CI consumers see one stable
	set of keys.  `error_report` is the shared builder behind it."""
	from lang.driftc.packages.verify_deployed_v1 import error_report, new_report
	rep = error_report(tmp_path, code="verify-error", message="boom")
	for key in (
		"ok", "package_dir", "package_id", "version", "source_content_id",
		"artifact_sha256", "trust_source", "mode", "author_kid",
		"certifier_kid", "certifier_kids", "provenance_ok",
		"no_evidence_sentinel", "modules", "warnings", "errors",
	):
		assert key in rep, f"backstop report missing key {key!r}"
	assert rep["ok"] is False
	assert rep["errors"] == [{"code": "verify-error", "message": "boom"}]
	# Same key set as a normal (empty) report.
	assert set(rep) == set(new_report(tmp_path))


def test_facade_rejects_multiple_trust_sources(tmp_path: Path) -> None:
	"""The facade is the sanctioned integration surface, so it enforces
	'exactly one trust source' itself — not relying on the CLI's argparse
	mutually-exclusive group.  A direct API caller combining a bundled key
	with an explicit source is a usage error, not a silent preference."""
	from lang.driftc.packages.verify_deployed_v1 import (
		VerifyPackageOptions, VerifyPackageUsageError, verify_deployed_package,
	)
	deployed, _a, _e = _build_good_dir(tmp_path)
	opts = VerifyPackageOptions(
		package_dir=deployed,
		trust_store_path=tmp_path / "store.json",  # never read; conflict fires first
		allow_bundled_pubkey=True,
	)
	import pytest
	with pytest.raises(VerifyPackageUsageError):
		verify_deployed_package(opts)


def test_not_a_directory_is_usage_error(tmp_path: Path) -> None:
	"""Pointing the verb at a bare `.zdmp` (or a non-directory) is an
	invocation error: exit 2, no report body."""
	deployed, _a, _e = _build_good_dir(tmp_path)
	zdmp = deployed / f"{_PKG}.zdmp"
	buf = io.StringIO()
	try:
		with contextlib.redirect_stdout(buf):
			rc = main(["trust", "verify-package", str(zdmp), "--json", "--allow-bundled-pubkey"])
	except SystemExit as ex:
		rc = int(ex.code if ex.code is not None else 0)
	assert rc == 2


def test_bad_trust_store_path_is_usage_error_even_for_corrupt_package(tmp_path: Path) -> None:
	"""An unreadable --trust-store is bad invocation INPUT, validated before
	the artifact is read: a corrupt package must not mask it as a
	malformed-artifact verification failure (exit 1)."""
	deployed, _a, _e = _build_good_dir(tmp_path)
	(deployed / f"{_PKG}.zdmp").write_bytes(b"not a zstd frame")
	rc, report = _run(deployed, "--trust-store", str(tmp_path / "does-not-exist.json"))
	assert rc == 2
	assert report is None


def test_malformed_author_pubkey_is_usage_error_even_for_corrupt_package(tmp_path: Path) -> None:
	"""A malformed --author-pubkey-b64 (valid base64 but not 32 bytes) is
	bad invocation INPUT, validated before the artifact is read."""
	deployed, _a, _e = _build_good_dir(tmp_path)
	(deployed / f"{_PKG}.zdmp").write_bytes(b"not a zstd frame")
	rc, report = _run(deployed, "--author-pubkey-b64", "AAAA")  # decodes to 3 bytes
	assert rc == 2
	assert report is None


def test_invalid_base64_author_pubkey_is_usage_error(tmp_path: Path) -> None:
	"""A --author-pubkey-b64 that is not even valid base64 must be a usage
	error (exit 2), not escape to the exit-1 backstop.  `b64decode` signals
	this with binascii.Error; the facade normalizes it to ValueError."""
	deployed, _a, _e = _build_good_dir(tmp_path)
	(deployed / f"{_PKG}.zdmp").write_bytes(b"not a zstd frame")
	rc, report = _run(deployed, "--author-pubkey-b64", "not@@base64")
	assert rc == 2
	assert report is None


def test_json_backstop_emits_full_schema_on_unexpected_error(tmp_path: Path, monkeypatch) -> None:
	"""Fault-inject an unexpected verifier exception and assert the CLI's
	--json backstop (cli.py) emits ONE full-schema ok=false report and
	exit 1 -- pinning the real CLI path, not just the helper."""
	import lang.drift.cli as climod
	deployed, _a, _e = _build_good_dir(tmp_path)

	def _boom(_opts):
		raise RuntimeError("kaboom inside the verifier")

	monkeypatch.setattr(climod, "verify_deployed_package", _boom)
	buf = io.StringIO()
	try:
		with contextlib.redirect_stdout(buf):
			rc = main(["trust", "verify-package", str(deployed), "--json", "--allow-bundled-pubkey"])
	except SystemExit as ex:
		rc = int(ex.code if ex.code is not None else 0)
	assert rc == 1
	report = json.loads(buf.getvalue())
	for key in (
		"ok", "package_dir", "package_id", "version", "source_content_id",
		"artifact_sha256", "trust_source", "mode", "author_kid",
		"certifier_kid", "certifier_kids", "provenance_ok",
		"no_evidence_sentinel", "modules", "warnings", "errors",
	):
		assert key in report, f"backstop --json report missing key {key!r}"
	assert report["ok"] is False
	assert any(e["code"] == "verify-error" for e in report["errors"]), report
	assert "kaboom" in report["errors"][0]["message"]
