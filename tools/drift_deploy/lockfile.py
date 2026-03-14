# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift-lock.json read/write/verify.

The lock file records the exact resolved dependency graph per artifact.
It is the reproducibility artifact — checked into version control,
used by CI to get deterministic builds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.drift_deploy.resolver import ResolvedDep


LOCK_SCHEMA_VERSION = 1


def write_lock(
	path: Path,
	artifacts: dict[str, dict[str, ResolvedDep]],
) -> None:
	"""
	Write drift-lock.json.

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
				"version": dep.version,
				"integrity": dep.integrity,
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
	Read drift-lock.json.

	Returns {artifact_name → {package_id → ResolvedDep}}.
	Raises ValueError on invalid lock file.
	"""
	data = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(data, dict):
		raise ValueError("drift-lock.json must be a JSON object")
	sv = data.get("schema_version")
	if sv != LOCK_SCHEMA_VERSION:
		raise ValueError(f"unsupported drift-lock.json schema_version: {sv}")
	artifacts_obj = data.get("artifacts")
	if not isinstance(artifacts_obj, dict):
		raise ValueError("drift-lock.json missing 'artifacts' object")

	result: dict[str, dict[str, ResolvedDep]] = {}
	for art_name, art_data in artifacts_obj.items():
		if not isinstance(art_data, dict):
			raise ValueError(f"drift-lock.json artifact '{art_name}' must be an object")
		resolved_obj = art_data.get("resolved")
		if not isinstance(resolved_obj, dict):
			raise ValueError(f"drift-lock.json artifact '{art_name}' missing 'resolved' object")
		resolved: dict[str, ResolvedDep] = {}
		for pkg_id, dep_data in resolved_obj.items():
			if not isinstance(dep_data, dict):
				raise ValueError(f"drift-lock.json artifact '{art_name}' dep '{pkg_id}' must be an object")
			version = dep_data.get("version")
			integrity = dep_data.get("integrity")
			dep_type = dep_data.get("dep_type", "direct")
			if not isinstance(version, str) or not isinstance(integrity, str):
				raise ValueError(f"drift-lock.json artifact '{art_name}' dep '{pkg_id}' missing version/integrity")
			resolved[pkg_id] = ResolvedDep(version=version, integrity=integrity, dep_type=dep_type)
		result[art_name] = resolved
	return result


def verify_lock_integrity(
	lock_deps: dict[str, ResolvedDep],
	package_index: dict[str, list],
) -> list[str]:
	"""
	Verify lock file integrity against the current package index.

	Returns a list of error messages (empty = all good).
	"""
	from tools.drift_deploy.resolver import PackageEntry
	errors: list[str] = []
	for pkg_id, dep in lock_deps.items():
		entries = package_index.get(pkg_id, [])
		matching = [e for e in entries if str(e.version) == dep.version]
		if not matching:
			errors.append(
				f"locked dependency '{pkg_id}' version '{dep.version}' not found under package roots"
			)
			continue
		entry: PackageEntry = matching[0]
		expected_integrity = f"sha256:{entry.sha256}"
		if dep.integrity != expected_integrity:
			errors.append(
				f"locked dependency '{pkg_id}' integrity mismatch "
				f"(expected {dep.integrity}, got {expected_integrity})"
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
