# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: v1 prepass keys by (pkg, version), so when the same
versioned package sits under two `--package-root` directories the
verify pass loses the path-keyed lookup for one of the duplicates
and `load_package_v1_with_policy` is called WITHOUT
`resolved_closure`.  O3 then correctly fail-closes with:

    v1 trust: package <pkg>.zdmp declares required_deps but the
    caller did not pass resolved_closure

This is exactly the shape the drift-web preflight regression
flagged on 2026-05-21: web-client@0.4.1 sat under both the local
project lib root and the orch run's `--package-root .../lib`,
and the dropped-prepass duplicate failed the closure gate.

The fix (driftc.py): add a path-keyed `_prepass_by_path` next to
the identity-keyed `_prepass`.  This test pins it by setting up
the EXACT shape the team reported -- same versioned parent in two
roots, with its dep in only one root -- and asserting the
consumer compile now succeeds.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


CHILD_SRC = """\
module child;

export { hello };

pub fn hello() nothrow -> Int {
	return 42;
}
"""

PARENT_SRC = """\
module parent;

import child;

export { parent_hello };

pub fn parent_hello() nothrow -> Int {
	return child.hello();
}
"""

CONSUMER_SRC = """\
module consumer;

import parent;

pub fn main() nothrow -> Int {
	return parent.parent_hello();
}
"""

_TEST_SCI = "sha256:" + ("0" * 64)


def _sign_into_root(
	*, pkg_path: Path, pkg_root: Path, pkg_id: str, version: str,
	priv_seed: bytes, kid: str, dep_graph_entries: tuple = (),
) -> None:
	"""Sign + place a pre-built `.dmp` (with sentinel SCI) under
	`<pkg_root>/<pkg_id>/<version>/` with v1 author + cert claim
	sidecars.  `dep_graph_entries` is `((pkg_id, version), ...)`
	naming each dep the cert claim should attest; the helper builds
	`DepGraphEntry` rows from the same kid + sentinel SCI.

	Modelled on `_sign_package` in
	`test_pkg_transitive_dep_resolution.py` -- centralised here so
	the duplicate-root regression doesn't need to drag in the
	deps-on-disk reading logic indirectly.
	"""
	from lang.driftc.packages.author_claim_v1 import AuthorClaimBody
	from lang.driftc.packages.cert_claim_v1 import (
		CertClaimBody, CertSuite, DepGraphEntry, Toolchain,
	)
	from tools.drift_author.author_publish import (
		SignAuthorClaimOptions, sign_and_write_author_claim,
	)
	from tools.drift_deploy.cert_emit import (
		SignCertClaimOptions, sign_and_write_cert_claim,
	)
	dest = pkg_root / pkg_id / version
	dest.mkdir(parents=True, exist_ok=True)
	shutil.copy2(str(pkg_path), str(dest / f"{pkg_id}.dmp"))
	pkg_bytes = pkg_path.read_bytes()

	# dep_graph rows: same-key shortcut (this fixture shares one kid
	# across all artifacts).
	dep_graph: list[DepGraphEntry] = []
	for dep_id, dep_ver in dep_graph_entries:
		dep_dmp = pkg_root / dep_id / dep_ver / f"{dep_id}.dmp"
		dep_bytes = dep_dmp.read_bytes()
		dep_graph.append(DepGraphEntry(
			package_id=dep_id, version=dep_ver,
			artifact_sha256="sha256:" + sha256(dep_bytes).hexdigest(),
			source_content_id=_TEST_SCI,
			author_kid=kid, cert_kid=kid,
			dep_kind="direct",
		))

	# Author claim: declares required_deps so the v1 closure walker
	# fires on the consumer side -- the exact code path this test
	# is pinning.
	required_deps_tuple = tuple(
		__import__(
			"lang.driftc.packages.author_claim_v1", fromlist=["RequiredDep"]
		).RequiredDep(name=d_id, version_range=d_ver.rsplit(".", 1)[0])
		for d_id, d_ver in dep_graph_entries
	)
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=AuthorClaimBody(
			schema_version=1, package_id=pkg_id, version=version,
			namespaces=(f"{pkg_id}.*",),
			source_content_id=_TEST_SCI,
			required_deps=required_deps_tuple,
			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed, sidecar_dir=dest,
	))
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=CertClaimBody(
			schema_version=1, package_id=pkg_id, version=version,
			artifact_sha256="sha256:" + sha256(pkg_bytes).hexdigest(),
			source_content_id=_TEST_SCI, target="test-target",
			toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="test"),
			dep_graph=tuple(dep_graph),
			cert_suite=CertSuite(
				id="drift-deploy/test", version="1.0",
				result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64),
			),
			run_id=f"test-{pkg_id}",
			run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=priv_seed, sidecar_dir=dest,
	))


