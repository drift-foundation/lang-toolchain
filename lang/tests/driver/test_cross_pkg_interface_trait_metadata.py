# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: cross-package method dispatch through a
`pub interface` trait fails at compile-time with
`missing trait metadata for '<pkg.Interface>'` from the call
resolver.

Reported as app-team `compiler-findings.md` #1 (2026-05-17);
deterministic 5-line carrier at `/tmp/sgw-repro1/REPRO.drift`
against the cert'd `mariadb-rpc` package -- their case is
`pool.ConnectionPool.close()` where `close` is declared by
`ConnectionSource: pub interface` in
`mariadb.rpc.managed` and implemented for `ConnectionPool` in
`mariadb.rpc.pool`.

**Pre-fix shape** (verified deterministic against both the
app-team carrier AND the 2-pkg reduced repro below):

    lang.driftc.method_resolver.ResolutionError:
      missing trait metadata for 'iface_pkg.Doer'
      at call_resolver.py:2694 (resolve_method_call)

**Root cause** (read-only diagnosis 2026-05-17):

Drift has TWO surface keywords -- `pub trait` and `pub interface`
-- both of which can be `implement`'d.  At the in-memory
dispatch layer, the two surface forms diverge in storage:
TraitDefs live in `trait_world.traits` (one dict per module),
while interface schemas live in `type_table.interface_bases`
(keyed by base TypeId, with full method signatures as
`InterfaceMethodSchema` using `GenericTypeExpr`-typed params).
At the PACKAGE EMISSION layer, this storage split combined with
the export-name split also splits dispatch-metadata round-trip:

  * `pub trait` decls → name lands in `exports.traits`;
    encoder reads `trait_world.traits` and emits
    `trait_metadata` entries with encoded methods.
  * `pub interface` decls → name lands in
    `exports.types.interfaces`; encoder ignored interface
    schemas entirely (`type_table.interface_bases` was never
    consulted), so `trait_metadata` was EMPTY for that name.

Encoder at `driftc.py:11080` -> `_encode_trait_metadata_for_module`
filtered `trait_world.traits` by `name in exported`, where
`exported = set(exports.traits)`.  Interfaces failed the filter
because (a) their name was in `exports.types.interfaces` not
`exports.traits`, AND (b) their methods weren't in
`trait_world.traits` even if the name had matched.

Consumer-side loader at `driftc.py:2130-2244` scanned each
package's `exports.traits` (only) for "expected trait names"
and cross-checked with `trait_metadata`.  Interfaces weren't in
that set; the K26 workaround at `driftc.py:9470` then explicitly
marked external interface impl trait_keys as MISSING; downstream
`trait_index.is_missing(TraitKey)` returned True for the
interface; `call_resolver.py:2694` bailed with ResolutionError.

Same-package dispatch worked because the
`implement Iface for Thing` lowering wires the impl method as
a method-on-Thing via `callable_registry.register_inherent_method`
regardless of which keyword declared the interface; only
cross-package serialization dropped the interface metadata.

**Fix (option (a) per K direction 2026-05-17):**
Union `exports.types.interfaces` with `exports.traits` at BOTH
sites (emitter + loader) for dispatch-metadata purposes.  Keep
`exports.types.interfaces` as a separate namespace -- consumers
that inspect the type-export namespace today aren't broken by
the union (this is an additive consultation, not a
reclassification).

