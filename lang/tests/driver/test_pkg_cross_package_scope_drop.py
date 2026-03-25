# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: owned local must be dropped on all return paths when the
controlling match scrutinee comes from a cross-package call.

Proven discriminator:
  - Source-built: 0 leaks
  - Consumer-built: 16-byte Arc leak (missing scope drop on Err arm)

This test builds a package, consumes it, links, runs under Valgrind, and
asserts 0 definitely-lost bytes.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root
from lang.language_runtime import build_runtime_archive, runtime_archive_path, runtime_archive_variant

ROOT = Path(__file__).resolve().parents[3]

_skip_no_valgrind = pytest.mark.skipif(
	shutil.which("valgrind") is None,
	reason="valgrind not available",
)

LIB_SOURCE = """\
module mylib;

import std.core as core;
import std.concurrent as conc;
import std.sync as sync;

export { create, use_handle, Handle };

pub struct Handle {
\tpub flag: conc.Arc<sync.AtomicBool>,
\tpub value: Int
}

pub fn create() -> core.Result<Handle, String> {
\tval a = conc.arc(sync.atomic_bool(false));
\treturn core.Result::Ok(Handle(flag = move a, value = 42));
}

pub fn use_handle(h: &mut Handle) -> core.Result<Int, String> {
\treturn core.Result::Ok(h.value);
}
"""

CONSUMER_SOURCE = """\
module consumer;

import std.core as core;
import mylib;

fn run() -> Int {
\tmatch mylib.create() {
\t\tcore.Result::Err(_) => { return 1; },
\t\tcore.Result::Ok(h) => {
\t\t\tvar handle = move h;
\t\t\tmatch mylib.use_handle(&mut handle) {
\t\t\t\tcore.Result::Err(_) => {
\t\t\t\t\treturn 2;
\t\t\t\t},
\t\t\t\tcore.Result::Ok(v) => {
\t\t\t\t\tif v != 42 { return 3; }
\t\t\t\t\treturn 0;
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}

pub fn main() nothrow -> Int {
\treturn try run() catch { 99 };
}
"""


def _build_signed_package(tmp_path: Path) -> tuple[Path, Path]:
	"""Build, sign, and set up a package. Returns (pkg_root, trust_path)."""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	lib_dir = tmp_path / "lib_src"
	lib_dir.mkdir()
	(lib_dir / "mylib.drift").write_text(LIB_SOURCE)

	pkg_path = tmp_path / "mylib.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "-M", str(lib_dir), str(lib_dir / "mylib.drift"),
		 "--stdlib-root", str(stdlib_root()),
		 "--target-word-bits", "64",
		 "--package-id", "mylib", "--package-version", "0.1.0",
		 "--package-target", "test-target",
		 "--emit-package", str(pkg_path), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"lib build failed: {res.stderr[:300]}"

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = pkg_path.read_bytes()

	pkg_root = tmp_path / "libs" / "mylib" / "0.1.0"
	pkg_root.mkdir(parents=True)
	shutil.copy2(str(pkg_path), str(pkg_root / "mylib.dmp"))
	(pkg_root / "mylib.sig").write_text(json.dumps({
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{sha256(pkg_bytes).hexdigest()}",
		"signatures": [{"algo": "ed25519", "kid": kid,
			"sig": base64.b64encode(priv.sign(pkg_bytes)).decode("ascii"),
			"pubkey": pub_b64}],
	}, separators=(",", ":"), sort_keys=True))
	(tmp_path / "trust.json").write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"mylib.*": [kid]}, "revoked": [],
	}))
	return tmp_path / "libs", tmp_path / "trust.json"


@_skip_no_valgrind
def test_cross_package_scope_drop_no_leak(tmp_path: Path) -> None:
	"""Package-consumer path must not leak owned locals on any return arm."""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	pkg_root, trust_path = _build_signed_package(tmp_path)

	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text(CONSUMER_SOURCE)

	out_bin = tmp_path / "consumer_bin"
	# Compile and link
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 str(consumer_dir / "consumer.drift"),
		 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
		 "--package-root", str(pkg_root), "--dep", "mylib@0.1.0",
		 "--trust-store", str(trust_path),
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:500]}"
	assert out_bin.exists(), "binary not produced"

	# Run under Valgrind
	vg = subprocess.run(
		["valgrind", "--leak-check=full", "--error-exitcode=42", str(out_bin)],
		capture_output=True, text=True, timeout=30,
	)
	# Parse Valgrind output
	no_leaks = "no leaks are possible" in vg.stderr or "All heap blocks were freed" in vg.stderr
	lost_match = re.search(r"definitely lost: (\d+) bytes", vg.stderr)
	lost_bytes = int(lost_match.group(1)) if lost_match else (0 if no_leaks else -1)

	assert lost_bytes == 0, (
		f"Valgrind found {lost_bytes} bytes definitely lost on the package-consumer path. "
		f"Scope drops for owned locals may be missing on a return arm.\n"
		f"Valgrind stderr:\n{vg.stderr[-500:]}"
	)
