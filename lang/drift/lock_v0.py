# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Drift lockfile (v0).

This lockfile is intentionally minimal: it records the exact package identities
and content hashes used by a build to support reproducible vendoring and CI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from lang.drift.dmir_pkg_v0 import canonical_json_bytes


def _load_lock_json(path: Path) -> dict[str, Any]:
	data = __import__("json").loads(path.read_text(encoding="utf-8"))
	if not isinstance(data, dict):
		raise ValueError("lockfile must be a JSON object")
	if data.get("format") != "drift-lock" or data.get("version") != 0:
		raise ValueError("unsupported lockfile format/version (upgrade drift?)")
	allowed_top = {"format", "version", "packages", "x"}
	unknown_top = sorted(set(data.keys()) - allowed_top)
	if unknown_top:
		raise ValueError(f"lockfile has unknown top-level fields: {', '.join(unknown_top)}")
	if "x" in data and not isinstance(data.get("x"), dict):
		raise ValueError("lockfile top-level 'x' must be an object")
	return data


@dataclass(frozen=True)
class ObservedIdentity:
	package_id: str
	package_version: str
	target: str


@dataclass(frozen=True)
class LockEntry:
	package_id: str
	package_version: str
	target: str
	observed_identity: ObservedIdentity
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
				"observed_identity": {
					"package_id": e.observed_identity.package_id,
					"version": e.observed_identity.package_version,
					"target": e.observed_identity.target,
				},
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
	tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
	tmp.write_bytes(canonical_json_bytes(obj))
	os.replace(tmp, path)


def load_lock_entries_v0(path: Path) -> dict[str, LockEntry]:
	data = _load_lock_json(path)
	pkgs = data.get("packages")
	if not isinstance(pkgs, dict):
		raise ValueError("lockfile packages must be an object")

	allowed_fields = {
		"version",
		"target",
		"observed_identity",
		"pkg_sha256",
		"sig_sha256",
		"sig_kids",
		"modules",
		"source_id",
		"path",
		"x",
	}

	out: dict[str, LockEntry] = {}
	for package_id, raw in pkgs.items():
		if not isinstance(package_id, str) or not package_id:
			raise ValueError("lockfile packages keys must be non-empty strings")
		if not isinstance(raw, dict):
			raise ValueError(f"lockfile entry for package_id '{package_id}' must be an object")
		unknown_fields = sorted(set(raw.keys()) - allowed_fields)
		if unknown_fields:
			raise ValueError(f"lockfile entry for package_id '{package_id}' has unknown fields: {', '.join(unknown_fields)}")
		if "x" in raw and not isinstance(raw.get("x"), dict):
			raise ValueError(f"lockfile entry for package_id '{package_id}' field 'x' must be an object")
		version = raw.get("version")
		target = raw.get("target")
		observed_raw = raw.get("observed_identity")
		pkg_sha256 = raw.get("pkg_sha256")
		sig_sha256 = raw.get("sig_sha256")
		sig_kids = raw.get("sig_kids")
		modules = raw.get("modules")
		source_id = raw.get("source_id")
		path_str = raw.get("path")
		if not isinstance(version, str) or not version:
			raise ValueError(f"lockfile entry for package_id '{package_id}' is missing version")
		if not isinstance(target, str) or not target:
			raise ValueError(f"lockfile entry for package_id '{package_id}' is missing target")
		if not isinstance(observed_raw, dict):
			raise ValueError(f"lockfile entry for package_id '{package_id}' is missing observed_identity")
		obs_pid = observed_raw.get("package_id")
		obs_ver = observed_raw.get("version")
		obs_target = observed_raw.get("target")
		if not isinstance(obs_pid, str) or not obs_pid:
			raise ValueError(f"lockfile entry for package_id '{package_id}' observed_identity is missing package_id")
		if not isinstance(obs_ver, str) or not obs_ver:
			raise ValueError(f"lockfile entry for package_id '{package_id}' observed_identity is missing version")
		if not isinstance(obs_target, str) or not obs_target:
			raise ValueError(f"lockfile entry for package_id '{package_id}' observed_identity is missing target")
		if obs_pid != package_id or obs_ver != version or obs_target != target:
			raise ValueError(f"lockfile entry for package_id '{package_id}' observed_identity does not match entry fields")
		if not isinstance(pkg_sha256, str) or not pkg_sha256.startswith("sha256:"):
			raise ValueError(f"lockfile entry for package_id '{package_id}' is missing pkg_sha256")
		if sig_sha256 is not None and (not isinstance(sig_sha256, str) or not sig_sha256.startswith("sha256:")):
			raise ValueError(f"lockfile entry for package_id '{package_id}' sig_sha256 must be null or 'sha256:<hex>'")
		if not isinstance(sig_kids, list) or any((not isinstance(k, str) or not k) for k in sig_kids):
			raise ValueError(f"lockfile entry for package_id '{package_id}' sig_kids must be a list of strings")
		if not isinstance(modules, list) or any((not isinstance(m, str) or not m) for m in modules):
			raise ValueError(f"lockfile entry for package_id '{package_id}' modules must be a list of strings")
		if not isinstance(source_id, str) or not source_id or source_id == "unknown":
			raise ValueError(
				f"lockfile entry for package_id '{package_id}' is missing source_id; regenerate the lockfile with 'drift vendor'"
			)
		if not isinstance(path_str, str) or not path_str:
			raise ValueError(f"lockfile entry for package_id '{package_id}' is missing path")

		out[package_id] = LockEntry(
			package_id=package_id,
			package_version=version,
			target=target,
			observed_identity=ObservedIdentity(
				package_id=obs_pid,
				package_version=obs_ver,
				target=obs_target,
			),
			pkg_sha256=pkg_sha256,
			sig_sha256=sig_sha256,
			sig_kids=list(sig_kids),
			modules=list(modules),
			source_id=source_id,
			path=path_str,
		)

	return out


def load_lock(path: Path) -> dict[str, Any]:
	"""
	Load and validate a lockfile, returning the raw decoded JSON object.

	Prefer `load_lock_entries_v0` for type-safe access.
	"""
	load_lock_entries_v0(path)
	return _load_lock_json(path)