Carriers (2-pkg shape; mirrors the sgw mariadb.rpc.managed +
mariadb.rpc.pool + consumer split):

  V1. Minimal: `pub interface Doer { fn do_thing(self: &mut Self) -> Int }`
      in pkg A, `implement Doer for Thing` in pkg B, consumer
      calls `t.do_thing()`.  Pre-fix: ResolutionError.  Post-fix:
      compile + run clean.
  V2. Interface method with a NON-SCALAR param/return type
      referencing another exported type from pkg A
      (`fn handle(self: &mut Self, req: &Req) -> Status`, where
      `Req`/`Status` are pkg A exports).  Pins that interface
      metadata encoding is not scalar-only -- if the encoder
      stops short of round-tripping cross-type references,
      the fix is incomplete.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _build_and_sign_pkg(
	tmp_path: Path,
	pkg_id: str,
	sources: dict[str, str],
	deps: list[tuple[str, str]] | None = None,
	priv_key_bytes: bytes | None = None,
	trust_path_existing: Path | None = None,
) -> tuple[Path, Path, bytes]:
	"""Build + sign a package.  Returns (pkg_root, trust_path, priv_key_bytes).

	`sources` maps relative filename → drift source text.  All files are
	written under tmp_path/<pkg_id>_src/ then compiled into a single .dmp.
	If `deps` is provided, --package-root + --dep flags are added so the
	build can resolve cross-package references.  If `priv_key_bytes` is
	provided, the same signing key (and trust namespace) are reused so
	multiple packages share one trust store; otherwise a fresh key is
	generated.
	"""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.drift.crypto import compute_ed25519_kid

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
	# v1 fixture: stamp SCI into the manifest so the verifier can
	# cross-bind author/cert claims to the .dmp.
	_TEST_SCI = "sha256:" + ("0" * 64)
	cmd += [
		"--package-id", pkg_id,
		"--package-version", "0.1.0",
		"--package-target", "drift-dev",
		"--source-content-id", _TEST_SCI,
		"--emit-package", str(dmp),
		"--test-build-only",
	]
	res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=sanitizer_timeout(60))
	assert res.returncode == 0, f"build of {pkg_id} failed:\n{res.stderr[-1500:]}"

	if priv_key_bytes is None:
		priv = Ed25519PrivateKey.generate()
		priv_key_bytes = priv.private_bytes_raw()
	else:
		priv = Ed25519PrivateKey.from_private_bytes(priv_key_bytes)
	pub_raw = priv.public_key().public_bytes_raw()
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = dmp.read_bytes()

	# v1 sidecars: replace `.sig` envelope with author + cert claims.
	from lang.driftc.packages.author_claim_v1 import make_author_claim_body, AuthorClaimBody
	from lang.driftc.packages.cert_claim_v1 import (
	make_cert_claim_body,
		CertClaimBody, CertSuite, Toolchain,
	)
	from tools.drift_author.author_publish import (
		SignAuthorClaimOptions, sign_and_write_author_claim,
	)
	from tools.drift_deploy.cert_emit import (
		SignCertClaimOptions, sign_and_write_cert_claim,
	)
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=make_author_claim_body(
			artifact_kind="package", package_id=pkg_id, version="0.1.0",
			namespaces=(f"{pkg_id}.*",),
			source_content_id=_TEST_SCI,
			required_deps=(), 			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_key_bytes,
		sidecar_dir=pkg_root_dir,
	))
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=make_cert_claim_body(
			artifact_kind="package", artifact_path=f"{pkg_id}.dmp", package_id=pkg_id, version="0.1.0",
			artifact_sha256="sha256:" + hashlib.sha256(pkg_bytes).hexdigest(),
			source_content_id=_TEST_SCI, target="drift-dev",
			toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="test"),
			dep_graph=(),
			cert_suite=CertSuite(id="drift-deploy/test", version="1.0",
				result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64)),
			run_id=f"test-{pkg_id}",
			run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=priv_key_bytes,
		sidecar_dir=pkg_root_dir,
	))

	if trust_path_existing is not None:
		trust_path = trust_path_existing
	else:
		trust_path = tmp_path / "trust.json"
		trust_path.write_text(json.dumps({
			"format": "drift-trust", "version": 1,
			"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
			"namespaces": {
				"iface_pkg.*": {"authors": [kid], "certifiers": [kid]},
				"impl_pkg.*": {"authors": [kid], "certifiers": [kid]},
				"std.*": {"authors": [kid], "certifiers": [kid]},
			},
			"revoked": [],
		}, separators=(",", ":"), sort_keys=True))

	return tmp_path / "pkg_root", trust_path, priv_key_bytes


def _compile_consumer(
	tmp_path: Path,
	pkg_root: Path,
	trust_path: Path,
	deps: list[tuple[str, str]],
	source: str,
) -> subprocess.CompletedProcess[str]:
	src_dir = tmp_path / "consumer_src"
	src_dir.mkdir(exist_ok=True)
	src = src_dir / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / "main_bin"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--stdlib-root", str(ROOT / "stdlib"),
		"--package-root", str(pkg_root),
	]
	for dep_id, dep_ver in deps:
		cmd += ["--dep", f"{dep_id}@{dep_ver}"]
	cmd += [
		"--trust-store", str(trust_path),
		"--entry", "main::main",
		str(src),
		"-o", str(out_bin),
	]
	return subprocess.run(
		cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)


# ─── V1: minimal interface dispatch across packages ────────────────


_V1_IFACE_PKG_SRC = """\
module iface_pkg;
export { Doer };

pub interface Doer {
	fn do_thing(self: &mut Self) -> Int
}
"""

_V1_IMPL_PKG_SRC = """\
module impl_pkg;
import iface_pkg as iface_pkg;
export { Thing, open };

pub struct Thing { pub n: Int }

implement iface_pkg.Doer for Thing {
	pub fn do_thing(self: &mut Thing) nothrow -> Int { return self.n; }
}

pub fn open(n: Int) nothrow -> Thing { return Thing(n = n); }
"""

_V1_CONSUMER_SRC = """\
module main;
import std.core as core;
import impl_pkg as impl_pkg;
import iface_pkg as iface_pkg;

pub fn main() nothrow -> Int {
	var t = impl_pkg.open(42);
	val n = t.do_thing();
	if n == 42 { return 0; }
	return 1;
}
"""


