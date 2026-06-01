# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Deploy step: build stdlib package and emit v1 cert claim.

Produces:
  ${DIST}/lib/stdlib/std.dmp
  ${DIST}/lib/stdlib/std.author-claim          (copied from caller-provided input)
  ${DIST}/lib/stdlib/std.cert-claim.<cert-kid>.json   (emitted here)
  ${DIST}/lib/stdlib/stdlib_dep.txt
  ${DIST}/lib/compiler/lang/driftc/packages/core_trust_v1.json

**Author-key-out-of-orch boundary.**  This deploy step performs the
certifier role only.  It does NOT hold, generate, or read any
author private key.  The Foundation stdlib `<std>.author-claim`
is an INPUT to this step, produced out-of-band by a Foundation-
controlled `drift-author publish` run (offline / separate signing
service / pre-provisioned artifact).  The deploy step:

  - reads the externally-provided author claim,
  - validates its body against the build's package identity + SCI,
  - **loads** the certifier seed from the operator-supplied path
    (`--certifier-key-file` / `$DRIFT_SIGN_KEY_FILE`).  The deploy
    step does not generate certifier seeds either; it consumes
    a key the operator already provisioned for this host,
  - signs the cert claim over the just-built `.dmp` with that
    certifier seed,
  - copies the author claim alongside the cert claim into the
    install tree, and
  - writes a `core_trust_v1.json` that maps the Foundation
    author kid (from the caller-supplied pubkey) into the
    `authors` role and the certifier kid (derived from the
    loaded seed) into the `certifiers` role.

The two roles in `core_trust_v1.json` are recorded
independently.  An organization MAY choose to use the same key
for both roles in a given release -- the trust store will list
that kid in both role lists, and `compose_verify` will see it
satisfy both role checks.  The toolchain neither requires nor
forbids this; the role separation is about which claim body is
signed at which step, not about forcing two distinct on-disk
keys.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _stdlib_sci(repo_root: Path, version: str) -> str:
	"""Compute the canonical source content id for the stdlib build.

	v1 verify requires every package's manifest SCI to match the
	author/cert claim body SCI; this helper centralises the input
	to all three sites so they cannot drift.
	"""
	from lang.driftc.packages.source_content_id import (
		compute_artifact_source_content_id,
	)
	stdlib_dir = repo_root / "stdlib"
	module_paths_rel = sorted(
		str(p.relative_to(repo_root))
		for p in stdlib_dir.rglob("*.drift")
	)
	return compute_artifact_source_content_id(
		kind="library",
		package_id="std",
		version=version,
		module_namespace="std",
		entry_module="std",
		module_paths=module_paths_rel,
		package_deps=[],
		native_deps=[],
		unsafe=False,
		asset_paths=[],
		source_root=repo_root,
	)


def build_stdlib_package(repo_root: Path, stage: Path, version: str) -> tuple[Path, str]:
	"""Build stdlib .dmp package. Returns `(dmp_path, source_content_id)`."""
	print("[deploy] building stdlib package...", flush=True)

	dmp_path = stage / "std.dmp"
	empty_stdlib = stage / "_empty_stdlib"
	empty_stdlib.mkdir(parents=True, exist_ok=True)

	# Find all stdlib .drift files.
	stdlib_dir = repo_root / "stdlib"
	sources = sorted(str(p) for p in stdlib_dir.rglob("*.drift"))
	if not sources:
		raise RuntimeError("no .drift files found under stdlib/")

	python = repo_root / ".venv" / "bin" / "python3"
	env = dict(os.environ)
	env["PYTHONPATH"] = str(repo_root)

	sci = _stdlib_sci(repo_root, version)
	cmd = [
		str(python), "-m", "lang.driftc",
		"--dev",
		"--stdlib-root", str(empty_stdlib),
		"-M", "stdlib",
	] + sources + [
		"--package-id", "std",
		"--package-version", version,
		"--package-target", "drift-dev",
		"--source-content-id", sci,
		# std.codec.gzip_encode / gzip_decode call into libz via the
		# runtime-owned shim in lang/language_runtime/codec_gzip_runtime.c.
		# The shim's deflate / inflate symbols are unresolved at the .o
		# level; consumers of the stdlib package auto-link -lz from this
		# native_deps.link_libs entry. Note: because stdlib is compiled
		# monolithically, every consumer Drift binary will carry libz.so.1
		# in DT_NEEDED at runtime regardless of whether it calls into the
		# gzip surface (std.codec's wrappers are emitted into the IR
		# unconditionally and reference codec_gzip_runtime.o, so
		# -Wl,--as-needed cannot drop libz). Accepted cost — libz is
		# universal on x86_64 Linux, the only supported target.
		"--native-link-lib", "z",
		"--emit-package", str(dmp_path),
		"--json",
	]

	result = subprocess.run(
		cmd, env=env, cwd=str(repo_root),
		capture_output=True,
	)
	if result.returncode != 0:
		sys.stderr.buffer.write(result.stderr)
		raise RuntimeError("stdlib package build failed")

	if not dmp_path.exists():
		raise RuntimeError("stdlib package build produced no output")

	return dmp_path, sci


