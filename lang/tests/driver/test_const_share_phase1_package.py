# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 1 ConstShare structural synthesis — package roundtrip.

Producer-side: a module containing a struct that auto-derives
ConstShare via composition is published as a `.dmp` package.
The synthesized impl + method body must serialize alongside
hand-written ones (via `module_exports[mid]["impls"]` →
`_encode_impl_headers_for_module` and via `_pre_typecheck_hirs`
→ `encode_hir_funcs`).

Consumer-side: a separate module imports the `.dmp`,
instantiates the auto-derived struct, calls
`holder.const_share()`, and uses both owners.

If this test fails, the regression is in:
  - synthesis happening AFTER the `_pre_typecheck_hirs` snapshot
    (synthesized HIR not in the snapshot → not in package);
  - `module_exports[mid]["impls"]` not picking up synthesized
    `ImplMeta`;
  - `signatures_by_id` not picking up the synthesized signature;
  - consumer-side method resolution failing to find the impl
    via `LinkedWorld` / `GlobalTraitImplIndex`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


# Library: a struct that auto-derives ConstShare via the v1
# composition rule (one ConstArc<String> field).
LIB_SOURCE = """\
module sharedlib;

import std.core as core;

export { Holder, make_holder };

pub struct Holder {
\tpub handle: core.ConstArc<String>
}

pub fn make_holder(s: String) nothrow -> Holder {
\treturn Holder(handle = core.const_arc<type String>(move s));
}
"""


# Consumer: imports the signed package, calls
# `holder.const_share()` on a Holder that was auto-derived.
# Compile success means synthesized impl serialized AND consumer-
# side method resolution found it.
CONSUMER_SOURCE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import sharedlib as lib;

use trait shareable.ConstShare;

fn main() nothrow -> Int {
\tval h = lib.make_holder("hello");
\tval h2 = h.const_share();
\treturn 0;
}
"""


def _b64(data: bytes) -> str:
	import base64
	return base64.b64encode(data).decode("ascii")


def _publish_signed_pkg(
	lib_dir: Path,
	*,
	src_files: list[Path],
	package_id: str,
	package_version: str,
	namespace_glob: str,
	dest_pkg_root: Path,
	dest_trust_path: Path,
) -> None:
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.drift.crypto import compute_ed25519_kid

	pkg_path = lib_dir / f"{package_id}.dmp"
	rc = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc",
			"--dev",
			"-M", str(lib_dir),
			"--stdlib-root", str(stdlib_root()),
			*[str(p) for p in src_files],
			"--package-id", package_id,
			"--package-version", package_version,
			"--package-target", "drift-dev",
			"--emit-package", str(pkg_path),
			"--json",
		],
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	assert rc.returncode == 0, f"lib '{package_id}' build failed:\n{rc.stdout}\n---\n{rc.stderr[:1000]}"

	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes_raw()
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	sidecar = {
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{sha256(pkg_bytes).hexdigest()}",
		"signatures": [{"algo": "ed25519", "kid": kid, "sig": _b64(sig_raw), "pubkey": pub_b64}],
	}
	sig_path = pkg_path.with_suffix(".sig")
	sig_path.write_text(json.dumps(sidecar, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	trust = {
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {namespace_glob: [kid]},
		"revoked": [],
	}
	dest_trust_path.write_text(json.dumps(trust, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	dest_dir = dest_pkg_root / package_id / package_version
	dest_dir.mkdir(parents=True, exist_ok=True)
	shutil.copy2(str(pkg_path), str(dest_dir / f"{package_id}.dmp"))
	shutil.copy2(str(sig_path), str(dest_dir / f"{package_id}.sig"))


@pytest.fixture(scope="module")
def _built_sharedlib(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	"""Build + sign the sharedlib package."""
	base = tmp_path_factory.mktemp("cs_phase1_pkg")
	lib_dir = base / "lib"
	lib_dir.mkdir(parents=True, exist_ok=True)
	(lib_dir / "sharedlib.drift").write_text(LIB_SOURCE, encoding="utf-8")

	pkg_root = base / "pkg_root"
	trust_path = base / "trust.json"
	_publish_signed_pkg(
		lib_dir,
		src_files=[lib_dir / "sharedlib.drift"],
		package_id="sharedlib",
		package_version="1.0.0",
		namespace_glob="sharedlib.*",
		dest_pkg_root=pkg_root,
		dest_trust_path=trust_path,
	)
	return pkg_root, trust_path


def _compile_consumer(
	source: str,
	*,
	pkg_root: Path,
	trust_path: Path,
	tmp_path: Path,
	dep: str = "sharedlib@1.0.0",
) -> tuple[int, list[str]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = tmp_path / "a.out"

	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--package-root", str(pkg_root),
		"--dep", dep,
		"--trust-store", str(trust_path),
		str(src),
		"-o", str(out),
		"--json",
		"--test-build-only",
	]
	rc = subprocess.run(
		cmd,
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	if not rc.stdout.strip():
		return rc.returncode, [rc.stderr[:2000]]
	result = json.loads(rc.stdout)
	msgs = [d["message"] for d in result.get("diagnostics", []) if d.get("severity") == "error"]
	return result.get("exit_code", rc.returncode), msgs


# ── Producer/consumer roundtrip ───────────────────────────────────


def test_phase1_synthesized_const_share_survives_package_roundtrip(
	_built_sharedlib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""Publishes a `sharedlib` package containing `Holder`
	(synthesized ConstShare impl).  A consumer module imports
	the package, instantiates Holder, calls `const_share()`.
	Compile success means:

	  - synthesized HIR was captured in `_pre_typecheck_hirs` and
	    serialized into the .dmp's `hir_funcs`;
	  - synthesized `ImplMeta` was in
	    `module_exports[sharedlib]["impls"]` and serialized into
	    the .dmp's `impl_headers`;
	  - synthesized signature was in `signatures_by_id` and
	    serialized into the .dmp's `signatures`;
	  - consumer-side type-check found the impl via the linked
	    world / `GlobalTraitImplIndex` after package load;
	  - HIR→MIR lowered the synthesized body successfully both
	    on the producer (for `.dmp` payload) and on the
	    consumer (re-checking through compile_stubbed_funcs).

	If this test fails, regression is in the snapshot-timing
	refactor or the helper's `module_exports` /
	`signatures_by_id` updates."""
	pkg_root, trust_path = _built_sharedlib
	exit_code, msgs = _compile_consumer(
		CONSUMER_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
	)
	if exit_code != 0:
		pytest.fail(
			f"consumer compile failed; synthesized ConstShare impl "
			f"did not survive the package roundtrip.  exit_code="
			f"{exit_code} diagnostics:\n" + "\n".join(msgs)
		)
