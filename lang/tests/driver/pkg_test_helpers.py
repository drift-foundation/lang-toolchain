# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Shared helpers for package-consumer driver and memcheck tests.

Provides _build_signed_stdlib() for tests that need a signed stdlib .dmp
without using the session-scoped conftest.py fixture (e.g., per-test
tmp_path isolation, or memcheck tests that run outside the driver suite).
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]
STDLIB_DIR = ROOT / "stdlib"
STD_VERSION = "0.0.0-test"


def _build_signed_stdlib(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
	"""Build a v1-signed stdlib package.

	Returns (pkg_root, trust_path, core_trust_path, empty_stdlib).

	v1 layout per the trust-v1 audit:
	  - `std.dmp` with `source_content_id` stamped into the manifest;
	  - `std.author-claim` (source identity attestation);
	  - `std.cert-claim.<kid>.json` (artifact + cert_suite attestation);
	  - Foundation-bootstrap key plays both roles for stdlib in dev.
	"""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.drift.crypto import compute_ed25519_kid
	from lang.driftc.packages.author_claim_v1 import AuthorClaimBody, make_author_claim_body
	from lang.driftc.packages.cert_claim_v1 import (
		CertClaimBody, CertSuite, Toolchain, make_cert_claim_body,
	)
	from lang.driftc.packages.source_content_id import (
		compute_artifact_source_content_id,
	)
	from tools.drift_author.author_publish import (
		SignAuthorClaimOptions, sign_and_write_author_claim,
	)
	from tools.drift_deploy.cert_emit import (
		SignCertClaimOptions, sign_and_write_cert_claim,
	)

	stdlib_files = sorted(STDLIB_DIR.rglob("*.drift"))
	assert stdlib_files, "no stdlib .drift files"

	pkg_dir = tmp_path / "lib"
	pkg_dir.mkdir(parents=True, exist_ok=True)
	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir(parents=True, exist_ok=True)

	# Compute SCI before driftc emits so the manifest carries the stamp.
	module_paths_rel = sorted(str(p.relative_to(ROOT)) for p in stdlib_files)
	sci = compute_artifact_source_content_id(
		kind="package",
		package_id="std",
		version=STD_VERSION,
		module_namespace="std",
		entry_module="std",
		module_paths=module_paths_rel,
		package_deps=[],
		native_deps=[],
		unsafe=False,
		asset_paths=[],
		source_root=ROOT,
	)

	std_pkg_path = tmp_path / "std_build" / "std.dmp"
	std_pkg_path.parent.mkdir(parents=True, exist_ok=True)
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "--dev", "-M", str(STDLIB_DIR),
		 "--stdlib-root", str(empty_stdlib),
		 *(str(p) for p in stdlib_files),
		 "--package-id", "std",
		 "--package-version", STD_VERSION,
		 "--package-target", "test-target",
		 "--source-content-id", sci,
		 "--emit-package", str(std_pkg_path),
		 "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180),
	)
	assert res.returncode == 0, f"stdlib build failed: {res.stderr[:500]}"

	std_dest = pkg_dir / "std" / STD_VERSION
	std_dest.mkdir(parents=True)
	shutil.copy2(str(std_pkg_path), str(std_dest / "std.dmp"))

	# Generate Foundation-bootstrap key (same kid plays author and
	# certifier roles for stdlib in dev).
	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(
		encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
	)
	priv_seed_raw = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = (std_dest / "std.dmp").read_bytes()
	artifact_sha256 = "sha256:" + sha256(pkg_bytes).hexdigest()

	# v1 author claim (source-identity attestation).
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=make_author_claim_body(
			package_id="std",
			version=STD_VERSION,
			artifact_kind="package",
			namespaces=("std.*", "lang.*", "drift.*"),
			source_content_id=sci,
			required_deps=(),
			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed_raw,
		sidecar_dir=std_dest,
	))

	# v2 cert claim (artifact + cert_suite attestation).
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=make_cert_claim_body(
			package_id="std",
			version=STD_VERSION,
			artifact_kind="package",
			artifact_path="std.dmp",
			artifact_sha256=artifact_sha256,
			source_content_id=sci,
			target="test-target",
			toolchain=Toolchain(
				driftc_version=STD_VERSION, drift_rt_abi=1, driftc_commit="test",
			),
			dep_graph=(),
			cert_suite=CertSuite(
				id="drift-deploy/test", version="1.0", result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64),
			),
			run_id="stdlib-test",
			run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=priv_seed_raw,
		sidecar_dir=std_dest,
	))

	# v1 core trust: bootstrap kid trusted as both author and
	# certifier for the reserved namespaces.
	core_trust_path = tmp_path / "core_trust.json"
	core_trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {
			"std.*":   {"authors": [kid], "certifiers": [kid]},
			"lang.*":  {"authors": [kid], "certifiers": [kid]},
			"drift.*": {"authors": [kid], "certifiers": [kid]},
		},
		"revoked": [],
	}, separators=(",", ":"), sort_keys=True))

	# v1 project trust: trusts the kid for std.* only (mimics a
	# consumer that's installed stdlib but no other deps).
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {
			"std.*": {"authors": [kid], "certifiers": [kid]},
		},
		"revoked": [],
	}))

	return pkg_dir, trust_path, core_trust_path, empty_stdlib


