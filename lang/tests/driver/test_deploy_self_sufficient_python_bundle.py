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

from lang.codegen.llvm.test_utils import host_word_bits
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
	obj = {
		"format": "drift-trust",
		"version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {ns: [kid] for ns in namespaces},
		"revoked": [],
	}
	_write_file(path, json.dumps(obj, separators=(",", ":"), sort_keys=True))


def _write_sig_sidecar(pkg_path: Path, *, pkg_bytes: bytes, kid: str, sig_raw: bytes, pub_b64: str) -> None:
	sidecar = pkg_path.with_suffix(".sig")
	obj = {
		"format": "dmir-pkg-sig",
		"version": 0,
		"package_sha256": f"sha256:{_sha256_hex(pkg_bytes)}",
		"signatures": [{"algo": "ed25519", "kid": kid, "sig": _b64(sig_raw), "pubkey": pub_b64}],
	}
	_write_file(sidecar, json.dumps(obj, separators=(",", ":"), sort_keys=True))


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
	pkg_bytes = pkg_path.read_bytes()
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=kid, sig_raw=priv.sign(pkg_bytes), pub_b64=pub_b64)
	_write_trust_store(
		dist / "lib" / "compiler" / "lang" / "driftc" / "packages" / "core_trust.json",
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
		timeout=180,
	)
	assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
	assert out_ir.exists()
