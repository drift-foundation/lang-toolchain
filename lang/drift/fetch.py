# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from lang.drift.dmir_pkg_v0 import PackageFormatError, read_identity_v0
from lang.drift.dmir_pkg_v0 import sha256_hex
from lang.drift.errors import DriftError, DriftIdentity
from lang.drift.index_v0 import IndexEntry, load_index, save_index, upsert_entry
from lang.drift.lock_v0 import LockEntry, load_lock_entries_v0
from lang.drift.sources_v0 import load_sources_v0


@dataclass(frozen=True)
class FetchOptions:
	sources_path: Path
	cache_dir: Path = Path("cache") / "driftpm"
	force: bool = False
	# Fetch lockfile lives under the `drift/` namespace alongside the
	# sources file it pairs with.  Distinct from the build lockfile at
	# `drift/lock.json` (different schema, different consumer): this is
	# the package fetch verification lockfile, paired with `drift/sources.json`.
	lock_path: Path = Path("drift") / "sources.lock.json"


@dataclass(frozen=True)
class FetchSelected:
	package_id: str
	identity: DriftIdentity
	source_id: str | None
	index_path: str | None
	artifact_path: str | None
	cache_path: str | None
	sha256: str | None
	signer_kids: list[str] | None
	unsigned: bool | None

	def to_dict(self) -> dict[str, Any]:
		return {
			"package_id": self.package_id,
			"identity": self.identity.to_dict(),
			"source_id": self.source_id,
			"index_path": self.index_path,
			"artifact_path": self.artifact_path,
			"cache_path": self.cache_path,
			"sha256": self.sha256,
			"signer_kids": list(self.signer_kids) if self.signer_kids is not None else None,
			"unsigned": self.unsigned,
		}


@dataclass(frozen=True)
class FetchReport:
	ok: bool
	mode: str | None  # "lock" | "unlocked"
	selected: list[FetchSelected]
	errors: list[DriftError]
	cache_index_written: bool

	def to_dict(self) -> dict[str, Any]:
		selected_sorted = sorted(self.selected, key=lambda s: (s.package_id,))
		errors_sorted = sorted(
			self.errors,
			key=lambda e: (
				e.reason_code,
				(e.identity.package_id if e.identity is not None and e.identity.package_id is not None else ""),
				(e.source_id or ""),
				(e.artifact_path or ""),
			),
		)
		return {
			"ok": self.ok,
			"mode": self.mode,
			"selected": [s.to_dict() for s in selected_sorted],
			"errors": [e.to_dict() for e in errors_sorted],
			"cache_index_written": self.cache_index_written,
		}


def _normalize_error_identity(err: DriftError) -> DriftError:
	"""
	Ensure `identity` is set when the package_id is otherwise known.

	For example, some errors may set claimed/observed identity but omit `identity`.
	"""
	if err.identity is not None and err.identity.package_id:
		return err
	fallback = err.claimed_identity or err.observed_identity
	if fallback is None or not fallback.package_id:
		return err
	return replace(err, identity=fallback)


def _normalize_repo_rel_path(path_str: str, *, what: str) -> str:
	p = PurePosixPath(path_str.replace("\\", "/"))
	if p.is_absolute():
		raise ValueError(f"{what} must be a relative path, got: {path_str}")
	if not p.parts or str(p) == ".":
		raise ValueError(f"{what} must be non-empty, got: {path_str}")
	if any(part in (".", "..") for part in p.parts):
		raise ValueError(f"{what} must not contain '.' or '..', got: {path_str}")
	return str(p)