def test_duplicate_package_root_does_not_drop_resolved_closure(
	tmp_path: Path,
) -> None:
	"""Same versioned parent in two `--package-root` dirs must NOT
	cause v1 verification to lose `resolved_closure` for whichever
	duplicate the (pkg, version)-keyed prepass overwrote.
	"""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.drift.crypto import compute_ed25519_kid

	# One key signs everything in this fixture.
	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	priv_seed = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	libs_a = tmp_path / "libs_a"
	libs_b = tmp_path / "libs_b"
	libs_a.mkdir()
	libs_b.mkdir()
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {
			"parent.*": {"authors": [kid], "certifiers": [kid]},
			"child.*":  {"authors": [kid], "certifiers": [kid]},
		},
		"revoked": [],
	}))

	# Build child@0.0.0 (no deps).
	child_src_dir = tmp_path / "child_src"
	child_src_dir.mkdir()
	(child_src_dir / "child.drift").write_text(CHILD_SRC)
	child_dmp = tmp_path / "child.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "-M", str(child_src_dir), str(child_src_dir / "child.drift"),
		 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
		 "--package-id", "child", "--package-version", "0.0.0",
		 "--package-target", "test-target",
		 "--source-content-id", _TEST_SCI,
		 "--emit-package", str(child_dmp), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"child build failed: {res.stderr[:500]}"
	_sign_into_root(
		pkg_path=child_dmp, pkg_root=libs_a, pkg_id="child", version="0.0.0",
		priv_seed=priv_seed, kid=kid,
	)

	# Build parent@0.1.0 (declares child as required_dep + imports
	# it).  Consumes child via libs_a + --dep at build time.
	parent_src_dir = tmp_path / "parent_src"
	parent_src_dir.mkdir()
	(parent_src_dir / "parent.drift").write_text(PARENT_SRC)
	parent_dmp = tmp_path / "parent.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "-M", str(parent_src_dir), str(parent_src_dir / "parent.drift"),
		 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
		 "--package-id", "parent", "--package-version", "0.1.0",
		 "--package-target", "test-target",
		 "--source-content-id", _TEST_SCI,
		 "--package-dep", "child=0",
		 "--package-root", str(libs_a),
		 "--dep", "child@0.0.0",
		 "--trust-store", str(trust_path),
		 "--emit-package", str(parent_dmp), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"parent build failed: {res.stderr[:500]}"
	_sign_into_root(
		pkg_path=parent_dmp, pkg_root=libs_a, pkg_id="parent", version="0.1.0",
		priv_seed=priv_seed, kid=kid,
		dep_graph_entries=(("child", "0.0.0"),),
	)

	# Mirror parent into libs_b -- the EXACT failure shape the
	# orch run hit on 2026-05-21.  libs_b deliberately does NOT
	# carry child; only the same (pkg, version) of parent.
	src = libs_a / "parent" / "0.1.0"
	dst = libs_b / "parent" / "0.1.0"
	dst.mkdir(parents=True)
	for f in src.iterdir():
		shutil.copy2(f, dst / f.name)

	# Sanity: parent in both roots, child in libs_a only.
	assert (libs_a / "parent" / "0.1.0").is_dir()
	assert (libs_b / "parent" / "0.1.0").is_dir()
	assert (libs_a / "child" / "0.0.0").is_dir()
	assert not (libs_b / "child").exists()

	# Byte-identity guard for the test premise: `shutil.copy2` copies
	# content bytes (plus mtime/mode metadata), so the two `parent.dmp`
	# files MUST hash to the same sha256.  This pins what the
	# "OK on duplicate roots" path actually means -- duplicates must
	# be byte-identical; the production-side conflicting-duplicate
	# fail-closed (exercised by the sibling test below) covers what
	# happens when they're NOT.
	a_dmp = (libs_a / "parent" / "0.1.0" / "parent.dmp").read_bytes()
	b_dmp = (libs_b / "parent" / "0.1.0" / "parent.dmp").read_bytes()
	assert sha256(a_dmp).hexdigest() == sha256(b_dmp).hexdigest(), (
		"test premise broken: mirrored parent.dmp files diverged in bytes; "
		"the OK-path regression only applies to byte-identical duplicates"
	)

	# Compile a consumer with BOTH roots present.  Pre-fix this
	# would error:
	#   "package parent.zdmp declares required_deps but the
	#    caller did not pass resolved_closure"
	# for whichever parent the (pkg, version) prepass key dropped.
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text(CONSUMER_SRC)
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(consumer_dir / "consumer.drift"),
		"--stdlib-root", str(stdlib),
		"--target-word-bits", "64",
		"--package-root", str(libs_a),
		"--package-root", str(libs_b),
		"--dep", "parent@0.1.0",
		"--dep", "child@0.0.0",
		"--trust-store", str(trust_path),
		"--entry", "consumer::main",
		"--emit-ir", str(tmp_path / "consumer.ll"),
		"--test-build-only",
		"--json",
	]
	res = subprocess.run(
		cmd, cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	stdout = res.stdout.strip()
	stderr = res.stderr.strip()
	msgs: list[str] = []
	if stdout:
		try:
			diag = json.loads(stdout)
			msgs = [d.get("message", "")[:300] for d in diag.get("diagnostics", [])]
		except json.JSONDecodeError:
			msgs = [stdout[:300]]
	combined = "\n".join(msgs) + "\n" + stderr
	assert "did not pass resolved_closure" not in combined, (
		"prepass duplicate-root regression returned: same (pkg, version) "
		"under two `--package-root` dirs caused the path-keyed prepass "
		"lookup to lose one duplicate; that candidate then hit "
		f"load_package_v1_with_policy without resolved_closure.  diag: {combined!r}"
	)
	assert res.returncode == 0, (
		f"consumer compile failed (rc={res.returncode}); diagnostics: "
		f"{msgs!r}; stderr: {stderr!r}"
	)


def test_conflicting_duplicate_package_root_fails_closed(
	tmp_path: Path,
) -> None:
	"""When two `--package-root` dirs both hold a file labeled
	`<pkg>@<version>` but the bytes (and therefore artifact_sha256 /
	source_content_id) DIFFER, the prepass conflict guard MUST
	reject the compile rather than silently let one identity win.

	This pins the safety the first-write-wins `_prepass.setdefault`
	relies on -- the team flagged it when reviewing the previous
	OK-path fix.  Without this guard, a hostile mirror could ship a
	differently-signed .dmp under the same `(pkg, version)` label
	and the closure walker would attest the wrong identity for one
	of the duplicates.
	"""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.drift.crypto import compute_ed25519_kid

	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	priv_seed = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	libs_a = tmp_path / "libs_a"
	libs_b = tmp_path / "libs_b"
	libs_a.mkdir()
	libs_b.mkdir()
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {
			"child.*":  {"authors": [kid], "certifiers": [kid]},
		},
		"revoked": [],
	}))

	# Build two DIFFERENT `child` artifacts both labeled `0.0.0`.
	# Different source -> different bytes -> different SCI.
	# (Real-world repro: same versioned package re-built from
	# different sources, mistakenly placed in two roots; or a
	# hostile mirror substituting bytes under a trusted label.)
	def _build_and_sign(libs_root: Path, src_text: str) -> Path:
		src_dir = tmp_path / f"child_src_{libs_root.name}"
		src_dir.mkdir()
		(src_dir / "child.drift").write_text(src_text)
		dmp = tmp_path / f"child_{libs_root.name}.dmp"
		# Use a distinct (still sentinel-prefixed) SCI per variant so
		# the prepass conflict guard sees two different identities.
		# The driftc emit doesn't actually recompute SCI from source
		# in this path -- it stamps whatever --source-content-id
		# says into the manifest.
		sci_suffix = "a" if libs_root is libs_a else "b"
		variant_sci = "sha256:" + (sci_suffix * 64)
		r = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc",
			 "-M", str(src_dir), str(src_dir / "child.drift"),
			 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
			 "--package-id", "child", "--package-version", "0.0.0",
			 "--package-target", "test-target",
			 "--source-content-id", variant_sci,
			 "--emit-package", str(dmp), "--test-build-only"],
			cwd=ROOT, capture_output=True, text=True,
			timeout=sanitizer_timeout(120),
		)
		assert r.returncode == 0, f"child build failed: {r.stderr[:500]}"
		# Sign with a manifest-stamped SCI matching what we passed.
		# This requires building author + cert claim bodies that
		# carry `variant_sci`; reuse `_sign_into_root` but with a
		# custom SCI sentinel by patching _TEST_SCI locally.
		dest = libs_root / "child" / "0.0.0"
		dest.mkdir(parents=True, exist_ok=True)
		shutil.copy2(str(dmp), str(dest / "child.dmp"))
		pkg_bytes = dmp.read_bytes()
		from lang.driftc.packages.author_claim_v1 import AuthorClaimBody
		from lang.driftc.packages.cert_claim_v1 import (
			CertClaimBody, CertSuite, Toolchain,
		)
		from tools.drift_author.author_publish import (
			SignAuthorClaimOptions, sign_and_write_author_claim,
		)
		from tools.drift_deploy.cert_emit import (
			SignCertClaimOptions, sign_and_write_cert_claim,
		)
		sign_and_write_author_claim(SignAuthorClaimOptions(
			body=AuthorClaimBody(
				schema_version=1, package_id="child", version="0.0.0",
				namespaces=("child.*",),
				source_content_id=variant_sci,
				required_deps=(),
				release_utc="2026-05-19T00:00:00Z",
			),
			seed32=priv_seed, sidecar_dir=dest,
		))
		sign_and_write_cert_claim(SignCertClaimOptions(
			body=CertClaimBody(
				schema_version=1, package_id="child", version="0.0.0",
				artifact_sha256="sha256:" + sha256(pkg_bytes).hexdigest(),
				source_content_id=variant_sci, target="test-target",
				toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="test"),
				dep_graph=(),
				cert_suite=CertSuite(
					id="drift-deploy/test", version="1.0",
					result="pass",
					result_evidence_sha256="sha256:" + ("f" * 64),
				),
				run_id="test-child",
				run_started_utc="2026-05-19T00:00:00Z",
				evidence_sha256="sha256:" + ("0" * 64),
			),
			seed32=priv_seed, sidecar_dir=dest,
		))
		return dest

	dest_a = _build_and_sign(libs_a, CHILD_SRC)
	# Use materially different source to guarantee divergent bytes.
	_build_and_sign(libs_b, CHILD_SRC.replace("return 42;", "return 7;"))

	# Confirm the test premise: the two child.dmp files genuinely differ.
	a_bytes = (libs_a / "child" / "0.0.0" / "child.dmp").read_bytes()
	b_bytes = (libs_b / "child" / "0.0.0" / "child.dmp").read_bytes()
	assert sha256(a_bytes).hexdigest() != sha256(b_bytes).hexdigest(), (
		"test premise broken: the two child.dmp files hash identically; "
		"this test is supposed to exercise CONFLICTING duplicates"
	)

	# Compile a trivial consumer.  Either child variant would work
	# on its own, but with BOTH roots in play the conflict guard
	# must refuse.
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text("""\
module consumer;

import child;

pub fn main() nothrow -> Int {
	return child.hello();
}
""")
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(consumer_dir / "consumer.drift"),
		"--stdlib-root", str(stdlib),
		"--target-word-bits", "64",
		"--package-root", str(libs_a),
		"--package-root", str(libs_b),
		"--dep", "child@0.0.0",
		"--trust-store", str(trust_path),
		"--entry", "consumer::main",
		"--emit-ir", str(tmp_path / "consumer.ll"),
		"--test-build-only",
		"--json",
	]
	res = subprocess.run(
		cmd, cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	stdout = res.stdout.strip()
	msgs: list[str] = []
	if stdout:
		try:
			diag = json.loads(stdout)
			msgs = [d.get("message", "")[:1500] for d in diag.get("diagnostics", [])]
		except json.JSONDecodeError:
			msgs = [stdout[:1500]]
	combined = "\n".join(msgs)
	assert res.returncode != 0, (
		f"conflicting duplicate accepted silently (rc=0); the prepass "
		f"guard should have rejected it.  diagnostics: {msgs!r}"
	)
	assert "different source_content_id" in combined, (
		f"expected the conflict-guard diagnostic to name "
		f"'different source_content_id...'; got: {combined!r}"
	)
	# The diagnostic must list BOTH paths (so the operator can
	# tell which copies disagree).
	assert str(libs_a) in combined and str(libs_b) in combined, (
		f"expected the conflict-guard diagnostic to name both "
		f"`--package-root` paths; got: {combined!r}"
	)


def test_envelope_divergence_same_sci_different_artifact_fails_hard(
	tmp_path: Path,
) -> None:
	"""NEGATIVE regression — envelope-divergence policy (0.33.22).

	Two visible package candidates with the SAME
	`(package_id, version, source_content_id)` but DIFFERENT
	`artifact_sha256` is a HARD FAILURE.  The prepass MUST:

	  1. fail closed (non-zero exit) — it must NOT silently select one
	     by `--package-root` argv precedence;
	  2. fail regardless of argv order (it's not a precedence question);
	  3. emit an actionable `envelope divergence` diagnostic naming the
	     package id/version, the source_content_id, and EACH candidate's
	     path + artifact_sha256, with likely-causes and suggested-fixes.

	Rationale: after path canonicalization (0.33.21) made
	`artifact_sha256` build-path-independent, same-SCI/different-artifact
	is no longer benign rebuild/path noise.  In the non-malicious case it
	signals a producer/toolchain/package-emitter change, a package-format
	migration, a target/platform mismatch, stale/mixed roots, or a
	remaining nondeterminism bug; in the malicious case it is exactly the
	shape to notice — the same source-identity claim attached to different
	produced bytes.  Silent root-precedence selection is the wrong default
	for that threat, so the toolchain refuses and surfaces every candidate.

	This is the deliberate counterpart to the byte-identical duplicate-root
	case (`test_duplicate_package_root_does_not_drop_resolved_closure`),
	which still resolves by path dedup.  This test previously asserted the
	*opposite* (accept + argv precedence); the variance it relied on was an
	absolute-path artifact difference that 0.33.21 canonicalized away, which
	is what surfaced this policy decision.
	"""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.drift.crypto import compute_ed25519_kid

	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	priv_seed = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	libs_a = tmp_path / "libs_a"
	libs_b = tmp_path / "libs_b"
	libs_a.mkdir()
	libs_b.mkdir()
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {
			"child.*":  {"authors": [kid], "certifiers": [kid]},
		},
		"revoked": [],
	}))

	# This test intentionally CONSTRUCTS the envelope-divergence shape:
	# the same trusted source-identity stamp (`--source-content-id`
	# sentinel) attached to two packages whose payload bytes differ —
	# the shape a cross-toolchain / cert-pool rollover would produce (the
	# same source re-emitted by a different compiler build yields a
	# different `.dmp`, same SCI).  It is NOT a *natural* source of
	# variance; it exists to pin that the prepass now FAILS HARD on
	# same-SCI / different-artifact_sha256 (policy 0.33.22), rather than
	# silently selecting one by `--package-root` precedence.
	#
	# Variant B differs by an EXPLICIT, INTENTIONAL emitted-content
	# delta: an extra exported function.  That is captured in the
	# serialized package (exports + signatures + hir_funcs), so the two
	# `.dmp` files genuinely differ in bytes, while the stamped SCI
	# stays uniform (stamped via the CLI flag, per the fixture note at
	# line 393).  Do NOT manufacture the difference with a trailing
	# comment or by building under different directories: comments are
	# not a reliable emitted-payload difference, and 0.33.21 canonicalized
	# absolute `loc.file` paths to project-relative, so path differences
	# no longer change the artifact bytes (which is correct — that change
	# is what makes artifact_sha256 reproducible across build paths, and
	# is exactly why unexplained divergence is now treated as a hard error).
	#
	# Both variants export `hello` so the consumer *would* compile against
	# either; the point is that the prepass refuses to choose at all.
	def _build_variant(src_text: str, variant_name: str) -> Path:
		src_dir = tmp_path / f"child_src_{variant_name}"
		src_dir.mkdir()
		(src_dir / "child.drift").write_text(src_text)
		dmp = tmp_path / f"child_{variant_name}.dmp"
		r = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc",
			 "-M", str(src_dir), str(src_dir / "child.drift"),
			 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
			 "--package-id", "child", "--package-version", "0.0.0",
			 "--package-target", "test-target",
			 "--source-content-id", _TEST_SCI,
			 "--emit-package", str(dmp), "--test-build-only"],
			cwd=ROOT, capture_output=True, text=True,
			timeout=sanitizer_timeout(120),
		)
		assert r.returncode == 0, (
			f"child variant {variant_name} build failed: {r.stderr[:500]}"
		)
		return dmp

	# Variant B adds an extra exported, never-imported function so the
	# emitted package payload genuinely differs while the stamped SCI
	# stays uniform.
	child_src_variant_b = (
		"module child;\n\n"
		"export { hello, envelope_variant_b };\n\n"
		"pub fn hello() nothrow -> Int {\n\treturn 42;\n}\n\n"
		"pub fn envelope_variant_b() nothrow -> Int {\n\treturn 7;\n}\n"
	)
	dmp_a = _build_variant(CHILD_SRC, "a")
	dmp_b = _build_variant(child_src_variant_b, "b")

	# Test premise: two artifact files for the same authored identity
	# (same SCI) but different bytes (different artifact_sha256).
	a_bytes = dmp_a.read_bytes()
	b_bytes = dmp_b.read_bytes()
	assert sha256(a_bytes).hexdigest() != sha256(b_bytes).hexdigest(), (
		"test premise broken: the two child.dmp artifact files hash "
		"identically; this test needs envelope divergence (different "
		"artifact_sha256 under uniform source_content_id).  Variant B "
		"must differ by an emitted-content delta (it adds an exported "
		"function); if a future change stops capturing that into the "
		"dmir, vary another field the emit DOES capture.  Do NOT fall "
		"back to a trailing comment or a different build directory — "
		"neither changes the artifact bytes on current toolchains."
	)

	# Manifest SCI uniformity check (independent of the prepass under
	# test): peek both manifests directly.
	from lang.driftc.packages.dmir_pkg_v0 import load_dmir_pkg_v0
	pkg_a = load_dmir_pkg_v0(dmp_a)
	pkg_b = load_dmir_pkg_v0(dmp_b)
	assert pkg_a.manifest.get("source_content_id") == _TEST_SCI
	assert pkg_b.manifest.get("source_content_id") == _TEST_SCI

	_sign_into_root(
		pkg_path=dmp_a, pkg_root=libs_a, pkg_id="child", version="0.0.0",
		priv_seed=priv_seed, kid=kid,
	)
	_sign_into_root(
		pkg_path=dmp_b, pkg_root=libs_b, pkg_id="child", version="0.0.0",
		priv_seed=priv_seed, kid=kid,
	)

	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text("""\
module consumer;

import child;

pub fn main() nothrow -> Int {
	return child.hello();
}
""")

	def _compile_with_roots(*roots: Path) -> tuple[int, list[str], str]:
		cmd = [
			sys.executable, "-m", "lang.driftc.driftc",
			str(consumer_dir / "consumer.drift"),
			"--stdlib-root", str(stdlib),
			"--target-word-bits", "64",
		]
		for r in roots:
			cmd.extend(["--package-root", str(r)])
		cmd.extend([
			"--dep", "child@0.0.0",
			"--trust-store", str(trust_path),
			"--entry", "consumer::main",
			"--emit-ir", str(tmp_path / f"consumer_{'_'.join(p.name for p in roots)}.ll"),
			"--test-build-only",
			"--json",
		])
		res = subprocess.run(
			cmd, cwd=ROOT, capture_output=True, text=True,
			timeout=sanitizer_timeout(120),
		)
		# Stdout under `--json` must be a single parseable JSON object
		# (or empty if the driver chose not to emit a terminal payload).
		# On envelope divergence the prepass fails closed, emitting the
		# error as the terminal JSON payload on stdout (exit_code 1 +
		# diagnostics); the human path prints the same error to stderr.
		# `_diag_text` below reads from whichever channel carried it.
		stdout = res.stdout.strip()
		stdout_diag: dict | None = None
		if stdout:
			stdout_diag = json.loads(stdout)  # raises if not valid JSON
		return res.returncode, stdout_diag, res.stderr

	path_a = libs_a / "child" / "0.0.0" / "child.dmp"
	path_b = libs_b / "child" / "0.0.0" / "child.dmp"
	sha_a = sha256(a_bytes).hexdigest()
	sha_b = sha256(b_bytes).hexdigest()

	def _diag_text(stdout_diag: dict | None, stderr: str) -> str:
		"""Combined diagnostic text from the JSON stdout payload + stderr."""
		parts = [stderr]
		if stdout_diag:
			for d in stdout_diag.get("diagnostics", []) or []:
				parts.append(str(d.get("message", "")))
		return "\n".join(parts)

	def _assert_envelope_divergence_failure(roots: tuple[Path, ...]) -> None:
		rc, stdout_diag, stderr = _compile_with_roots(*roots)
		order = " ".join(p.name for p in roots)
		# 1. fail closed — no silent selection.
		assert rc != 0, (
			f"envelope divergence MUST fail closed (roots: {order}); "
			f"got rc={rc}; stdout_diag={stdout_diag!r}; stderr={stderr!r}"
		)
		text = _diag_text(stdout_diag, stderr)
		# 3. actionable diagnostic naming the policy + every candidate.
		assert "envelope divergence" in text, (
			f"missing envelope-divergence diagnostic (roots: {order}); got: {text!r}"
		)
		# package id/version + SCI
		assert "child" in text and "0.0.0" in text, f"missing pkg id/version; got: {text!r}"
		assert _TEST_SCI in text, f"missing source_content_id; got: {text!r}"
		# BOTH candidate paths named
		assert str(path_a) in text and str(path_b) in text, (
			f"both candidate paths must be named; got: {text!r}"
		)
		# BOTH artifact SHAs named
		assert sha_a in text and sha_b in text, (
			f"both artifact_sha256 values must be named; got: {text!r}"
		)
		# causes + fixes present (actionable)
		assert "likely causes" in text and "suggested fixes" in text, (
			f"diagnostic must list likely causes + suggested fixes; got: {text!r}"
		)
		# It must NOT have silently picked a winner by precedence.
		assert "argv precedence" not in text and "Selecting by" not in text, (
			f"must not select by precedence on divergence; got: {text!r}"
		)

	# 2. Fails regardless of argv order — it is not a precedence question.
	_assert_envelope_divergence_failure((libs_a, libs_b))
	_assert_envelope_divergence_failure((libs_b, libs_a))
