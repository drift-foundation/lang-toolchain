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


def test_envelope_variance_same_sci_resolves_via_argv_precedence(
	tmp_path: Path,
) -> None:
	"""Same `(package_id, version, source_content_id)` under two
	`--package-root` dirs but with DIFFERENT `artifact_sha256` (envelope
	variance from independent sign/build runs over the same source) is
	NOT a hard conflict.  The prepass must:

	  1. accept the compile (envelope variance is the steady state for
	     two-lane staging vs certified consumption, re-deploys, and
	     consumer-side gate tests that pass both a tmp build root and
	     `$DRIFT_PKG_ROOT`);
	  2. select the artifact from the argv-first `--package-root` and
	     drop the others from the candidate set BEFORE the verify loop,
	     so a lower-precedence same-SCI loser cannot break the selected
	     winner (its trust-gate result is never consulted because it
	     never reaches the verify loop -- no fallback in either
	     direction: if the argv-first winner fails verification, the
	     compile fails);
	  3. emit an operator-visible diagnostic naming the chosen path and
	     the dropped path(s).

	Swapping argv order must flip the chosen winner -- pinning that
	selection is argv-precedence, not sort-by-path.

	Pins the web-team-reported bug
	(drift-web `tools/run-consumer-tests.sh` consumer-check):
	the same `web-client@0.4.1` was present in both
	`/home/.../certified/...` and a `/tmp/...` build root with the same
	SCI but different artifact_sha256, and the prepass refused the
	compile.
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

	# Build child@0.0.0 twice with the SAME `--source-content-id`
	# sentinel but from two source trees that differ in a way driftc's
	# emit captures into the artifact bytes (varying source body via a
	# trailing comment changes parsed spans, which the emitted dmir
	# reflects).  This produces:
	#   - identical manifest `source_content_id` (we stamp it via the
	#     CLI flag, per the existing fixture's note at line 393);
	#   - different `.dmp` bytes => different `artifact_sha256`;
	# which is exactly the envelope-variance shape.  No hand-mutation
	# of .dmp internals -- both artifacts go through the public driftc
	# emit + drift-deploy sign path.
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

	dmp_a = _build_variant(CHILD_SRC, "a")
	dmp_b = _build_variant(
		CHILD_SRC + "\n// envelope-variance-marker: variant-b\n",
		"b",
	)

	# Test premise: two artifact files for the same authored identity.
	a_bytes = dmp_a.read_bytes()
	b_bytes = dmp_b.read_bytes()
	assert sha256(a_bytes).hexdigest() != sha256(b_bytes).hexdigest(), (
		"test premise broken: the two child.dmp artifact files hash "
		"identically; this test needs envelope variance (different "
		"artifact_sha256 under uniform source_content_id).  If driftc "
		"changed to not capture the comment into the emitted dmir, "
		"adjust the variant input to vary something the emit DOES "
		"capture (e.g. an additional pub fn that's never imported)."
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
		# The envelope-variance info diagnostic is operator-facing and
		# is emitted to stderr to avoid producing concatenated JSON
		# objects on stdout, which would break parsers.
		stdout = res.stdout.strip()
		stdout_diag: dict | None = None
		if stdout:
			stdout_diag = json.loads(stdout)  # raises if not valid JSON
		return res.returncode, stdout_diag, res.stderr

	winner_path_a = libs_a / "child" / "0.0.0" / "child.dmp"
	loser_path_b = libs_b / "child" / "0.0.0" / "child.dmp"

	# Round 1: libs_a first.  argv-first artifact (libs_a copy) should win.
	rc1, stdout_diag1, stderr1 = _compile_with_roots(libs_a, libs_b)
	assert rc1 == 0, (
		f"envelope variance compile (libs_a first) failed; rc={rc1}; "
		f"stdout_diag: {stdout_diag1!r}; stderr: {stderr1!r}"
	)
	# JSON stdout payload must be present and report success cleanly
	# -- no errors leaked into it, and no `info` packets either (the
	# variance info goes to stderr).
	assert stdout_diag1 is not None and stdout_diag1.get("exit_code") == 0, (
		f"expected clean success JSON payload; got: {stdout_diag1!r}"
	)
	assert not stdout_diag1.get("diagnostics"), (
		f"unexpected JSON diagnostics in success payload "
		f"(variance info should be on stderr): {stdout_diag1!r}"
	)
	# Operator-visible info diagnostic must be in stderr, naming chosen + dropped.
	assert "envelope variance" in stderr1, (
		f"expected envelope-variance info diagnostic on stderr; "
		f"stderr was: {stderr1!r}"
	)
	assert "argv precedence" in stderr1
	import re as _re
	_chosen1 = _re.search(r"chosen:\s*(\S+)", stderr1)
	_dropped1 = _re.search(r"dropped:\s*\n\s*-\s*(\S+)", stderr1)
	assert _chosen1 is not None and _dropped1 is not None, (
		f"variance info diagnostic shape unexpected; stderr: {stderr1!r}"
	)
	assert _chosen1.group(1) == str(winner_path_a), (
		f"expected libs_a path as chosen in argv-first round; "
		f"got chosen={_chosen1.group(1)!r}; stderr: {stderr1!r}"
	)
	assert _dropped1.group(1) == str(loser_path_b), (
		f"expected libs_b path as dropped in argv-first round; "
		f"got dropped={_dropped1.group(1)!r}; stderr: {stderr1!r}"
	)

	# Round 2: argv order swapped.  libs_b wins now.
	rc2, stdout_diag2, stderr2 = _compile_with_roots(libs_b, libs_a)
	assert rc2 == 0, (
		f"envelope variance compile (libs_b first) failed; rc={rc2}; "
		f"stdout_diag: {stdout_diag2!r}; stderr: {stderr2!r}"
	)
	assert stdout_diag2 is not None and stdout_diag2.get("exit_code") == 0, (
		f"expected clean success JSON payload (swapped order); "
		f"got: {stdout_diag2!r}"
	)
	assert not stdout_diag2.get("diagnostics"), (
		f"unexpected JSON diagnostics in success payload "
		f"(swapped order): {stdout_diag2!r}"
	)
	assert "envelope variance" in stderr2, (
		f"expected envelope-variance info diagnostic on stderr "
		f"(swapped order); stderr was: {stderr2!r}"
	)
	# Selection must be argv-order, not sort-by-path: libs_b is now the chosen winner.
	# Locate the chosen/dropped lines explicitly to avoid the trivial
	# "both paths appear somewhere in the message" false-positive.
	_chosen_match = _re.search(r"chosen:\s*(\S+)", stderr2)
	_dropped_match = _re.search(r"dropped:\s*\n\s*-\s*(\S+)", stderr2)
	assert _chosen_match is not None and _dropped_match is not None, (
		f"variance info diagnostic shape unexpected; stderr: {stderr2!r}"
	)
	assert _chosen_match.group(1) == str(loser_path_b), (
		f"expected libs_b path as chosen in swapped round; "
		f"got chosen={_chosen_match.group(1)!r}; stderr: {stderr2!r}"
	)
	assert _dropped_match.group(1) == str(winner_path_a), (
		f"expected libs_a path as dropped in swapped round; "
		f"got dropped={_dropped_match.group(1)!r}; stderr: {stderr2!r}"
	)
