# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lang.codegen.llvm.test_utils import host_word_bits, sanitizer_timeout
from lang.driftc.driftc import main as driftc_main
from lang.drift.crypto import compute_ed25519_kid
from tools.deploy.steps.bundle import bundle_compiler, bundle_docs_and_examples, bundle_runtime_archives
from tools.deploy.steps.pex import build_drift_pex, build_driftc_pex

import pytest

ROOT = Path(__file__).resolve().parents[3]

_skip_no_pex = pytest.mark.skipif(
	shutil.which("pex") is None and not (ROOT / ".venv" / "bin" / "pex").exists(),
	reason="pex not installed; deployed bundle requires PEX --scie eager",
)


def _b64(data: bytes) -> str:
	return base64.b64encode(data).decode("ascii")


def _sha256_hex(data: bytes) -> str:
	return sha256(data).hexdigest()


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _public_key_bytes(pub) -> bytes:
	if hasattr(pub, "public_bytes_raw"):
		return pub.public_bytes_raw()
	return pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def _write_trust_store(path: Path, *, kid: str, pub_b64: str, namespaces: list[str]) -> None:
	"""v1 role-tagged trust store; bootstrap kid covers both roles."""
	obj = {
		"format": "drift-trust",
		"version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {ns: {"authors": [kid], "certifiers": [kid]} for ns in namespaces},
		"revoked": [],
	}
	_write_file(path, json.dumps(obj, separators=(",", ":"), sort_keys=True))


_TEST_SCI = "sha256:" + ("0" * 64)


def _emit_v1_sidecars(
	pkg_path: Path,
	*,
	package_id: str,
	package_version: str,
	priv: Ed25519PrivateKey,
	target: str,
	namespaces: list[str],
) -> None:
	"""Emit `<pkg>.author-claim` + `<pkg>.cert-claim.<kid>.json` next
	to the .dmp.  Same shape `tools.deploy.steps.stdlib` writes for
	production deploys, just inlined for self-contained tests.
	"""
	from lang.driftc.packages.author_claim_v1 import AuthorClaimBody
	from lang.driftc.packages.cert_claim_v1 import CertClaimBody, CertSuite, Toolchain
	from tools.drift_author.author_publish import (
		SignAuthorClaimOptions, sign_and_write_author_claim,
	)
	from tools.drift_deploy.cert_emit import (
		SignCertClaimOptions, sign_and_write_cert_claim,
	)

	priv_seed = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	artifact_sha256 = "sha256:" + _sha256_hex(pkg_path.read_bytes())

	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=AuthorClaimBody(
			schema_version=1, package_id=package_id, version=package_version,
			namespaces=tuple(namespaces),
			source_content_id=_TEST_SCI,
			required_deps=(), 			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed,
		sidecar_dir=pkg_path.parent,
	))
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=CertClaimBody(
			schema_version=1, package_id=package_id, version=package_version,
			artifact_sha256=artifact_sha256, source_content_id=_TEST_SCI,
			target=target,
			toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="test"),
			dep_graph=(),
			cert_suite=CertSuite(id="drift-deploy/test", version="1.0",
				result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64)),
			run_id=f"test-{package_id}",
			run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=priv_seed,
		sidecar_dir=pkg_path.parent,
	))


def _gen_keys() -> tuple[Ed25519PrivateKey, str, str]:
	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	return priv, compute_ed25519_kid(pub_raw), _b64(pub_raw)


def _empty_stdlib_root(tmp_path: Path) -> Path:
	root = tmp_path / "_empty_stdlib"
	root.mkdir(parents=True, exist_ok=True)
	return root


def _build_std_package(tmp_path: Path) -> Path:
	build_dir = tmp_path / "_pkg_build"
	module_dir = build_dir / "std" / "testlib"
	_write_file(
		module_dir / "testlib.drift",
		"""module std.testlib;

export { ANSWER };

pub const ANSWER: Int = 42;
""",
	)
	pkg_path = tmp_path / "dist" / "lib" / "stdlib" / "std.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build_dir),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(module_dir / "testlib.drift"),
		"--package-id", "std",
		"--package-version", "0.0.0-test",
		"--package-target", "test-target",
		"--source-content-id", _TEST_SCI,
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build std package"
	return pkg_path


def _write_consumer(tmp_path: Path) -> Path:
	src = tmp_path / "consumer" / "main.drift"
	_write_file(
		src,
		"""module main;

import std.testlib as testlib;

fn main() nothrow -> Int {
	return testlib.ANSWER;
}
""",
	)
	return src


@_skip_no_pex
def test_deployed_wrapper_uses_bundled_python_dependencies_only(
	tmp_path: Path, pex_scie_base: Path,
) -> None:
	dist = tmp_path / "dist"
	dist.mkdir(parents=True, exist_ok=True)
	clang = shutil.which("clang")
	assert clang, "clang not found"

	# Build PEX executables.
	build_driftc_pex(ROOT, dist)
	build_drift_pex(ROOT, dist)

	# Bundle compiler sources and runtime archives.
	bundle_compiler(ROOT, dist)
	bundle_runtime_archives(ROOT, dist)
	bundle_docs_and_examples(dist)

	priv, kid, pub_b64 = _gen_keys()
	pkg_path = _build_std_package(tmp_path)
	_emit_v1_sidecars(
		pkg_path, package_id="std", package_version="0.0.0-test",
		priv=priv, target="test-target",
		namespaces=["std.*", "lang.*", "drift.*"],
	)
	# v1 loader reads `core_trust_v1.json`.
	_write_trust_store(
		dist / "lib" / "compiler" / "lang" / "driftc" / "packages" / "core_trust_v1.json",
		kid=kid,
		pub_b64=pub_b64,
		namespaces=["std.*", "lang.*", "drift.*"],
	)

	src = _write_consumer(tmp_path)
	out_ir = tmp_path / "out.ll"
	# Run with stripped environment — no ambient Python packages.
	run_env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV"):
		run_env.pop(key, None)
	run_env["SCIE_BASE"] = str(pex_scie_base)
	result = subprocess.run(
		[
			str(dist / "bin" / "driftc"),
			"-M", str(src.parent),
			str(src),
			"--target-word-bits", str(host_word_bits()),
			"--emit-ir", str(out_ir),
			"--json",
		],
		text=True,
		capture_output=True,
		env=run_env,
		cwd=tmp_path,
		timeout=sanitizer_timeout(180),
	)
	assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
	assert out_ir.exists()
