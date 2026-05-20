# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""ConstShare structural synthesis — cross-module + package-mixed
verification (Phase 1 follow-up; not a separate release).

This file pins behavior already covered by the Phase 1
implementation (release 0.31.43) — same-build multi-module
composition AND package-mixed composition (producer publishes
Inner with auto-derived ConstShare; consumer composes Outer
over the packaged Inner).  No new compiler logic; this is a
test/docs patch confirming Phase 1's per-iteration fixed-point
+ visibility-aware proof world already cover both scenarios.

What this file pins:
  - Positive: A.Outer composes over B.Inner where B is a
    same-build source module; both auto-derive;
    `outer.const_share()` resolves and dispatches into the
    synthesized Inner.const_share through the field.
  - Positive: A struct using B's struct as TWO fields auto-
    derives correctly (deduplication / single registration of
    B.Inner's impl).
  - Positive: package-mixed — consumer composes Outer over
    a struct that was auto-derived in a separately published
    package.
  - Negative (constructable form): A imports B, B's struct is
    nameable, but B's struct contains a non-ConstShare field
    (`Arc<T>`) → composition fails at field qualification, A
    does NOT auto-derive.

What this file does NOT pin:
  - Pure prover-level visibility regression
    (`linked_world.visible_world(M)` correctly excluding
    non-imported impls).  That is unconstructable in source
    (Drift's compile-multiple-files mode auto-resolves
    cross-source-file names regardless of explicit imports)
    and is pinned at the unit level by
    `test_const_share_phase1_visibility_unit.py`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _compile_two_modules(
	tmp_path: Path,
	*,
	mod_a_source: str,
	mod_b_source: str,
	mod_a_name: str = "a",
	mod_b_name: str = "b",
	entry_module: str | None = None,
	test_build_only: bool = True,
) -> tuple[int, list[dict]]:
	"""Compile two source-module files in a single build.  Returns
	(exit_code, error_diagnostics)."""
	src_root = tmp_path / "src"
	src_root.mkdir(parents=True, exist_ok=True)
	(src_root / f"{mod_b_name}.drift").write_text(mod_b_source, encoding="utf-8")
	(src_root / f"{mod_a_name}.drift").write_text(mod_a_source, encoding="utf-8")

	cmd = [
		sys.executable, "-m", "lang.driftc",
		"-M", str(src_root),
		"--stdlib-root", str(stdlib_root()),
		str(src_root / f"{mod_a_name}.drift"),
		str(src_root / f"{mod_b_name}.drift"),
		"--json",
	]
	if test_build_only:
		cmd.append("--test-build-only")
	if entry_module is not None:
		cmd.extend(["--entry", f"{entry_module}::main"])

	rc = subprocess.run(
		cmd,
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
		timeout=120,
	)
	if not rc.stdout.strip():
		return rc.returncode, [{"message": rc.stderr[:2000], "code": "STDERR"}]
	try:
		payload = json.loads(rc.stdout)
	except json.JSONDecodeError:
		return rc.returncode, [{"message": rc.stdout[:2000], "code": "PARSE"}]
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return payload.get("exit_code", rc.returncode), errs


# ── Positive — A imports B; A.Outer composes over B.Inner ────────


def test_phase2_outer_in_a_composes_over_inner_in_b(tmp_path: Path) -> None:
	"""Module B defines an eligible Inner struct.  Module A
	imports B, declares Outer with a B.Inner field, and calls
	outer.const_share().

	Both modules must auto-derive ConstShare via Phase 1's
	per-iteration fixed-point: B's Inner registers first
	(its only field is a ConstArc<String>, qualifies on
	iteration 1), then A's Outer (which now sees B's Inner as
	ConstShare-provable) registers on iteration 2.

	Phase 2 doesn't add new compiler logic in this slice — it
	verifies that Phase 1's fixed-point/per-iteration model
	already covers same-build multi-module composition, with
	the visibility-aware proof world correctly seeing B's
	impls from A's perspective (because A imports B)."""
	mod_b = """\
module b;

import std.core as core;

export { Inner };

pub struct Inner {
\tpub handle: core.ConstArc<String>
}
"""
	mod_a = """\
module a;

import std.core as core;
import std.core.shareable as shareable;
import b as b;

use trait shareable.ConstShare;

pub struct Outer {
\tpub inner: b.Inner,
\tpub tag: Int
}

fn assert_cs<T>() nothrow -> Void require T is shareable.ConstShare { }

fn main() nothrow -> Int {
\tval i = b.Inner(handle = core.const_arc<type String>("hi"));
\tval o = Outer(inner = i, tag = 1);
\tassert_cs<type Outer>();
\tval o2 = o.const_share();
\treturn 0;
}
"""
	rc, errs = _compile_two_modules(
		tmp_path,
		mod_a_source=mod_a,
		mod_b_source=mod_b,
		entry_module="a",
	)
	assert rc == 0, (
		"cross-module composition must auto-derive: rc={}, errs:\n{}".format(
			rc,
			"\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs),
		)
	)


def test_phase2_a_inner_visible_via_b_reexport(tmp_path: Path) -> None:
	"""When B re-exports its inner type and A imports B, the
	composition still works through the re-export chain.
	Pins that visible_world sees re-exported types correctly."""
	mod_b = """\
module b;

import std.core as core;

export { Wrapped };

pub struct Wrapped {
\tpub h: core.ConstArc<Int>
}
"""
	mod_a = """\
module a;

import std.core as core;
import std.core.shareable as shareable;
import b as b;

use trait shareable.ConstShare;

pub struct UseTwo {
\tpub one: b.Wrapped,
\tpub two: b.Wrapped
}

fn assert_cs<T>() nothrow -> Void require T is shareable.ConstShare { }

fn main() nothrow -> Int {
\tval w1 = b.Wrapped(h = core.const_arc<type Int>(1));
\tval w2 = b.Wrapped(h = core.const_arc<type Int>(2));
\tval u = UseTwo(one = w1, two = w2);
\tassert_cs<type UseTwo>();
\tval u2 = u.const_share();
\treturn 0;
}
"""
	rc, errs = _compile_two_modules(
		tmp_path,
		mod_a_source=mod_a,
		mod_b_source=mod_b,
		entry_module="a",
	)
	assert rc == 0, (
		"struct using re-exported B type twice must auto-derive: rc={}, errs:\n{}".format(
			rc,
			"\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs),
		)
	)


# ── Negative — A without B import cannot reach B.Inner ───────────


def test_phase2_a_field_with_blocked_b_type_does_not_derive(tmp_path: Path) -> None:
	"""Driver-level negative: when A's field is a B-defined
	struct that DOES NOT itself qualify for ConstShare (e.g. it
	wraps an `Arc<T>`, which is not ConstShare), A does NOT
	auto-derive — the composition fails at the field level.

	This is the constructable form of "visibility limits
	composition": both modules are visible to each other, but
	the field type's qualification is REFUTED, so A's
	qualifier blocks correctly.

	The pure prover-level visibility regression (impl invisible
	through visible_world exclusion) is unconstructable in
	source and pinned at the unit level by
	`test_const_share_phase1_visibility_unit.py`."""
	mod_b = """\
module b;

import std.core as core;

export { Holder };

pub struct Holder {
\tpub a: core.Arc<Int>
}
"""
	mod_a = """\
module a;

import std.core as core;
import std.core.shareable as shareable;
import b as b;

use trait shareable.ConstShare;

pub struct Outer {
\tpub inner: b.Holder
}

fn assert_cs<T>() nothrow -> Void require T is shareable.ConstShare { }

fn main() nothrow -> Int {
\tassert_cs<type Outer>();
\treturn 0;
}
"""
	rc, errs = _compile_two_modules(
		tmp_path,
		mod_a_source=mod_a,
		mod_b_source=mod_b,
		entry_module="a",
	)
	assert rc != 0, (
		"A.Outer with a field of B.Holder (which contains "
		"Arc<Int> — non-ConstShare) MUST NOT auto-derive.  "
		"Compiled cleanly with rc={}".format(rc)
	)
	# The expected diagnostic is E_REQUIREMENT_NOT_SATISFIED on
	# the assert_cs call site.
	rejected = any(
		e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "ConstShare" in e.get("message", "")
		for e in errs
	)
	assert rejected, (
		"expected E_REQUIREMENT_NOT_SATISFIED naming ConstShare; "
		"got:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


# ── Package/source mixed: producer publishes B; consumer composes A over B ──


def _publish_pkg_with_inner_struct(tmp_path: Path) -> tuple[Path, str, str]:
	"""Helper: build a `producer-lib` package containing
	`Inner { handle: core.ConstArc<String> }` (auto-derives
	ConstShare) and return (pkg_root, package_id, version)."""
	import shutil
	from hashlib import sha256

	def _b64(data: bytes) -> str:
		import base64
		return base64.b64encode(data).decode("ascii")

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.drift.crypto import compute_ed25519_kid

	lib_dir = tmp_path / "producer_src"
	lib_dir.mkdir(parents=True, exist_ok=True)
	(lib_dir / "producer_inner.drift").write_text(
		"""\
module producer.inner;

import std.core as core;

export { Inner };

pub struct Inner {
\tpub handle: core.ConstArc<String>
}
""",
		encoding="utf-8",
	)

	pkg_path = lib_dir / "producer-lib.dmp"
	rc = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc",
			"--dev",
			"-M", str(lib_dir),
			"--stdlib-root", str(stdlib_root()),
			str(lib_dir / "producer_inner.drift"),
			"--package-id", "producer-lib",
			"--package-version", "1.0.0",
			"--package-target", "drift-dev",
			"--source-content-id", "sha256:" + ("0" * 64),
			"--emit-package", str(pkg_path),
			"--json",
		],
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	assert rc.returncode == 0, (
		"producer-lib build failed:\n" + rc.stdout + "\n---\n" + rc.stderr[:1000]
	)

	# v1 sidecars: replace v0 `.sig` envelope with author + cert claims.
	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes_raw()
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)
	pkg_bytes = pkg_path.read_bytes()
	_TEST_SCI = "sha256:" + ("0" * 64)

	from cryptography.hazmat.primitives import serialization as _v1_serialization
	priv_seed = priv.private_bytes(
		encoding=_v1_serialization.Encoding.Raw,
		format=_v1_serialization.PrivateFormat.Raw,
		encryption_algorithm=_v1_serialization.NoEncryption(),
	)
	from lang.driftc.packages.author_claim_v1 import AuthorClaimBody
	from lang.driftc.packages.cert_claim_v1 import (
		CertClaimBody, CertSuite, Toolchain,
	)
	from lang.driftc.packages.sidecar_naming import (
		author_claim_filename, cert_claim_filename,
	)
	from tools.drift_author.author_publish import (
		SignAuthorClaimOptions, sign_and_write_author_claim,
	)
	from tools.drift_deploy.cert_emit import (
		SignCertClaimOptions, sign_and_write_cert_claim,
	)

	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=AuthorClaimBody(
			schema_version=1, package_id="producer-lib", version="1.0.0",
			namespaces=("producer.*",), source_content_id=_TEST_SCI,
			required_deps=(), target_class="library",
			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed, sidecar_dir=lib_dir,
	))
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=CertClaimBody(
			schema_version=1, package_id="producer-lib", version="1.0.0",
			artifact_sha256="sha256:" + sha256(pkg_bytes).hexdigest(),
			source_content_id=_TEST_SCI, target="drift-dev",
			toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="test"),
			dep_graph=(),
			cert_suite=CertSuite(id="drift-deploy/test", version="1.0",
				result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64)),
			run_id="test-producer-lib",
			run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=priv_seed, sidecar_dir=lib_dir,
	))

	trust = {
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"producer.*": {"authors": [kid], "certifiers": [kid]}},
		"revoked": [],
	}
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps(trust, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	pkg_root = tmp_path / "pkg_root"
	dest_dir = pkg_root / "producer-lib" / "1.0.0"
	dest_dir.mkdir(parents=True, exist_ok=True)
	shutil.copy2(str(pkg_path), str(dest_dir / "producer-lib.dmp"))
	# v1 sidecars travel with the artifact.
	for src in (
		lib_dir / author_claim_filename("producer-lib"),
		lib_dir / cert_claim_filename("producer-lib", kid),
	):
		shutil.copy2(str(src), str(dest_dir / src.name))

	return pkg_root, trust_path, "producer-lib", "1.0.0"


def test_phase2_consumer_struct_composes_over_packaged_inner(tmp_path: Path) -> None:
	"""Producer publishes `producer-lib` containing
	`producer.inner.Inner` (auto-derived ConstShare during
	producer's build, serialized into .dmp).  Consumer module
	imports the package, declares
	`Outer { inner: producer.inner.Inner }`, and the consumer-side
	synthesis (Phase 1) qualifies Outer because B's auto-derived
	impl is visible through the loaded package's LinkedWorld.

	`outer.const_share()` resolves and dispatches into B's
	synthesized Inner.const_share via the trait registry.

	Stop conditions if this test fails:
	  - B's synthesized impl didn't serialize into the .dmp
	    (snapshot timing or impl_metas missing entries) →
	    re-audit `_pre_typecheck_hirs` capture and
	    `module_exports[mid]["impls"]` writes.
	  - Consumer-side LinkedWorld doesn't load B's impl →
	    re-audit `provider_v0` / package-load impl decoding.
	  - Consumer's qualification of Outer's `inner: B.Inner`
	    field returns REFUTED instead of PROVED → re-audit
	    `prove_is(visible_world(consumer))` against the loaded
	    impl."""
	pkg_root, trust_path, pkg_id, pkg_version = _publish_pkg_with_inner_struct(tmp_path)

	consumer_root = tmp_path / "consumer_src"
	consumer_root.mkdir(parents=True, exist_ok=True)
	(consumer_root / "main.drift").write_text(
		"""\
module main;

import std.core as core;
import std.core.shareable as shareable;
import producer.inner as producer_inner;

use trait shareable.ConstShare;

pub struct Outer {
\tpub inner: producer_inner.Inner,
\tpub tag: Int
}

fn assert_cs<T>() nothrow -> Void require T is shareable.ConstShare { }

fn main() nothrow -> Int {
\tval i = producer_inner.Inner(handle = core.const_arc<type String>("hi"));
\tval o = Outer(inner = i, tag = 1);
\tassert_cs<type Outer>();
\tval o2 = o.const_share();
\treturn 0;
}
""",
		encoding="utf-8",
	)

	out_bin = tmp_path / "consumer.out"
	rc = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc",
			"-M", str(consumer_root),
			"--target-word-bits", "64",
			"--package-root", str(pkg_root),
			"--dep", f"{pkg_id}@{pkg_version}",
			"--trust-store", str(trust_path),
			str(consumer_root / "main.drift"),
			"-o", str(out_bin),
			"--json",
			"--test-build-only",
		],
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
		timeout=120,
	)
	if not rc.stdout.strip():
		pytest.fail(
			"consumer compile produced no JSON; rc={} stderr:\n{}".format(
				rc.returncode, rc.stderr[:2000]
			)
		)
	payload = json.loads(rc.stdout)
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	exit_code = payload.get("exit_code", rc.returncode)
	assert exit_code == 0, (
		"consumer struct composing over packaged producer Inner "
		"must auto-derive: rc={} errs:\n{}".format(
			exit_code,
			"\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs),
		)
	)
