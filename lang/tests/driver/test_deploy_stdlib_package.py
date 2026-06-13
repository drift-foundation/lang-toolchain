# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for deploy-style signed stdlib package loading.

Validates the deploy pipeline's integrity contract:
- Signed stdlib packages compile successfully
- Unsigned stdlib packages are rejected
- Tampered stdlib packages are rejected
- Unsigned allowed only with explicit --allow-unsigned-from
- Integration test exercising the deployed wrapper behavior (subprocess)
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import host_word_bits, sanitizer_timeout
from lang.driftc.driftc import main as driftc_main
from lang.drift.crypto import compute_ed25519_kid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


# ── Helpers ──────────────────────────────────────────────────────────


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


def _write_trust_store(path: Path, *, kid: str, pub_b64: str, namespaces: list[str], revoked: list[str] | None = None) -> None:
	"""Write a v1 role-tagged trust store. Foundation-bootstrap: the
	`kid` plays both author and certifier roles for every namespace
	(matches the dev shape the stdlib tests need).
	"""
	revoked = revoked or []
	obj = {
		"format": "drift-trust",
		"version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {ns: {"authors": [kid], "certifiers": [kid]} for ns in namespaces},
		"revoked": revoked,
	}
	_write_file(path, json.dumps(obj, separators=(",", ":"), sort_keys=True))


# Sentinel SCI used by every test in this file -- the v1 invariant
# is that the manifest stamp and the author/cert claim bodies all
# carry the SAME `source_content_id` string.  None of these tests
# assert anything about source-identity contents, so a constant
# value satisfies the verifier without source-tree hashing.
_TEST_SCI = "sha256:" + ("0" * 64)


def _emit_v1_sidecars(
	pkg_path: Path,
	*,
	package_id: str,
	package_version: str,
	priv: Ed25519PrivateKey,
	target: str,
	namespaces: list[str],
) -> tuple[Path, Path]:
	"""Emit `<pkg>.author-claim` + `<pkg>.cert-claim.<kid>.json`
	sidecars next to the .dmp.  Replaces the v0 `_write_sig_sidecar`
	helper; the v1 trust gate verifies these instead of the gone
	`.sig` envelope.
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

	author = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=AuthorClaimBody(
			schema_version=1, package_id=package_id, version=package_version,
			namespaces=tuple(namespaces),
			source_content_id=_TEST_SCI,
			required_deps=(),
			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed,
		sidecar_dir=pkg_path.parent,
	))
	cert = sign_and_write_cert_claim(SignCertClaimOptions(
		body=CertClaimBody(
			schema_version=1, package_id=package_id, version=package_version,
			artifact_sha256=artifact_sha256,
			source_content_id=_TEST_SCI,
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
	return author, cert


@dataclass(frozen=True)
class _DeployKeys:
	priv: Ed25519PrivateKey
	kid: str
	pub_b64: str


def _gen_keys() -> _DeployKeys:
	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)
	return _DeployKeys(priv=priv, kid=kid, pub_b64=pub_b64)


def _empty_stdlib_root(tmp_path: Path) -> Path:
	"""Return an empty directory to suppress auto-detection of repo stdlib."""
	d = tmp_path / "_empty_stdlib"
	d.mkdir(parents=True, exist_ok=True)
	return d


def _build_std_package(tmp_path: Path) -> Path:
	"""Build a minimal package with a std.* module (requires --dev to emit)."""
	build_dir = tmp_path / "_pkg_build"
	module_dir = build_dir / "std" / "testlib"
	_write_file(
		module_dir / "testlib.drift",
		"""module std.testlib;

export { ANSWER };

pub const ANSWER: Int = 42;
""",
	)
	pkg_path = tmp_path / "pkgs" / "std.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M",
		str(build_dir),
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
	"""Write a main.drift that imports the test std module."""
	consumer_dir = tmp_path / "consumer"
	src = consumer_dir / "main.drift"
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


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


# Runner script written to tmp_path and executed as subprocess.
# Patches load_core_trust_store to use an isolated trust file instead of
# the in-repo core_trust.json.  First argv is the trust store path;
# remaining argv are forwarded to driftc.main().
_SUBPROCESS_RUNNER = """\
import sys, json
from pathlib import Path
from lang.driftc.packages import trust_v1
_core_path = Path(sys.argv[1])
_orig = trust_v1.load_core_trust_store
def _patched():
    return trust_v1.load_trust_store_json(_core_path)
