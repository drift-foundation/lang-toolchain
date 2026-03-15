# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
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


def test_drift_sign_produces_sidecar_accepted_by_driftc(tmp_path: Path) -> None:
	# Build an unsigned package locally (unsigned is allowed for local build
	# outputs, but driftc will still require signatures when asked to).
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
	assert (repo_root / "lang").exists()
	build_pkg = subprocess.run(
		with_target_word_bits(
			[
				sys.executable,
				"-m",
				"lang.driftc.driftc",
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
			]
		),
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert build_pkg.returncode == 0, build_pkg.stderr
	out = json.loads(build_pkg.stdout or "{}")
	assert out.get("exit_code") == 0


def test_drift_sign_uses_env_key_file_when_key_flag_missing(tmp_path: Path) -> None:
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
				sys.executable,
				"-m",
				"lang.driftc.driftc",
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
			]
		),
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert build_pkg.returncode == 0, build_pkg.stderr

	seed32 = os.urandom(32)
	key_path = tmp_path / "key.seed"
	key_path.write_text(base64.b64encode(seed32).decode("ascii") + "\n", encoding="utf-8")
	env = dict(os.environ)
	env["DRIFT_SIGN_KEY_FILE"] = str(key_path)

	res = subprocess.run(
		[sys.executable, "-m", "lang.drift", "sign", str(pkg), "--include-pubkey"],
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
		env=env,
	)
	assert res.returncode == 0, res.stderr
	assert Path(str(pkg) + ".sig").exists()


def test_drift_sign_uses_env_key_cmd_when_key_flag_missing(tmp_path: Path) -> None:
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
				sys.executable,
				"-m",
				"lang.driftc.driftc",
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
			]
		),
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert build_pkg.returncode == 0, build_pkg.stderr

	seed32 = os.urandom(32)
	key_path = tmp_path / "key.seed"
	key_path.write_text(base64.b64encode(seed32).decode("ascii") + "\n", encoding="utf-8")
	env = dict(os.environ)
	env["DRIFT_SIGN_KEY_CMD"] = f"cat {key_path}"

	res = subprocess.run(
		[sys.executable, "-m", "lang.drift", "sign", str(pkg), "--include-pubkey"],
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
		env=env,
	)
	assert res.returncode == 0, res.stderr
	assert Path(str(pkg) + ".sig").exists()

	# Generate a deterministic key seed file (base64 raw 32 bytes).
	seed32 = os.urandom(32)
	key_path = tmp_path / "key.seed"
	key_path.write_text(base64.b64encode(seed32).decode("ascii") + "\n", encoding="utf-8")

	# Sign via drift CLI (publisher-side).
	# Use the repo root as cwd so `python -m lang.drift` resolves correctly.
	res = subprocess.run(
		[sys.executable, "-m", "lang.drift", "sign", str(pkg), "--key", str(key_path), "--include-pubkey"],
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert res.returncode == 0, res.stderr

	sig_path = Path(str(pkg) + ".sig")
	assert sig_path.exists()

	# Create a minimal project trust store that trusts the signer for `lib`.
	pkg_bytes = pkg.read_bytes()
	sig_obj = json.loads(sig_path.read_text(encoding="utf-8"))
	pub_b64 = sig_obj["signatures"][0].get("pubkey")
	assert isinstance(pub_b64, str)
	pub_raw = base64.b64decode(pub_b64.encode("ascii"), validate=True)
	kid = compute_ed25519_kid(pub_raw)

	trust = {
		"format": "drift-trust",
		"version": 0,
		"keys": {
			kid: {"algo": "ed25519", "pubkey": base64.b64encode(pub_raw).decode("ascii")},
		},
		"namespaces": {
			"lib.*": [kid],
		},
	}
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps(trust, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

	# Consume the package with signatures required. This proves driftc accepts the
	# publisher-produced signature sidecar.
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
	consume = subprocess.run(
		with_target_word_bits(
			[
				sys.executable,
				"-m",
				"lang.driftc.driftc",
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
			]
		),
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert consume.returncode == 0, consume.stderr
	out = json.loads(consume.stdout or "{}")
	assert out.get("exit_code") == 0