# ── Shared v1 library-package publisher ────────────────────────────


def publish_v1_pkg(
	*,
	lib_dir: Path,
	src_files: list[Path],
	package_id: str,
	package_version: str = "0.0.0",
	namespace_glob: str | None = None,
	dest_pkg_root: Path,
	dest_trust_path: Path | None = None,
	merge_into_trust: dict | None = None,
	target: str = "drift-dev",
	package_root_overrides: list[Path] | None = None,
	trust_store_for_build: Path | None = None,
	core_trust_for_build: Path | None = None,
	required_deps: tuple = (),
	package_deps: tuple[tuple[str, str], ...] = (),
	dep_pins: tuple[tuple[str, str], ...] = (),
	stdlib_root_override: Path | None = None,
	priv_seed: bytes | None = None,
) -> dict:
	"""Build + sign a library package end-to-end with v1 trust artifacts.

	This is the SEMANTIC migration target K asked for: the helper
	stamps a real `source_content_id` into the package's manifest,
	emits BOTH the v1 author claim and the v1 cert claim sidecars
	alongside the `.dmp`, copies all three (the .dmp + two
	sidecars) into the dest pkg_root at the canonical
	`<root>/<pkg>/<version>/` layout, and writes a v1 role-tagged
	trust JSON.

	Use this from any test that publishes a library package
	fixture.  Replaces every per-file `_publish_signed_pkg`-style
	helper that emitted v0 `.sig` envelopes; the inline regex
	migration was insufficient for these files because flipping
	just the trust JSON left the sidecar half v0 (the failure
	mode K flagged after the bulk regex pass).

	Args:
	  lib_dir: directory holding the package's source files and
	    where the build outputs go before copy to dest.
	  src_files: source `.drift` files to feed to driftc.
	  package_id: e.g. "acme.util".
	  package_version: defaults to "0.0.0".
	  namespace_glob: defaults to `f"{package_id}.*"`.  Use the
	    package's actual module namespace when it differs from the
	    package id (e.g. `net-tls` package → `net_tls.*` modules).
	  dest_pkg_root: target package root layout
	    `<root>/<package_id>/<package_version>/`.
	  dest_trust_path: when set, writes a fresh v1 trust JSON.
	    When None, requires `merge_into_trust` to receive the new
	    namespace entry instead.
	  merge_into_trust: when set, the helper merges this kid into
	    an existing trust dict (caller serializes).  Mutually
	    exclusive with `dest_trust_path` (writes the merged trust
	    when provided).
	  target: package target string; default `"drift-dev"`.
	  required_deps: optional `(RequiredDep, ...)` for the
	    author claim body when the package has declared deps.
	  package_root_overrides: extra `--package-root` args (e.g. a
	    stdlib pkg_root the build depends on).
	  trust_store_for_build / core_trust_for_build: forwarded to
	    driftc as `--trust-store` / `--dev-core-trust-store` so the
	    build can verify any deps under `package_root_overrides`.
	  stdlib_root_override: override the stdlib source path (default
	    empty).

	Returns: dict with `pkg_path` (Path), `kid`, `pub_b64`, `sci`,
	`author_sidecar`, `cert_sidecar` — useful when callers need to
	mutate one piece for adversarial tests.
	"""
	import os
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.drift.crypto import compute_ed25519_kid
	from lang.driftc.packages.author_claim_v1 import AuthorClaimBody, make_author_claim_body
	from lang.driftc.packages.cert_claim_v1 import (
		CertClaimBody, CertSuite, Toolchain, make_cert_claim_body,
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

	# Test fixtures use a sentinel SCI: the v1 invariant is that
	# manifest stamp, author claim body, and cert claim body all
	# carry the SAME SCI string.  None of these tests assert
	# anything about source identity contents, so a constant
	# satisfies the verifier without requiring source-tree hashing.
	sci = "sha256:" + ("0" * 64)
	if namespace_glob is None:
		namespace_glob = f"{package_id}.*"

	pkg_path = lib_dir / f"{package_id}.dmp"
	empty_stdlib = lib_dir / "_empty_stdlib_for_publish"
	empty_stdlib.mkdir(parents=True, exist_ok=True)
	stdlib_root = stdlib_root_override or empty_stdlib

	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		"--dev",
		"-M", str(lib_dir),
		"--stdlib-root", str(stdlib_root),
		*(str(p) for p in src_files),
		"--package-id", package_id,
		"--package-version", package_version,
		"--package-target", target,
		"--source-content-id", sci,
		"--emit-package", str(pkg_path),
		"--test-build-only",
	]
	for r in (package_root_overrides or []):
		cmd.extend(["--package-root", str(r)])
	# `--package-dep NAME=RANGE` stamps the dep into the published
	# package's manifest `required_deps` field (what the v1 closure
	# walker reads from `LoadedPackage.required_deps`).  `RANGE` is
	# the owner-declared range shape — `"M"` (any M.x.x) or `"M.N"`
	# (any M.N.x), per the v2 dep grammar; the manifest-emit boundary
	# rejects exact pins and `^`/`~` shapes here.
	for dep_name, dep_range in package_deps:
		cmd.extend(["--package-dep", f"{dep_name}={dep_range}"])
	# `--dep NAME@VERSION` pins the resolver to an exact (`M.N.P`)
	# version for each consumed dep.  driftc requires this for every
	# consumed dep when `--package-root` is set.  Range form is the
	# manifest stamp above; exact form is the build-time pin here.
	for dep_name, dep_exact in dep_pins:
		cmd.extend(["--dep", f"{dep_name}@{dep_exact}"])
	if trust_store_for_build is not None:
		cmd.extend(["--trust-store", str(trust_store_for_build)])
	if core_trust_for_build is not None:
		cmd.extend(["--dev-core-trust-store", str(core_trust_for_build)])

	env = {**os.environ, "PYTHONPATH": str(ROOT)}
	res = subprocess.run(
		cmd, capture_output=True, text=True, cwd=str(ROOT),
		timeout=sanitizer_timeout(120), env=env,
	)
	assert res.returncode == 0, (
		f"v1 publish '{package_id}' build failed:\n"
		f"stdout: {res.stdout[:500]}\nstderr: {res.stderr[:1500]}"
	)

	# Allow callers to provide a pre-existing 32-byte seed (e.g. the
	# trust-CLI tests need the published package's signer kid to match
	# the kid in an `.author-profile` they already created).  Default
	# is a freshly generated ephemeral key.
	if priv_seed is None:
		_priv = Ed25519PrivateKey.generate()
		priv_seed = _priv.private_bytes(
			encoding=serialization.Encoding.Raw,
			format=serialization.PrivateFormat.Raw,
			encryption_algorithm=serialization.NoEncryption(),
		)
	priv = Ed25519PrivateKey.from_private_bytes(priv_seed)
	pub_raw = priv.public_key().public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = pkg_path.read_bytes()
	artifact_sha256 = "sha256:" + sha256(pkg_bytes).hexdigest()

	# Emit author + cert sidecars next to the .dmp in lib_dir.
	author_sidecar = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=make_author_claim_body(
			package_id=package_id,
			version=package_version,
			artifact_kind="package",
			namespaces=(namespace_glob,) if namespace_glob.endswith(".*") else (
				namespace_glob, f"{namespace_glob}.*",
			),
			source_content_id=sci,
			required_deps=required_deps,
			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed,
		sidecar_dir=lib_dir,
	))
	cert_sidecar = sign_and_write_cert_claim(SignCertClaimOptions(
		body=make_cert_claim_body(
			package_id=package_id,
			version=package_version,
			artifact_kind="package",
			artifact_path=pkg_path.name,
			artifact_sha256=artifact_sha256,
			source_content_id=sci,
			target=target,
			toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="test"),
			dep_graph=(),
			cert_suite=CertSuite(
				id="drift-deploy/test", version="1.0", result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64),
			),
			run_id=f"test-{package_id}",
			run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=priv_seed,
		sidecar_dir=lib_dir,
	))

	# Stage at canonical pkg-root layout.  Skip copies whose source
	# and destination are the same path (a few fixtures emit
	# directly into the pkg-root layout, which would otherwise
	# raise SameFileError).
	dest_dir = dest_pkg_root / package_id / package_version
	dest_dir.mkdir(parents=True, exist_ok=True)

	def _copy_if_distinct(src: Path, dst: Path) -> None:
		try:
			same = src.resolve() == dst.resolve()
		except OSError:
			same = False
		if same:
			return
		shutil.copy2(str(src), str(dst))

	_copy_if_distinct(pkg_path, dest_dir / f"{package_id}.dmp")
	_copy_if_distinct(author_sidecar, dest_dir / author_sidecar.name)
	_copy_if_distinct(cert_sidecar, dest_dir / cert_sidecar.name)

	# Trust JSON.
	if merge_into_trust is not None:
		merge_into_trust.setdefault("keys", {})[kid] = {
			"algo": "ed25519", "pubkey": pub_b64,
		}
		ns_entry = merge_into_trust.setdefault("namespaces", {}).setdefault(
			namespace_glob, {"authors": [], "certifiers": []},
		)
		if isinstance(ns_entry, list):  # defensive: stale v0-shape entry
			ns_entry = {"authors": list(ns_entry), "certifiers": list(ns_entry)}
			merge_into_trust["namespaces"][namespace_glob] = ns_entry
		ns_entry["authors"].append(kid)
		ns_entry["certifiers"].append(kid)
		merge_into_trust["format"] = "drift-trust"
		merge_into_trust["version"] = 1
		merge_into_trust.setdefault("revoked", [])
	elif dest_trust_path is not None:
		trust = {
			"format": "drift-trust", "version": 1,
			"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
			"namespaces": {
				namespace_glob: {"authors": [kid], "certifiers": [kid]},
			},
			"revoked": [],
		}
		dest_trust_path.write_text(
			json.dumps(trust, separators=(",", ":"), sort_keys=True),
			encoding="utf-8",
		)

	return {
		"pkg_path": pkg_path,
		"kid": kid,
		"pub_b64": pub_b64,
		"sci": sci,
		"artifact_sha256": artifact_sha256,
		"author_sidecar": author_sidecar,
		"cert_sidecar": cert_sidecar,
		"priv_seed": priv_seed,
	}


