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
	sha256: str  # "sha256:<hex>"
	filename: str


def save_lock(path: Path, entries: list[LockEntry]) -> None:
	obj = {
		"format": "drift-lock",
		"version": 0,
		"packages": [
			{
				"package_id": e.package_id,
				"package_version": e.package_version,
				"target": e.target,
				"sha256": e.sha256,
				"filename": e.filename,
			}
			for e in sorted(entries, key=lambda e: (e.package_id, e.target, e.package_version))
		],
	}
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(canonical_json_bytes(obj))


def load_lock(path: Path) -> dict[str, Any]:
	data = __import__("json").loads(path.read_text(encoding="utf-8"))
	if not isinstance(data, dict):
		raise ValueError("lockfile must be a JSON object")
	if data.get("format") != "drift-lock" or data.get("version") != 0:
		raise ValueError("unsupported lockfile format/version")
	return data

