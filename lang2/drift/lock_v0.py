# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Drift lockfile (v0).

This lockfile is intentionally minimal: it records the exact package identities
and content hashes used by a build to support reproducible vendoring and CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from lang2.drift.dmir_pkg_v0 import canonical_json_bytes


@dataclass(frozen=True)
class LockEntry:
	package_id: str
	package_version: str
	target: str
	pkg_sha256: str  # "sha256:<hex>" of pkg.dmp bytes
	sig_sha256: str | None  # "sha256:<hex>" of pkg.dmp.sig bytes (when required)
	sig_kids: list[str]
	modules: list[str]
	source_id: str
	path: str


def save_lock(path: Path, entries: list[LockEntry]) -> None:
	obj = {
		"format": "drift-lock",
		"version": 0,
		"packages": {
			e.package_id: {
				"version": e.package_version,
				"target": e.target,
				"pkg_sha256": e.pkg_sha256,
				"sig_sha256": e.sig_sha256,
				"sig_kids": list(e.sig_kids),
				"modules": list(e.modules),
				"source_id": e.source_id,
				"path": e.path,
			}
			for e in sorted(entries, key=lambda e: (e.package_id, e.target, e.package_version))
		},
	}
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(canonical_json_bytes(obj))


def load_lock(path: Path) -> dict[str, Any]:
	data = __import__("json").loads(path.read_text(encoding="utf-8"))
	if not isinstance(data, dict):
		raise ValueError("lockfile must be a JSON object")
	if data.get("format") != "drift-lock" or data.get("version") != 0:
		raise ValueError("unsupported lockfile format/version")
	pkgs = data.get("packages")
	if not isinstance(pkgs, dict):
		raise ValueError("lockfile packages must be an object")

	for package_id, raw in pkgs.items():
		if not isinstance(package_id, str) or not package_id:
			raise ValueError("lockfile packages keys must be non-empty strings")
		if not isinstance(raw, dict):
			raise ValueError(f"lockfile entry for package_id '{package_id}' must be an object")
		version = raw.get("version")
		target = raw.get("target")
		pkg_sha256 = raw.get("pkg_sha256")
		source_id = raw.get("source_id")
		path_str = raw.get("path")
		if not isinstance(version, str) or not version:
			raise ValueError(f"lockfile entry for package_id '{package_id}' is missing version")
		if not isinstance(target, str) or not target:
			raise ValueError(f"lockfile entry for package_id '{package_id}' is missing target")
		if not isinstance(pkg_sha256, str) or not pkg_sha256.startswith("sha256:"):
			raise ValueError(f"lockfile entry for package_id '{package_id}' is missing pkg_sha256")
		if not isinstance(source_id, str) or not source_id or source_id == "unknown":
			raise ValueError(
				f"lockfile entry for package_id '{package_id}' is missing source_id; regenerate the lockfile with 'drift vendor'"
			)
		if not isinstance(path_str, str) or not path_str:
			raise ValueError(f"lockfile entry for package_id '{package_id}' is missing path")

	return data
