# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Shared helpers for package-consumer driver and memcheck tests.

Provides _build_signed_stdlib() for tests that need a signed stdlib .dmp
without using the session-scoped conftest.py fixture (e.g., per-test
tmp_path isolation, or memcheck tests that run outside the driver suite).
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STDLIB_DIR = ROOT / "stdlib"
STD_VERSION = "0.0.0-test"


def _build_signed_stdlib(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
	"""Build signed stdlib package.

	Returns (pkg_root, trust_path, core_trust_path, empty_stdlib).
	"""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	stdlib_files = sorted(str(p) for p in STDLIB_DIR.rglob("*.drift"))
	assert stdlib_files, "no stdlib .drift files"

	pkg_dir = tmp_path / "libs"
	pkg_dir.mkdir(parents=True, exist_ok=True)
	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir(parents=True, exist_ok=True)

	std_pkg_path = tmp_path / "std_build" / "std.dmp"
	std_pkg_path.parent.mkdir(parents=True, exist_ok=True)
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "--dev", "-M", str(STDLIB_DIR),
		 "--stdlib-root", str(empty_stdlib),
		 *stdlib_files,
		 "--package-id", "std",
		 "--package-version", STD_VERSION,
		 "--package-target", "test-target",
		 "--emit-package", str(std_pkg_path),
		 "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"stdlib build failed: {res.stderr[:500]}"

	std_dest = pkg_dir / "std" / STD_VERSION
	std_dest.mkdir(parents=True)
	shutil.copy2(str(std_pkg_path), str(std_dest / "std.dmp"))

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = (std_dest / "std.dmp").read_bytes()

	(std_dest / "std.sig").write_text(json.dumps({
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{sha256(pkg_bytes).hexdigest()}",
		"signatures": [{"algo": "ed25519", "kid": kid,
			"sig": base64.b64encode(priv.sign(pkg_bytes)).decode("ascii"),
			"pubkey": pub_b64}],
	}, separators=(",", ":"), sort_keys=True))

	core_trust_path = tmp_path / "core_trust.json"
	core_trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"std.*": [kid], "lang.*": [kid], "drift.*": [kid]},
		"revoked": [],
	}, separators=(",", ":"), sort_keys=True))

	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"std.*": [kid]}, "revoked": [],
	}))

	return pkg_dir, trust_path, core_trust_path, empty_stdlib
