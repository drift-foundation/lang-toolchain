# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from lang2.drift.crypto import compute_ed25519_kid


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_drift_trust_revoke_blocks_package_consumption(tmp_path: Path) -> None:
	# Build a tiny module package we can sign and then consume from a separate
	# source module.
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	repo_root = Path.cwd()

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

	seed32 = os.urandom(32)
	key_path = tmp_path / "key.seed"
	key_path.write_text(base64.b64encode(seed32).decode("ascii") + "\n", encoding="utf-8")

	# Publisher-side signing.
	sign = subprocess.run(
		[
			sys.executable,
			"-m",
			"lang2.drift",
			"sign",
			str(pkg),
			"--key",
			str(key_path),
			"--include-pubkey",
		],
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert sign.returncode == 0, sign.stderr
	sidecar = Path(str(pkg) + ".sig")
	sig_obj = json.loads(sidecar.read_text(encoding="utf-8"))
	pub_b64 = sig_obj["signatures"][0].get("pubkey")
	assert isinstance(pub_b64, str)
	pub_raw = base64.b64decode(pub_b64.encode("ascii"), validate=True)
	kid = compute_ed25519_kid(pub_raw)

	# Create trust store using drift tooling.
	trust_path = tmp_path / "drift" / "trust.json"
	add_key = subprocess.run(
		[
			sys.executable,
			"-m",
			"lang2.drift",
			"trust",
			"add-key",
			"--trust-store",
			str(trust_path),
			"--namespace",
			"lib.*",
			"--pubkey",
			pub_b64,
		],
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert add_key.returncode == 0, add_key.stderr

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)

	# Verify package consumption succeeds before revocation.
	def _consume() -> dict:
		res = subprocess.run(
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
		assert res.returncode in (0, 1), res.stderr
		return json.loads(res.stdout or "{}")

	ok = _consume()
	assert ok.get("exit_code") == 0

	# Revoke and verify driftc rejects the package.
	revoke = subprocess.run(
		[
			sys.executable,
			"-m",
			"lang2.drift",
			"trust",
			"revoke",
			"--trust-store",
			str(trust_path),
			"--kid",
			kid,
			"--reason",
			"test",
		],
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert revoke.returncode == 0, revoke.stderr

	out2 = _consume()
	assert out2.get("exit_code") == 1
	diags = out2.get("diagnostics") or []
	assert any("revoked" in str(d.get("message", "")).lower() for d in diags), diags
