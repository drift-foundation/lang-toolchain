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
) -> dict[str, list[PackageEntry]]:
	"""
	Build a package index from package roots.

	Returns {package_id → [PackageEntry, ...]}, one entry per distinct version.
	If the same package_id+version appears in multiple roots, the first root wins.

	`load_manifest` is a callable(Path) → dict that loads a .dmp manifest.
	If None, uses the dmir_pkg_v0 loader.
	"""
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
			entry = PackageEntry(
				package_id=pkg_id,
				version=pkg_ver,
				path=dmp_path,
				sha256=sha,
				required_deps=req_deps,
				author_key=ak,
				source_content_id=scid,
				source_attestation_key=sak,
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