def fetch_v0(opts: FetchOptions) -> FetchReport:
	"""
	Fetch packages from local directory repositories into a project-local cache.

MVP constraints:
- sources are local directories only (no network)
- we fetch "everything" listed in each index
- conflicts on package_id are hard errors unless --force
	"""
	selected_items: list[FetchSelected] = []
	cache_index_written = False
	mode: str | None = None

	try:
		# Decide mode early for consistent reporting even if lock parsing fails.
		mode = "lock" if opts.lock_path.exists() else "unlocked"

		sources = load_sources_v0(opts.sources_path)

		cache_dir = opts.cache_dir
		pkgs_dir = cache_dir / "pkgs"
		pkgs_dir.mkdir(parents=True, exist_ok=True)
		cache_index_path = cache_dir / "index.json"

		merged = load_index(cache_index_path)

		lock_entries: dict[str, LockEntry] | None = None
		if mode == "lock":
			try:
				lock_entries = load_lock_entries_v0(opts.lock_path)
			except Exception as err:
				raise DriftError(
					reason_code="LOCK_INVALID",
					message=str(err),
					mode="lock",
					artifact_path=str(opts.lock_path),
				) from err

		sorted_sources = sorted(sources.sources, key=lambda s: (s.priority, s.source_id))
		sources_by_id: dict[str, Path] = {}
		for s in sorted_sources:
			if s.source_id in sources_by_id:
				raise ValueError(f"duplicate source id '{s.source_id}' in sources file")
			sources_by_id[s.source_id] = s.path

		if lock_entries is not None:
			for package_id in sorted(lock_entries.keys()):
				locked = lock_entries[package_id]
				identity = DriftIdentity(package_id=locked.package_id, version=locked.package_version, target=locked.target)
				repo = sources_by_id.get(locked.source_id)
				if repo is None:
					raise DriftError(
						reason_code="LOCK_SOURCE_UNKNOWN",
						message="lock pins package to unknown source_id",
						mode="lock",
						identity=identity,
						source_id=locked.source_id,
						signer_kids=list(locked.sig_kids),
					)

				try:
					rel = _normalize_repo_rel_path(locked.path, what=f"lock path for package_id '{package_id}'")
				except ValueError as err:
					raise DriftError(
						reason_code="LOCK_PATH_INVALID",
						message=str(err),
						mode="lock",
						identity=identity,
						source_id=locked.source_id,
						signer_kids=list(locked.sig_kids),
					) from err

				src_pkg = repo / rel
				if not src_pkg.exists():
					raise DriftError(
						reason_code="LOCK_ARTIFACT_MISSING",
						message="missing locked package file",
						mode="lock",
						identity=identity,
						source_id=locked.source_id,
						artifact_path=str(src_pkg),
						signer_kids=list(locked.sig_kids),
					)

				pkg_bytes = src_pkg.read_bytes()
				got_sha = f"sha256:{sha256_hex(pkg_bytes)}"
				if got_sha != locked.pkg_sha256:
					raise DriftError(
						reason_code="LOCK_SHA_MISMATCH",
						message="sha256 mismatch for locked package",
						mode="lock",
						identity=identity,
						source_id=locked.source_id,
						artifact_path=str(src_pkg),
						sha256_expected=locked.pkg_sha256,
						sha256_got=got_sha,
						signer_kids=list(locked.sig_kids),
					)

				try:
					ident = read_identity_v0(src_pkg)
				except PackageFormatError as err:
					raise DriftError(
						reason_code="LOCK_PKG_FORMAT_INVALID",
						message=f"invalid locked package: {err}",
						mode="lock",
						identity=identity,
						source_id=locked.source_id,
						artifact_path=str(src_pkg),
						signer_kids=list(locked.sig_kids),
					) from err
				obs = locked.observed_identity
				if ident.package_id != obs.package_id or ident.package_version != obs.package_version or ident.target != obs.target:
					raise DriftError(
						reason_code="LOCK_IDENTITY_MISMATCH",
						message="package identity mismatch for locked package",
						mode="lock",
						identity=identity,
						claimed_identity=DriftIdentity(package_id=obs.package_id, version=obs.package_version, target=obs.target),
						observed_identity=DriftIdentity(
							package_id=ident.package_id,
							version=ident.package_version,
							target=ident.target,
						),
						source_id=locked.source_id,
						artifact_path=str(src_pkg),
						signer_kids=list(locked.sig_kids),
					)

				dst_pkg = pkgs_dir / rel
				dst_pkg.parent.mkdir(parents=True, exist_ok=True)
				shutil.copyfile(src_pkg, dst_pkg)

				if locked.sig_sha256 is not None:
					src_sig = repo / (rel + ".sig")
					if not src_sig.exists():
						raise DriftError(
							reason_code="LOCK_SIGNATURE_MISSING",
							message="missing signature sidecar for locked package",
							mode="lock",
							identity=identity,
							source_id=locked.source_id,
							artifact_path=str(src_pkg),
							signer_kids=list(locked.sig_kids),
						)
					dst_sig = pkgs_dir / (rel + ".sig")
					dst_sig.parent.mkdir(parents=True, exist_ok=True)
					shutil.copyfile(src_sig, dst_sig)
					got_sig_sha = f"sha256:{sha256_hex(dst_sig.read_bytes())}"
					if got_sig_sha != locked.sig_sha256:
						raise DriftError(
							reason_code="LOCK_SIG_SHA_MISMATCH",
							message="signature sha256 mismatch for locked package",
							mode="lock",
							identity=identity,
							source_id=locked.source_id,
							artifact_path=str(src_pkg),
							sha256_expected=locked.sig_sha256,
							sha256_got=got_sig_sha,
							signer_kids=list(locked.sig_kids),
						)

				upsert_entry(
					merged,
					entry=IndexEntry(
						package_id=locked.package_id,
						package_version=locked.package_version,
						target=locked.target,
						sha256=locked.pkg_sha256,
						filename=rel,
						signers=list(locked.sig_kids),
						signed=(locked.sig_sha256 is not None),
						source_id=locked.source_id,
						path=rel,
					),
					force=opts.force,
				)

				selected_items.append(
					FetchSelected(
						package_id=locked.package_id,
						identity=identity,
						source_id=locked.source_id,
						index_path=str(repo / "index.json"),
						artifact_path=str(src_pkg),
						cache_path=str(dst_pkg),
						sha256=locked.pkg_sha256,
						signer_kids=list(locked.sig_kids),
						unsigned=(locked.sig_sha256 is None),
					)
				)

			save_index(cache_index_path, merged)
			cache_index_written = True
			return FetchReport(ok=True, mode="lock", selected=selected_items, errors=[], cache_index_written=cache_index_written)

		# Unlocked mode: gather all candidates.
		@dataclass(frozen=True)
		class _Candidate:
			priority: int
			source_id: str
			normalized_path: str
			repo: Path
			index_path: Path
			entry: IndexEntry

		candidates: dict[str, list[_Candidate]] = {}

		for src in sorted_sources:
			repo = src.path
			index_path = repo / "index.json"
			index_obj = load_index(index_path)
			pkgs = index_obj.get("packages") or {}
			if not isinstance(pkgs, dict):
				raise DriftError(
					reason_code="INDEX_INVALID_ENTRY",
					message="source index packages must be an object",
					mode="unlocked",
					source_id=src.source_id,
					index_path=str(index_path),
				)

			for package_id, raw in pkgs.items():
				if not isinstance(raw, dict):
					raise DriftError(
						reason_code="INDEX_INVALID_ENTRY",
						message="source index entry must be an object",
						mode="unlocked",
						identity=DriftIdentity(package_id=package_id),
						source_id=src.source_id,
						index_path=str(index_path),
					)
				entry = IndexEntry(
					package_id=package_id,
					package_version=str(raw.get("package_version", "")),
					target=str(raw.get("target", "")),
					sha256=str(raw.get("sha256", "")),
					filename=str(raw.get("filename", "")),
					signers=list(raw.get("signers") or []),
					signed=(bool(raw.get("signed")) if isinstance(raw.get("signed"), bool) else (not bool(raw.get("unsigned", False)))),
					source_id=src.source_id,
					path=str(raw.get("path") or raw.get("filename", "")),
				)
				if not entry.package_version or not entry.target or not entry.filename or not entry.sha256:
					raise DriftError(
						reason_code="INDEX_INVALID_ENTRY",
						message=f"invalid index entry for {package_id} in {index_path}",
						mode="unlocked",
						identity=DriftIdentity(package_id=package_id, version=entry.package_version or None, target=entry.target or None),
						source_id=src.source_id,
						index_path=str(index_path),
					)

				try:
					normalized_path = _normalize_repo_rel_path(entry.path, what=f"index path for package_id '{package_id}' in {index_path}")
				except ValueError as err:
					raise DriftError(
						reason_code="INDEX_INVALID_ENTRY",
						message=str(err),
						mode="unlocked",
						identity=DriftIdentity(package_id=package_id, version=entry.package_version or None, target=entry.target or None),
						source_id=src.source_id,
						index_path=str(index_path),
					) from err

				candidates.setdefault(package_id, []).append(
					_Candidate(
						priority=src.priority,
						source_id=src.source_id,
						normalized_path=normalized_path,
						repo=repo,
						index_path=index_path,
						entry=entry,
					)
				)

		selected_map: dict[str, _Candidate] = {}
		for package_id, cand_list in candidates.items():
			by_identity: dict[tuple[str, str], list[_Candidate]] = {}
			for c in cand_list:
				by_identity.setdefault((c.entry.package_version, c.entry.target), []).append(c)
			for (ver, target), group in by_identity.items():
				if len(group) <= 1:
					continue
				details = ", ".join(
					f"{c.source_id}:{c.normalized_path}" for c in sorted(group, key=lambda c: (c.priority, c.source_id, c.normalized_path))
				)
				raise DriftError(
					reason_code="AMBIGUOUS_IDENTITY",
					message=f"ambiguous package identity: ({package_id}, {ver}, {target}) found in multiple sources: {details}; lock it with 'drift vendor'",
					mode="unlocked",
					identity=DriftIdentity(package_id=package_id, version=ver, target=target),
				)

			selected_map[package_id] = min(cand_list, key=lambda c: (c.priority, c.source_id, c.normalized_path))

		for package_id in sorted(selected_map.keys()):
			cand = selected_map[package_id]
			entry = cand.entry
			repo = cand.repo
			index_path = cand.index_path
			src_pkg = repo / cand.normalized_path
			if not src_pkg.exists():
				raise DriftError(
					reason_code="INDEX_MISSING_PACKAGE_FILE",
					message="missing package file referenced by index",
					mode="unlocked",
					identity=DriftIdentity(package_id=entry.package_id, version=entry.package_version, target=entry.target),
					source_id=cand.source_id,
					index_path=str(index_path),
					artifact_path=str(src_pkg),
				)

			try:
				ident = read_identity_v0(src_pkg)
			except PackageFormatError as err:
				raise DriftError(
					reason_code="INDEX_PKG_FORMAT_INVALID",
					message=f"invalid package referenced by index: {err}",
					mode="unlocked",
					identity=DriftIdentity(package_id=entry.package_id, version=entry.package_version, target=entry.target),
					source_id=cand.source_id,
					index_path=str(index_path),
					artifact_path=str(src_pkg),
				) from err
			if ident.package_id != entry.package_id or ident.package_version != entry.package_version or ident.target != entry.target:
				raise DriftError(
					reason_code="INDEX_IDENTITY_MISMATCH",
					message="package identity mismatch for package referenced by index",
					mode="unlocked",
					identity=DriftIdentity(package_id=entry.package_id, version=entry.package_version, target=entry.target),
					claimed_identity=DriftIdentity(package_id=entry.package_id, version=entry.package_version, target=entry.target),
					observed_identity=DriftIdentity(package_id=ident.package_id, version=ident.package_version, target=ident.target),
					source_id=cand.source_id,
					index_path=str(index_path),
					artifact_path=str(src_pkg),
				)

			dst_name = cand.normalized_path
			dst_pkg = pkgs_dir / dst_name
			dst_pkg.parent.mkdir(parents=True, exist_ok=True)
			shutil.copyfile(src_pkg, dst_pkg)

			src_sig = repo / (cand.normalized_path + ".sig")
			if src_sig.exists():
				dst_sig = pkgs_dir / (dst_name + ".sig")
				dst_sig.parent.mkdir(parents=True, exist_ok=True)
				shutil.copyfile(src_sig, dst_sig)

			hex_digest = sha256_hex(dst_pkg.read_bytes())
			got_sha = f"sha256:{hex_digest}"
			if entry.sha256 != got_sha:
				raise DriftError(
					reason_code="INDEX_SHA_MISMATCH",
					message="sha256 mismatch between index and package bytes",
					mode="unlocked",
					identity=DriftIdentity(package_id=entry.package_id, version=entry.package_version, target=entry.target),
					source_id=cand.source_id,
					index_path=str(index_path),
					artifact_path=str(src_pkg),
					sha256_expected=entry.sha256,
					sha256_got=got_sha,
				)

			upsert_entry(
				merged,
				entry=IndexEntry(
					**{
						**entry.__dict__,
						"filename": dst_name,
						"path": dst_name,
					}
				),
				force=opts.force,
			)

			selected_items.append(
				FetchSelected(
					package_id=entry.package_id,
					identity=DriftIdentity(package_id=entry.package_id, version=entry.package_version, target=entry.target),
					source_id=cand.source_id,
					index_path=str(index_path),
					artifact_path=str(src_pkg),
					cache_path=str(dst_pkg),
					sha256=entry.sha256,
					signer_kids=list(entry.signers),
					unsigned=entry.unsigned,
				)
			)

		save_index(cache_index_path, merged)
		cache_index_written = True
		return FetchReport(ok=True, mode="unlocked", selected=selected_items, errors=[], cache_index_written=cache_index_written)
	except DriftError as err:
		return FetchReport(
			ok=False,
			mode=mode,
			selected=selected_items,
			errors=[_normalize_error_identity(err)],
			cache_index_written=cache_index_written,
		)
	except Exception as err:
		wrapped = DriftError(reason_code="INTERNAL_ERROR", message=str(err), mode=mode)
		return FetchReport(ok=False, mode=mode, selected=selected_items, errors=[wrapped], cache_index_written=cache_index_written)
