# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: consumer-side drop for nested struct containing Arc.

Proven discriminator for the web.rest certification leak:
  - Package exports Outer { inner: Inner } where Inner { arc: Arc<AtomicBool> }
  - Package function returns Outer
  - Consumer calls package function, binds result to owned local
  - Consumer must emit DropValue for the owned local on scope exit
  - Bug: _needs_runtime_drop returns False for Outer because copy_status
    or has_drop fails for cross-package nested struct with Arc field

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

ROOT = Path(__file__).resolve().parents[3]


# Minimal package: Inner wraps Arc<AtomicBool>, Outer wraps Inner.
# Function create_outer() returns Outer by value.
LIB_SOURCE = """\
module mylib;

import std.core as core;
import std.concurrent as conc;
import std.sync as sync;

export { create_outer, Outer, Inner };

pub struct Inner {
\tpub stopped: conc.Arc<sync.AtomicBool>,
\tpub value: Int
}

pub struct Outer {
\tpub inner: Inner,
\tpub tag: Int
}

pub fn create_outer() nothrow -> Outer {
\tval a = conc.arc(sync.atomic_bool(false));
\treturn Outer(inner = Inner(stopped = move a, value = 42), tag = 1);
}
"""

# Consumer: call create_outer(), bind to owned local, let scope end.
CONSUMER_SOURCE = """\
module consumer;

import std.core as core;
import mylib;

pub fn main() nothrow -> Int {
\tvar o = mylib.create_outer();
\tval result = o.inner.value;
\tif result != 42 { return 1; }
\treturn 0;
}
"""


def _build_signed_package(tmp_path: Path) -> tuple[Path, Path]:
	"""Build, sign, and set up a package. Returns (pkg_root, trust_path)."""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.drift.crypto import compute_ed25519_kid

	lib_dir = tmp_path / "lib_src"
	lib_dir.mkdir()
	(lib_dir / "mylib.drift").write_text(LIB_SOURCE)

	from lang.tests.driver.pkg_test_helpers import emit_v1_sidecars_inline
	_TEST_SCI = "sha256:" + ("0" * 64)
	pkg_path = tmp_path / "mylib.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "-M", str(lib_dir), str(lib_dir / "mylib.drift"),
		 "--stdlib-root", str(stdlib_root()),
		 "--target-word-bits", "64",
		 "--package-id", "mylib", "--package-version", "0.1.0",
		 "--package-target", "test-target",
		 "--source-content-id", _TEST_SCI,
		 "--emit-package", str(pkg_path), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"lib build failed: {res.stderr[:300]}"

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	pkg_root = tmp_path / "libs" / "mylib" / "0.1.0"
	pkg_root.mkdir(parents=True)
	shutil.copy2(str(pkg_path), str(pkg_root / "mylib.dmp"))
	# v1 author + cert claim sidecars next to the staged .dmp.
	emit_v1_sidecars_inline(
		pkg_root / "mylib.dmp",
		package_id="mylib", package_version="0.1.0",
		priv=priv, namespaces=["mylib.*"],
	)
	(tmp_path / "trust.json").write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"mylib.*": {"authors": [kid], "certifiers": [kid]}},
		"revoked": [],
	}))
	return tmp_path / "libs", tmp_path / "trust.json"



def test_nested_struct_arc_drop_no_leak(tmp_path: Path) -> None:
	"""Consumer-owned nested struct with Arc must be dropped on scope exit."""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	pkg_root, trust_path = _build_signed_package(tmp_path)

	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text(CONSUMER_SOURCE)

	out_bin = tmp_path / "consumer_bin"
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
	no_leaks = "no leaks are possible" in vg.stderr or "All heap blocks were freed" in vg.stderr
	lost_match = re.search(r"definitely lost: (\d+) bytes", vg.stderr)
	lost_bytes = int(lost_match.group(1)) if lost_match else (0 if no_leaks else -1)

	assert lost_bytes == 0, (
		f"Valgrind found {lost_bytes} bytes definitely lost. "
		f"Consumer-owned nested struct with Arc field must be dropped.\n"
		f"Valgrind stderr:\n{vg.stderr[-500:]}"
	)