trust_v1.load_core_trust_store = _patched
from lang.driftc.driftc import main
sys.exit(main(sys.argv[2:]))
"""


def _run_driftc_subprocess(
	tmp_path: Path,
	*,
	core_trust_path: Path,
	extra_argv: list[str],
	env_override: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
	"""Run driftc as a subprocess with an isolated core trust store.

	Uses a runner script that patches load_core_trust_store at import time
	so no in-repo files are mutated.  Fully xdist-safe.
	"""
	runner = tmp_path / "_driftc_runner.py"
	runner.write_text(_SUBPROCESS_RUNNER, encoding="utf-8")
	# Inject PYTHONPATH=repo_root so the spawned `python _driftc_runner.py`
	# can import `lang.*`.  Without this, sys.path[0] in the subprocess is
	# the script's dir (tmp_path) -- `lang/` is not there, and
	# `from lang.driftc.packages import trust_v1` fails with
	# `ModuleNotFoundError: No module named 'lang'`.  Mirrors the
	# `env["PYTHONPATH"] = str(repo_root)` shape used by the stdlib_pkg
	# fixture in this directory's `conftest.py`.
	repo_root = Path(__file__).resolve().parents[3]
	env = {**os.environ, "PYTHONPATH": str(repo_root), **(env_override or {})}
	return subprocess.run(
		[sys.executable, str(runner), str(core_trust_path)] + extra_argv,
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(120),
		env=env,
	)


# ── Test 1: signed stdlib package compiles ───────────────────────────


def test_signed_stdlib_package_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A correctly signed std.* package compiles without --dev."""
	home = tmp_path / "home"
	monkeypatch.setenv("HOME", str(home))

	keys = _gen_keys()
	pkg_path = _build_std_package(tmp_path)
	_emit_v1_sidecars(
		pkg_path, package_id="std", package_version="0.0.0-test",
		priv=keys.priv, target="test-target",
		namespaces=["std.*", "lang.*", "drift.*"],
	)

	core_trust_path = tmp_path / "core_trust.json"
	_write_trust_store(core_trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*"])

	main_src = _write_consumer(tmp_path)
	pkg_root = pkg_path.parent
	empty_stdlib = _empty_stdlib_root(tmp_path)

	rc, payload = _run_driftc_json(
		[
			"-M", str(main_src.parent),
			"--stdlib-root", str(empty_stdlib),
			"--package-root", str(pkg_root),
			"--dep", "std@0.0.0-test",
			"--dev",
			"--dev-core-trust-store", str(core_trust_path),
			"--trust-store", str(trust_path),
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc == 0, f"expected success, got diagnostics: {payload.get('diagnostics', [])}"


# ── Test 2: unsigned stdlib package rejected ─────────────────────────


def test_unsigned_stdlib_package_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A std.* package without a .sig sidecar is rejected."""
	home = tmp_path / "home"
	monkeypatch.setenv("HOME", str(home))

	keys = _gen_keys()
	pkg_path = _build_std_package(tmp_path)
	# Intentionally skip signing — no .sig sidecar.

	core_trust_path = tmp_path / "core_trust.json"
	_write_trust_store(core_trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*"])

	main_src = _write_consumer(tmp_path)
	pkg_root = pkg_path.parent
	empty_stdlib = _empty_stdlib_root(tmp_path)

	rc, payload = _run_driftc_json(
		[
			"-M", str(main_src.parent),
			"--stdlib-root", str(empty_stdlib),
			"--package-root", str(pkg_root),
			"--dep", "std@0.0.0-test",
			"--dev",
			"--dev-core-trust-store", str(core_trust_path),
			"--trust-store", str(trust_path),
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "sidecar" in messages.lower() or "signature" in messages.lower(), f"expected sidecar/signature error, got: {messages}"


# ── Test 3: tampered stdlib package rejected ─────────────────────────


def test_tampered_stdlib_package_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A signed std.* package with tampered bytes is rejected."""
	home = tmp_path / "home"
	monkeypatch.setenv("HOME", str(home))

	keys = _gen_keys()
	pkg_path = _build_std_package(tmp_path)
	_emit_v1_sidecars(
		pkg_path, package_id="std", package_version="0.0.0-test",
		priv=keys.priv, target="test-target",
		namespaces=["std.*", "lang.*", "drift.*"],
	)
	pkg_bytes = pkg_path.read_bytes()

	# Tamper: flip a byte in the package after signing.  The cert
	# claim was signed over the original artifact bytes; the v1
	# verifier should reject the byte-mismatch.
	tampered = bytearray(pkg_bytes)
	tampered[-1] ^= 0xFF
	pkg_path.write_bytes(bytes(tampered))

	core_trust_path = tmp_path / "core_trust.json"
	_write_trust_store(core_trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*"])

	main_src = _write_consumer(tmp_path)
	pkg_root = pkg_path.parent
	empty_stdlib = _empty_stdlib_root(tmp_path)

	rc, payload = _run_driftc_json(
		[
			"-M", str(main_src.parent),
			"--stdlib-root", str(empty_stdlib),
			"--package-root", str(pkg_root),
			"--dep", "std@0.0.0-test",
			"--dev",
			"--dev-core-trust-store", str(core_trust_path),
			"--trust-store", str(trust_path),
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "hash" in messages.lower() or "integrity" in messages.lower() or "sha256" in messages.lower() or "signature" in messages.lower(), f"expected integrity/hash/signature error, got: {messages}"


# ── Test 4: unsigned reserved namespace always rejected ──────────────


def test_unsigned_stdlib_rejected_even_with_allow_flag(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Unsigned std.* packages are rejected even with --allow-unsigned-from.

	Reserved namespaces (std.*, lang.*, drift.*) can never be satisfied by
	unsigned packages — this is a hard policy, not overridable.
	"""
	home = tmp_path / "home"
	monkeypatch.setenv("HOME", str(home))

	keys = _gen_keys()
	pkg_path = _build_std_package(tmp_path)
	# No sidecar — unsigned.

	core_trust_path = tmp_path / "core_trust.json"
	_write_trust_store(core_trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*"])

	main_src = _write_consumer(tmp_path)
	pkg_root = pkg_path.parent
	empty_stdlib = _empty_stdlib_root(tmp_path)

	rc, payload = _run_driftc_json(
		[
			"-M", str(main_src.parent),
			"--stdlib-root", str(empty_stdlib),
			"--package-root", str(pkg_root),
			"--dep", "std@0.0.0-test",
			"--allow-unsigned-from", str(pkg_root),
			"--dev",
			"--dev-core-trust-store", str(core_trust_path),
			"--trust-store", str(trust_path),
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	# Reserved namespaces cannot be unsigned, even with --allow-unsigned-from.
	assert rc != 0
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "unsigned" in messages.lower() or "sidecar" in messages.lower() or "signature" in messages.lower(), f"expected unsigned/signature rejection, got: {messages}"


# ── Test 5: integration — subprocess with production core trust ──────


def test_deploy_wrapper_integration(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Integration test: simulate deployed wrapper behavior via subprocess.

	Builds a signed std.* package, writes the core trust store to an
	isolated temp path, and compiles without --dev flags via a runner
	script that patches load_core_trust_store.  No in-repo files are
	mutated — fully xdist-safe.
	"""
	home = tmp_path / "home"
	monkeypatch.setenv("HOME", str(home))

	keys = _gen_keys()
	pkg_path = _build_std_package(tmp_path)
	_emit_v1_sidecars(
		pkg_path, package_id="std", package_version="0.0.0-test",
		priv=keys.priv, target="test-target",
		namespaces=["std.*", "lang.*", "drift.*"],
	)

	core_trust_path = tmp_path / "core_trust.json"
	_write_trust_store(core_trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])

	main_src = _write_consumer(tmp_path)
	pkg_root = pkg_path.parent
	empty_stdlib = _empty_stdlib_root(tmp_path)

	# Run driftc as subprocess — no --dev, no --dev-core-trust-store.
	# The runner script patches load_core_trust_store to use our temp file.
	result = _run_driftc_subprocess(
		tmp_path,
		core_trust_path=core_trust_path,
		extra_argv=[
			"-M", str(main_src.parent),
			"--stdlib-root", str(empty_stdlib),
			"--package-root", str(pkg_root),
			"--dep", "std@0.0.0-test",
			"--target-word-bits", str(host_word_bits()),
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
			"--json",
		],
		env_override={"HOME": str(home)},
	)
	if result.returncode != 0:
		stdout_payload = {}
		if result.stdout.strip():
			try:
				stdout_payload = json.loads(result.stdout)
			except json.JSONDecodeError:
				pass
		diags = stdout_payload.get("diagnostics", [])
		pytest.fail(f"subprocess driftc failed (rc={result.returncode})\ndiags: {diags}\nstderr: {result.stderr}")

	assert (tmp_path / "out.ll").exists()


# ── Test 6: integration — subprocess tampered rejected ───────────────


def test_deploy_wrapper_tampered_rejected_integration(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Integration test: tampered package is rejected in production mode."""
	home = tmp_path / "home"
	monkeypatch.setenv("HOME", str(home))

	keys = _gen_keys()
	pkg_path = _build_std_package(tmp_path)
	_emit_v1_sidecars(
		pkg_path, package_id="std", package_version="0.0.0-test",
		priv=keys.priv, target="test-target",
		namespaces=["std.*", "lang.*", "drift.*"],
	)
	pkg_bytes = pkg_path.read_bytes()

	# Tamper after signing — the cert claim's `artifact_sha256`
	# field no longer matches; v1 verify must reject.
	tampered = bytearray(pkg_bytes)
	tampered[-1] ^= 0xFF
	pkg_path.write_bytes(bytes(tampered))

	core_trust_path = tmp_path / "core_trust.json"
	_write_trust_store(core_trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])

	main_src = _write_consumer(tmp_path)
	pkg_root = pkg_path.parent
	empty_stdlib = _empty_stdlib_root(tmp_path)

	result = _run_driftc_subprocess(
		tmp_path,
		core_trust_path=core_trust_path,
		extra_argv=[
			"-M", str(main_src.parent),
			"--stdlib-root", str(empty_stdlib),
			"--package-root", str(pkg_root),
			"--dep", "std@0.0.0-test",
			"--target-word-bits", str(host_word_bits()),
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
			"--json",
		],
		env_override={"HOME": str(home)},
	)
	assert result.returncode != 0, "expected compilation to fail with tampered package"
