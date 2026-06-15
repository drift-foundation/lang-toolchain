# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Emitted-package regression for bare-name local-nominal precedence
(Producer C, the `TypeTable._eval_generic_type_expr` site).

A producer package `boxlib` declares its OWN `variant Box<T>` plus a variant
`Crate { Holds(b: Box<Int>), Nothing }` whose payload field references that
local `Box<Int>`.  A consumer loads the `boxlib` package WHILE `std.core` also
exposes its re-exported `core.Box` (the unique cross-module `Box` alias), then
constructs and matches the producer's `Crate` and runs.

This is the only path that exercises `TypeTable._eval_generic_type_expr`: the
producer's `Crate` schema (with the bare `Box<Int>` payload field) is serialized
into the `.dmp` and re-evaluated during package DESERIALIZATION on the consumer
side.  Without the bare-name precedence fix at that site, the field's bare `Box`
is hijacked by `std.core`'s re-exported `core.Box` (different TypeId) → the
construct/match mismatches or ICEs.  The source-mode `_lower_generic_expr`
coverage lives separately in `test_bare_name_local_nominal_precedence.py`.
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

from lang.driftc.packages.author_claim_v1 import AuthorClaimBody as _V1_AuthorClaimBody
from lang.driftc.packages.author_claim_v1 import sign_body as _v1_author_sign_body  # noqa: F401
from lang.driftc.packages.cert_claim_v1 import (
	CertClaimBody as _V1_CertClaimBody,
	CertSuite as _V1_CertSuite,
	Toolchain as _V1_Toolchain,
)
from lang.driftc.packages.sidecar_naming import (
	author_claim_filename as _v1_author_claim_filename,
	cert_claim_filename as _v1_cert_claim_filename,
)
from tools.drift_author.author_publish import (
	SignAuthorClaimOptions as _V1_SignAuthorClaimOptions,
	sign_and_write_author_claim as _v1_sign_and_write_author_claim,
)
from tools.drift_deploy.cert_emit import (
	SignCertClaimOptions as _V1_SignCertClaimOptions,
	sign_and_write_cert_claim as _v1_sign_and_write_cert_claim,
)
from cryptography.hazmat.primitives import serialization as _v1_serialization

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


# Producer: a local generic `Box<T>` plus a variant whose payload field is the
# local `Box<Int>` — the field type that must survive schema (de)serialization
# without being hijacked by `std.core`'s re-exported `core.Box`.
LIB_SOURCE = """\
module boxlib;

pub variant Box<T> { Empty, One(v: T) }

pub variant Crate { Holds(b: Box<Int>), Nothing }

pub fn make_crate(n: Int) nothrow -> Crate {
	return Crate::Holds(Box::One(n));
}

pub fn crate_get(c: &Crate) nothrow -> Int {
	match c {
		Crate::Holds(b) => {
			match b {
				Box::One(v) => { return *v; },
				Box::Empty() => { return -1; },
			}
		},
		Crate::Nothing() => { return 0; },
	}
}

export { Box, Crate, make_crate, crate_get };
"""


# Consumer imports BOTH the producer package AND std.core (which re-exports
# `core.Box`).  It DIRECTLY constructs the producer's `Crate` (with a nested
# `lib.Box<Int>` payload) and matches it — exercising the deserialized,
# consumer-visible field schema's nominal identity, not just producer helpers.
CONSUMER_SOURCE = """\
module main;

import std.core as core;
import boxlib as lib;

fn main() nothrow -> Int {
	val c: lib.Crate = lib.Crate::Holds(lib.Box<Int>::One(7));
	match c {
		lib.Crate::Holds(b) => {
			match b {
				lib.Box::One(v) => { return v; },
				lib.Box::Empty() => { return -1; },
			}
		},
		lib.Crate::Nothing() => { return 0; },
	}
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
			sys.executable, "-m", "lang.driftc", "--dev",
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
	assert rc.returncode == 0, f"lib '{package_id}' build failed:\n{rc.stdout}\n---\n{rc.stderr[:1200]}"

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
		body=_V1_AuthorClaimBody(
			schema_version=1, package_id=package_id, version=package_version,
			namespaces=(package_id, f"{package_id}.*"),
			source_content_id=_TEST_SCI, required_deps=(), release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed, sidecar_dir=lib_dir,
	))
	_v1_sign_and_write_cert_claim(_V1_SignCertClaimOptions(
		body=_V1_CertClaimBody(
			schema_version=1, package_id=package_id, version=package_version,
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
	author_sidecar = lib_dir / _v1_author_claim_filename(package_id)
	shutil.copy2(str(author_sidecar), str(dest_dir / author_sidecar.name))
	cert_sidecar = lib_dir / _v1_cert_claim_filename(package_id, kid)
	shutil.copy2(str(cert_sidecar), str(dest_dir / cert_sidecar.name))


@pytest.fixture(scope="module")
def _built_boxlib(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	base = tmp_path_factory.mktemp("boxlib_pkg")
	lib_dir = base / "lib"
	lib_dir.mkdir(parents=True, exist_ok=True)
	(lib_dir / "boxlib.drift").write_text(LIB_SOURCE, encoding="utf-8")
	pkg_root = base / "pkg_root"
	trust_path = base / "trust.json"
	_publish_signed_pkg(
		lib_dir,
		src_files=[lib_dir / "boxlib.drift"],
		package_id="boxlib",
		package_version="1.0.0",
		namespace_glob="boxlib.*",
		dest_pkg_root=pkg_root,
		dest_trust_path=trust_path,
	)
	return pkg_root, trust_path


def test_consumer_local_box_field_survives_package_roundtrip(
	_built_boxlib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""The consumer loads `boxlib` (whose `Crate` variant has a `Box<Int>`
	payload field) WHILE `std.core` exposes its re-exported `core.Box`, then
	DIRECTLY constructs `lib.Crate::Holds(lib.Box<Int>::One(7))` and matches it,
	running → exit 7.  (The producer's `make_crate` / `crate_get` helpers remain
	exported as producer-side controls but are not used by the consumer.)

	Directly constructing + matching the consumer-visible, deserialized variant
	proves its `Box<Int>` payload field's nominal identity is the producer's own
	`Box` (evaluated during package schema deserialization via
	`TypeTable._eval_generic_type_expr`), not the cross-module `core.Box`
	re-export alias.
	"""
	pkg_root, trust_path = _built_boxlib
	src = tmp_path / "main.drift"
	src.write_text(CONSUMER_SOURCE, encoding="utf-8")
	out = tmp_path / "a.out"
	build = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc", "--dev",
			"--target-word-bits", "64",
			"--stdlib-root", str(stdlib_root()),
			"--package-root", str(pkg_root),
			"--dep", "boxlib@1.0.0",
			"--trust-store", str(trust_path),
			str(src), "-o", str(out),
		],
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	assert build.returncode == 0, (
		"consumer build failed; the producer's local `Box<Int>` field was likely "
		f"hijacked by the re-exported core.Box during schema deserialization:\n{build.stderr[-1500:]}"
	)
	run = subprocess.run([str(out)], capture_output=True, text=True)
	assert run.returncode == 7, f"expected 7 from crate_get, got {run.returncode}: {run.stderr[-400:]}"
