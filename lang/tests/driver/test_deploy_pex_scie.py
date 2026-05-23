# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Staged PEX --scie eager deploy artifact regression tests.

Validates the deployed bin/driftc PEX executable in an isolated temporary
staged layout.  These tests exercise artifact behavior that external consumers
care about, without invoking the publish step or mutating any persistent
deploy location.

Coverage:

  1. Staged bin/driftc is a real PEX/scie artifact, not a shell wrapper
  2. No ambient Python packages are required (PEX is self-contained)
  3. Staged install tree can be treated as read-only from the consumer
  4. Signed stdlib package loading/verification works through the artifact
  5. Runtime archive link path works through the staged artifact
  6. Deploy-root resolution works correctly through symlinked entry paths
  7. Public drift CLI subcommands backed by tools/ packages are bundled

Out of scope:

  - Real publish to ~/opt/drift
  - current symlink switching via publish step
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
from lang.drift.crypto import compute_ed25519_kid
from tools.deploy.steps.bundle import bundle_compiler, bundle_docs_and_examples, bundle_runtime_archives
from tools.deploy.steps.pex import build_drift_pex, build_driftc_pex

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
	to the .dmp.  Replaces the v0 `_write_sig_sidecar`; the v1 trust
	gate verifies these instead of the gone `.sig` envelope.
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


# ---------------------------------------------------------------------------
# Module-scoped fixtures: build the deploy tree and scie cache ONCE per
# worker process instead of per-test.  Each PEX deploy test creates ~920 MB
# of temp files (dist + scie extraction cache).  With 5 tests, the old
# per-test approach accumulated ~4.6 GB in page cache, exhausting RAM on
# 14–16 GB machines under ASAN.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _shared_deploy_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
	"""Build PEX + compiler bundle + signed stdlib once for the module."""
	base = tmp_path_factory.mktemp("pex_shared")
	dist = base / "dist"
	dist.mkdir(parents=True, exist_ok=True)
	build_driftc_pex(ROOT, dist)
	build_drift_pex(ROOT, dist)
	bundle_compiler(ROOT, dist)
	bundle_runtime_archives(ROOT, dist)
	bundle_docs_and_examples(dist)

	# Build and sign a test stdlib package.
	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)

	build_dir = base / "_pkg_build"
	module_dir = build_dir / "std" / "testlib"
	_write_file(
		module_dir / "testlib.drift",
		"""module std.testlib;

export { ANSWER };

pub const ANSWER: Int = 42;
""",
	)
	empty_stdlib = base / "_empty_stdlib"
	empty_stdlib.mkdir(parents=True, exist_ok=True)
	pkg_path = dist / "lib" / "stdlib" / "std.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build_dir),
		"--stdlib-root", str(empty_stdlib),
		str(module_dir / "testlib.drift"),
		"--package-id", "std",
		"--package-version", "0.0.0-test",
		"--package-target", "test-target",
		"--source-content-id", _TEST_SCI,
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build std package"
	_emit_v1_sidecars(
		pkg_path, package_id="std", package_version="0.0.0-test",
		priv=priv, target="test-target",
		namespaces=["std.*", "lang.*", "drift.*"],
	)
	# Bundled compiler reads core trust from `core_trust_v1.json`
	# (v1 loader); also emit `core_trust.json` for any path that
	# still consults the legacy filename so deploy bundles keep
	# working through the cutover.
	_write_trust_store(
		dist / "lib" / "compiler" / "lang" / "driftc" / "packages" / "core_trust_v1.json",
		kid=kid,
		pub_b64=pub_b64,
		namespaces=["std.*", "lang.*", "drift.*"],
	)
	return dist


@pytest.fixture(scope="module")
def _shared_scie_base(pex_scie_base: Path) -> Path:
	"""Alias the session-scoped scie cache for module-scoped use."""
	return pex_scie_base


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


def _pex_run_env(scie_base: Path) -> dict[str, str]:
	"""Clean env for PEX subprocess: no ambient Python, shared scie cache."""
	env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV"):
		env.pop(key, None)
	env["SCIE_BASE"] = str(scie_base)
	return env


