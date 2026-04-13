# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: hidden lambda callback target from stdlib generic instantiation
must be included in package payload and resolved by consumers.

Self-contained test: builds a library package from source, signs it with an
ephemeral key, and consumes it through the full package-consumer path.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from lang.codegen.llvm.test_utils import sanitizer_timeout

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

LIB_SOURCE = """\
module mylib;

import std.core as core;
import std.concurrent as conc;

export { run_in_background };

pub fn run_in_background() nothrow -> conc.VirtualThread<Int> {
\tval cb = core.callback0(|| nothrow => { return 42; });
\treturn conc.spawn_cb(move cb);
}
"""

CONSUMER_SOURCE = """\
module consumer;

import mylib;

pub fn main() nothrow -> Int {
\tmylib.run_in_background();
\treturn 0;
}
"""


def test_pkg_hidden_lambda_construct_iface_resolved(tmp_path: Path) -> None:
	"""Package function calling conc.spawn_cb must include hidden lambda in payload."""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	# Step 1: Build library package
	lib_dir = tmp_path / "lib_src"
	lib_dir.mkdir()
	(lib_dir / "mylib.drift").write_text(LIB_SOURCE)

	pkg_path = tmp_path / "mylib.dmp"
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		"-M", str(lib_dir),
		str(lib_dir / "mylib.drift"),
		"--stdlib-root", str(stdlib),
		"--target-word-bits", "64",
		"--package-id", "mylib",
		"--package-version", "0.1.0",
		"--package-target", "test-target",
		"--emit-package", str(pkg_path),
		"--test-build-only",
	]
	res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120))
	assert res.returncode == 0, f"library package build failed: {res.stderr[:500]}"

	# Step 2: Sign with ephemeral key
	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	pkg_sha_hex = sha256(pkg_bytes).hexdigest()

	# Step 3: Set up package root
	pkg_root = tmp_path / "libs" / "mylib" / "0.1.0"
	pkg_root.mkdir(parents=True)
	import shutil
	shutil.copy2(str(pkg_path), str(pkg_root / "mylib.dmp"))

	sig_sidecar = pkg_root / "mylib.sig"
	sig_obj = {
		"format": "dmir-pkg-sig",
		"version": 0,
		"package_sha256": f"sha256:{pkg_sha_hex}",
		"signatures": [{"algo": "ed25519", "kid": kid, "sig": base64.b64encode(sig_raw).decode("ascii"), "pubkey": pub_b64}],
	}
	sig_sidecar.write_text(json.dumps(sig_obj, separators=(",", ":"), sort_keys=True))

	# Step 4: Write trust store
	trust_path = tmp_path / "trust.json"
	trust_obj = {
		"format": "drift-trust",
		"version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"mylib.*": [kid]},
		"revoked": [],
	}
	trust_path.write_text(json.dumps(trust_obj))

	# Step 5: Compile consumer against the signed library package
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text(CONSUMER_SOURCE)

	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(consumer_dir / "consumer.drift"),
		"--stdlib-root", str(stdlib),
		"--target-word-bits", "64",
		"--package-root", str(tmp_path / "libs"),
		"--dep", "mylib@0.1.0",
		"--trust-store", str(trust_path),
		"--entry", "consumer::main",
		"--emit-ir", str(tmp_path / "consumer.ll"),
		"--test-build-only",
		"--json",
	]
	res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120))
	stdout = res.stdout.strip()
	if stdout:
		try:
			diag = json.loads(stdout)
			msgs = [d.get("message", "")[:200] for d in diag.get("diagnostics", [])]
		except json.JSONDecodeError:
			msgs = [stdout[:200]]
	else:
		msgs = []
	assert res.returncode == 0, f"consumer compilation failed — hidden lambda callback target not resolved: {msgs}"