def _validate_external_stdlib_author_claim(
	author_claim_path: Path,
	*,
	expected_version: str,
	expected_sci: str,
	expected_author_kid: str,
) -> None:
	"""Validate a caller-provided `std.author-claim`.

	The deploy step accepts the author claim as an input artifact
	(Foundation-produced offline / out-of-band).  We re-parse and
	cross-check before emitting the cert claim so a stale or
	mismatched author claim is caught at deploy time rather than
	silently shipped to consumers.

	Checks:
	  - body.package_id == "std"
	  - body.version    == expected_version (the build's stdlib version)
	  - body.source_content_id == expected_sci (the build's SCI;
	    proves the externally-signed source identity matches the
	    bytes this deploy is about to build a cert claim against)
	  - body.namespaces covers std.*, lang.*, drift.*
	  - at least one signature is by `expected_author_kid`
	"""
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	claim = load_author_claim_json(author_claim_path.read_text(encoding="utf-8"))
	if claim.body.package_id != "std":
		raise RuntimeError(
			f"stdlib author claim at {author_claim_path}: body.package_id is "
			f"{claim.body.package_id!r}, expected 'std'"
		)
	if claim.body.version != expected_version:
		raise RuntimeError(
			f"stdlib author claim at {author_claim_path}: body.version is "
			f"{claim.body.version!r}, expected {expected_version!r}.  The "
			f"externally-signed author claim must match the version this "
			f"deploy is building."
		)
	if claim.body.source_content_id != expected_sci:
		raise RuntimeError(
			f"stdlib author claim at {author_claim_path}: body."
			f"source_content_id is {claim.body.source_content_id!r}, but "
			f"this deploy computed SCI {expected_sci!r} from the stdlib "
			f"source tree.  Re-run the Foundation author-publish step "
			f"against the source tree this deploy is building."
		)
	required_ns = {"std.*", "lang.*", "drift.*"}
	declared = set(claim.body.namespaces)
	if not required_ns.issubset(declared):
		missing = sorted(required_ns - declared)
		raise RuntimeError(
			f"stdlib author claim at {author_claim_path}: namespaces "
			f"{sorted(declared)!r} do not cover the reserved set; missing "
			f"{missing!r}"
		)
	signer_kids = {sig.kid for sig in claim.signatures}
	if expected_author_kid not in signer_kids:
		raise RuntimeError(
			f"stdlib author claim at {author_claim_path}: no signature by "
			f"the configured Foundation author kid "
			f"({expected_author_kid!r}).  Signer kids in the claim: "
			f"{sorted(signer_kids)!r}"
		)