@_skip_no_pex
@_skip_deploy_disabled
def test_pex_binary_is_not_a_shell_script(_shared_deploy_dist: Path) -> None:
	"""Verify bin/driftc is a native executable, not a bash wrapper."""
	driftc = _shared_deploy_dist / "bin" / "driftc"
	assert driftc.exists()
	assert os.access(str(driftc), os.X_OK)
	# Read the first 4 bytes — should be ELF magic or a #! shebang for the
	# scie launcher, but NOT "#!/usr/bin/env bash" or "#!/bin/bash".
	header = driftc.read_bytes()[:128]
	assert not header.startswith(b"#!/usr/bin/env bash"), "bin/driftc is still a bash wrapper"
	assert not header.startswith(b"#!/bin/bash"), "bin/driftc is still a bash wrapper"


@_skip_no_pex
@_skip_deploy_disabled
def test_pex_deployed_self_sufficient_no_ambient_python(
	tmp_path: Path, _shared_deploy_dist: Path, _shared_scie_base: Path,
) -> None:
	"""
	Deployed PEX executable works without ambient Python packages.

	Verifies the key deploy contract: the PEX embeds its own interpreter
	and third-party deps (lark, llvmlite, cryptography).  No pip-installed
	packages on the host should be required.
	"""
	src = _write_consumer(tmp_path)
	out_ir = tmp_path / "out.ll"

	run_env = _pex_run_env(_shared_scie_base)

	result = subprocess.run(
		[
			str(_shared_deploy_dist / "bin" / "driftc"),
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
def test_pex_deployed_drift_author_subcommand_is_bundled(
	tmp_path: Path, _shared_deploy_dist: Path, _shared_scie_base: Path,
) -> None:
	"""The deployed `drift` PEX must bundle tools.drift_author.

	Regression: drift 0.32.16 shipped the public `drift author`
	dispatcher in lang.drift.cli, but the deployed PEX omitted the
	backing tools.drift_author package and failed with ModuleNotFoundError.
	"""
	run_env = _pex_run_env(_shared_scie_base)
	result = subprocess.run(
		[
			str(_shared_deploy_dist / "bin" / "drift"),
			"author",
			"--help",
		],
		text=True,
		capture_output=True,
		env=run_env,
		cwd=tmp_path,
		timeout=180,
	)
	assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
	assert "drift author" in result.stdout
	assert "--manifest" in result.stdout
	assert "ModuleNotFoundError" not in result.stderr


@_skip_no_pex
@_skip_deploy_disabled
def test_pex_deployed_readonly_install_tree(
	tmp_path: Path, _shared_deploy_dist: Path, _shared_scie_base: Path,
) -> None:
	"""
	Deployed PEX works when the install tree is read-only.

	The scie extraction cache is outside the install tree (in $HOME or
	$SCIE_BASE).  No writes should touch lib/runtime/ or other lib/ dirs.

	Uses a private copy of the deploy tree because it chmod's runtime dirs.
	"""
	dist = tmp_path / "dist"
	shutil.copytree(str(_shared_deploy_dist), str(dist))

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

	run_env = _pex_run_env(_shared_scie_base)

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
def test_pex_deployed_signed_stdlib_package_verification(
	tmp_path: Path, _shared_deploy_dist: Path, _shared_scie_base: Path,
) -> None:
	"""
	Signed stdlib package is loaded and verified by the PEX-deployed compiler.
	"""
	src = _write_consumer(tmp_path)
	out_ir = tmp_path / "out.ll"

	run_env = _pex_run_env(_shared_scie_base)

	result = subprocess.run(
		[
			str(_shared_deploy_dist / "bin" / "driftc"),
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
def test_pex_deployed_runtime_archive_link(
	tmp_path: Path, _shared_deploy_dist: Path, _shared_scie_base: Path,
) -> None:
	"""
	Runtime archive linking works from the PEX-deployed toolchain.

	Compiles a trivial program to a linked binary (not just IR) to verify
	that the deployed pre-built runtime archives are found and usable.
	"""
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

	run_env = _pex_run_env(_shared_scie_base)

	result = subprocess.run(
		[
			str(_shared_deploy_dist / "bin" / "driftc"),
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
def test_pex_entry_resolves_deploy_root_through_symlink(
	tmp_path: Path, _shared_deploy_dist: Path, _shared_scie_base: Path,
) -> None:
	"""
	The PEX entry point resolves the deploy root correctly when invoked
	through a symlink (as happens with <dest>/current -> drift-VERSION/).
	"""
	# Create a symlink mimicking the current -> version dir layout.
	link = tmp_path / "current"
	link.symlink_to(_shared_deploy_dist)

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

	run_env = _pex_run_env(_shared_scie_base)

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
