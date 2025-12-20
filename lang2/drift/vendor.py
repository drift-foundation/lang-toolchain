# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from lang2.drift.dmir_pkg_v0 import read_identity_v0, sha256_hex
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
			source_id=str(raw["source_id"]) if isinstance(raw.get("source_id"), str) and raw.get("source_id") else None,
			path=str(raw["path"]) if isinstance(raw.get("path"), str) and raw.get("path") else None,
		)
		src_pkg = opts.cache_dir / "pkgs" / entry.filename
		if not src_pkg.exists():
			raise ValueError(f"missing cached package file: {src_pkg}")

		dst_pkg = opts.dest_dir / entry.filename
		shutil.copyfile(src_pkg, dst_pkg)
		src_sig = src_pkg.with_suffix(src_pkg.suffix + ".sig")
		if src_sig.exists():
			shutil.copyfile(src_sig, opts.dest_dir / src_sig.name)

		# Record exact bytes in the lockfile so future fetches can reproduce the
		# same artifacts (or fail loudly).
		pkg_hex = sha256_hex(dst_pkg.read_bytes())
		pkg_sha = f"sha256:{pkg_hex}"
		if entry.sha256 and entry.sha256 != pkg_sha:
			raise ValueError(f"cached package sha256 mismatch for {entry.package_id}: index {entry.sha256} != bytes {pkg_sha}")
		sig_sha: str | None = None
		if src_sig.exists():
			sig_bytes = (opts.dest_dir / src_sig.name).read_bytes()
			sig_sha = f"sha256:{sha256_hex(sig_bytes)}"
		ident = read_identity_v0(dst_pkg)
		mod_ids: list[str] = []
		raw_mods = ident.manifest.get("modules")
		if isinstance(raw_mods, list):
			for m in raw_mods:
				if isinstance(m, dict) and isinstance(m.get("module_id"), str):
					mod_ids.append(m["module_id"])

		out_entries.append(
			LockEntry(
				package_id=entry.package_id,
				package_version=entry.package_version,
				target=entry.target,
				pkg_sha256=pkg_sha,
				sig_sha256=sig_sha,
				sig_kids=sorted(set(entry.signers)),
				modules=sorted(set(mod_ids)),
				source_id=entry.source_id or "",
				path=entry.path or entry.filename,
			)
		)

	if selected:
		missing = sorted(selected - set(pkgs_map.keys()))
		if missing:
			raise ValueError(f"requested package ids not found in cache index: {', '.join(missing)}")

	for e in out_entries:
		if not e.source_id:
			raise ValueError(
				f"cache entry for package_id '{e.package_id}' is missing source_id; re-fetch the package via 'drift fetch' before vendoring"
			)

	save_lock(opts.lock_path, out_entries)
