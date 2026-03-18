# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
End-to-end trust workflow tests using drift init + drift trust <profile>.

These exercise the full publisher → consumer trust pipeline through
the CLI: build package, sign, create profile via drift init, trust
profile via drift trust, compile consumer, revoke, verify rejection.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from lang.drift.crypto import compute_ed25519_kid
from lang.tests.driver.driver_cli_helpers import with_target_word_bits


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_drift_trust_revoke_blocks_package_consumption(tmp_path: Path) -> None:
	"""Full round-trip: init → sign → trust → consume → revoke → reject."""
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) -> Int {
	return a + b;
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	repo_root = Path.cwd()

	build_pkg = subprocess.run(
		with_target_word_bits(
			[
				sys.executable, "-m", "lang.driftc.driftc",
				"-M", str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				"--package-id", "test.pkg",
				"--package-version", "0.0.0",
				"--package-target", "test-target",
				"--emit-package", str(pkg),
				"--json",
			]
		),
		cwd=str(repo_root), check=False, capture_output=True, text=True,
	)
	assert build_pkg.returncode == 0, build_pkg.stderr

	seed32 = os.urandom(32)
	key_path = tmp_path / "key.seed"
	key_path.write_text(base64.b64encode(seed32).decode("ascii") + "\n", encoding="utf-8")

	# Sign the package.
	sign = subprocess.run(
		[
			sys.executable, "-m", "lang.drift",
			"sign", str(pkg), "--key", str(key_path), "--include-pubkey",
		],
		cwd=str(repo_root), check=False, capture_output=True, text=True,
	)
	assert sign.returncode == 0, sign.stderr

	# Create publisher profile via drift init.
	profile_path = tmp_path / "test.author-profile"
	init_cmd = subprocess.run(
		[
			sys.executable, "-m", "lang.drift",
			"init",
			"--key", str(key_path),
			"--name", "Test Publisher",
			"--namespace", "lib.*",
			"--out", str(profile_path),
			"--yes",
		],
		cwd=str(repo_root), check=False, capture_output=True, text=True,
	)
	assert init_cmd.returncode == 0, init_cmd.stderr
	assert profile_path.exists()

	# Consumer trusts the profile.
	trust_path = tmp_path / "drift" / "trust.json"
	trust_cmd = subprocess.run(
		[
			sys.executable, "-m", "lang.drift",
			"trust", str(profile_path),
			"--trust-store", str(trust_path),
			"--yes",
		],
		cwd=str(repo_root), check=False, capture_output=True, text=True,
	)
	assert trust_cmd.returncode == 0, trust_cmd.stderr

	# Read kid from trust store.
	trust_obj = json.loads(trust_path.read_text(encoding="utf-8"))
	kids = list(trust_obj.get("keys", {}).keys())
	assert len(kids) == 1
	kid = kids[0]

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)

	def _consume() -> dict:
		res = subprocess.run(
			with_target_word_bits(
				[
					sys.executable, "-m", "lang.driftc.driftc",
					"-M", str(tmp_path),
					"--package-root", str(tmp_path),
					"--dep", "test.pkg@0.0.0",
					"--require-signatures",
					"--trust-store", str(trust_path),
					str(tmp_path / "main.drift"),
					"--emit-ir", str(tmp_path / "out.ll"),
					"--json",
				]
			),
			cwd=str(repo_root), check=False, capture_output=True, text=True,
		)
		assert res.returncode in (0, 1), res.stderr
		return json.loads(res.stdout or "{}")

	ok = _consume()
	assert ok.get("exit_code") == 0

	# Revoke and verify driftc rejects the package.
	revoke = subprocess.run(
		[
			sys.executable, "-m", "lang.drift",
			"trust", "revoke",
			"--trust-store", str(trust_path),
			"--kid", kid,
			"--reason", "test",
		],
		cwd=str(repo_root), check=False, capture_output=True, text=True,
	)
	assert revoke.returncode == 0, revoke.stderr

	out2 = _consume()
	assert out2.get("exit_code") == 1
	diags = out2.get("diagnostics") or []
	assert any("revoked" in str(d.get("message", "")).lower() for d in diags), diags


def test_drift_init_then_trust_profile_adds_key_to_namespace(tmp_path: Path) -> None:
	"""drift init + drift trust <profile> round-trip adds key to trust store."""
	repo_root = Path.cwd()
	key_path = tmp_path / "key.seed"
	key_path.write_text(base64.b64encode(os.urandom(32)).decode("ascii") + "\n", encoding="utf-8")

	# Create profile.
	profile_path = tmp_path / "publisher.author-profile"
	init_cmd = subprocess.run(
		[
			sys.executable, "-m", "lang.drift",
			"init",
			"--key", str(key_path),
			"--name", "Test Publisher",
			"--org", "TestOrg",
			"--namespace", "test.pkg.*",
			"--out", str(profile_path),
			"--yes",
		],
		cwd=str(repo_root), check=False, capture_output=True, text=True,
	)
	assert init_cmd.returncode == 0, init_cmd.stderr
	assert profile_path.exists()

	# Verify profile is valid JSON with expected format.
	profile_obj = json.loads(profile_path.read_text())
	assert profile_obj["format"] == "author-profile"
	assert profile_obj["publisher"]["name"] == "Test Publisher"
	assert profile_obj["publisher"]["org"] == "TestOrg"
	assert profile_obj["namespaces"] == ["test.pkg.*"]
	kid = profile_obj["key"]["kid"]

	# Trust the profile.
	trust_path = tmp_path / "drift" / "trust.json"
	trust_cmd = subprocess.run(
		[
			sys.executable, "-m", "lang.drift",
			"trust", str(profile_path),
			"--trust-store", str(trust_path),
			"--yes",
		],
		cwd=str(repo_root), check=False, capture_output=True, text=True,
	)
	assert trust_cmd.returncode == 0, trust_cmd.stderr

	trust_obj = json.loads(trust_path.read_text(encoding="utf-8"))
	assert kid in trust_obj.get("keys", {})
	assert kid in (trust_obj.get("namespaces", {}).get("test.pkg.*") or [])
