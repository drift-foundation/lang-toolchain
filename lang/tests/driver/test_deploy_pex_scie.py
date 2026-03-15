# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Staged PEX --scie eager deploy artifact regression tests.

Validates the deployed bin/driftc PEX executable in an isolated temporary
staged layout.  These tests exercise artifact behavior that external consumers
care about, without invoking step_publish.sh or mutating any persistent
deploy location.

Coverage:

  1. Staged bin/driftc is a real PEX/scie artifact, not a shell wrapper
  2. No ambient Python packages are required (PEX is self-contained)
  3. Staged install tree can be treated as read-only from the consumer
  4. Signed stdlib package loading/verification works through the artifact
  5. Runtime archive link path works through the staged artifact
  6. Deploy-root resolution works correctly through symlinked entry paths

Out of scope:

  - Real publish to ~/opt/drift
  - current symlink switching via step_publish.sh
  - Long-lived machine-global deploy state

Runnable directly via pytest without a prior just deploy.
Requires pex in the project venv; skipped otherwise.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.driftc import main as driftc_main
from lang.driftc.packages.signature_v0 import compute_ed25519_kid

ROOT = Path(__file__).resolve().parents[3]

_skip_no_pex = pytest.mark.skipif(
	shutil.which("pex") is None and not (ROOT / ".venv" / "bin" / "pex").exists(),
	reason="pex not installed",
)
_skip_deploy_disabled = pytest.mark.skipif(
	os.environ.get("DRIFT_DEPLOY_TEST") == "0",
	reason="deploy tests disabled via DRIFT_DEPLOY_TEST=0",
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


def _gen_keys() -> tuple[Ed25519PrivateKey, str, str]:
	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	return priv, compute_ed25519_kid(pub_raw), _b64(pub_raw)


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
	sidecar = Path(str(pkg_path) + ".sig")
	obj = {
		"format": "dmir-pkg-sig",
		"version": 0,
		"package_sha256": f"sha256:{_sha256_hex(pkg_bytes)}",
		"signatures": [{"algo": "ed25519", "kid": kid, "sig": _b64(sig_raw), "pubkey": pub_b64}],
	}
	_write_file(sidecar, json.dumps(obj, separators=(",", ":"), sort_keys=True))


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


def _pex_cmd() -> str:
	"""Return path to pex binary."""
	venv_pex = ROOT / ".venv" / "bin" / "pex"
	if venv_pex.exists():
		return str(venv_pex)
	pex = shutil.which("pex")
	assert pex is not None, "pex not found"
	return pex


def _build_pex_binary(dist: Path) -> None:
	"""Build a PEX --scie eager executable at dist/bin/driftc."""
	env = dict(os.environ)
	env["REPO_ROOT"] = str(ROOT)
	env["DIST"] = str(dist)
	result = subprocess.run(
		["/bin/bash", str(ROOT / "tools" / "deploy" / "step_build_pex.sh")],
		text=True,
		capture_output=True,
		env=env,
		timeout=300,
	)
	assert result.returncode == 0, f"PEX build failed:\nstdout={result.stdout}\nstderr={result.stderr}"


def _bundle_compiler_sources(dist: Path) -> None:
	"""Run step_bundle.sh to populate lib/compiler/ and lib/runtime/."""
	clang = shutil.which("clang")
	assert clang, "clang not found"
	env = dict(os.environ)
	env["REPO_ROOT"] = str(ROOT)
	env["DIST"] = str(dist)
	env["CLANG"] = clang
	result = subprocess.run(
		["/bin/bash", str(ROOT / "tools" / "deploy" / "step_bundle.sh")],
		text=True,
		capture_output=True,
		env=env,
		timeout=180,
	)
	assert result.returncode == 0, f"bundle failed:\nstdout={result.stdout}\nstderr={result.stderr}"


def _setup_deploy_tree(tmp_path: Path) -> Path:
	"""Build PEX + bundle into a simulated deploy tree."""
	dist = tmp_path / "dist"
	dist.mkdir(parents=True, exist_ok=True)
	_build_pex_binary(dist)
	_bundle_compiler_sources(dist)
	return dist


def _setup_signed_stdlib(tmp_path: Path, dist: Path) -> tuple[str, str]:
	"""Build a test stdlib package, sign it, install into dist, return (kid, pub_b64)."""
	priv, kid, pub_b64 = _gen_keys()
	pkg_path = _build_std_package(tmp_path)
	pkg_bytes = pkg_path.read_bytes()
	# Install package
	stdlib_dir = dist / "lib" / "stdlib"
	stdlib_dir.mkdir(parents=True, exist_ok=True)
	(stdlib_dir / "std.dmp").write_bytes(pkg_bytes)
	_write_sig_sidecar(
		stdlib_dir / "std.dmp",
		pkg_bytes=pkg_bytes,
		kid=kid,
		sig_raw=priv.sign(pkg_bytes),
		pub_b64=pub_b64,
	)
	# Install core trust store
	_write_trust_store(
		dist / "lib" / "compiler" / "lang" / "driftc" / "packages" / "core_trust.json",
		kid=kid,
		pub_b64=pub_b64,
		namespaces=["std.*", "lang.*", "drift.*"],
	)
	return kid, pub_b64


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
@_skip_deploy_disabled
def test_pex_binary_is_not_a_shell_script(tmp_path: Path) -> None:
	"""Verify bin/driftc is a native executable, not a bash wrapper."""
	dist = tmp_path / "dist"
	dist.mkdir(parents=True, exist_ok=True)
	_build_pex_binary(dist)
	driftc = dist / "bin" / "driftc"
	assert driftc.exists()
	assert os.access(str(driftc), os.X_OK)
	# Read the first 4 bytes — should be ELF magic or a #! shebang for the
	# scie launcher, but NOT "#!/usr/bin/env bash" or "#!/bin/bash".
	header = driftc.read_bytes()[:128]
	assert not header.startswith(b"#!/usr/bin/env bash"), "bin/driftc is still a bash wrapper"
	assert not header.startswith(b"#!/bin/bash"), "bin/driftc is still a bash wrapper"


@_skip_no_pex
@_skip_deploy_disabled
def test_pex_deployed_self_sufficient_no_ambient_python(tmp_path: Path) -> None:
	"""
	Deployed PEX executable works without ambient Python packages.

	Verifies the key deploy contract: the PEX embeds its own interpreter
	and third-party deps (lark, llvmlite, cryptography).  No pip-installed
	packages on the host should be required.
	"""
	dist = _setup_deploy_tree(tmp_path)
	_setup_signed_stdlib(tmp_path, dist)
	src = _write_consumer(tmp_path)
	out_ir = tmp_path / "out.ll"

	# Run with a clean environment — strip PYTHONPATH and related vars to
	# ensure the PEX is truly self-contained.
	run_env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV"):
		run_env.pop(key, None)
	run_env["HOME"] = str(tmp_path / "home")

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


@_skip_no_pex
@_skip_deploy_disabled
def test_pex_deployed_readonly_install_tree(tmp_path: Path) -> None:
	"""
	Deployed PEX works when the install tree is read-only.

	The scie extraction cache is outside the install tree (in $HOME or
	$SCIE_BASE).  No writes should touch lib/runtime/ or other lib/ dirs.
	"""
	dist = _setup_deploy_tree(tmp_path)
	_setup_signed_stdlib(tmp_path, dist)
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""module main;

fn main() nothrow -> Int {
	return 0;
}
""",
	)
	out = tmp_path / "a.out"

	# Make runtime dir read-only.
	runtime_root = dist / "lib" / "runtime"
	for path in [runtime_root, *runtime_root.rglob("*")]:
		if path.is_dir():
			path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
		else:
			path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

	run_env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV"):
		run_env.pop(key, None)
	run_env["HOME"] = str(tmp_path / "home")

	result = subprocess.run(
		[
			str(dist / "bin" / "driftc"),
			"--target-word-bits", "64",
			"-M", str(tmp_path),
			str(src),
			"-o", str(out),
		],
		text=True,
		capture_output=True,
		env=run_env,
		cwd=tmp_path,
		timeout=180,
	)
	assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
	assert out.exists()
	assert not list(runtime_root.rglob(".build.lock"))


@_skip_no_pex
@_skip_deploy_disabled
def test_pex_deployed_signed_stdlib_package_verification(tmp_path: Path) -> None:
	"""
	Signed stdlib package is loaded and verified by the PEX-deployed compiler.
	"""
	dist = _setup_deploy_tree(tmp_path)
	_setup_signed_stdlib(tmp_path, dist)
	src = _write_consumer(tmp_path)
	out_ir = tmp_path / "out.ll"

	run_env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV"):
		run_env.pop(key, None)
	run_env["HOME"] = str(tmp_path / "home")

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
	# Verify the JSON output indicates success.
	output = json.loads(result.stdout.strip())
	assert output.get("exit_code") == 0


@_skip_no_pex
@_skip_deploy_disabled
def test_pex_deployed_runtime_archive_link(tmp_path: Path) -> None:
	"""
	Runtime archive linking works from the PEX-deployed toolchain.

	Compiles a trivial program to a linked binary (not just IR) to verify
	that the deployed pre-built runtime archives are found and usable.
	"""
	dist = _setup_deploy_tree(tmp_path)
	_setup_signed_stdlib(tmp_path, dist)
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""module main;

fn main() nothrow -> Int {
	return 7;
}
""",
	)
	out = tmp_path / "test_bin"

	run_env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV"):
		run_env.pop(key, None)
	run_env["HOME"] = str(tmp_path / "home")

	result = subprocess.run(
		[
			str(dist / "bin" / "driftc"),
			"-M", str(tmp_path),
			str(src),
			"-o", str(out),
		],
		text=True,
		capture_output=True,
		env=run_env,
		cwd=tmp_path,
		timeout=180,
	)
	assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
	assert out.exists()

	# Run the compiled binary and verify exit code.
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=30)
	assert run.returncode == 7


@_skip_no_pex
@_skip_deploy_disabled
def test_pex_entry_resolves_deploy_root_through_symlink(tmp_path: Path) -> None:
	"""
	The PEX entry point resolves the deploy root correctly when invoked
	through a symlink (as happens with <dest>/current -> drift-VERSION/).
	"""
	dist = _setup_deploy_tree(tmp_path)
	_setup_signed_stdlib(tmp_path, dist)

	# Create a symlink mimicking the current -> version dir layout.
	link = tmp_path / "current"
	link.symlink_to(dist)

	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""module main;

fn main() nothrow -> Int {
	return 0;
}
""",
	)
	out_ir = tmp_path / "out.ll"

	run_env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV"):
		run_env.pop(key, None)
	run_env["HOME"] = str(tmp_path / "home")

	result = subprocess.run(
		[
			str(link / "bin" / "driftc"),
			"-M", str(tmp_path),
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
