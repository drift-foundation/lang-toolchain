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

from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.driftc import main as driftc_main
from lang.driftc.packages.signature_v0 import compute_ed25519_kid

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
	revoked = revoked or []
	obj = {
		"format": "drift-trust",
		"version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {ns: [kid] for ns in namespaces},
		"revoked": revoked,
	}
	_write_file(path, json.dumps(obj, separators=(",", ":"), sort_keys=True))


def _write_sig_sidecar(pkg_path: Path, *, pkg_bytes: bytes, kid: str, sig_raw: bytes, pub_b64: str | None = None) -> Path:
	pkg_sha_hex = _sha256_hex(pkg_bytes)
	entry: dict = {"algo": "ed25519", "kid": kid, "sig": _b64(sig_raw)}
	if pub_b64 is not None:
		entry["pubkey"] = pub_b64
	sidecar = pkg_path.with_suffix(".sig")
	obj = {
		"format": "dmir-pkg-sig",
		"version": 0,
		"package_sha256": f"sha256:{pkg_sha_hex}",
		"signatures": [entry],
	}
	_write_file(sidecar, json.dumps(obj, separators=(",", ":"), sort_keys=True))
	return sidecar


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
from lang.driftc.packages import trust_v0
_core_path = Path(sys.argv[1])
_orig = trust_v0.load_core_trust_store
def _patched():
    return trust_v0.load_trust_store_json(_core_path)
trust_v0.load_core_trust_store = _patched
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
	env = {**os.environ, **(env_override or {})}
	return subprocess.run(
		[sys.executable, str(runner), str(core_trust_path)] + extra_argv,
		capture_output=True,
		text=True,
		timeout=120,
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
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

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
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

	# Tamper: flip a byte in the package after signing.
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
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

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
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

	# Tamper after signing.
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
			"--target-word-bits", str(host_word_bits()),
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
			"--json",
		],
		env_override={"HOME": str(home)},
	)
	assert result.returncode != 0, "expected compilation to fail with tampered package"
