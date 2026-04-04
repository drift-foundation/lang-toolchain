# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: cross-package type table linking must produce ONE TypeVar
per struct type parameter, even when two packages provide the same generic
struct (e.g., Handle<T> from std.sync).

Without deduplication in the linker, the second package creates a renamed
TypeVar (T→T0) which breaks trait solver unification for Copy proofs.

This is a focused regression for the linker fix in type_table_link_v0.py.
The broader driver-level coverage is in test_pkg_array_string_scope_drop.py.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def test_cross_package_typevar_dedup_for_copy_proof(tmp_path: Path) -> None:
	"""Two packages providing the same generic struct must share TypeVar identity.

	Build std as a package, build a lib that uses std.sync.Handle<T> against it,
	then compile a consumer against both packages. The consumer's borrow checker
	must recognize Handle<Byte> as Copy. If the linker created T0 instead of
	reusing T, the Copy proof fails and the borrow checker rejects valid code
	(use-after-move on a Copy type).
	"""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid
	import base64
	import json
	import shutil
	from hashlib import sha256

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir()
	stdlib_dir = Path(str(stdlib))

	def sign_and_stage(pkg_path, pkg_id, version):
		dest = pkg_root / pkg_id / version
		dest.mkdir(parents=True, exist_ok=True)
		shutil.copy2(str(pkg_path), str(dest / f"{pkg_id}.dmp"))
		pb = (dest / f"{pkg_id}.dmp").read_bytes()
		(dest / f"{pkg_id}.sig").write_text(json.dumps({
			"format": "dmir-pkg-sig", "version": 0,
			"package_sha256": f"sha256:{sha256(pb).hexdigest()}",
			"signatures": [{"algo": "ed25519", "kid": kid,
				"sig": base64.b64encode(priv.sign(pb)).decode("ascii"),
				"pubkey": pub_b64}],
		}, separators=(",", ":"), sort_keys=True))

	pkg_root = tmp_path / "libs"
	pkg_root.mkdir()

	# Build std package
	stdlib_files = sorted(str(p) for p in stdlib_dir.rglob("*.drift"))
	std_pkg = tmp_path / "std.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "-M", str(stdlib_dir), "--stdlib-root", str(empty_stdlib),
		 *stdlib_files,
		 "--package-id", "std", "--package-version", "0.0.0-test",
		 "--package-target", "test-target",
		 "--emit-package", str(std_pkg), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"std build: {res.stderr[:300]}"
	sign_and_stage(std_pkg, "std", "0.0.0-test")

	core_trust = tmp_path / "core_trust.json"
	core_trust.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"std.*": [kid], "lang.*": [kid], "drift.*": [kid]},
		"revoked": [],
	}))

	# Build lib that uses std.log (which internally uses Handle<Byte>
	# in _enqueue_with_policy — the exact function that triggers the
	# Copy proof failure when TypeVars are deduplicated incorrectly).
	lib_dir = tmp_path / "lib_src"
	lib_dir.mkdir()
	(lib_dir / "mylib.drift").write_text(
		"module mylib;\n"
		"import std.core as core;\n"
		"import std.io as io;\n"
		"export { greet };\n"
		"pub fn greet() nothrow -> Int { return 42; }\n"
	)
	lib_pkg = tmp_path / "mylib.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "-M", str(lib_dir), str(lib_dir / "mylib.drift"),
		 "--stdlib-root", str(empty_stdlib),
		 "--package-root", str(pkg_root),
		 "--dep", "std@0.0.0-test",
		 "--dev-core-trust-store", str(core_trust),
		 "--target-word-bits", "64",
		 "--package-id", "mylib", "--package-version", "0.1.0",
		 "--package-target", "test-target",
		 "--emit-package", str(lib_pkg), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"mylib build: {res.stderr[:300]}"
	sign_and_stage(lib_pkg, "mylib", "0.1.0")

	trust = tmp_path / "trust.json"
	trust.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"std.*": [kid], "mylib.*": [kid]},
		"revoked": [],
	}))

	# Consumer: loads both packages. The borrow checker runs on std.log
	# functions recompiled from HIR. If Handle<T>'s Copy impl has a
	# renamed TypeVar (T0), the proof fails and _enqueue_with_policy
	# is rejected for "use after move of 'h'" (Handle<Byte> is Copy).
	consumer_dir = tmp_path / "consumer"
	consumer_dir.mkdir()
	(consumer_dir / "main.drift").write_text(
		"module main;\n"
		"import std.core as core;\n"
		"import mylib;\n"
		"\n"
		"pub fn main() nothrow -> Int {\n"
		"\treturn mylib.greet();\n"
		"}\n"
	)

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 str(consumer_dir / "main.drift"),
		 "--stdlib-root", str(empty_stdlib),
		 "--package-root", str(pkg_root),
		 "--dep", "std@0.0.0-test", "--dep", "mylib@0.1.0",
		 "--trust-store", str(trust),
		 "--dev-core-trust-store", str(core_trust),
		 "--target-word-bits", "64",
		 "--entry", "main::main",
		 "--emit-ir", str(tmp_path / "out.ll")],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, (
		f"consumer compilation failed — Copy proof for Handle<Byte> likely broken "
		f"by TypeVar dedup failure in linker:\n{res.stderr[:500]}"
	)
