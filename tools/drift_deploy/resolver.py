# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Dependency resolver for drift deploy.

Implements constraint-aggregation + highest-satisfying-all resolution.
Fully order-independent: same constraints + same available packages
always produces the same resolved graph.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.drift_deploy.semver import Constraint, SemVer, parse_constraint, parse_version
from lang.driftc.packages.dmir_pkg_v0 import is_owner_declared_range


@dataclass(frozen=True)
class PackageEntry:
	"""A discovered package in the package index.

	`required_deps` is the producer's published consumer-facing
	requirement (ranges, drawn from the producer's manifest
	`package_deps` at publish time).  Consumers walk this
	transitively to build their own exact lock.  Never carries
	lock-exact pins.

	`source_content_id` and `source_attestation_key` are read from
	the `.source-attestation` sidecar (or empty if no sidecar is
	present — Phase B graceful path, Phase C tightens for source-
	rebuild mode).  These together pin the package's *source*
	identity and the key that attested it, decoupled from the
	`.dmp` byte sha256 — required for source-rebuild certification
	where rebuilt bytes are expected to differ from the author's
	original artifact.
	"""
	package_id: str
	version: SemVer
	path: Path
	sha256: str
	required_deps: list[tuple[str, str]]  # [(dep_name, range_string), ...]
	author_key: str = ""  # "ed25519:<kid>" from signature sidecar
	source_content_id: str = ""  # "sha256:<hex>" from .source-attestation body
	source_attestation_key: str = ""  # "ed25519:<kid>" of attestation signer
	# Module ids carried inside this `.dmp`'s manifest (the `modules[i]
	# .module_id` list).  Load-bearing for trust verification: the trust
	# store's namespace allowlist is keyed by MODULE namespace
	# (`net_tls.*`, not the package id `net-tls`).  PackageEntry retains
	# the module_ids so source-rebuild trust verification at verify /
	# index time can call `TrustStore.allowed_kids_for_module(mid)`
	# with the correct key.  Empty tuple for co-artifacts and for
	# pre-0.31.1 index entries built without trust.
	module_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConstraintEntry:
	"""A constraint on a package, with provenance."""
	constraint: Constraint
	source: str  # e.g., "artifact 'tls-tool'" or "net.tls 0.3.2"


@dataclass(frozen=True)
class ResolvedDep:
	"""A single resolved dependency entry as it appears in a lock v4.

	Under the 0.30 source-attestation model the lock records two
	independent identities for each dep:
	  - artifact identity: `sha256` (byte fingerprint of `.dmp`) +
	    `author_key` (kid that signed the artifact).
	  - source identity: `source_content_id` (canonical hash of the
	    declared source/build inputs, see
	    `tools/drift_deploy/source_attestation.compute_source_content_id`)
	    + `source_attestation_key` (kid that signed the
	    `.source-attestation` body).

	Default-strict consumption (`drift build` / `drift deploy` with
	no flags) requires ALL of `(version, sha256, author_key,
	source_content_id, source_attestation_key)` to match the
	on-disk package + its signed attestation sidecar — bytes AND
	source identity, both anchored by signatures.

	Source-rebuild mode (Phase D) tolerates `sha256` drift as long
	as the source-identity half (`source_content_id` +
	`source_attestation_key`) re-verifies; the trust root in that
	mode is the source-attestation key, never the rebuilt artifact
	signer.

	Co-artifact entries leave `sha256`, `author_key`,
	`source_content_id`, and `source_attestation_key` empty —
	they're filled in at deploy time when the co-artifact is built.
	The verifier skips co-artifact entries entirely (with a fail-
	closed allowlist on the `package_id`).
	"""
	version: str  # exact M.N.P under v3+ lock; range string only in pre-resolution intermediate structures
	sha256: str  # hex digest of the resolved .dmp file ("" for co-artifact deps)
	dep_type: str  # "direct", "transitive", or "co-artifact"
	package_id: str = ""  # package name
	author_key: str = ""  # "ed25519:<kid>" of artifact signer
	source_content_id: str = ""  # "sha256:<hex>" — canonical source identity
	source_attestation_key: str = ""  # "ed25519:<kid>" of source-attestation signer


class ResolutionError(Exception):
	"""Raised when dependency resolution fails."""
	pass


def _sha256_file(path: Path) -> str:
	"""
	Compute sha256 of the canonical (uncompressed) package bytes.

	For .zdmp files, decompresses first — the hash identity is always
	the uncompressed payload, matching signature semantics.
	"""
	if path.suffix == ".zdmp":
		from lang.driftc.packages.zdmp import decompress_zdmp
		raw = decompress_zdmp(path.read_bytes())
		return hashlib.sha256(raw).hexdigest()
	h = hashlib.sha256()
	with path.open("rb") as f:
		while True:
			chunk = f.read(65536)
			if not chunk:
				break
			h.update(chunk)
	return h.hexdigest()


