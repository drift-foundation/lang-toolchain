# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from lang.drift.dmir_pkg_v0 import read_identity_v0, sha256_hex
from lang.drift.index_v0 import IndexEntry, load_index
from lang.drift.lock_v0 import LockEntry, ObservedIdentity, save_lock


@dataclass(frozen=True)
class VendorOptions:
	cache_dir: Path = Path("cache") / "driftpm"
	dest_dir: Path = Path("vendor") / "driftpkgs"
	lock_path: Path = Path("drift.lock.json")
	package_ids: list[str] | None = None
	json: bool = False


def _normalize_lock_rel_path(path_str: str, *, what: str) -> str:
	p = PurePosixPath(path_str.replace("\\", "/"))
	if p.is_absolute():
		raise ValueError(f"{what} must be a relative path, got: {path_str}")
	if not p.parts or str(p) == ".":
		raise ValueError(f"{what} must be non-empty, got: {path_str}")
	if any(part in (".", "..") for part in p.parts):
		raise ValueError(f"{what} must not contain '.' or '..', got: {path_str}")
	return str(p)


def _emit_vendor_report(*, report: dict[str, Any], as_json: bool) -> None:
	if as_json:
		print(json.dumps(report, sort_keys=True, separators=(",", ":")))
		return
	# Human output: keep it compact and actionable.
	ok = bool(report.get("ok"))
	if ok:
		return
	errors = report.get("errors")
	if isinstance(errors, list):
		print(f"vendor: {len(errors)} package(s) failed; not writing lockfile", flush=True)
		for e in errors:
			if not isinstance(e, dict):
				continue
			pid = e.get("package_id", "?")
			ver = e.get("version", "?")
			target = e.get("target", "?")
			code = e.get("reason_code", "?")
			msg = e.get("message", "")
			suffix = f": {msg}" if isinstance(msg, str) and msg else ""
			print(f"- ({pid}, {ver}, {target}) {code}{suffix}", flush=True)
		print("rerun with --json for a machine-readable report", flush=True)


def vendor_v0(opts: VendorOptions) -> int:
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
	errors: list[dict[str, Any]] = []
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
			signed=(bool(raw.get("signed")) if isinstance(raw.get("signed"), bool) else (not bool(raw.get("unsigned", False)))),
			source_id=str(raw["source_id"]) if isinstance(raw.get("source_id"), str) and raw.get("source_id") else None,
			path=str(raw["path"]) if isinstance(raw.get("path"), str) and raw.get("path") else None,
		)
		version = entry.package_version or None
		target = entry.target or None
		try:
			if not entry.source_id:
				raise ValueError("cache index missing source_id")
			lock_rel = _normalize_lock_rel_path(
				entry.path or entry.filename,
				what=f"cache entry path for package_id '{entry.package_id}'",
			)
			src_pkg = opts.cache_dir / "pkgs" / lock_rel
			if not src_pkg.exists():
				raise ValueError(f"missing cached package file: {src_pkg}")

			src_sig = src_pkg.with_suffix(src_pkg.suffix + ".sig")

			# Record exact bytes in the lockfile so future fetches can reproduce the
			# same artifacts (or fail loudly).
			pkg_hex = sha256_hex(src_pkg.read_bytes())
			pkg_sha = f"sha256:{pkg_hex}"
			if entry.sha256 and entry.sha256 != pkg_sha:
				raise ValueError(
					f"cached package sha256 mismatch for {entry.package_id}: index {entry.sha256} != bytes {pkg_sha}"
				)
			sig_sha: str | None = None
			if src_sig.exists():
				sig_bytes = src_sig.read_bytes()
				sig_sha = f"sha256:{sha256_hex(sig_bytes)}"
			ident = read_identity_v0(src_pkg)
			mod_ids: list[str] = []
			raw_mods = ident.manifest.get("modules")
			if isinstance(raw_mods, list):
				for m in raw_mods:
					if isinstance(m, dict) and isinstance(m.get("module_id"), str):
						mod_ids.append(m["module_id"])

			# Only after all validations pass, copy into the vendor directory.
			dst_pkg = opts.dest_dir / lock_rel
			dst_pkg.parent.mkdir(parents=True, exist_ok=True)
			shutil.copyfile(src_pkg, dst_pkg)
			if src_sig.exists():
				dst_sig = dst_pkg.with_suffix(dst_pkg.suffix + ".sig")
				dst_sig.parent.mkdir(parents=True, exist_ok=True)
				shutil.copyfile(src_sig, dst_sig)

			out_entries.append(
				LockEntry(
					package_id=entry.package_id,
					package_version=entry.package_version,
					target=entry.target,
					observed_identity=ObservedIdentity(
						package_id=ident.package_id,
						package_version=ident.package_version,
						target=ident.target,
					),
					pkg_sha256=pkg_sha,
					sig_sha256=sig_sha,
					sig_kids=sorted(set(entry.signers)),
					modules=sorted(set(mod_ids)),
					source_id=entry.source_id,
					path=lock_rel,
				)
			)
		except Exception as err:
			msg = str(err)
			code = "VENDOR_ERROR"
			if msg == "cache index missing source_id":
				code = "MISSING_SOURCE_ID"
				msg = "cache/index entry is missing source_id; re-run 'drift fetch' then vendor again"
			errors.append(
				{
					"package_id": entry.package_id,
					"version": version,
					"target": target,
					"reason_code": code,
					"message": msg,
				}
			)
			continue

	if selected:
		missing = sorted(selected - set(pkgs_map.keys()))
		if missing:
			for package_id in missing:
				errors.append(
					{
						"package_id": package_id,
						"version": None,
						"target": None,
						"reason_code": "REQUESTED_NOT_IN_CACHE",
						"message": "requested package_id not found in cache index",
					}
				)

	lock_path_str = str(opts.lock_path)
	report: dict[str, Any] = {
		"ok": len(errors) == 0,
		"vendored_count": len(out_entries),
		"error_count": len(errors),
		"errors": sorted(errors, key=lambda e: (str(e.get("package_id") or ""), str(e.get("reason_code") or ""))),
		"lock_written": False,
		"lock_path": lock_path_str,
	}

	if errors:
		_emit_vendor_report(report=report, as_json=opts.json)
		return 2

	save_lock(opts.lock_path, out_entries)
	report["lock_written"] = True
	_emit_vendor_report(report=report, as_json=opts.json)
	return 0
