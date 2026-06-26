# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 4 ConstShare structural synthesis — non-generic variant
package roundtrip.

Producer-side: a module publishes a non-generic variant with
mixed payload arms — one Copy+Frozen-path arm (`Number(n: Int)`),
one ConstShare-path arm (`Text(handle: core.ConstArc<String>)`),
and one zero-payload arm (`Empty`).  The synthesized impl carries:
  - `target_type_id` = the variant base TypeId (no template needed
    for non-generic variants, mirrors Phase 1 struct path);
  - HIR body that's a real `HMatchExpr` over `self` with
    arm-specific payload reconstruction;
  - signatures + impl_headers identical to user-written
    serialization.

Consumer-side reconstruction must:
  - prove `Multi is shareable.ConstShare` via the require-bounded
    helper after package load;
  - dispatch `m.const_share()` to the synthesized method through
    the consumer-side `LinkedWorld` / `GlobalTraitImplIndex` /
    `CallableRegistry`;
  - re-typecheck the synthesized match-arm reconstruction body
    (this is the new HIR shape for variants — Phase 1's struct
    path doesn't exercise it).

We learned in Phase 3 that source-mode tests can't catch
serialization gaps; this test pins the variant path against the
same risk.

If this test fails, the regression is in:
  - non-generic variant impl serialization (impl_headers,
    signatures, hir_funcs) for the synthesized `HMatchExpr` body;
  - consumer-side decode of the variant match arms (ctor_base
    TypeExpr, binder list, pattern_arg_form, binder_field_indices);
  - consumer-side method dispatch on a variant receiver;
  - HIR→MIR re-lowering of the synthesized match-arm body in the
    consumer build.
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


# Library: a non-generic variant whose every arm qualifies under
# the Phase 4 contract.  Mixed arm shapes exercise both the
# Copy+Frozen path (Number's Int) and the ConstShare path
# (Text's ConstArc<String>).
LIB_SOURCE = """\
module sharedlib;

import std.core as core;
import std.core.shareable as shareable;

use trait shareable.ConstShare;

export { Multi, make_multi_text, make_multi_number, make_multi_empty };

pub variant Multi {
\tEmpty,
\tNumber(n: Int),
\tText(handle: core.ConstArc<String>)
}

pub fn make_multi_empty() nothrow -> Multi {
\treturn Multi::Empty();
}

pub fn make_multi_number(n: Int) nothrow -> Multi {
\treturn Multi::Number(n);
}

pub fn make_multi_text(s: String) nothrow -> Multi {
\treturn Multi::Text(core.const_arc<type String>(move s));
}
"""


# Consumer: imports the signed package, asserts the variant is
# ConstShare via a generic require-bounded helper, then constructs
# values for each arm shape and calls `.const_share()` on them.
# Compile success means the synthesized variant impl survived
# serialization AND consumer-side method resolution finds the
# arm-specific path lowering.
CONSUMER_SOURCE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import sharedlib as lib;

use trait shareable.ConstShare;

fn assert_cs<T>() nothrow -> Void require T is shareable.ConstShare { }

fn main() nothrow -> Int {
\tassert_cs<type lib.Multi>();
\tval m_empty = lib.make_multi_empty();
\tval m_empty2 = m_empty.const_share();
\tval m_num = lib.make_multi_number(42);
\tval m_num2 = m_num.const_share();
\tval m_text = lib.make_multi_text("hello");
\tval m_text2 = m_text.const_share();
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
			artifact_kind="package", artifact_path=f"{package_id}.zdmp", package_id=package_id, version=package_version,
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
	"""Build + sign the sharedlib package containing the variant."""
	base = tmp_path_factory.mktemp("cs_phase4_pkg")
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


def test_phase4_synthesized_variant_const_share_survives_package_roundtrip(
	_built_sharedlib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""Publishes `sharedlib` containing the non-generic variant
	`Multi { Empty, Number(n: Int), Text(handle: ConstArc<String>) }`.
	Consumer asserts `Multi is ConstShare`, constructs values for
	each arm shape (via the package's `make_multi_*` helpers),
	and calls `.const_share()` on each.

	Compile success demonstrates:

	  - synthesized HIR for the variant body (a real `HMatchExpr`
	    over `self` with one arm per case) was captured in
	    `_pre_typecheck_hirs` and serialized into the .dmp's
	    `hir_funcs`;
	  - synthesized `ImplMeta` with target_type_id = variant base,
	    `impl_type_params=[]`, `require_expr=None` (non-generic)
	    was in `module_exports[sharedlib]["impls"]` and
	    serialized into the .dmp's `impl_headers`;
	  - synthesized signature was in `signatures_by_id` and
	    serialized into the .dmp's `signatures`;
	  - consumer-side reconstruction of the match-arm HIR
	    correctly rebinds ctor_base to `Multi`, payload binders
	    to per-arm field names, and ctor reconstruction via
	    `HQualifiedMember(Multi, ctor_name)`;
	  - consumer-side method dispatch on a variant receiver finds
	    the synthesized method through `GlobalTraitImplIndex` and
	    bridges to the synthesized HIR body for re-typechecking
	    (Phase 4's match-body re-check is the new shape relative
	    to Phase 1's struct-ctor body);
	  - HIR→MIR lowering of the synthesized match-arm body
	    succeeded both on the producer (for `.dmp` payload) and
	    on the consumer (re-checking through
	    `compile_stubbed_funcs`).

	If this test fails, regression is in non-generic variant
	serialization, consumer-side variant-impl decode, or
	consumer-side variant-method dispatch."""
	pkg_root, trust_path = _built_sharedlib
	exit_code, msgs = _compile_consumer(
		CONSUMER_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
	)
	if exit_code != 0:
		pytest.fail(
			f"consumer compile failed; synthesized non-generic variant "
			f"ConstShare impl did not survive the package roundtrip.  "
			f"exit_code={exit_code} diagnostics:\n" + "\n".join(msgs)
		)
