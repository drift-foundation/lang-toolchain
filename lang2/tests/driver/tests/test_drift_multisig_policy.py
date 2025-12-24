# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lang2.drift.crypto import compute_ed25519_kid


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_driftc_accepts_sidecar_when_any_signature_valid_and_allowed(tmp_path: Path) -> None:
	"""
	Lock policy: if a sidecar contains multiple signatures, driftc accepts the
	package if any signature is valid and trusted/allowed, even if another
	signature is revoked.
	"""
	repo_root = Path.cwd()

	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

pub fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	build_pkg = subprocess.run(
		[
			sys.executable,
			"-m",
			"lang2.driftc.driftc",
			"-M",
			str(tmp_path),
			str(tmp_path / "lib" / "lib.drift"),
			"--package-id",
			"test.pkg",
			"--package-version",
			"0.0.0",
			"--package-target",
			"test-target",
			"--emit-package",
			str(pkg),
			"--json",
		],
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert build_pkg.returncode == 0, build_pkg.stderr
	out = json.loads(build_pkg.stdout or "{}")
	assert out.get("exit_code") == 0

	# Prepare two signing keys.
	seed1 = os.urandom(32)
	seed2 = os.urandom(32)
	key1 = tmp_path / "k1.seed"
	key2 = tmp_path / "k2.seed"
	key1.write_text(base64.b64encode(seed1).decode("ascii") + "\n", encoding="utf-8")
	key2.write_text(base64.b64encode(seed2).decode("ascii") + "\n", encoding="utf-8")

	# Compute kids/public keys (for trust store).
	priv1 = Ed25519PrivateKey.from_private_bytes(seed1)
	priv2 = Ed25519PrivateKey.from_private_bytes(seed2)
	pub1_raw = priv1.public_key().public_bytes_raw()
	pub2_raw = priv2.public_key().public_bytes_raw()
	kid1 = compute_ed25519_kid(pub1_raw)
	kid2 = compute_ed25519_kid(pub2_raw)

	# Sign once, then append a second signature.
	sign1 = subprocess.run(
		[sys.executable, "-m", "lang2.drift", "sign", str(pkg), "--key", str(key1)],
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert sign1.returncode == 0, sign1.stderr
	sign2 = subprocess.run(
		[sys.executable, "-m", "lang2.drift", "sign", str(pkg), "--key", str(key2), "--add-signature"],
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert sign2.returncode == 0, sign2.stderr

	# Trust both keys for lib.*, but revoke one of them.
	trust = {
		"format": "drift-trust",
		"version": 0,
		"keys": {
			kid1: {"algo": "ed25519", "pubkey": base64.b64encode(pub1_raw).decode("ascii")},
			kid2: {"algo": "ed25519", "pubkey": base64.b64encode(pub2_raw).decode("ascii")},
		},
		"namespaces": {"lib.*": [kid1, kid2]},
		"revoked": {kid1: {"reason": "test"}},
	}
	trust_path = tmp_path / "drift" / "trust.json"
	trust_path.parent.mkdir(parents=True, exist_ok=True)
	trust_path.write_text(json.dumps(trust, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

	_write_file(
		tmp_path / "main.drift",
		"""
module main

import lib as lib

fn main() returns Int {
	return lib.add(40, 2)
}
""".lstrip(),
	)
	consume = subprocess.run(
		[
			sys.executable,
			"-m",
			"lang2.driftc.driftc",
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--require-signatures",
			"--trust-store",
			str(trust_path),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
			"--json",
		],
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert consume.returncode == 0, consume.stderr
	payload = json.loads(consume.stdout or "{}")
	assert payload.get("exit_code") == 0
