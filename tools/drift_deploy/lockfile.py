# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift/lock.json read/write/verify.

The lock file records the dependency compatibility contract per artifact.
It is checked into version control and used by CI for reproducible builds.

Schema v2 (two-layer model):
  Lock file = compatibility contract (version range + author trust).
  Certified snapshot = exact freeze (orchestrator-managed).

The lock pins major.minor version range and signing key.  Patch updates
within the minor are accepted silently.  Minor/major bumps and key
rotation require `prepare`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.drift_deploy.resolver import ResolvedDep, version_compat_range


LOCK_SCHEMA_VERSION = 2


def write_lock(
	path: Path,
	artifacts: dict[str, dict[str, ResolvedDep]],
) -> None:
	"""
	Write drift/lock.json (schema v2).

	`artifacts` is {artifact_name → {package_id → ResolvedDep}}.
	"""
	obj: dict[str, Any] = {
		"schema_version": LOCK_SCHEMA_VERSION,
		"artifacts": {},
	}
	for art_name in sorted(artifacts.keys()):
		resolved = artifacts[art_name]
		resolved_obj: dict[str, Any] = {}
		for pkg_id in sorted(resolved.keys()):
			dep = resolved[pkg_id]
			resolved_obj[pkg_id] = {
				"version": version_compat_range(dep.version),
				"package_id": dep.package_id or pkg_id,
				"author_key": dep.author_key,
				"dep_type": dep.dep_type,
			}
		obj["artifacts"][art_name] = {"resolved": resolved_obj}
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
		encoding="utf-8",
	)


def read_lock(path: Path) -> dict[str, dict[str, ResolvedDep]]:
	"""
	Read drift/lock.json.

	Accepts schema v2 only.  v1 locks are rejected with a clear
	message to run `prepare`.

	Returns {artifact_name → {package_id → ResolvedDep}}.
	"""
	data = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(data, dict):
		raise ValueError("drift/lock.json must be a JSON object")
	sv = data.get("schema_version")
	if sv == 1:
		raise ValueError(
			"drift/lock.json uses schema v1 (artifact-byte identity). "
			"Run 'drift prepare' to regenerate with schema v2 "
			"(compatibility range + author trust)."
		)
	if sv != LOCK_SCHEMA_VERSION:
		raise ValueError(
			f"unsupported drift/lock.json schema_version: {sv} "
			f"(expected {LOCK_SCHEMA_VERSION}; run 'drift prepare' to regenerate)"
		)
	artifacts_obj = data.get("artifacts")
	if not isinstance(artifacts_obj, dict):
		raise ValueError("drift/lock.json missing 'artifacts' object")

	result: dict[str, dict[str, ResolvedDep]] = {}
	for art_name, art_data in artifacts_obj.items():
		if not isinstance(art_data, dict):
			raise ValueError(f"drift/lock.json artifact '{art_name}' must be an object")
		resolved_obj = art_data.get("resolved")
		if not isinstance(resolved_obj, dict):
			raise ValueError(f"drift/lock.json artifact '{art_name}' missing 'resolved' object")
		resolved: dict[str, ResolvedDep] = {}
		for pkg_id, dep_data in resolved_obj.items():
			if not isinstance(dep_data, dict):
				raise ValueError(f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' must be an object")
			version = dep_data.get("version")
			if not isinstance(version, str):
				raise ValueError(f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' missing version")
			dep_pkg_id = dep_data.get("package_id", "")
			dep_author_key = dep_data.get("author_key", "")
			dep_type = dep_data.get("dep_type", "direct")
			if dep_type != "co-artifact":
				if not dep_pkg_id:
					raise ValueError(
						f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
						f"missing required 'package_id'"
					)
				if not dep_author_key:
					raise ValueError(
						f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
						f"missing required 'author_key' — packages must be "
						f"signed before locking; run 'drift prepare' after signing"
					)
				# "unsigned" is an explicit opt-in for development builds
				# where packages are consumed via --allow-unsigned-from.
			resolved[pkg_id] = ResolvedDep(
				version=version,
				integrity="",
				dep_type=dep_type,
				package_id=dep_pkg_id or pkg_id,
				author_key=dep_author_key,
			)
		result[art_name] = resolved
	return result


def verify_lock_compatibility(
	lock_deps: dict[str, ResolvedDep],
	package_index: dict[str, list],
) -> list[str]:
	"""
	Verify lock file compatibility against the current package index.

	Checks:
	  - A version matching the locked minor range exists
	  - The signing key matches (author trust)

	Does NOT check artifact bytes or source digest — those concerns
	belong to signature verification and certified snapshots.

	Returns a list of error messages (empty = all good).
	"""
	from tools.drift_deploy.resolver import PackageEntry
	errors: list[str] = []
	for pkg_id, dep in lock_deps.items():
		if dep.dep_type == "co-artifact":
			continue
		entries = package_index.get(pkg_id, [])
		if not entries:
			errors.append(
				f"locked dependency '{pkg_id}' not found under package roots"
			)
			continue
		# Find any version in the locked minor range.
		locked_range = dep.version  # e.g. "0.3"
		compatible = [
			e for e in entries
			if version_compat_range(str(e.version)) == locked_range
		]
		if not compatible:
			available = sorted({str(e.version) for e in entries})
			errors.append(
				f"locked dependency '{pkg_id}' requires version {locked_range}.* "
				f"but available versions are: {', '.join(available)}"
			)
			continue
		# Check author key against the best (highest) compatible version.
		best = max(compatible, key=lambda e: e.version)
		# Skip author check for explicitly unsigned dev packages.
		if dep.author_key == "unsigned":
			continue
		if not best.author_key:
			errors.append(
				f"locked dependency '{pkg_id}' is unsigned in the current "
				f"package root (expected signer {dep.author_key})"
			)
		elif dep.author_key != best.author_key:
			errors.append(
				f"locked dependency '{pkg_id}' signing key changed\n"
				f"  previous: {dep.author_key}\n"
				f"  current:  {best.author_key}\n"
				f"  run 'drift prepare' to accept the new key"
			)
	return errors




def expand_to_dep_flags(resolved: dict[str, ResolvedDep]) -> list[str]:
	"""
	Expand a resolved graph into --dep flags for driftc.

	Returns ["--dep", "net.tls@0.3.2", "--dep", "acme.crypto@0.9.0", ...].
	Order is deterministic (sorted by package_id).
	"""
	flags: list[str] = []
	for pkg_id in sorted(resolved.keys()):
		dep = resolved[pkg_id]
		flags.extend(["--dep", f"{pkg_id}@{dep.version}"])
	return flags
