# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from lang2.drift.dmir_pkg_v0 import PackageFormatError, read_identity_v0
from lang2.drift.dmir_pkg_v0 import sha256_hex
from lang2.drift.index_v0 import IndexEntry, load_index, save_index, upsert_entry
from lang2.drift.lock_v0 import load_lock
from lang2.drift.sources_v0 import load_sources_v0


@dataclass(frozen=True)
class FetchOptions:
	sources_path: Path
	cache_dir: Path = Path("cache") / "driftpm"
	force: bool = False
	lock_path: Path = Path("drift.lock.json")


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

	lock_obj: dict[str, object] | None = None
	lock_pkgs: dict[str, object] | None = None
	if opts.lock_path.exists():
		lock_obj = load_lock(opts.lock_path)
		raw_pkgs = lock_obj.get("packages")
		if not isinstance(raw_pkgs, dict):
			raise ValueError("lockfile packages must be an object")
		lock_pkgs = raw_pkgs

	sorted_sources = sorted(sources.sources, key=lambda s: (s.priority, s.source_id))
	sources_by_id: dict[str, Path] = {}
	for s in sorted_sources:
		if s.source_id in sources_by_id:
			raise ValueError(f"duplicate source id '{s.source_id}' in sources file")
		sources_by_id[s.source_id] = s.path

	# Collect candidates from all sources. In lock mode, selection is pinned by
	# the lock when `source_id` is present; otherwise selection is deterministic
	# by (priority, source_id).
	#
	# The key is only `package_id` (pinned rule: single version per package id per
	# build). If multiple sources provide the same package id, we deterministically
	# select one; we do not merge.
	@dataclass(frozen=True)
	class _Candidate:
		priority: int
		source_id: str
		repo: Path
		entry: IndexEntry

	candidates: dict[str, list[_Candidate]] = {}

	for src in sorted_sources:
		repo = src.path
		index_path = repo / "index.json"
		index_obj = load_index(index_path)
		pkgs = index_obj.get("packages") or {}
		if not isinstance(pkgs, dict):
			raise ValueError("source index packages must be an object")

		for package_id, raw in pkgs.items():
			# If a lock exists and the package is not in the lock, do not fetch it.
			if lock_pkgs is not None and package_id not in lock_pkgs:
				continue
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
				source_id=src.source_id,
				path=str(raw.get("filename", "")),
			)
			if not entry.package_version or not entry.target or not entry.filename or not entry.sha256:
				raise ValueError(f"invalid index entry for {package_id} in {index_path}")

			candidates.setdefault(package_id, []).append(
				_Candidate(priority=src.priority, source_id=src.source_id, repo=repo, entry=entry)
			)

	selected: dict[str, _Candidate] = {}
	for package_id, cand_list in candidates.items():
		if lock_pkgs is None:
			selected[package_id] = min(cand_list, key=lambda c: (c.priority, c.source_id))
			continue
		want_raw = lock_pkgs.get(package_id)
		if not isinstance(want_raw, dict):
			raise ValueError(f"lock entry for package_id '{package_id}' must be an object")
		want_src = want_raw.get("source_id")
		want_src_str = want_src if isinstance(want_src, str) else ""
		if not want_src_str:
			raise ValueError(
				f"lock entry for package_id '{package_id}' is missing source_id; regenerate the lockfile with 'drift vendor'"
			)
		if want_src_str == "unknown":
			# Legacy/placeholder lock entries must not silently re-enable heuristic
			# selection. If the lock doesn't pin a source and multiple candidates
			# exist, fail loudly with a clear remediation path.
			if len(cand_list) != 1:
				raise ValueError(
					f"lock entry for package_id '{package_id}' uses source_id 'unknown' but multiple sources provide it; regenerate the lockfile with 'drift vendor'"
				)
			selected[package_id] = cand_list[0]
			continue
		eligible = cand_list
		if want_src_str != "unknown":
			eligible = [c for c in cand_list if c.source_id == want_src_str]
			if not eligible:
				raise ValueError(f"lock pins package_id '{package_id}' to unknown source id '{want_src_str}'")
		selected[package_id] = min(eligible, key=lambda c: (c.priority, c.source_id))

	for package_id in sorted(selected.keys()):
		cand = selected[package_id]
		entry = cand.entry
		repo = cand.repo
		index_path = repo / "index.json"

		want: dict[str, object] | None = None
		if lock_pkgs is not None:
			want_raw = lock_pkgs.get(package_id)
			if not isinstance(want_raw, dict):
				raise ValueError(f"lock entry for package_id '{package_id}' must be an object")
			want = want_raw
			want_ver = want.get("version")
			want_target = want.get("target")
			want_sha = want.get("pkg_sha256")
			want_path = want.get("path")
			if want_ver != entry.package_version or want_target != entry.target:
				raise ValueError(f"lock mismatch for package_id '{package_id}' in source {index_path}")
			if want_sha != entry.sha256:
				raise ValueError(f"sha256 mismatch for package_id '{package_id}' vs lock in source {index_path}")
			if isinstance(want_path, str) and want_path and want_path != entry.filename:
				raise ValueError(f"lock path mismatch for package_id '{package_id}' in source {index_path}")
			want_sig_sha = want.get("sig_sha256")
			if want_sig_sha is not None and not isinstance(want_sig_sha, str):
				raise ValueError(f"lock sig_sha256 for package_id '{package_id}' must be a string or null")

		src_pkg = repo / entry.filename
		if not src_pkg.exists():
			raise ValueError(f"missing package file referenced by index: {src_pkg}")

		try:
			ident = read_identity_v0(src_pkg)
		except PackageFormatError as err:
			raise ValueError(f"invalid package referenced by index: {src_pkg} ({err})") from err
		if ident.package_id != entry.package_id or ident.package_version != entry.package_version or ident.target != entry.target:
			raise ValueError(
				"package identity mismatch for "
				f"{src_pkg}: index expects ({entry.package_id}, {entry.package_version}, {entry.target}) "
				f"but package declares ({ident.package_id}, {ident.package_version}, {ident.target})"
			)

		# Copy into cache with deterministic, identity-derived name.
		dst_name = entry.filename
		dst_pkg = pkgs_dir / dst_name
		shutil.copyfile(src_pkg, dst_pkg)

		# If a sidecar exists in the repository, mirror it.
		src_sig = repo / (entry.filename + ".sig")
		if src_sig.exists():
			shutil.copyfile(src_sig, pkgs_dir / (dst_name + ".sig"))
			if want is not None:
				want_sig_sha = want.get("sig_sha256")
				if isinstance(want_sig_sha, str) and want_sig_sha:
					hex_sig = sha256_hex((pkgs_dir / (dst_name + ".sig")).read_bytes())
					if want_sig_sha != f"sha256:{hex_sig}":
						raise ValueError(f"sig sha256 mismatch for fetched package {dst_pkg}")
		elif want is not None:
			want_sig_sha = want.get("sig_sha256")
			if isinstance(want_sig_sha, str) and want_sig_sha:
				raise ValueError(f"missing signature sidecar for locked package {dst_pkg}")

		# Guardrail: ensure the copied bytes match the index sha256.
		hex_digest = sha256_hex(dst_pkg.read_bytes())
		got_sha = f"sha256:{hex_digest}"
		if entry.sha256 != got_sha:
			raise ValueError(
				f"sha256 mismatch for fetched package {dst_pkg}: expected {entry.sha256} from {index_path}, got {got_sha}"
			)

		upsert_entry(
			merged,
			entry=IndexEntry(
				**{
					**entry.__dict__,
					"filename": str((pkgs_dir / dst_name).name),
				}
			),
			force=opts.force,
		)

	save_index(cache_index_path, merged)

	if lock_pkgs is not None:
		missing = sorted(set(lock_pkgs.keys()) - set((merged.get("packages") or {}).keys()))
		if missing:
			raise ValueError(f"lock requested packages not found in sources: {', '.join(missing)}")