def test_v1_minimal_interface_dispatch_across_packages(tmp_path: Path) -> None:
	"""THE BUG: trait method dispatch on a concrete type that
	implements a `pub interface` from another package fails at
	compile with `missing trait metadata for 'iface_pkg.Doer'`.

	Post-fix expectation: compile + link + run; binary returns 0
	(the field value, 42, matched)."""
	# Build pkg A (iface_pkg) first, with its own trust store.
	pkg_root, trust_path, priv_key = _build_and_sign_pkg(
		tmp_path, "iface_pkg", {"iface.drift": _V1_IFACE_PKG_SRC},
	)
	# Build pkg B (impl_pkg) reusing the same key + trust store.
	_pkg_root_2, _trust_path_2, _priv_key_2 = _build_and_sign_pkg(
		tmp_path, "impl_pkg",
		{"impl.drift": _V1_IMPL_PKG_SRC},
		deps=[("iface_pkg", "0.1.0")],
		priv_key_bytes=priv_key,
		trust_path_existing=trust_path,
	)
	# Compile consumer.
	res = _compile_consumer(
		tmp_path, pkg_root, trust_path,
		deps=[("iface_pkg", "0.1.0"), ("impl_pkg", "0.1.0")],
		source=_V1_CONSUMER_SRC,
	)
	assert "missing trait metadata" not in res.stderr, (
		f"V1: ResolutionError still fires for cross-package "
		f"`pub interface` trait dispatch.  The encoder + loader "
		f"union of `exports.types.interfaces` into the trait-"
		f"metadata path at `driftc.py:11080` / `driftc.py:2150` "
		f"was reverted or never landed.\n\n{res.stderr[-1500:]}"
	)
	assert res.returncode == 0, (
		f"V1 compile failed but NOT with the known shape:\n"
		f"{res.stderr[-1500:]}"
	)
	out_bin = tmp_path / "main_bin"
	assert out_bin.exists(), "V1 binary not produced"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, (
		f"V1 binary exited {run.returncode}, expected 0 "
		f"(should return 0 when t.do_thing() returns 42)"
	)


# ─── V2: interface method with non-scalar param + return ───────────
#
# Per user-spec direction: "Add at least one method signature with
# a nontrivial param or return type from another exported type if
# cheap, to prove interface metadata encoding is not scalar-only."
#
# The interface method here takes `&Req` and returns `Status`, where
# both `Req` and `Status` are exported structs from pkg A.  If the
# trait-metadata encoder fails to round-trip cross-type references
# in interface method signatures, this carrier surfaces it -- the
# consumer-side call to `t.handle(...)` would either fail to resolve
# or fail later at runtime.

_V2_IFACE_PKG_SRC = """\
module iface_pkg;
export { Doer, Req, Status };

pub struct Req { pub path: String }
pub struct Status { pub code: Int }

pub interface Doer {
	fn handle(self: &mut Self, req: &Req) -> Status
}
"""

_V2_IMPL_PKG_SRC = """\
module impl_pkg;
import iface_pkg as iface_pkg;
export { Thing, open };

pub struct Thing { pub tag: Int }

implement iface_pkg.Doer for Thing {
	pub fn handle(self: &mut Thing, req: &iface_pkg.Req) nothrow -> iface_pkg.Status {
		return iface_pkg.Status(code = self.tag + req.path.byte_length());
	}
}

pub fn open(tag: Int) nothrow -> Thing { return Thing(tag = tag); }
"""

_V2_CONSUMER_SRC = """\
module main;
import std.core as core;
import impl_pkg as impl_pkg;
import iface_pkg as iface_pkg;

pub fn main() nothrow -> Int {
	var t = impl_pkg.open(10);
	val req = iface_pkg.Req(path = "abcd");
	val st = t.handle(&req);
	if st.code == 14 { return 0; }
	return 1;
}
"""


def test_v2_interface_method_with_nonscalar_signature(tmp_path: Path) -> None:
	"""Interface method whose param + return type both reference
	other exported types from the same package (`&Req`,
	`Status`).  Pins that the trait-metadata encoder round-trips
	cross-type references in interface method signatures, not
	just scalars.

	Expected: t.handle(&Req{path="abcd"}) on Thing{tag=10} returns
	Status{code=10 + 4 = 14}; binary returns 0."""
	pkg_root, trust_path, priv_key = _build_and_sign_pkg(
		tmp_path, "iface_pkg", {"iface.drift": _V2_IFACE_PKG_SRC},
	)
	_build_and_sign_pkg(
		tmp_path, "impl_pkg",
		{"impl.drift": _V2_IMPL_PKG_SRC},
		deps=[("iface_pkg", "0.1.0")],
		priv_key_bytes=priv_key,
		trust_path_existing=trust_path,
	)
	res = _compile_consumer(
		tmp_path, pkg_root, trust_path,
		deps=[("iface_pkg", "0.1.0"), ("impl_pkg", "0.1.0")],
		source=_V2_CONSUMER_SRC,
	)
	assert "missing trait metadata" not in res.stderr, (
		f"V2: ResolutionError on a non-scalar interface method "
		f"signature.  Same fix as V1 should close this; if V1 "
		f"passes but V2 fails, the encoder is dropping cross-type "
		f"references inside interface method signatures.\n\n"
		f"{res.stderr[-1500:]}"
	)
	assert res.returncode == 0, (
		f"V2 compile failed:\n{res.stderr[-1500:]}"
	)
	out_bin = tmp_path / "main_bin"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, (
		f"V2 binary exited {run.returncode}, expected 0 "
		f"(should return 0 when t.handle(&Req{{path='abcd'}}) "
		f"returns Status{{code=14}})"
	)
