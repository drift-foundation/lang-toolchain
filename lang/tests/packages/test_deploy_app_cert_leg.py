# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Producer regression: `drift deploy` emits the app cert leg correctly.

Before this fix, `_deploy_artifact_impl`'s app branch was a `pass` — apps
got only the unsigned provenance bundle, no author/cert legs — and
`_emit_cert_claim_for_artifact` hard-coded the signed `artifact_path` to
`<id>.zdmp`.  `verify_deployed_app` requires the cert claim's signed
`artifact_path` to name the on-disk BINARY, so an app could never close the
three-leg agreement.

These pin the producer half:
  - an app cert claim's signed `artifact_path` is the binary filename (NOT
    `<id>.zdmp`), and binds the binary's sha256 + artifact_kind "app";
  - a package cert claim still uses the `<id>.zdmp` locator (no regression).
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from lang.driftc.packages.cert_claim_v1 import load_cert_claim_json
from tools.drift_deploy.drift_deploy import (
	CertSuiteOptions,
	_emit_cert_claim_for_artifact,
)
from tools.drift_deploy.provenance import CompilerInfo


def _cert_suite() -> CertSuiteOptions:
	return CertSuiteOptions(
		id="anthropic/release-gate",
		version="1.0",
		result="pass",
		result_evidence_sha256="sha256:" + ("f" * 64),
		no_evidence_sentinel=False,
	)


def _write_seed(tmp_path: Path) -> Path:
	p = tmp_path / "cert.seed"
	p.write_text(base64.b64encode(bytes(range(32))).decode("ascii"), encoding="utf-8")
	return p


def _emit(tmp_path: Path, *, name: str, kind: str, artifact_file: Path) -> Path:
	"""Run the deploy cert emitter for one artifact and return the sidecar."""
	prov = tmp_path / f"{name}.provenance.zst"
	prov.write_bytes(b"(provenance bundle stub)")
	sha = "sha256:" + hashlib.sha256(artifact_file.read_bytes()).hexdigest()
	return _emit_cert_claim_for_artifact(
		artifact_file,
		cert_key=_write_seed(tmp_path),
		package_id=name,
		package_version="0.1.0",
		artifact_kind=kind,
		target="linux-x86_64",
		compiler_info=CompilerInfo(version="0.33.61", abi=18, commit=""),
		source_content_id="sha256:" + ("a" * 64),
		artifact_sha256=sha,
		resolved_deps={},  # no deps → empty dep_graph (no identity reqs)
		direct_dep_ids=set(),
		staged_pkg_root=tmp_path,
		provenance_path=prov,
		cert_suite_options=_cert_suite(),
	)


def test_app_cert_locator_is_the_binary(tmp_path: Path) -> None:
	"""App cert claim's signed artifact_path names the binary, not a .zdmp."""
	binary = tmp_path / "uflowsd"
	binary.write_bytes(b"\x7fELF...stub app binary...")
	sidecar = _emit(tmp_path, name="uflowsd", kind="app", artifact_file=binary)
	body = load_cert_claim_json(sidecar.read_text(encoding="utf-8")).body
	assert body.artifact_kind == "app"
	# The exact locator verify_deployed_app matches against: the binary
	# filename, NOT `uflowsd.zdmp`.
	assert body.artifact_path == "uflowsd"
	assert body.artifact_sha256 == "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()


def test_package_cert_locator_still_zdmp(tmp_path: Path) -> None:
	"""Package cert claim keeps the `<id>.zdmp` locator (no regression)."""
	dmp = tmp_path / "demo.lib.dmp"
	dmp.write_bytes(b"(stub package container bytes)")
	sidecar = _emit(tmp_path, name="demo.lib", kind="package", artifact_file=dmp)
	body = load_cert_claim_json(sidecar.read_text(encoding="utf-8")).body
	assert body.artifact_kind == "package"
	assert body.artifact_path == "demo.lib.zdmp"
