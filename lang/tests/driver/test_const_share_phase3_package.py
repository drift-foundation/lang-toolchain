# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 3 ConstShare structural synthesis — generic-struct package
roundtrip.

Producer-side: a module publishes a generic struct
`pub struct Box<T> require T is shareable.ConstShare { pub value: T }`.
The synthesized impl carries:
  - `impl_type_params=["T"]`,
  - `target_type_id` = the `Box<T_typevar>` template (NOT the base),
  - `impl_target_type_args=[typevar_T_id]`,
  - require clause `T is shareable.ConstShare` (verbatim),
  - HIR body returning `Box<type T>(value = self.value.const_share())`.

All of that must serialize alongside hand-written generic impls (via
`module_exports[mid]["impls"]` → `_encode_impl_headers_for_module` and
via `_pre_typecheck_hirs` → `encode_hir_funcs`) and reconstruct on the
consumer side so:

  - `Box<core.ConstArc<String>>` proves `is shareable.ConstShare`
    (require-clause discharges via `T → ConstArc<String>` and
    `ConstArc<String> is ConstShare` via the stdlib impl);
  - `box.const_share()` resolves to the synthesized method through the
    consumer-side `LinkedWorld` / `GlobalTraitImplIndex` /
    `CallableRegistry`;
  - the call type-checks under typevar substitution at the receiver
    boundary.

If this test fails, the regression is in:
  - generic-impl serialization (`impl_target_type_args`,
    `impl_type_params`, `target_type_id`-as-template, require_expr
    encoding);
  - generic-impl deserialization on the consumer (typevar identity
    rebinding, template self-type reconstruction);
  - `requires_by_fn` not surviving the roundtrip for synthesized
    impls;
  - consumer-side method-resolve failing to bind `T → ConstArc<String>`
    for the synthesized generic method.
"""
from __future__ import annotations

import json
from lang.driftc.packages.author_claim_v1 import AuthorClaimBody as _V1_AuthorClaimBody
from lang.driftc.packages.author_claim_v1 import make_author_claim_body
from lang.driftc.packages.cert_claim_v1 import make_cert_claim_body
from lang.driftc.packages.cert_claim_v1 import (
	CertClaimBody as _V1_CertClaimBody,
	CertSuite as _V1_CertSuite,
	Toolchain as _V1_Toolchain,
)
from lang.driftc.packages.sidecar_naming import (
	author_claim_filename as _v1_author_claim_filename,
	cert_claim_filename as _v1_cert_claim_filename,
)
from lang.driftc.packages.cert_claim_v1 import sign_body as _v1_cert_sign_body
from lang.driftc.packages.author_claim_v1 import sign_body as _v1_author_sign_body
from tools.drift_author.author_publish import (
	SignAuthorClaimOptions as _V1_SignAuthorClaimOptions,
	sign_and_write_author_claim as _v1_sign_and_write_author_claim,
)
from tools.drift_deploy.cert_emit import (
	SignCertClaimOptions as _V1_SignCertClaimOptions,
	sign_and_write_cert_claim as _v1_sign_and_write_cert_claim,
)
from lang.drift.crypto import compute_ed25519_kid as _v1_compute_ed25519_kid, ed25519_sign_from_seed as _v1_ed25519_sign_from_seed
from cryptography.hazmat.primitives import serialization as _v1_serialization
import os
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


# Library: a generic struct that auto-derives ConstShare under
# the user-declared require clause.  Phase 3 contract: no
# implicit constraint strengthening; `Box<T>` derives only
# because the require clause already proves the field qualifies.
LIB_SOURCE = """\
module sharedlib;

import std.core as core;
import std.core.shareable as shareable;

use trait shareable.ConstShare;

export { Box, make_box };

pub struct Box<T> require T is shareable.ConstShare {
\tpub value: T
}

