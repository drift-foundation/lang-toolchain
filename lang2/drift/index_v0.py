# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Drift package repository index (v0).

This is a deliberately tiny, deterministic JSON index used by `drift publish`
and `drift fetch`. It is not a registry protocol; it's a local/offline format
for directory-based repositories.

Pinned MVP rule: a repository contains at most one version per package_id.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from lang2.drift.dmir_pkg_v0 import canonical_json_bytes


@dataclass(frozen=True)
class IndexEntry:
	package_id: str
	package_version: str
	target: str
	sha256: str  # "sha256:<hex>"
	filename: str
	signers: list[str]  # kids
	unsigned: bool
	# Optional provenance: which source repository provided this entry and the
	# path within that repository. These fields are informational for MVP; the
	# lockfile may pin them for reproducibility.
	source_id: str | None = None
	path: str | None = None


def _empty_index() -> dict[str, Any]:
	return {"format": "drift-index", "version": 0, "packages": {}}


def load_index(path: Path) -> dict[str, Any]:
	if not path.exists():
		return _empty_index()
	obj = path.read_text(encoding="utf-8")
	data = __import__("json").loads(obj)
	if not isinstance(data, dict):
		raise ValueError("index must be a JSON object")
	if data.get("format") != "drift-index" or data.get("version") != 0:
		raise ValueError("unsupported index format/version")
	if "packages" not in data or not isinstance(data["packages"], dict):
		raise ValueError("index missing packages map")
	return data


def save_index(path: Path, index_obj: Mapping[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(canonical_json_bytes(dict(index_obj)))


def get_entry(index_obj: Mapping[str, Any], package_id: str) -> Optional[IndexEntry]:
	pkgs = index_obj.get("packages")
	if not isinstance(pkgs, dict):
		return None
	raw = pkgs.get(package_id)
	if not isinstance(raw, dict):
		return None
	try:
		return IndexEntry(
			package_id=package_id,
			package_version=str(raw["package_version"]),
			target=str(raw["target"]),
			sha256=str(raw["sha256"]),
			filename=str(raw["filename"]),
			signers=list(raw.get("signers") or []),
			unsigned=bool(raw.get("unsigned", False)),
			source_id=str(raw["source_id"]) if isinstance(raw.get("source_id"), str) and raw.get("source_id") else None,
			path=str(raw["path"]) if isinstance(raw.get("path"), str) and raw.get("path") else None,
		)
	except KeyError:
		return None


def upsert_entry(
	index_obj: dict[str, Any],
	*,
	entry: IndexEntry,
	force: bool,
) -> None:
	pkgs = index_obj.setdefault("packages", {})
	if not isinstance(pkgs, dict):
		raise ValueError("index packages map is not an object")

	existing = pkgs.get(entry.package_id)
	if existing is not None and not isinstance(existing, dict):
		raise ValueError("index packages entry is not an object")

	if existing is not None and not force:
		prev_ver = str(existing.get("package_version", ""))
		prev_target = str(existing.get("target", ""))
		prev_sha = str(existing.get("sha256", ""))
		if prev_ver != entry.package_version or prev_target != entry.target:
			raise ValueError(
				f"package_id '{entry.package_id}' already published as {prev_ver} for {prev_target} (use --force to replace)"
			)
		if prev_sha and prev_sha != entry.sha256:
			raise ValueError(
				f"package_id '{entry.package_id}' already published with different sha256 (use --force to replace)"
			)

	pkgs[entry.package_id] = {
		"package_version": entry.package_version,
		"target": entry.target,
		"sha256": entry.sha256,
		"filename": entry.filename,
		"signers": sorted(set(entry.signers)),
		"unsigned": entry.unsigned,
	}
	if entry.source_id:
		pkgs[entry.package_id]["source_id"] = entry.source_id
	if entry.path:
		pkgs[entry.package_id]["path"] = entry.path