def _read_author_key(dmp_path: Path) -> str:
	"""Extract the first signing key id from the .sig sidecar adjacent to a .dmp/.zdmp."""
	for suffix in (".sig",):
		sig_path = dmp_path.with_suffix(suffix)
		if not sig_path.exists():
			# Try .zdmp sibling
			zdmp_sibling = dmp_path.with_suffix(".zdmp")
			sig_path = zdmp_sibling.with_suffix(suffix)
		if not sig_path.exists():
			continue
		try:
			data = json.loads(sig_path.read_text())
			sigs = data.get("signatures", [])
			if sigs and isinstance(sigs, list):
				kid = sigs[0].get("kid", "")
				if isinstance(kid, str) and kid:
					return kid
		except (json.JSONDecodeError, OSError, KeyError):
			pass
	return ""


def _read_source_attestation_meta(
	dmp_path: Path,
	manifest: dict[str, Any],
) -> tuple[str, str]:
	"""Extract `(source_content_id, source_attestation_key)` from the
	`.source-attestation` sidecar adjacent to a `.dmp` / `.zdmp`,
	cross-bound to the package's own `.dmp` manifest and with the
	signature verified.

	Returns `("", "")` if any of the following:
	  - sidecar is absent (legacy package; drift_prepare turns this
	    into a fail-fast republish-required error for non-co-artifact
	    deps when it sees the empty fields);
	  - sidecar fails structural load (format/version/body shape/
	    body_sha256 self-check);
	  - sidecar signature does not verify with the carried pubkey;
	  - sidecar body does not bind to the .dmp it sits next to:
	    `package_id`, `version`, `target_class`, `required_deps`,
	    or `source_content_id` (when the manifest carries a stamp)
	    don't match.

	Cross-binding is the load-bearing trust check.  A validly
	signed attestation for some other package/version/target placed
	next to a `.dmp` would otherwise get locked as that .dmp's
	source identity, defeating the whole "rebuilder cannot sign as
	the package owner" contract.  Mismatch → log on stderr (so the
	user knows which package's sidecar is misbound) and return
	empty so the caller can treat the package as un-attested.

	Hard fail at prepare time (not here): drift_prepare iterates
	resolved deps and refuses to write a v4 lock whose non-co-
	artifact entries have empty source identity.  Doing the fail-
	fast at the per-package index walk would block unrelated builds
	on one corrupt sidecar; doing it at prepare time scopes the
	error to packages the consumer actually needs.
	"""
	from tools.drift_deploy.source_attestation import (
		read_attestation_sidecar,
		verify_attestation,
	)
	import sys
	# Adjacent to either .dmp or .zdmp; try both.
	candidates = [
		dmp_path.with_suffix(".source-attestation"),
		dmp_path.with_suffix(".zdmp").with_suffix(".source-attestation"),
	]
	sidecar_path: Path | None = None
	for path in candidates:
		if path.exists():
			sidecar_path = path
			break
	if sidecar_path is None:
		return ("", "")

	def _warn(reason: str) -> tuple[str, str]:
		# stderr-warn instead of raising so a single corrupt
		# sidecar in the package root doesn't take down the entire
		# index walk for unrelated builds.  drift_prepare turns the
		# empty result into a fail-fast republish-required error
		# for any RESOLVED non-co-artifact dep — scoped to the
		# packages the consumer actually needs.
		print(
			f"warning: source attestation at '{sidecar_path}' rejected: "
			f"{reason} — package will be treated as un-attested.  "
			f"`drift prepare` will fail if this package is a non-co-"
			f"artifact dep; republish with toolchain >= 0.30.0.",
			file=sys.stderr,
		)
		return ("", "")

	try:
		sidecar = read_attestation_sidecar(sidecar_path)
	except (ValueError, OSError) as e:
		return _warn(f"sidecar will not load ({e})")

	if not sidecar.signatures:
		return _warn("no signatures present")
	kid = sidecar.signatures[0].kid

	# Signature verification BEFORE recording the kid — otherwise
	# the lock would carry a kid whose signature does not actually
	# verify, and the strict verifier would have to re-validate
	# every load.  Verifying once at index time, before the value
	# is written into a lock, keeps the "lock entries are
	# internally signed and coherent" invariant.
	try:
		verify_attestation(sidecar, expected_signer_kid=kid)
	except ValueError as e:
		return _warn(f"signature does not verify ({e})")

	# Cross-binding: the sidecar body must describe THIS .dmp.
	body = sidecar.body
	manifest_pkg_id = manifest.get("package_id")
	manifest_version = manifest.get("package_version")
	manifest_target = manifest.get("target")
	manifest_scid = manifest.get("source_content_id")  # Phase A stamp; may be None for legacy
	manifest_required_deps = manifest.get("required_deps") or []

	if body.package_id != manifest_pkg_id:
		return _warn(
			f"body.package_id {body.package_id!r} != "
			f".dmp manifest['package_id'] {manifest_pkg_id!r}"
		)
	if body.version != manifest_version:
		return _warn(
			f"body.version {body.version!r} != "
			f".dmp manifest['package_version'] {manifest_version!r}"
		)
	if body.target_class != manifest_target:
		return _warn(
			f"body.target_class {body.target_class!r} != "
			f".dmp manifest['target'] {manifest_target!r}"
		)
	# Under v4 the .dmp manifest stamp is REQUIRED.  Republishing
	# a package with toolchain >= 0.30.0 always emits the stamp;
	# its absence means the package was published with an older
	# toolchain and is not source-mode certifiable, full stop.
	# Allowing a sidecar to substitute for a missing stamp would
	# let an old package be retroactively "upgraded" into source-
	# mode by dropping a sidecar next to it on disk — the artifact
	# itself would never declare the source identity it was built
	# from, and the trust chain would rest on adjacency alone.
	from tools.drift_deploy.source_attestation import validate_sha256_hex_id
	if not isinstance(manifest_scid, str) or not manifest_scid:
		return _warn(
			"`.dmp` manifest has no `source_content_id` stamp; the "
			"package must be republished with toolchain >= 0.30.0 so "
			"the artifact itself records the source identity it was "
			"built from (a sidecar alone is not enough — that would "
			"let an old package be 'upgraded' into source-mode by "
			"adjacency)"
		)
	try:
		validate_sha256_hex_id(
			manifest_scid,
			field="`.dmp` manifest['source_content_id'] stamp",
		)
	except ValueError as e:
		return _warn(str(e))
	if body.source_content_id != manifest_scid:
		return _warn(
			f"body.source_content_id {body.source_content_id!r} != "
			f".dmp manifest['source_content_id'] {manifest_scid!r}"
		)
	# required_deps must match exactly (set-of-pairs comparison so
	# ordering doesn't matter; the wire shape is a list).
	body_rd = {(d.name, d.version) for d in body.required_deps}
	manifest_rd: set[tuple[str, str]] = set()
	for entry in manifest_required_deps:
		if isinstance(entry, dict):
			n = entry.get("name")
			v = entry.get("version")
			if isinstance(n, str) and isinstance(v, str):
				manifest_rd.add((n, v))
	if body_rd != manifest_rd:
		return _warn(
			f"body.required_deps {sorted(body_rd)} != "
			f".dmp manifest['required_deps'] {sorted(manifest_rd)}"
		)

	return (body.source_content_id, kid)