def sign_v1_pkg_into_root(
	*,
	pkg_path: Path,
	package_id: str,
	package_version: str = "0.0.0",
	namespace_glob: str | None = None,
	extra_namespaces: tuple = (),
	dest_pkg_root: Path,
	dest_trust_path: Path | None = None,
	merge_into_trust: dict | None = None,
	target: str = "drift-dev",
	required_deps: tuple = (),
	dep_graph_entries: tuple = (),
) -> dict:
	"""Sign an already-built `.dmp` with v1 author + cert claim
	sidecars, write a v1 trust JSON, and copy all three into the
	canonical pkg-root layout.

	Sister to `publish_v1_pkg` for tests that need to build the
	`.dmp` themselves with custom driftc args (extra deps, package
	root overrides, etc.) but still want the standard v1 sign + copy
	pattern.  Callers MUST have passed `--source-content-id
	sha256:000...0` to the driftc emit so the manifest stamp matches
	the sentinel SCI both sidecar bodies use.

	`dep_graph_entries` accepts two shapes:

	  - Same-key fixture shortcut (legacy / common):
	      `(("pkg_id", "version"), ...)`
	    Each row stamps the CURRENT package's kid as both the dep's
	    `author_kid` and `cert_kid`, and uses the sentinel SCI for
	    every dep.  This is only correct when ALL packages in the
	    fixture share one Foundation-bootstrap key (the common case
	    in this repo's tests).

	  - Full dep identity (multi-key fixtures, adversarial tests):
	      `(DepGraphEntry(...), ...)`  -- pass cert_claim_v1.DepGraphEntry
	    instances directly when a dep has a different kid, a
	    non-sentinel SCI, or a `dep_kind` other than `"direct"`.
	    Required when the test exercises multi-key trust or
	    cross-certifier scenarios.

	The shortcut is convenient but limited: a same-key fixture cannot
	exercise cross-kid mismatches between the dep's claim signers
	and the consumer's resolved closure.  Adversarial tests live in
	`lang/tests/packages/test_v1_adversarial.py` and bypass this
	helper entirely.

	Returns the same dict shape as `publish_v1_pkg` so callers can
	inspect kid/sci/etc.
	"""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.drift.crypto import compute_ed25519_kid
	from lang.driftc.packages.author_claim_v1 import AuthorClaimBody, make_author_claim_body
	from lang.driftc.packages.cert_claim_v1 import (
		CertClaimBody, CertSuite, DepGraphEntry, Toolchain,
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

	sci = "sha256:" + ("0" * 64)
	if namespace_glob is None:
		namespace_glob = f"{package_id}.*"

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
	pkg_bytes = pkg_path.read_bytes()
	artifact_sha256 = "sha256:" + sha256(pkg_bytes).hexdigest()

	dest_dir = dest_pkg_root / package_id / package_version
	dest_dir.mkdir(parents=True, exist_ok=True)
	# Build dep_graph rows from either the same-key (pkg_id, version)
	# shortcut OR fully-specified DepGraphEntry instances -- see the
	# docstring for the two accepted shapes.
	dep_graph: list[DepGraphEntry] = []
	for entry in dep_graph_entries:
		if isinstance(entry, DepGraphEntry):
			dep_graph.append(entry)
			continue
		dep_id, dep_ver = entry
		dep_dmp = dest_pkg_root / dep_id / dep_ver / f"{dep_id}.dmp"
		dep_bytes = dep_dmp.read_bytes()
		# Same-key shortcut: stamp the CURRENT package's kid as the
		# dep's author + cert kid and the fixture's sentinel SCI.
		# Only correct when every package in the fixture shares one
		# Foundation-bootstrap key.  For multi-key scenarios pass a
		# DepGraphEntry directly.
		dep_graph.append(DepGraphEntry(
			package_id=dep_id, version=dep_ver,
			artifact_sha256="sha256:" + sha256(dep_bytes).hexdigest(),
			source_content_id=sci,
			author_kid=kid, cert_kid=kid,
			dep_kind="direct",
		))

	# Author claim namespaces include the primary `namespace_glob`
	# plus any `extra_namespaces` the caller passes (e.g. stdlib
	# packages cover `std.*`, `lang.*`, `drift.*` together).  The
	# v1 author-claim verifier requires the claim to cover every
	# module the package exposes -- a `std.dmp` whose claim only
	# names `std.*` fails when the consumer loads `lang.atomic`.
	primary_ns = (namespace_glob,) if namespace_glob.endswith(".*") else (
		namespace_glob, f"{namespace_glob}.*",
	)
	all_namespaces = tuple(primary_ns) + tuple(extra_namespaces)
	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=make_author_claim_body(
			package_id=package_id, version=package_version,
			artifact_kind="package",
			namespaces=all_namespaces,
			source_content_id=sci,
			required_deps=required_deps, 			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed,
		sidecar_dir=pkg_path.parent,
	))
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=make_cert_claim_body(
			package_id=package_id, version=package_version,
			artifact_kind="package", artifact_path=pkg_path.name,
			artifact_sha256=artifact_sha256, source_content_id=sci,
			target=target,
			toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="test"),
			dep_graph=tuple(dep_graph),
			cert_suite=CertSuite(id="drift-deploy/test", version="1.0",
				result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64)),
			run_id=f"test-{package_id}",
			run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=priv_seed,
		sidecar_dir=pkg_path.parent,
	))

	def _copy_if_distinct(src: Path, dst: Path) -> None:
		"""Skip the copy when the source already lives at the
		destination (common when the test emits its .dmp directly
		into the canonical pkg-root layout).  Without this guard
		`shutil.copy2` raises SameFileError on identical paths."""
		try:
			same = src.resolve() == dst.resolve()
		except OSError:
			same = False
		if same:
			return
		shutil.copy2(str(src), str(dst))

	_copy_if_distinct(pkg_path, dest_dir / f"{package_id}.dmp")
	src_author = pkg_path.parent / author_claim_filename(package_id)
	_copy_if_distinct(src_author, dest_dir / src_author.name)
	src_cert = pkg_path.parent / cert_claim_filename(package_id, kid)
	_copy_if_distinct(src_cert, dest_dir / src_cert.name)

	# Trust JSON: same logic as publish_v1_pkg.
	if merge_into_trust is not None:
		merge_into_trust.setdefault("keys", {})[kid] = {
			"algo": "ed25519", "pubkey": pub_b64,
		}
		ns_entry = merge_into_trust.setdefault("namespaces", {}).setdefault(
			namespace_glob, {"authors": [], "certifiers": []},
		)
		if isinstance(ns_entry, list):
			ns_entry = {"authors": list(ns_entry), "certifiers": list(ns_entry)}
			merge_into_trust["namespaces"][namespace_glob] = ns_entry
		ns_entry["authors"].append(kid)
		ns_entry["certifiers"].append(kid)
		merge_into_trust["format"] = "drift-trust"
		merge_into_trust["version"] = 1
		merge_into_trust.setdefault("revoked", [])
	elif dest_trust_path is not None:
		trust = {
			"format": "drift-trust", "version": 1,
			"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
			"namespaces": {
				namespace_glob: {"authors": [kid], "certifiers": [kid]},
			},
			"revoked": [],
		}
		dest_trust_path.write_text(
			json.dumps(trust, separators=(",", ":"), sort_keys=True),
			encoding="utf-8",
		)

	return {
		"pkg_path": pkg_path,
		"kid": kid,
		"pub_b64": pub_b64,
		"sci": sci,
		"artifact_sha256": artifact_sha256,
		"priv_seed": priv_seed,
	}


