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


@dataclass(frozen=True)
class PackageEntry:
	"""A discovered package in the package index."""
	package_id: str
	version: SemVer
	path: Path
	sha256: str
	package_deps: list[tuple[str, str]]  # [(dep_name, constraint_string), ...]
	author_key: str = ""  # "ed25519:<kid>" from signature sidecar


@dataclass(frozen=True)
class ConstraintEntry:
	"""A constraint on a package, with provenance."""
	constraint: Constraint
	source: str  # e.g., "artifact 'tls-tool'" or "net.tls 0.3.2"


@dataclass(frozen=True)
class ResolvedDep:
	"""A single resolved dependency.

	v2 lock: version is major.minor compatibility range (e.g. "0.3"),
	author_key is the signing key kid, integrity is unused.
	v1 legacy: version is exact, integrity is sha256 of artifact bytes.
	"""
	version: str  # v2: "major.minor" range; v1: exact version
	integrity: str  # v1: "sha256:<hex>"; v2: "" (unused)
	dep_type: str  # "direct", "transitive", or "co-artifact"
	package_id: str = ""  # package name
	author_key: str = ""  # "ed25519:<kid>" of signer


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


def version_compat_range(version: str) -> str:
	"""Extract the major.minor compatibility range from a version string.

	"0.3.14" → "0.3"
	"1.2.0" → "1.2"
	"0.3" → "0.3"
	"""
	parts = version.split(".")
	if len(parts) >= 2:
		return f"{parts[0]}.{parts[1]}"
	return version


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
			except Exception:
				# If a .zdmp failed, try .dmp sibling as fallback.
				if dmp_path.suffix == ".zdmp":
					dmp_sibling = dmp_path.with_suffix(".dmp")
					if dmp_sibling.exists():
						try:
							manifest = load_manifest(dmp_sibling)
							dmp_path = dmp_sibling
						except Exception:
							continue
					else:
						continue
				else:
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

			# Extract package_deps from manifest.
			raw_deps = manifest.get("package_deps", [])
			pkg_deps: list[tuple[str, str]] = []
			if isinstance(raw_deps, list):
				for dep in raw_deps:
					if isinstance(dep, dict):
						name = dep.get("name")
						ver = dep.get("version")
						if isinstance(name, str) and isinstance(ver, str):
							pkg_deps.append((name, ver))

			sha = _sha256_file(dmp_path)
			# Extract author_key from adjacent .sig sidecar.
			ak = _read_author_key(dmp_path)
			entry = PackageEntry(
				package_id=pkg_id,
				version=pkg_ver,
				path=dmp_path,
				sha256=sha,
				package_deps=pkg_deps,
				author_key=ak,
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
		for tdep_name, tdep_constraint_str in selected.package_deps:
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

	# Step 4: Build result.
	result: dict[str, ResolvedDep] = {}
	for pkg_id, (ver, entry) in resolved.items():
		result[pkg_id] = ResolvedDep(
			version=str(ver),
			integrity="",
			dep_type="direct" if pkg_id in direct_set else "transitive",
			package_id=pkg_id,
			author_key=entry.author_key,
		)
	return result