def build_package_index(
	package_roots: list[Path],
	load_manifest: Any = None,
	*,
	trust_store: Any = None,
	core_trust_store: Any = None,
	run_snapshot: Any = None,
) -> dict[str, list[PackageEntry]]:
	"""
	Build a package index from package roots.

	Returns {package_id → [PackageEntry, ...]}, one entry per distinct version.
	If the same package_id+version appears in multiple roots, the first root wins.

	`load_manifest` is a callable(Path) → dict that loads a .dmp manifest.
	If None, uses the dmir_pkg_v0 loader.

	Three mutually-exclusive verification modes:

	**(1) Parse-only** (default): `trust_store=None`,
	`run_snapshot=None`.  Read `.sig` / `.source-attestation`
	fields without cryptographic verification; return whatever
	discovery finds.  Used by strict mode (`drift build` /
	`drift deploy` / `drift prepare` default), where the lock is
	the authoritative trust root.

	**(2) Trust-store (producer / staging)**: `trust_store=...`.
	Per-package cryptographic verification against orch's own
	trust store.  Used by ORCH when staging packages into the run
	libs root.  Failure is a HARD ERROR (`ResolutionError`) — the
	package is not silently pruned, because fallback to an older
	trusted in-range version would mask the exact package orch
	staged for certification.

	Three gates fire in order for each discovered `.dmp` / `.zdmp`
	under this mode:

	  a. **Missing `.sig` is fatal.**  An on-disk package with no
	     `.sig` sidecar (empty `author_key`) raises.  Unsigned
	     packages have nothing for the trust gate to verify
	     against.  (The dev-opt-in `allow_unsigned_roots` path
	     from `signature_v0.verify_package_signatures` is NOT
	     applied here; source-rebuild callers never set it.)
	  b. **`.sig` cryptographic verify + per-module-namespace
	     trust.**  Delegated to `verify_package_signatures`, which
	     (a) verifies the canonical envelope-over-sha256 Ed25519
	     signature against the trust store's pubkey, and (b)
	     enforces per-module namespace trust using each
	     `module_id` from the manifest's `modules` list against
	     `trust_store.allowed_kids_for_module(module_id)`.
	     Namespaces follow MODULE ids (e.g. `net_tls.*`), NOT
	     package ids.
	  c. **`.source-attestation` signer allowlist + revocation.**
	     `_read_source_attestation_meta` verifies the sidecar body
	     cross-binding and self-signature; this gate adds the
	     per-module-namespace allowlist check on the sidecar's
	     recorded signer kid AND the revocation check against BOTH
	     trust layers.

	**(3) Run-snapshot (downstream source-rebuild consumer)**:
	`run_snapshot=...` (a loaded `RunSnapshot` from
	`tools.drift_deploy.run_snapshot.load_run_snapshot`).  Each
	discovered package is verified against the snapshot's entry
	for `(pkg_id, version)`: missing entry → `ResolutionError`;
	`source_content_id` / `author_key` / `source_attestation_key`
	mismatch → `ResolutionError`.  The downstream's local
	`drift/trust.json` is NOT consulted — the snapshot IS the
	source-of-truth for what this run authorised.

	`trust_store` and `run_snapshot` are MUTUALLY EXCLUSIVE —
	passing both raises `ValueError` (they represent different
	verification contracts and must not silently overlay).
	"""
	if trust_store is not None and run_snapshot is not None:
		raise ValueError(
			"build_package_index: `trust_store` and `run_snapshot` are "
			"mutually exclusive.  `trust_store` is the producer-side "
			"author-verification mode (used by orch staging); "
			"`run_snapshot` is the consumer-side source-identity-pin "
			"mode (used by downstream source-rebuild consumption).  A "
			"single call cannot serve both roles."
		)
	_load_verifier = None  # deferred import; only wired if trust_store provided
	if trust_store is not None:
		from lang.driftc.packages.signature_v0 import verify_package_signatures as _vps
		from lang.driftc.packages.trust_v0 import TrustStore as _TrustStore
		import sys as _sys
		# Callers that pass only a merged store can use it for both
		# roles — `verify_package_signatures` differentiates the two
		# only for reserved namespaces (`std.*`, `lang.*`, `drift.*`),
		# which downstream deploy-side packages don't declare.
		if core_trust_store is None:
			core_trust_store = trust_store

		def _load_verifier(path: Path, manifest: dict[str, Any]) -> str | None:  # type: ignore[misc]
			"""Return None on success, an error message on failure."""
			if path.suffix == ".zdmp":
				from lang.driftc.packages.zdmp import decompress_zdmp
				pkg_bytes = decompress_zdmp(path.read_bytes())
			else:
				pkg_bytes = path.read_bytes()
			try:
				_vps(
					pkg_path=path,
					pkg_bytes=pkg_bytes,
					pkg_manifest=manifest,
					trust=trust_store,
					core_trust=core_trust_store,
					require_signatures=True,
					allow_unsigned_roots=[],  # no unsigned allowance in source-rebuild
				)
			except ValueError as e:
				return str(e)
			return None
	# `PackageMetadataError` is the loader's distinguished signal for
	# "container loaded fine, but the metadata violates the v3
	# contract" (pre-cut `package_deps` key, malformed `required_deps`
	# shape).  Imported here so the ``except`` below can let it
	# propagate while still swallowing true I/O / container-level
	# corruption.
	from lang.driftc.packages.dmir_pkg_v0 import PackageMetadataError

	if load_manifest is None:
		from lang.driftc.packages.dmir_pkg_v0 import load_dmir_pkg_v0, load_dmir_pkg_v0_from_bytes
		def load_manifest(path: Path) -> dict[str, Any]:
			if path.suffix == ".zdmp":
				from lang.driftc.packages.zdmp import decompress_zdmp
				raw = decompress_zdmp(path.read_bytes())
				pkg = load_dmir_pkg_v0_from_bytes(raw, source_path=path)
				return pkg.manifest
			pkg = load_dmir_pkg_v0(path)
			return pkg.manifest

	index: dict[str, list[PackageEntry]] = {}
	seen: dict[tuple[str, str], Path] = {}  # (pkg_id, version_str) → first root
	seen_real_paths: set[str] = set()  # resolved physical paths (dedup symlinks)

	for root in package_roots:
		if not root.exists():
			continue
		if root.is_dir():
			import os
			all_pkg_files = sorted(
				Path(dp) / fn
				for dp, _, fns in os.walk(root, followlinks=True)
				for fn in fns if fn.endswith(".zdmp") or fn.endswith(".dmp")
			)
			# Deduplicate: when both foo.zdmp and foo.dmp exist in the
			# same directory, keep only .zdmp (published compressed form).
			zdmp_stems: set[tuple[str, str]] = set()
			for p in all_pkg_files:
				if p.suffix == ".zdmp":
					zdmp_stems.add((str(p.parent), p.stem))
			dmp_files = [
				p for p in all_pkg_files
				if p.suffix != ".dmp" or (str(p.parent), p.stem) not in zdmp_stems
			]
		else:
			dmp_files = [root] if root.suffix in (".zdmp", ".dmp") else []
		for dmp_path in dmp_files:
			if not dmp_path.is_file():
				continue

			# Deduplicate by resolved physical path. When the staged
			# package root symlinks into dest and dest is also a package
			# root, the same .dmp is reachable through both paths.
			real_path = str(dmp_path.resolve())
			if real_path in seen_real_paths:
				continue
			seen_real_paths.add(real_path)

			try:
				manifest = load_manifest(dmp_path)
			except PackageMetadataError as e:
				# Container loaded but metadata is contract-invalid —
				# pre-cut `package_deps` key, malformed `required_deps`
				# entry, etc.  This is exactly the "republish with 0.29"
				# case; surface it as a hard ResolutionError instead of
				# silently skipping, so the user sees actionable
				# guidance rather than a generic "dep not satisfied".
				raise ResolutionError(
					f"package at {dmp_path}: {e}"
				)
			except Exception as e:
				# A `.zdmp` is the published compressed artifact — if
				# the file exists but fails to load (decompression
				# error, bad magic, truncated header, ...), the
				# published package is bad and MUST NOT be silently
				# routed around.  Previous builds fell back to a
				# same-stem `.dmp` sibling when the `.zdmp` was
				# corrupt; that let a bad deploy masquerade as "works
				# locally" because the uncompressed build artifact was
				# still usable while the published shape was broken.
				# Fail loudly, name the bad file, and tell the user to
				# republish / reinstall.  No fallback.
				if dmp_path.suffix == ".zdmp":
					raise ResolutionError(
						f"failed to load published package {dmp_path}: "
						f"{e}.  The .zdmp is the authoritative published "
						f"artifact — a same-stem .dmp sibling will NOT "
						f"be used as a fallback, because that masks bad "
						f"deploys.  Republish or reinstall this package."
					)
				# Plain `.dmp` (no `.zdmp` present): keep the permissive
				# behaviour.  A lone unreadable `.dmp` under a package
				# root is treated as "not a package" and skipped.
				continue
			pkg_id = manifest.get("package_id")
			pkg_ver_str = manifest.get("package_version")
			if not isinstance(pkg_id, str) or not isinstance(pkg_ver_str, str):
				continue
			try:
				pkg_ver = parse_version(pkg_ver_str)
			except ValueError:
				continue

			key = (pkg_id, pkg_ver_str)
			if key in seen:
				# Same package+version in a later root or duplicate in same root.
				# First root wins per plan. Duplicate within same root → error.
				prev_root = seen[key]
				this_root = _find_root(dmp_path, package_roots)
				if this_root == prev_root:
					raise ResolutionError(
						f"duplicate package '{pkg_id}' version '{pkg_ver_str}' found in {this_root}"
					)
				continue  # later root, skip
			seen[key] = _find_root(dmp_path, package_roots)

			# Extract required_deps from .dmp manifest.  Under v3
			# the field is `required_deps` (owner-declared range per
			# dep, copied from the producer's manifest `package_deps`
			# at publish time).  Pre-cut `.dmp`s still carry the
			# legacy `package_deps` key — we reject those here with
			# a clear republish-required diagnostic rather than
			# silently reinterpreting the old field as the new
			# (which would leak producer-side shapes through the
			# consumer boundary).
			if "package_deps" in manifest:
				raise ResolutionError(
					f"package at {dmp_path} contains legacy `package_deps` "
					"metadata — this package was published with a pre-0.29 "
					"toolchain and must be republished with toolchain >= "
					"0.29.0.  v3 packages expose `required_deps` (owner-"
					"declared ranges) instead of the legacy key"
				)
			# Strict validation: every entry must be a well-formed
			# object with non-empty `name` and an `"M"` / `"M.N"`
			# range in `version`.  The default `.dmp` loader already
			# applies these rules via `_parse_required_deps`, but
			# `build_package_index` accepts a caller-supplied
			# `load_manifest` callable (custom loaders in tests).
			# Applying the same rules here prevents a custom loader
			# from smuggling malformed metadata past the resolver
			# and into a lock that the strict consumer-side verifier
			# would later reject.
			raw_deps = manifest.get("required_deps", [])
			req_deps: list[tuple[str, str]] = []
			if raw_deps is None:
				raw_deps = []
			if not isinstance(raw_deps, list):
				raise ResolutionError(
					f"package at {dmp_path}: `required_deps` must be an array"
				)
			for i, dep in enumerate(raw_deps):
				if not isinstance(dep, dict):
					raise ResolutionError(
						f"package at {dmp_path}: required_deps[{i}] must be an object"
					)
				name = dep.get("name")
				ver = dep.get("version")
				if not isinstance(name, str) or not name:
					raise ResolutionError(
						f"package at {dmp_path}: required_deps[{i}].name "
						f"must be a non-empty string"
					)
				if not isinstance(ver, str) or not ver:
					raise ResolutionError(
						f"package at {dmp_path}: required_deps[{i}].version "
						f"must be a non-empty string"
					)
				if not is_owner_declared_range(ver):
					raise ResolutionError(
						f"package at {dmp_path}: required_deps[{i}] "
						f"('{name}') version '{ver}' is not a valid "
						f"owner-declared range — `.dmp` metadata must "
						f"carry `\"M\"` (any M.x.x) or `\"M.N\"` (any "
						f"M.N.x).  Exact pins, `^`/`~` ranges, and other "
						f"shapes are malformed"
					)
				req_deps.append((name, ver))

			sha = _sha256_file(dmp_path)
			# Extract author_key from adjacent .sig sidecar.
			ak = _read_author_key(dmp_path)
			# Extract source identity + attestation signer from
			# the adjacent .source-attestation sidecar.  The helper
			# cross-binds the sidecar body to the .dmp's own
			# manifest fields and verifies the signature before
			# returning a non-empty kid; absent or unbound sidecar
			# yields ("", "") and `drift prepare` later refuses to
			# write a v4 lock entry without source identity for any
			# RESOLVED non-co-artifact dep.
			scid, sak = _read_source_attestation_meta(dmp_path, manifest)
			# Extract module_ids (the canonical trust-namespace keys)
			# from the manifest — trust allowlist is keyed by MODULE
			# namespace (`net_tls.*`), not package id (`net-tls`).
			# Package ids may contain characters (hyphens) that are
			# never valid in module paths; never pass pkg_id to
			# `TrustStore.allowed_kids_for_module`.
			#
			# Skip rules (non-string module_id, `*.__instantiations`)
			# come from the shared
			# `signature_v0.iter_trust_module_ids` predicate — same
			# source of truth as the `.sig` signer trust gate, so
			# the two cannot drift again.  PackageEntry retains
			# ALL module_ids from the manifest (for caller
			# introspection) but only the trust-participating subset
			# is used for the source-attestation allowlist gate
			# below.
			mod_ids: list[str] = []
			_modules = manifest.get("modules")
			if isinstance(_modules, list):
				for _m in _modules:
					if isinstance(_m, dict):
						_mid = _m.get("module_id")
						if isinstance(_mid, str) and _mid:
							mod_ids.append(_mid)
			trust_mod_ids: list[str] = []
			if _load_verifier is not None:
				from lang.driftc.packages.signature_v0 import iter_trust_module_ids
				trust_mod_ids = list(iter_trust_module_ids(manifest))
			# Cryptographic trust gate when a trust store is wired
			# in.  Failure is a HARD ERROR, not a silent prune —
			# dropping the offending package from the index would
			# let the resolver fall back to an older trusted
			# in-range version, silently masking the exact package
			# orch staged.  Source-rebuild's contract is "verify
			# what's on disk NOW against owner-namespace trust,"
			# so any disk package that fails the trust check must
			# surface as an error naming the file, not become
			# invisible.
			#
			# Three gates run here when `trust_store` is supplied:
			#   (a) `.sig` exists and `author_key` is non-empty.
			#       Missing `.sig` in source-rebuild is a hard
			#       error — the trust gate has nothing to verify
			#       against an unsigned disk package.  Skipping
			#       this would let a package with no `.sig` but
			#       a seemingly-valid `.source-attestation` pass,
			#       which violates the "installed artifact signer
			#       must verify against the trust store" contract.
			#   (b) `.sig` cryptographically verifies against the
			#       trust store's pubkeys AND every module_id the
			#       package declares maps to an allowlisted signer.
			#       Delegated to `verify_package_signatures`.
			#   (c) `source_attestation_key` is allowlisted for
			#       every module namespace AND not revoked.
			#       `_read_source_attestation_meta` already
			#       verifies the sidecar's self-signature; this
			#       step adds the namespace / revocation gate
			#       the sidecar alone can't enforce.
			if _load_verifier is not None:
				# Gate (a): missing .sig in source-rebuild is a
				# hard error for non-co-artifacts.  Co-artifacts
				# are injected post-index by the prepare caller,
				# not discovered here, so any .dmp reaching this
				# point is either a published package (must be
				# signed) or a dev-opt-in unsigned package (which
				# source-rebuild rejects by contract).
				if not ak:
					raise ResolutionError(
						f"package at '{dmp_path}' has no `.sig` "
						f"sidecar (empty author_key) — source-"
						f"rebuild requires every disk package to "
						f"cryptographically verify against the "
						f"trust store's namespace allowlist.  "
						f"An unsigned package has nothing to "
						f"verify; accepting it would bypass the "
						f"owner-namespace trust root.  Sign and "
						f"republish under a trusted kid before "
						f"using source-rebuild on this package."
					)
				# Gate (b): .sig verification + .sig-kid allowlist.
				err = _load_verifier(dmp_path, manifest)
				if err is not None:
					raise ResolutionError(
						f"package at '{dmp_path}' failed source-"
						f"rebuild trust verification: {err}.  The "
						f"`.sig` did not cryptographically verify "
						f"against the trust store's namespace "
						f"allowlist for this package's modules.  "
						f"Cannot silently fall back to an older "
						f"trusted in-range version — that would "
						f"mask the staged package orch intended to "
						f"certify.  Fix: update `drift/trust.json` "
						f"(or the user trust store) to authorise "
						f"the kid for the package's module "
						f"namespaces, or republish under an "
						f"already-trusted kid, then re-run the "
						f"source-rebuild pipeline."
					)
				# Gate (c): source_attestation_key allowlist +
				# revocation.  Applied per-module_id, matching the
				# .sig-kid gate in signature_v0.verify_package_
				# signatures EXACTLY — the module-id iteration
				# comes from the shared
				# `signature_v0.iter_trust_module_ids` predicate
				# (populated into `trust_mod_ids` above), so the
				# skip rules (`.__instantiations`, non-string) are
				# single-sourced and the two gates cannot drift.
				# Missing this predicate in the original patch
				# caused orch's MariaDB rebuild to fail on a
				# package whose `.sig` verified cleanly (orch
				# report 2026-04-21).
				if sak:
					for mid in trust_mod_ids:
						is_core = mid.startswith(("std.", "lang.", "drift."))
						allowed_for_mid = (
							core_trust_store.allowed_kids_for_module(mid)
							if is_core
							else trust_store.allowed_kids_for_module(mid)
						)
						if sak not in allowed_for_mid:
							raise ResolutionError(
								f"package at '{dmp_path}' `.source-"
								f"attestation` signer {sak!r} is not "
								f"in the trust store's namespace "
								f"allowlist for module '{mid}'.  "
								f"Source-rebuild requires the "
								f"attestation signer to be trusted "
								f"for every module the package "
								f"declares; update the trust store "
								f"or republish the sidecar under an "
								f"already-trusted kid."
							)
						# Revocation check against BOTH layers —
						# a kid revoked in either core or project
						# trust is treated as revoked.
						if (
							sak in core_trust_store.revoked_kids
							or sak in trust_store.revoked_kids
						):
							raise ResolutionError(
								f"package at '{dmp_path}' `.source-"
								f"attestation` signer {sak!r} is "
								f"REVOKED in the current trust "
								f"store.  Republish the sidecar "
								f"under a non-revoked kid before "
								f"using source-rebuild on this "
								f"package."
							)
			# Run-snapshot gate (mode 3: downstream source-rebuild
			# consumer).  The snapshot pins `(pkg_id, version)` to
			# an exact (source_content_id, author_key,
			# source_attestation_key) triple; the disk package must
			# match that triple or it's a same-version source swap,
			# an unauthorised re-sign, or a package not staged by
			# orch at all.  Hard fail on any mismatch.  The
			# downstream's local trust.json is NOT consulted —
			# author trust was verified by orch at staging time and
			# attested through the snapshot.
			if run_snapshot is not None:
				from tools.drift_deploy.run_snapshot import (
					verify_disk_entry_against_snapshot,
				)
				err = verify_disk_entry_against_snapshot(
					run_snapshot,
					pkg_id=pkg_id,
					version=pkg_ver_str,
					disk_source_content_id=scid,
					disk_author_key=ak,
					disk_source_attestation_key=sak,
				)
				if err is not None:
					raise ResolutionError(
						f"package at '{dmp_path}' does not match the "
						f"run snapshot: {err}"
					)
			entry = PackageEntry(
				package_id=pkg_id,
				version=pkg_ver,
				path=dmp_path,
				sha256=sha,
				required_deps=req_deps,
				author_key=ak,
				source_content_id=scid,
				source_attestation_key=sak,
				module_ids=tuple(mod_ids),
			)
			index.setdefault(pkg_id, []).append(entry)

	return index