# ── Thin v1 emit helpers for inline-fixture migration ─────────────


_INLINE_TEST_SCI = "sha256:" + ("0" * 64)


def write_v1_trust_store_inline(
	path: Path,
	*,
	kid: str,
	pub_b64: str,
	namespaces: list[str],
	revoked: list[str] | None = None,
) -> None:
	"""Write a v1 role-tagged trust store at `path`.

	Convenience helper for test fixtures that previously emitted a
	v0 flat-list trust JSON inline.  The Foundation-bootstrap
	pattern (same kid in `authors` and `certifiers` for each
	namespace) is hard-coded here because every caller wants it;
	production trust stores split the roles explicitly via
	`drift trust add --role`.
	"""
	revoked = revoked or []
	obj = {
		"format": "drift-trust",
		"version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {
			ns: {"authors": [kid], "certifiers": [kid]}
			for ns in namespaces
		},
		"revoked": revoked,
	}
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True), encoding="utf-8")


def emit_v1_sidecars_inline(
	pkg_path: Path,
	*,
	package_id: str,
	package_version: str,
	priv,
	namespaces: list[str],
	target: str = "test-target",
	sci: str = _INLINE_TEST_SCI,
) -> None:
	"""Emit `<pkg>.author-claim` + `<pkg>.cert-claim.<kid>.json`
	next to `pkg_path` using `priv` (Ed25519PrivateKey) as both
	the author and certifier kid (Foundation-bootstrap pattern).

	Replaces the v0 `.sig` envelope emit pattern that several test
	fixture files were using before the trust-v1 cutover.  Callers
	must have built the `.dmp` with `--source-content-id <sci>` so
	the manifest stamp matches the sidecar bodies (v1 verify
	checks three-way equality on SCI).
	"""
	from hashlib import sha256
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.author_claim_v1 import AuthorClaimBody, make_author_claim_body
	from lang.driftc.packages.cert_claim_v1 import (
		CertClaimBody, CertSuite, Toolchain, make_cert_claim_body,
	)
	from tools.drift_author.author_publish import (
		SignAuthorClaimOptions, sign_and_write_author_claim,
	)
	from tools.drift_deploy.cert_emit import (
		SignCertClaimOptions, sign_and_write_cert_claim,
	)

	priv_seed = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	artifact_sha256 = "sha256:" + sha256(pkg_path.read_bytes()).hexdigest()

	sign_and_write_author_claim(SignAuthorClaimOptions(
		body=make_author_claim_body(
			package_id=package_id, version=package_version,
			artifact_kind="package",
			namespaces=tuple(namespaces),
			source_content_id=sci,
			required_deps=(), 			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed,
		sidecar_dir=pkg_path.parent,
	))
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=make_cert_claim_body(
			package_id=package_id, version=package_version,
			artifact_kind="package", artifact_path=pkg_path.name,
			artifact_sha256=artifact_sha256, source_content_id=sci,
			target=target,
			toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="test"),
			dep_graph=(),
			cert_suite=CertSuite(id="drift-deploy/test", version="1.0",
				result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64)),
			run_id=f"test-{package_id}",
			run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=priv_seed,
		sidecar_dir=pkg_path.parent,
	))
