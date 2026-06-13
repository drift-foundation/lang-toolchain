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

from lang.codegen.llvm.test_utils import sanitizer_timeout

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
	from lang.drift.crypto import compute_ed25519_kid
	import base64
	import json
	import shutil
	from hashlib import sha256

	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir()
	stdlib_dir = Path(str(stdlib))
	pkg_root = tmp_path / "libs"
	pkg_root.mkdir()

	_TEST_SCI = "sha256:" + ("0" * 64)

	# Build std package with SCI stamp.
	stdlib_files = sorted(str(p) for p in stdlib_dir.rglob("*.drift"))
	std_pkg = tmp_path / "std.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "-M", str(stdlib_dir), "--stdlib-root", str(empty_stdlib),
		 *stdlib_files,
		 "--package-id", "std", "--package-version", "0.0.0-test",
		 "--package-target", "test-target",
		 "--source-content-id", _TEST_SCI,
		 "--emit-package", str(std_pkg), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180),
	)
	assert res.returncode == 0, f"std build: {res.stderr[:300]}"

	# v1: sign + stage via shared helper (single kid plays author
	# + certifier for both std.* and mylib.*).  The merged trust
	# dict accumulates each package's namespace coverage so a
	# single trust file authorises every package the consumer
	# loads.
	from lang.tests.driver.pkg_test_helpers import sign_v1_pkg_into_root
	core_trust_obj = {"format": "drift-trust", "version": 1, "keys": {}, "namespaces": {}, "revoked": []}
	std_info = sign_v1_pkg_into_root(
		pkg_path=std_pkg, package_id="std", package_version="0.0.0-test",
		namespace_glob="std.*",
		# Stdlib also exposes `lang.*` and `drift.*` modules
		# (e.g. lang.atomic).  The author claim must cover every
		# namespace the package exposes or v1 verify rejects loads
		# of uncovered modules.
		extra_namespaces=("lang.*", "drift.*"),
		target="test-target",
		dest_pkg_root=pkg_root, merge_into_trust=core_trust_obj,
	)
	kid = std_info["kid"]
	# Stdlib's bootstrap key also covers `lang.*` and `drift.*`.
	for ns in ("lang.*", "drift.*"):
		core_trust_obj["namespaces"][ns] = {
			"authors": [kid], "certifiers": [kid],
		}
	core_trust = tmp_path / "core_trust.json"
	core_trust.write_text(json.dumps(core_trust_obj, separators=(",", ":"), sort_keys=True))

	# Build lib that uses std.log (which internally uses Handle<Byte>
	# in _enqueue_with_policy -- the exact function that triggers
	# the Copy proof failure when TypeVars are deduplicated
	# incorrectly).
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
		 "--source-content-id", _TEST_SCI,
		 "--emit-package", str(lib_pkg), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180),
	)
	assert res.returncode == 0, f"mylib build: {res.stderr[:300]}"

	# Sign mylib via the same kid; merge into a fresh trust file
	# that covers both std.* and mylib.*.
	trust_obj = {"format": "drift-trust", "version": 1, "keys": {}, "namespaces": {}, "revoked": []}
	# Reuse the same bootstrap kid for mylib by using the same priv
	# seed -- but the helper generates a new key each call.  We
	# accept different kids here because the consumer trust file
	# below merges BOTH.
	mylib_info = sign_v1_pkg_into_root(
		pkg_path=lib_pkg, package_id="mylib", package_version="0.1.0",
		namespace_glob="mylib.*", target="test-target",
		dest_pkg_root=pkg_root, merge_into_trust=trust_obj,
	)
	# Authorise the std kid for std.* in the consumer's trust file.
	trust_obj["keys"][kid] = {"algo": "ed25519", "pubkey": std_info["pub_b64"]}
	trust_obj["namespaces"]["std.*"] = {"authors": [kid], "certifiers": [kid]}
	trust = tmp_path / "trust.json"
	trust.write_text(json.dumps(trust_obj, separators=(",", ":"), sort_keys=True))

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
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180),
	)
	assert res.returncode == 0, (
		f"consumer compilation failed — Copy proof for Handle<Byte> likely broken "
		f"by TypeVar dedup failure in linker:\n{res.stderr[:500]}"
	)