def _find_root(path: Path, roots: list[Path]) -> Path:
	"""Find which root a path belongs to."""
	resolved = path.resolve()
	for root in roots:
		try:
			resolved.relative_to(root.resolve())
			return root
		except ValueError:
			continue
	return path.parent


def resolve_artifact(
	artifact_name: str,
	direct_deps: list[tuple[str, str]],  # [(package_name, constraint_string), ...]
	package_index: dict[str, list[PackageEntry]],
	searched_roots: list[Path] | None = None,
) -> dict[str, ResolvedDep]:
	"""
	Resolve dependencies for a single artifact.

	Returns {package_id → ResolvedDep} on success.
	Raises ResolutionError on conflict or unsatisfied constraint.

	This is the constraint-aggregation + highest-satisfying-all algorithm
	from the plan.
	"""
	# Step 1: Initialize constraint map from direct deps.
	constraint_map: dict[str, list[ConstraintEntry]] = {}
	direct_set: set[str] = set()
	for dep_name, constraint_str in direct_deps:
		try:
			c = parse_constraint(constraint_str)
		except ValueError as e:
			raise ResolutionError(
				f"artifact '{artifact_name}': invalid constraint '{constraint_str}' for package '{dep_name}': {e}"
			)
		constraint_map.setdefault(dep_name, []).append(
			ConstraintEntry(constraint=c, source=f"artifact '{artifact_name}'")
		)
		direct_set.add(dep_name)

	# Step 2: Initialize resolved map and work queue.
	resolved: dict[str, tuple[SemVer, PackageEntry]] = {}  # pkg_id → (version, entry)
	work_queue: set[str] = set(constraint_map.keys())

	# Step 3: Iterate until work queue is empty.
	while work_queue:
		# 3a: Pop lexicographically smallest (determinism guarantee).
		pkg_id = min(work_queue)
		work_queue.discard(pkg_id)

		# 3b: Collect all constraints on this package.
		constraints = constraint_map.get(pkg_id, [])
		if not constraints:
			continue

		# 3c: Find highest version satisfying ALL constraints.
		available = package_index.get(pkg_id, [])
		candidates = []
		for entry in available:
			if all(ce.constraint.satisfies(entry.version) for ce in constraints):
				candidates.append(entry)

		if not candidates:
			# 3d: No version satisfies → error.
			roots_str = ", ".join(str(r) for r in (searched_roots or []))
			if len(constraints) == 1:
				raise ResolutionError(
					f"artifact '{artifact_name}': package dependency "
					f"'{pkg_id} {constraints[0].constraint}' not satisfied"
					f"{f' (searched: {roots_str})' if roots_str else ''}"
				)
			lines = [f"conflicting constraints on '{pkg_id}':"]
			for ce in constraints:
				lines.append(f"  {ce.source} requires {pkg_id} {ce.constraint}")
			if available:
				avail_str = ", ".join(str(e.version) for e in sorted(available, key=lambda e: e.version))
				lines.append(f"  available versions: {avail_str}")
			else:
				lines.append(f"  no versions found in package roots")
			lines.append("  no version satisfies all constraints")
			raise ResolutionError("\n".join(lines))

		# Select highest.
		selected = max(candidates, key=lambda e: e.version)

		# 3e: Check if already resolved.
		if pkg_id in resolved:
			prev_ver, _prev_entry = resolved[pkg_id]
			if prev_ver == selected.version:
				continue
			# Resolved version no longer satisfies — conflict.
			lines = [f"conflicting constraints on '{pkg_id}':"]
			for ce in constraints:
				lines.append(f"  {ce.source} requires {pkg_id} {ce.constraint}")
			lines.append(f"  previously resolved to {prev_ver}, which no longer satisfies")
			lines.append("  no version satisfies all constraints")
			raise ResolutionError("\n".join(lines))

		# 3f: Record resolution.
		resolved[pkg_id] = (selected.version, selected)

		# 3g: Load transitive deps.
		for tdep_name, tdep_constraint_str in selected.required_deps:
			try:
				tc = parse_constraint(tdep_constraint_str)
			except ValueError:
				raise ResolutionError(
					f"package '{pkg_id}' {selected.version} declares invalid constraint "
					f"'{tdep_constraint_str}' for dependency '{tdep_name}'"
				)
			constraint_map.setdefault(tdep_name, []).append(
				ConstraintEntry(constraint=tc, source=f"{pkg_id} {selected.version}")
			)
			if tdep_name in resolved:
				# Verify existing resolution satisfies new constraint.
				existing_ver, _existing_entry = resolved[tdep_name]
				if not tc.satisfies(existing_ver):
					# Find all constraints for this package to build a good error.
					all_constraints = constraint_map.get(tdep_name, [])
					lines = [f"conflicting constraints on '{tdep_name}':"]
					for ce in all_constraints:
						lines.append(f"  {ce.source} requires {tdep_name} {ce.constraint}")
					lines.append(f"  resolved version {existing_ver} does not satisfy {tc}")
					lines.append("  no version satisfies all constraints")
					raise ResolutionError("\n".join(lines))
			else:
				work_queue.add(tdep_name)

	# Step 4: Build result.  Each resolved entry carries the exact
	# `M.N.P` version of the picked candidate plus both halves of
	# the v4 identity model: artifact half (`sha256` + `author_key`)
	# and source half (`source_content_id` +
	# `source_attestation_key`).  All five are re-checked at strict
	# build/deploy time; source-rebuild mode (Phase D) tolerates
	# `sha256` drift only after the source half re-verifies.
	result: dict[str, ResolvedDep] = {}
	for pkg_id, (ver, entry) in resolved.items():
		result[pkg_id] = ResolvedDep(
			version=str(ver),
			sha256=entry.sha256,
			dep_type="direct" if pkg_id in direct_set else "transitive",
			package_id=pkg_id,
			author_key=entry.author_key,
			source_content_id=entry.source_content_id,
			source_attestation_key=entry.source_attestation_key,
		)
	return result
