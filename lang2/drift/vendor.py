# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from lang2.drift.index_v0 import IndexEntry, load_index
from lang2.drift.lock_v0 import LockEntry, save_lock


@dataclass(frozen=True)
class VendorOptions:
	cache_dir: Path = Path("cache") / "driftpm"
	dest_dir: Path = Path("vendor") / "driftpkgs"
	lock_path: Path = Path("drift.lock.json")
	package_ids: list[str] | None = None


def vendor_v0(opts: VendorOptions) -> None:
	"""
Vendor packages into a project directory for CI/offline use.

MVP behavior:
- copies selected packages from the local cache into vendor/driftpkgs
- writes a minimal lockfile containing the exact identities + hashes
	"""
	index_path = opts.cache_dir / "index.json"
	index_obj = load_index(index_path)
	pkgs_map = index_obj.get("packages") or {}
	if not isinstance(pkgs_map, dict):
		raise ValueError("cache index packages must be an object")

	selected = set(opts.package_ids or [])
	out_entries: list[LockEntry] = []
	opts.dest_dir.mkdir(parents=True, exist_ok=True)

	for package_id, raw in pkgs_map.items():
		if selected and package_id not in selected:
			continue
		if not isinstance(raw, dict):
			raise ValueError("cache index entry must be an object")
		entry = IndexEntry(
			package_id=package_id,
			package_version=str(raw.get("package_version", "")),
			target=str(raw.get("target", "")),
			sha256=str(raw.get("sha256", "")),
			filename=str(raw.get("filename", "")),
			signers=list(raw.get("signers") or []),
			unsigned=bool(raw.get("unsigned", False)),
		)
		src_pkg = opts.cache_dir / "pkgs" / entry.filename
		if not src_pkg.exists():
			raise ValueError(f"missing cached package file: {src_pkg}")

		dst_pkg = opts.dest_dir / entry.filename
		shutil.copyfile(src_pkg, dst_pkg)
		src_sig = src_pkg.with_suffix(src_pkg.suffix + ".sig")
		if src_sig.exists():
			shutil.copyfile(src_sig, opts.dest_dir / src_sig.name)

		out_entries.append(
			LockEntry(
				package_id=entry.package_id,
				package_version=entry.package_version,
				target=entry.target,
				sha256=entry.sha256,
				filename=entry.filename,
			)
		)

	if selected:
		missing = sorted(selected - set(pkgs_map.keys()))
		if missing:
			raise ValueError(f"requested package ids not found in cache index: {', '.join(missing)}")

	save_lock(opts.lock_path, out_entries)

