# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Case 5 of the six-case proof matrix: consumer-side conservative
behavior on cross-pkg generic-throws callees.

A consumer that wraps an imported generic-throws call in
`try { dep.g(); } catch m_main:E(e) { ... }` must NOT claim that E
covers the call -- generic-throws callees require catch-all coverage.
Pre-slice this already rejected; post-slice this still rejects.

Pins the conservative consumer-side behavior in case the slice
accidentally promoted generic to narrow somewhere.

Plan reference: work/cross-pkg-narrow-throws-metadata/plan.md, §4a-3.
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
	tmp_path: Path, pkg_id: str, sources: dict[str, str],
) -> tuple[Path, Path]:
	"""Build + sign a producer pkg. Returns (pkg_root, trust_path)."""
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
	assert res.returncode == 0, f"build of {pkg_id} failed:\n{res.stderr[-1500:]}"

	priv = Ed25519PrivateKey.generate()
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
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {f"{pkg_id}.*": [kid], "dep_pkg.*": [kid], "std.*": [kid]},
		"revoked": [],
	}, separators=(",", ":"), sort_keys=True))
	return tmp_path / "pkg_root", trust_path


_DEP_GENERIC_SRC = """\
module dep_pkg;
import std.core as core;
export { Boom, g };

pub error Boom { tag: String }

// g() is generic-throws (no `throws TYPE_LIST`); package metadata
// will carry declared_throws_event_fqns = None for it.
pub fn g() -> Int { throw Boom(tag = "leak"); }
"""


def test_case5_consumer_typed_catch_does_not_cover_generic_callee(tmp_path: Path) -> None:
	"""Consumer wraps `dep_pkg.g()` (generic-throws) in `try { } catch
	m_main:E(e) { }` and `main` is nothrow. Must reject: typed catch
	cannot cover a generic-throws call. Generic callees require catch-
	all."""
	pkg_root, trust_path = _build_and_sign_pkg(
		tmp_path, "dep_pkg", {"dep.drift": _DEP_GENERIC_SRC},
	)
	consumer_src = """\
module main;
import std.core as core;
import dep_pkg as dep_pkg;

pub error E { tag: String }

pub fn main() nothrow -> Int {
	try {
		val n = dep_pkg.g();
		return n;
	} catch main:E(e) {
		return 0;
	}
}
"""
	src_dir = tmp_path / "consumer_src"
	src_dir.mkdir(exist_ok=True)
	src = src_dir / "main.drift"
	src.write_text(consumer_src)
	out_bin = tmp_path / "main_bin"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--stdlib-root", str(ROOT / "stdlib"),
		"--package-root", str(pkg_root),
		"--dep", "dep_pkg@0.1.0",
		"--trust-store", str(trust_path),
		"--entry", "main::main",
		str(src),
		"-o", str(out_bin),
	]
	res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
	assert res.returncode != 0, (
		"consumer compile must fail: typed `catch main:E(e)` cannot "
		"cover a call to `dep_pkg.g()` (generic-throws). Generic-throws "
		"callees require catch-all.\n\n"
		f"stderr:\n{res.stderr[-1500:]}\nstdout:\n{res.stdout[-1500:]}"
	)
	# Failure shape: main is declared nothrow but may throw (because
	# the generic call isn't covered).  This is existing behavior pinned
	# as a regression guard.
	assert "is declared nothrow but may throw" in res.stderr, (
		f"expected 'is declared nothrow but may throw' diagnostic; "
		f"got:\n{res.stderr[-1500:]}"
	)


def test_case5_positive_control_catch_all_covers_generic(tmp_path: Path) -> None:
	"""Positive control: same shape but with a `catch _` catch-all
	added. Must compile -- catch-all covers generic-throws."""
	pkg_root, trust_path = _build_and_sign_pkg(
		tmp_path, "dep_pkg", {"dep.drift": _DEP_GENERIC_SRC},
	)
	consumer_src = """\
module main;
import std.core as core;
import dep_pkg as dep_pkg;

pub error E { tag: String }

pub fn main() nothrow -> Int {
	try {
		val n = dep_pkg.g();
		return n;
	} catch main:E(e) {
		return 0;
	} catch _ {
		return 1;
	}
}
"""
	src_dir = tmp_path / "consumer_src"
	src_dir.mkdir(exist_ok=True)
	src = src_dir / "main.drift"
	src.write_text(consumer_src)
	out_bin = tmp_path / "main_bin"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--stdlib-root", str(ROOT / "stdlib"),
		"--package-root", str(pkg_root),
		"--dep", "dep_pkg@0.1.0",
		"--trust-store", str(trust_path),
		"--entry", "main::main",
		str(src),
		"-o", str(out_bin),
	]
	res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
	assert res.returncode == 0, (
		f"positive control: catch-all must cover the generic call.\n\n"
		f"{res.stderr[-1500:]}"
	)
