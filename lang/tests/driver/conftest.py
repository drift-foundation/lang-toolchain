# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
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
from lang.driftc import driftc


@pytest.fixture(scope="session", autouse=True)
def _inject_target_word_bits_for_tests() -> None:
	"""
	Driver tests default to host word size unless explicitly specified.

	This keeps production code strict about target layout while allowing tests
	to avoid passing --target-word-bits everywhere.
	"""
	driftc._TEST_TARGET_WORD_BITS = host_word_bits()


@pytest.fixture(scope="session")
def pex_scie_base(tmp_path_factory: pytest.TempPathFactory) -> Path:
	"""Shared scie extraction cache for PEX deploy tests.

	The scie launcher extracts its embedded Python interpreter (~440 MB)
	on first invocation.  Sharing the cache across all PEX tests within
	a worker avoids duplicating this extraction per test.
	"""
	return tmp_path_factory.mktemp("pex_scie_base")


@dataclass(frozen=True)
class StdlibPackage:
	"""Signed stdlib .dmp for consumer-path tests."""
	pkg_root: Path      # directory containing std/<version>/std.dmp
	trust_path: Path    # trust store JSON
	version: str        # e.g. "0.27.120"
	stdlib_root: Path   # source stdlib dir (for --stdlib-root passthrough guard)


@pytest.fixture(scope="session")
def stdlib_package(tmp_path_factory: pytest.TempPathFactory) -> StdlibPackage:
	"""Build a signed stdlib package once per session.

	This exercises the same code path as the PEX/deploy pipeline:
	consumers use --package-root + --dep std@VERSION instead of
	--stdlib-root.  The type table state differs from source compilation
	and can expose bugs in has_drop, destructor_fns, and scope drop
	emission that are invisible to --stdlib-root tests.
	"""
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.versions import DRIFTC_VERSION

	tmp = tmp_path_factory.mktemp("stdlib_pkg")
	repo_root = Path(__file__).resolve().parents[3]
	stdlib_dir = repo_root / "stdlib"
	version = DRIFTC_VERSION

	# Build stdlib .dmp (same as deploy pipeline).
	dmp_path = tmp / "std.dmp"
	empty_stdlib = tmp / "_empty_stdlib"
	empty_stdlib.mkdir()
	sources = sorted(str(p) for p in stdlib_dir.rglob("*.drift"))
	assert sources, "no .drift files found under stdlib/"

	env = dict(os.environ)
	env["PYTHONPATH"] = str(repo_root)
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--dev",
		"--stdlib-root", str(empty_stdlib),
		"-M", "stdlib",
	] + sources + [
		"--package-id", "std",
		"--package-version", version,
		"--package-target", "drift-dev",
		"--emit-package", str(dmp_path),
		"--test-build-only",
	]
	res = subprocess.run(cmd, env=env, cwd=str(repo_root),
		capture_output=True, text=True, timeout=120)
	assert res.returncode == 0, f"stdlib package build failed: {res.stderr[:500]}"
	assert dmp_path.exists(), "stdlib .dmp not produced"

	# Sign.
	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = dmp_path.read_bytes()

	# Standard layout: <root>/std/<version>/std.dmp
	pkg_root = tmp / "libs"
	dest = pkg_root / "std" / version
	dest.mkdir(parents=True)
	import shutil
	shutil.copy2(str(dmp_path), str(dest / "std.dmp"))
	(dest / "std.sig").write_text(json.dumps({
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{sha256(pkg_bytes).hexdigest()}",
		"signatures": [{"algo": "ed25519", "kid": kid,
			"sig": base64.b64encode(priv.sign(pkg_bytes)).decode("ascii"),
			"pubkey": pub_b64}],
	}, separators=(",", ":"), sort_keys=True))

	# Trust store.
	trust_path = tmp / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"std.*": [kid], "lang.*": [kid]},
		"revoked": [],
	}))

	return StdlibPackage(
		pkg_root=pkg_root,
		trust_path=trust_path,
		version=version,
		stdlib_root=stdlib_dir,
	)
