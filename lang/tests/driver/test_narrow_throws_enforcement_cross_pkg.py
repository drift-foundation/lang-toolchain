# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Case 4 of the six-case proof matrix: cross-package producer-side
negative enforcement.

A producer that declares `pub fn f() throws E` and whose body calls
an imported package's generic-throws function (no narrow declaration)
without a wrapping catch-all must be rejected at PRODUCER package
build time. This proves bad metadata cannot be emitted -- the producer
cannot launder an unbounded throws-surface from its dependency
through a narrow declaration.

Plan reference: work/cross-pkg-narrow-throws-metadata/plan.md, §4a-2.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _build_and_sign_pkg(
	tmp_path: Path,
	pkg_id: str,
	sources: dict[str, str],
	*,
	deps: list[tuple[str, str]] | None = None,
	trust_path_existing: Path | None = None,
	expect_success: bool = True,
) -> tuple[Path, Path, subprocess.CompletedProcess[str]]:
	"""Build (and try to sign) a producer package. Returns
	(pkg_root, trust_path, build_result). When expect_success is False,
	skip signing and just return the build_result for inspection."""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	lib_dir = tmp_path / f"{pkg_id}_src"
	lib_dir.mkdir(exist_ok=True)
	for fname, text in sources.items():
		(lib_dir / fname).write_text(text)
	pkg_root_dir = tmp_path / "pkg_root" / pkg_id / "0.1.0"
	pkg_root_dir.mkdir(parents=True, exist_ok=True)
	dmp = pkg_root_dir / f"{pkg_id}.dmp"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--dev", "-M", str(lib_dir), "--stdlib-root", str(ROOT / "stdlib"),
	]
	if deps:
		cmd += ["--package-root", str(tmp_path / "pkg_root")]
		for dep_id, dep_ver in deps:
			cmd += ["--dep", f"{dep_id}@{dep_ver}"]
		if trust_path_existing is not None:
			cmd += ["--trust-store", str(trust_path_existing)]
	for fname in sources:
		cmd.append(str(lib_dir / fname))
	cmd += [
		"--package-id", pkg_id,
		"--package-version", "0.1.0",
		"--package-target", "drift-dev",
		"--emit-package", str(dmp),
		"--test-build-only",
	]
	res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
	if not expect_success:
		return tmp_path / "pkg_root", trust_path_existing or (tmp_path / "trust.json"), res

	assert res.returncode == 0, f"build of {pkg_id} failed:\n{res.stderr[-1500:]}"

	priv = Ed25519PrivateKey.generate()
	priv_key_bytes = priv.private_bytes_raw()
	pub_raw = priv.public_key().public_bytes_raw()
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = dmp.read_bytes()
	sig = priv.sign(pkg_bytes)
	(dmp.with_suffix(".sig")).write_text(json.dumps({
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{hashlib.sha256(pkg_bytes).hexdigest()}",
		"signatures": [{
			"algo": "ed25519", "kid": kid,
			"sig": base64.b64encode(sig).decode("ascii"),
			"pubkey": pub_b64,
		}],
	}, separators=(",", ":"), sort_keys=True))
	if trust_path_existing is not None:
		trust_path = trust_path_existing
	else:
		trust_path = tmp_path / "trust.json"
		trust_path.write_text(json.dumps({
			"format": "drift-trust", "version": 0,
			"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
			"namespaces": {f"{pkg_id}.*": [kid], "dep_pkg.*": [kid], "std.*": [kid]},
			"revoked": [],
		}, separators=(",", ":"), sort_keys=True))
	return tmp_path / "pkg_root", trust_path, res


_DEP_SRC = """\
module dep_pkg;
import std.core as core;
export { Boom, g };

pub error Boom { tag: String }

pub fn g() -> Int { throw Boom(tag = "leak"); }
"""


def test_case4_producer_rejects_throws_E_calling_imported_generic(tmp_path: Path) -> None:
	"""Producer's `f() throws E` body calls `dep_pkg.g()` (generic-throws,
	imported). Producer PACKAGE BUILD must reject -- proves bad metadata
	cannot be emitted into a `.dmp`."""
	# First build the dep producing generic-throws g().
	pkg_root, trust_path, _build = _build_and_sign_pkg(
		tmp_path, "dep_pkg", {"dep.drift": _DEP_SRC},
	)
	# Try to build a producer that declares `throws E` but body calls
	# dep_pkg.g() without wrapping in catch-all.
	producer_src = """\
module producer_pkg;
import std.core as core;
import dep_pkg as dep_pkg;
export { E, f };

pub error E { tag: String }

pub fn f() throws E -> Int {
	val n = dep_pkg.g();
	return n;
}
"""
	_pkg_root, _trust, res = _build_and_sign_pkg(
		tmp_path, "producer_pkg", {"producer.drift": producer_src},
		deps=[("dep_pkg", "0.1.0")],
		trust_path_existing=trust_path,
		expect_success=False,
	)
	assert res.returncode != 0, (
		"producer package build must reject `throws E` body that calls "
		"imported generic-throws callee without catch-all -- otherwise "
		"bad metadata could be emitted into the `.dmp`. Build stderr:\n"
		f"{res.stderr[-1500:]}"
	)
	assert "E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET" in res.stderr, (
		f"expected E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET in producer "
		f"build stderr; got:\n{res.stderr[-1500:]}"
	)


def test_case4_positive_control_throws_E_with_catch_all_wrap(tmp_path: Path) -> None:
	"""Positive control: same shape, but `f` wraps the imported call in
	a catch-all and rewraps any escape as E. Producer build must succeed
	-- the catch-all converts the generic escape into the declared
	narrow set."""
	pkg_root, trust_path, _build = _build_and_sign_pkg(
		tmp_path, "dep_pkg", {"dep.drift": _DEP_SRC},
	)
	producer_src = """\
module producer_pkg;
import std.core as core;
import dep_pkg as dep_pkg;
export { E, f };

pub error E { tag: String }

pub fn f() throws E -> Int {
	try {
		val n = dep_pkg.g();
		return n;
	} catch _ {
		throw E(tag = "rewrapped");
	}
}
"""
	_pkg_root, _trust, res = _build_and_sign_pkg(
		tmp_path, "producer_pkg", {"producer.drift": producer_src},
		deps=[("dep_pkg", "0.1.0")],
		trust_path_existing=trust_path,
		expect_success=False,  # we just want the result, will assert success below
	)
	assert res.returncode == 0, (
		f"positive control failed: catch-all + rewrap must let `throws E` "
		f"compile. Build stderr:\n{res.stderr[-1500:]}"
	)