def _build_stdlib_evidence_manifest_bytes(
	*,
	version: str,
	sci: str,
	artifact_sha256: str,
	drift_rt_abi: int,
	driftc_commit: str,
	run_id: str,
	run_started_utc: str,
) -> bytes:
	"""Serialise the stdlib build evidence into canonical JSON bytes.

	The returned bytes are what gets hashed into both
	`body.evidence_sha256` and `cert_suite.result_evidence_sha256`,
	and what gets written to disk as `std.build-manifest.json` next
	to the `.dmp`.  Centralising the serialisation here guarantees
	on-disk bytes and signed digest stay in lockstep.
	"""
	manifest = {
		"kind": "drift-deploy/stdlib",
		"package_id": "std",
		"version": version,
		"source_content_id": sci,
		"artifact_sha256": artifact_sha256,
		"toolchain": {
			"driftc_version": version,
			"drift_rt_abi": drift_rt_abi,
			"driftc_commit": driftc_commit,
		},
		"run_id": run_id,
		"run_started_utc": run_started_utc,
	}
	return (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")


def emit_stdlib_cert_claim(
	dmp_path: Path,
	*,
	version: str,
	sci: str,
	author_claim_path: Path,
	author_kid: str,
	cert_key_path: Path,
	run_started_utc: str,
	driftc_commit: str,
) -> tuple[str, str, Path, Path]:
	"""Validate the externally-provided `std.author-claim`, then sign
	a cert claim alongside the `.dmp` using the operator-supplied
	certifier key.

	The deploy step is **certifier-role only**: it does not mint
	the cert seed, it loads it from `cert_key_path`.  An operator
	can legitimately point `cert_key_path` at the same physical
	file used by `drift-author publish` earlier in the pipeline --
	the role separation in v1 is about WHAT GETS SIGNED (author
	claim vs cert claim), not necessarily about which on-disk seed
	file the operator chose for each role.  Foundation may
	organizationally play both roles using one key in staging;
	production setups should split.

	Author identity comes in as `(author_claim_path, author_kid)`
	— both produced outside this process by Foundation's author-
	signing flow.  The author seed is NEVER read here.

	`run_started_utc` is captured by the caller at the start of the
	overall deploy.  It is signed into the cert claim body and into
	the on-disk build manifest -- a synthetic constant in this
	field would attest a deploy that didn't actually run at that
	time, which §3.6 of trust-v1 forbids.

	Evidence binding: this function writes a real
	`std.build-manifest.json` next to the `.dmp` capturing the build
	identity (package id, version, SCI, .dmp hash, toolchain, run
	id, start time).  Both `body.evidence_sha256` and
	`cert_suite.result_evidence_sha256` are bound to that artifact's
	bytes.  The two fields use the same digest by design: for the
	stdlib deploy, the deploy IS the cert suite, and the build
	manifest IS the suite's evidence -- there is no separate suite
	report.  An external suite (test report, coverage PDF, vendor
	cert) would belong to a different deploy path; the stdlib path
	never accepts a hardcoded sentinel here.

	Returns `(cert_kid, cert_pub_b64, cert_sidecar_path,
	build_manifest_path)`.
	"""
	import base64
	from hashlib import sha256
	from lang.drift.crypto import compute_ed25519_kid, ed25519_sign_from_seed
	from lang.driftc.packages.cert_claim_v1 import (
		CertClaimBody, CertSuite, Toolchain,
	)
	from tools.drift_deploy.cert_emit import (
		SignCertClaimOptions, load_cert_seed32, sign_and_write_cert_claim,
	)
	from lang.versions import DRIFT_RT_ABI_VERSION

	# Cross-check the externally-signed author claim before we attest
	# the build with our own cert claim.  A mismatched author claim
	# would surface at consumer load time anyway, but failing here
	# gives the deploy operator a precise diagnostic.
	_validate_external_stdlib_author_claim(
		author_claim_path,
		expected_version=version,
		expected_sci=sci,
		expected_author_kid=author_kid,
	)

	# Load the certifier seed from the operator-supplied file.  The
	# deploy step does NOT generate seeds; it consumes one already
	# provisioned for this host.  `load_cert_seed32` is the same
	# loader the rest of `drift_deploy`'s cert-emit path uses.
	cert_seed = load_cert_seed32(cert_key_path)
	_sig, cert_pub_raw = ed25519_sign_from_seed(priv_seed32=cert_seed, message=b"")
	cert_kid = compute_ed25519_kid(cert_pub_raw)
	cert_pub_b64 = base64.b64encode(cert_pub_raw).decode("ascii")

	artifact_sha256 = "sha256:" + sha256(dmp_path.read_bytes()).hexdigest()

	run_id = f"stdlib-{version}-{run_started_utc}"

	# Generate and persist the build manifest BEFORE signing so the
	# on-disk bytes are the exact bytes we hash into the cert claim.
	manifest_bytes = _build_stdlib_evidence_manifest_bytes(
		version=version,
		sci=sci,
		artifact_sha256=artifact_sha256,
		drift_rt_abi=DRIFT_RT_ABI_VERSION,
		driftc_commit=driftc_commit,
		run_id=run_id,
		run_started_utc=run_started_utc,
	)
	manifest_path = dmp_path.parent / "std.build-manifest.json"
	manifest_path.write_bytes(manifest_bytes)
	evidence_sha = "sha256:" + sha256(manifest_bytes).hexdigest()

	cert_sidecar = sign_and_write_cert_claim(SignCertClaimOptions(
		body=CertClaimBody(
			schema_version=1, package_id="std", version=version,
			artifact_sha256=artifact_sha256, source_content_id=sci,
			target="drift-dev",
			toolchain=Toolchain(
				driftc_version=version,
				drift_rt_abi=DRIFT_RT_ABI_VERSION,
				driftc_commit=driftc_commit,
			),
			dep_graph=(),
			cert_suite=CertSuite(
				id="drift-deploy/stdlib", version="1.0",
				result="pass",
				result_evidence_sha256=evidence_sha,
			),
			run_id=run_id,
			run_started_utc=run_started_utc,
			evidence_sha256=evidence_sha,
		),
		seed32=cert_seed,
		sidecar_dir=dmp_path.parent,
	))
	# Drop the in-memory seed; the on-disk file (cert_key_path)
	# remains under the operator's control.  The cert kid + pubkey
	# are recorded in the v1 core trust store written below.
	del cert_seed

	return cert_kid, cert_pub_b64, cert_sidecar, manifest_path


def install_stdlib(
	dmp: Path,
	author_sidecar: Path,
	cert_sidecar: Path,
	dist: Path,
	*,
	build_manifest: Path,
) -> None:
	"""Install stdlib `.dmp` + v1 sidecars + build manifest + stdlib_dep.txt
	into dist.

	The build manifest is the inspectable evidence artifact bound by
	the cert claim's `evidence_sha256` (and by
	`cert_suite.result_evidence_sha256`); installing it next to the
	`.dmp` lets a consumer / auditor verify the digest without
	re-running the deploy.
	"""
	import shutil

	stdlib_dir = dist / "lib" / "stdlib"
	stdlib_dir.mkdir(parents=True, exist_ok=True)

	shutil.copy2(str(dmp), str(stdlib_dir / "std.dmp"))
	shutil.copy2(str(author_sidecar), str(stdlib_dir / author_sidecar.name))
	shutil.copy2(str(cert_sidecar), str(stdlib_dir / cert_sidecar.name))
	shutil.copy2(str(build_manifest), str(stdlib_dir / build_manifest.name))

	# Single source of truth: read the actual package manifest.
	write_stdlib_dep(dmp, dist)


def write_stdlib_dep(dmp: Path, dist: Path) -> None:
	"""Write stdlib_dep.txt by peeking the actual package manifest."""
	# Import inline — this module is available once bundle_compiler has run.
	sys.path.insert(0, str(dist / "lib" / "compiler"))
	try:
		from lang.driftc.packages.dmir_pkg_v0 import peek_package_id_and_version
	finally:
		sys.path.pop(0)

	result = peek_package_id_and_version(dmp)
	if result is None:
		raise RuntimeError(f"failed to peek package id/version from {dmp}")

	dep_spec = f"{result[0]}@{result[1]}"
	stdlib_dep_file = dist / "lib" / "stdlib" / "stdlib_dep.txt"
	stdlib_dep_file.write_text(dep_spec + "\n", encoding="utf-8")


def generate_core_trust_store_v1(
	*,
	author_kid: str,
	author_pub_b64: str,
	cert_kid: str,
	cert_pub_b64: str,
	dist: Path,
) -> None:
	"""Write the v1 role-tagged core trust store consumed by the
	bundled compiler at run time.

	The two role grants come from independent inputs:
	  - `author_kid` / `author_pub_b64` from the caller-supplied
	    Foundation author identity (the deploy step does not mint
	    them; the author claim was signed earlier, out-of-band);
	  - `cert_kid` / `cert_pub_b64` from the operator-supplied
	    certifier key file (`--certifier-key-file` or
	    `$DRIFT_SIGN_KEY_FILE`) that this deploy used to sign the
	    cert claim.

	The two kids MAY resolve to the same value when an
	organization chooses to use the same key for both roles in
	the current release.  The toolchain neither requires nor
	forbids this; the trust store naturally lists the same kid
	in both role lists, which is what longest-prefix lookup will
	see.  If a future release splits the keys, the same code path
	emits two distinct entries with no change here.
	"""
	print("[deploy] generating v1 core trust store...", flush=True)

	namespaces = ["std.*", "lang.*", "drift.*"]
	# `keys` is a kid-keyed dict; collapsing the (author_kid, cert_kid)
	# pair into a single entry when they match is correct by JSON
	# shape -- two list entries with identical kid in different role
	# lists are still a valid v1 trust store.
	keys: dict[str, dict[str, str]] = {
		author_kid: {"algo": "ed25519", "pubkey": author_pub_b64},
	}
	if cert_kid != author_kid:
		keys[cert_kid] = {"algo": "ed25519", "pubkey": cert_pub_b64}
	trust_store = {
		"format": "drift-trust",
		"version": 1,
		"keys": keys,
		"namespaces": {
			ns: {
				"authors":    [author_kid],
				"certifiers": [cert_kid],
			}
			for ns in namespaces
		},
		"revoked": [],
	}

	out_path = dist / "lib" / "compiler" / "lang" / "driftc" / "packages" / "core_trust_v1.json"
	out_path.parent.mkdir(parents=True, exist_ok=True)
	out_path.write_text(
		json.dumps(trust_store, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)
	print(
		f"wrote v1 trust store: {out_path} "
		f"(author={author_kid[:24]}..., cert={cert_kid[:24]}...)",
		flush=True,
	)


def build_and_install_stdlib(
	repo_root: Path,
	stage: Path,
	dist: Path,
	version: str,
	*,
	stdlib_author_claim_path: Path,
	stdlib_author_pubkey_b64: str,
	certifier_key_path: Path,
	driftc_commit: str,
) -> tuple[Path, Path, Path]:
	"""Full stdlib pipeline: build → validate caller-provided author
	claim → sign cert claim with the operator-supplied certifier
	key → install → core_trust_v1.json.

	Required external inputs:
	  - `stdlib_author_claim_path`: an already-emitted
	    `std.author-claim` produced out-of-band by Foundation's
	    author-signing flow (offline / separate signing service /
	    pre-provisioned artifact).  The deploy step VALIDATES this
	    artifact against the build but does NOT generate one.
	  - `stdlib_author_pubkey_b64`: the public key matching the
	    Foundation author kid that signed the claim.  Required
	    because the consumer-side `core_trust_v1.json` records
	    pubkeys, not just kids.
	  - `certifier_key_path`: the certifier seed file the deploy
	    host uses to sign the cert claim.  Resolved by the caller
	    via `--certifier-key-file` flag (preferred) or the
	    `DRIFT_SIGN_KEY_FILE` env fallback.  It is policy-allowed
	    for this seed to be the same physical file used by the
	    earlier `drift-author publish` -- the role separation is
	    about which claim body is signed at which step, not about
	    forcing two distinct on-disk keys.
	  - `driftc_commit`: short git SHA of the source the deploy is
	    building, recorded in the cert claim's `toolchain.driftc_commit`
	    and the build manifest's `toolchain.driftc_commit`.  The deploy
	    orchestrator (`tools/deploy/deploy.py`) reads this from
	    `DeployMetadata.git_commit`; the in-process
	    `lang.versions.DRIFTC_GIT_SHA` is empty in the source tree
	    (the bundle step stamps it into the deployed copy only), so
	    the value must be threaded in by the caller rather than read
	    here.

	Returns `(dmp_path, author_sidecar, cert_sidecar)`.
	"""
	import base64
	import datetime as _dt
	from lang.drift.crypto import compute_ed25519_kid

	# Capture the run-start timestamp at deploy entry, BEFORE the
	# stdlib build runs.  This is what gets signed into both the
	# cert claim body and the on-disk build manifest -- a synthetic
	# constant here would attest a deploy that didn't run at that
	# time (§3.6 of trust-v1).
	run_started_utc = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

	if not stdlib_author_claim_path.is_file():
		raise RuntimeError(
			f"stdlib deploy: required stdlib_author_claim_path does not "
			f"exist: {stdlib_author_claim_path}.  The stdlib author claim "
			f"must be produced out-of-band by Foundation before this "
			f"deploy runs (see doc/design/trust-v1.md §7.5)."
		)
	if not certifier_key_path.is_file():
		raise RuntimeError(
			f"stdlib deploy: required certifier_key_path does not exist: "
			f"{certifier_key_path}.  Set --certifier-key-file or "
			f"DRIFT_SIGN_KEY_FILE to a base64-encoded 32-byte Ed25519 "
			f"private seed.  The deploy step does not mint cert seeds."
		)

	# Derive the author kid from the provided pubkey so the deploy
	# step has a name to match against the claim's signatures.
	author_pub_raw = base64.b64decode(stdlib_author_pubkey_b64)
	if len(author_pub_raw) != 32:
		raise RuntimeError(
			f"stdlib deploy: stdlib_author_pubkey_b64 must decode to 32 "
			f"bytes (Ed25519 raw public key); got {len(author_pub_raw)}"
		)
	author_kid = compute_ed25519_kid(author_pub_raw)

	dmp, sci = build_stdlib_package(repo_root, stage, version)
	cert_kid, cert_pub_b64, cert, build_manifest = emit_stdlib_cert_claim(
		dmp,
		version=version,
		sci=sci,
		author_claim_path=stdlib_author_claim_path,
		author_kid=author_kid,
		cert_key_path=certifier_key_path,
		run_started_utc=run_started_utc,
		driftc_commit=driftc_commit,
	)
	# Stage the externally-provided author claim next to the .dmp so
	# install_stdlib can copy it into the dist tree from a known
	# location.  Skip the copy when the caller already placed the
	# claim there (idempotent).
	staged_author_claim = dmp.parent / "std.author-claim"
	if stdlib_author_claim_path.resolve() != staged_author_claim.resolve():
		import shutil as _sh
		_sh.copy2(str(stdlib_author_claim_path), str(staged_author_claim))
	author = staged_author_claim
	author_pub_b64 = stdlib_author_pubkey_b64
	install_stdlib(dmp, author, cert, dist, build_manifest=build_manifest)
	generate_core_trust_store_v1(
		author_kid=author_kid,
		author_pub_b64=author_pub_b64,
		cert_kid=cert_kid,
		cert_pub_b64=cert_pub_b64,
		dist=dist,
	)

	# Verify outputs.
	expected = [
		dist / "lib" / "stdlib" / "std.dmp",
		dist / "lib" / "stdlib" / author.name,
		dist / "lib" / "stdlib" / cert.name,
		dist / "lib" / "stdlib" / build_manifest.name,
		dist / "lib" / "compiler" / "lang" / "driftc" / "packages" / "core_trust_v1.json",
	]
	for f in expected:
		if not f.exists():
			raise RuntimeError(f"expected output not found: {f}")

	print("[deploy] stdlib package installed with v1 author + cert claims", flush=True)
	return dmp, author, cert