pub fn make_box<T>(v: T) nothrow -> Box<T> require T is shareable.ConstShare {
\treturn Box<type T>(value = move v);
}
"""


# Consumer: imports the signed package, instantiates
# `Box<ConstArc<String>>`, asserts ConstShare via a generic
# require-bounded helper, and calls `box.const_share()`.
# Compile success means the synthesized generic impl survived
# serialization AND consumer-side method resolution + require
# proof both work under typevar substitution.
CONSUMER_SOURCE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import sharedlib as lib;

use trait shareable.ConstShare;

fn assert_cs<T>() nothrow -> Void require T is shareable.ConstShare { }

fn main() nothrow -> Int {
\tval inner = core.const_arc<type String>("hi");
\tval b = lib.make_box<type core.ConstArc<String>>(move inner);
\tassert_cs<type lib.Box<core.ConstArc<String>>>();
\tval b2 = b.const_share();
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
			"--source-content-id", "sha256:" + ("0" * 64),
			"--emit-package", str(pkg_path),
			"--json",
		],
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	assert rc.returncode == 0, f"lib '{package_id}' build failed:\n{rc.stdout}\n---\n{rc.stderr[:1000]}"

	# v1 sidecars: replace `.sig` envelope with author + cert claims.
	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes_raw()
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)
	priv_seed = priv.private_bytes(
		encoding=_v1_serialization.Encoding.Raw,
		format=_v1_serialization.PrivateFormat.Raw,
		encryption_algorithm=_v1_serialization.NoEncryption(),
	)
	pkg_bytes = pkg_path.read_bytes()
	_TEST_SCI = "sha256:" + ("0" * 64)
	_v1_sign_and_write_author_claim(_V1_SignAuthorClaimOptions(
		body=make_author_claim_body(
			artifact_kind="package", package_id=package_id, version=package_version,
			namespaces=(package_id, f"{package_id}.*"),
			source_content_id=_TEST_SCI, required_deps=(), 			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed, sidecar_dir=lib_dir,
	))
	_v1_sign_and_write_cert_claim(_V1_SignCertClaimOptions(
		body=make_cert_claim_body(
			artifact_kind="package", artifact_path=f"{package_id}.dmp", package_id=package_id, version=package_version,
			artifact_sha256="sha256:" + sha256(pkg_bytes).hexdigest(),
			source_content_id=_TEST_SCI, target="drift-dev",
			toolchain=_V1_Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="test"),
			dep_graph=(),
			cert_suite=_V1_CertSuite(id="drift-deploy/test", version="1.0", result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64)),
			run_id=f"test-{package_id}", run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=priv_seed, sidecar_dir=lib_dir,
	))

	trust = {
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {namespace_glob: {"authors": [kid], "certifiers": [kid]}},
		"revoked": [],
	}
	dest_trust_path.write_text(json.dumps(trust, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	dest_dir = dest_pkg_root / package_id / package_version
	dest_dir.mkdir(parents=True, exist_ok=True)
	shutil.copy2(str(pkg_path), str(dest_dir / f"{package_id}.dmp"))
	# v1 sidecars travel with the artifact.
	author_sidecar = lib_dir / _v1_author_claim_filename(package_id)
	shutil.copy2(str(author_sidecar), str(dest_dir / author_sidecar.name))
	cert_sidecar = lib_dir / _v1_cert_claim_filename(package_id, kid)
	shutil.copy2(str(cert_sidecar), str(dest_dir / cert_sidecar.name))


@pytest.fixture(scope="module")
def _built_sharedlib(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	"""Build + sign the sharedlib package containing the generic Box."""
	base = tmp_path_factory.mktemp("cs_phase3_pkg")
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


def test_phase3_synthesized_generic_const_share_survives_package_roundtrip(
	_built_sharedlib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""Publishes `sharedlib` containing the generic `Box<T> require
	T is ConstShare`.  Consumer instantiates
	`Box<ConstArc<String>>`, asserts its ConstShare-ness via a
	generic require-bounded helper, and calls `const_share()`.

	Compile success demonstrates:

	  - synthesized HIR for the generic body (`Box<type T>(value =
	    self.value.const_share())`) was captured in
	    `_pre_typecheck_hirs` and serialized into the .dmp's
	    `hir_funcs`;
	  - synthesized `ImplMeta` carrying
	    `target_type_id=Box<T_typevar>` (template, not base),
	    `impl_type_params=["T"]`, and the verbatim require clause
	    was in `module_exports[sharedlib]["impls"]` and serialized
	    into the .dmp's `impl_headers`;
	  - synthesized signature with `impl_target_type_args=[typevar_T]`
	    was in `signatures_by_id` and serialized into the .dmp's
	    `signatures`;
	  - consumer-side reconstruction rebinds typevar identity so
	    `_match_impl_type_args(template_args=[T_typevar],
	    recv_args=[ConstArc<String>])` succeeds;
	  - consumer-side require proof discharges
	    `T is ConstShare` under the binding, via stdlib's
	    `ConstArc<U> is ConstShare require U is Frozen` impl chain;
	  - HIR→MIR lowered the synthesized body successfully both on
	    the producer (for `.dmp` payload) and on the consumer
	    (re-checking through `compile_stubbed_funcs`).

	If this test fails, the regression is in generic-impl
	serialization, generic-impl deserialization, or generic
	method-call resolution post-roundtrip."""
	pkg_root, trust_path = _built_sharedlib
	exit_code, msgs = _compile_consumer(
		CONSUMER_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
	)
	if exit_code != 0:
		pytest.fail(
			f"consumer compile failed; synthesized generic ConstShare "
			f"impl did not survive the package roundtrip.  "
			f"exit_code={exit_code} diagnostics:\n" + "\n".join(msgs)
		)
