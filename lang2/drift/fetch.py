# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from lang2.drift.dmir_pkg_v0 import sha256_hex
from lang2.drift.index_v0 import IndexEntry, load_index, save_index, upsert_entry
from lang2.drift.sources_v0 import load_sources_v0


@dataclass(frozen=True)
class FetchOptions:
	sources_path: Path
	cache_dir: Path = Path("cache") / "driftpm"
	force: bool = False


def fetch_v0(opts: FetchOptions) -> None:
	"""
Fetch packages from local directory repositories into a project-local cache.

MVP constraints:
- sources are local directories only (no network)
- we fetch "everything" listed in each index
- conflicts on package_id are hard errors unless --force
	"""
	sources = load_sources_v0(opts.sources_path)

	cache_dir = opts.cache_dir
	pkgs_dir = cache_dir / "pkgs"
	pkgs_dir.mkdir(parents=True, exist_ok=True)
	cache_index_path = cache_dir / "index.json"

	merged = load_index(cache_index_path)

	for src in sources.sources:
		repo = src.path
		index_path = repo / "index.json"
		index_obj = load_index(index_path)
		pkgs = index_obj.get("packages") or {}
		if not isinstance(pkgs, dict):
			raise ValueError("source index packages must be an object")

		for package_id, raw in pkgs.items():
			if not isinstance(raw, dict):
				raise ValueError("source index entry must be an object")
			entry = IndexEntry(
				package_id=package_id,
				package_version=str(raw.get("package_version", "")),
				target=str(raw.get("target", "")),
				sha256=str(raw.get("sha256", "")),
				filename=str(raw.get("filename", "")),
				signers=list(raw.get("signers") or []),
				unsigned=bool(raw.get("unsigned", False)),
			)
			if not entry.package_version or not entry.target or not entry.filename or not entry.sha256:
				raise ValueError(f"invalid index entry for {package_id} in {index_path}")

			src_pkg = repo / entry.filename
			if not src_pkg.exists():
				raise ValueError(f"missing package file referenced by index: {src_pkg}")

			# Copy into cache with deterministic, identity-derived name.
			dst_name = entry.filename
			dst_pkg = pkgs_dir / dst_name
			shutil.copyfile(src_pkg, dst_pkg)

			# If a sidecar exists in the repository, mirror it.
			src_sig = repo / (entry.filename + ".sig")
			if src_sig.exists():
				shutil.copyfile(src_sig, pkgs_dir / (dst_name + ".sig"))

			# Guardrail: ensure the copied bytes match the index sha256.
			hex_digest = sha256_hex(dst_pkg.read_bytes())
			if entry.sha256 != f"sha256:{hex_digest}":
				raise ValueError(f"sha256 mismatch for fetched package {dst_pkg}")

			upsert_entry(merged, entry=IndexEntry(**{**entry.__dict__, "filename": str((pkgs_dir / dst_name).name)}), force=opts.force)

	save_index(cache_index_path, merged)

