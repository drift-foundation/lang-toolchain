# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from lang.drift.crypto import b64_decode, compute_ed25519_kid
from lang.drift.dmir_pkg_v0 import PackageFormatError, read_identity_v0, sha256_hex
from lang.drift.errors import DriftError, DriftIdentity
from lang.drift.index_v0 import load_index
from lang.drift.lock_v0 import LockEntry, load_lock_entries_v0
from lang.drift.sources_v0 import SourcesV0, load_sources_v0

DoctorStatus = Literal["ok", "info", "degraded", "fatal"]


@dataclass(frozen=True)
class DoctorOptions:
	# Fetch sources file + its verification lockfile both live under the
	# `drift/` namespace.  Trust store unchanged.  Distinct from the build
	# lockfile at `drift/lock.json` (different schema, different consumer).
	sources_path: Path = Path("drift") / "sources.json"
	trust_store_path: Path = Path("drift") / "trust.json"
	lock_path: Path = Path("drift") / "sources.lock.json"
	cache_dir: Path = Path("cache") / "driftpm"
	vendor_dir: Path = Path("vendor") / "driftpkgs"
	deep: bool = False
	fail_on: Literal["fatal", "degraded"] = "fatal"


def _normalize_rel_path(path_str: str, *, what: str) -> str:
	p = PurePosixPath(path_str.replace("\\", "/"))
	if p.is_absolute():
		raise ValueError(f"{what} must be a relative path, got: {path_str}")
	if not p.parts or str(p) == ".":
		raise ValueError(f"{what} must be non-empty, got: {path_str}")
	if any(part in (".", "..") for part in p.parts):
		raise ValueError(f"{what} must not contain '.' or '..', got: {path_str}")
	return str(p)


def _sort_findings(findings: list[DriftError]) -> list[DriftError]:
	return sorted(
		findings,
		key=lambda e: (
			e.reason_code,
			(e.identity.package_id if e.identity is not None and e.identity.package_id is not None else ""),
			(e.source_id or ""),
			(e.artifact_path or ""),
		),
	)


@dataclass(frozen=True)
class DoctorCheckResult:
	check_id: str
	status: DoctorStatus
	summary: str
	findings: list[DriftError]
	data: dict[str, Any] | None = None

	def to_dict(self) -> dict[str, Any]:
		return {
			"check_id": self.check_id,
			"status": self.status,
			"summary": self.summary,
			"findings": [f.to_dict() for f in _sort_findings(self.findings)],
			"data": dict(self.data) if self.data is not None else None,
		}


@dataclass(frozen=True)
class DoctorReport:
	ok: bool
	checks: list[DoctorCheckResult]
	fatal_count: int
	degraded_count: int
	info_count: int

	def to_dict(self) -> dict[str, Any]:
		checks_sorted = sorted(self.checks, key=lambda c: c.check_id)
		return {
			"ok": self.ok,
			"fatal_count": self.fatal_count,
			"degraded_count": self.degraded_count,
			"info_count": self.info_count,
			"checks": [c.to_dict() for c in checks_sorted],
		}


def doctor_exit_code(report: DoctorReport, *, fail_on: Literal["fatal", "degraded"]) -> int:
	"""
	Exit code contract:
	- 0: no fatal findings, and (if fail_on=="degraded") no degraded findings
	- 1: degraded findings and no fatal findings (only when fail_on=="degraded")
	- 2: any fatal findings
	"""
	if report.fatal_count > 0:
		return 2
	if fail_on == "degraded" and report.degraded_count > 0:
		return 1
	return 0


def doctor_v0(opts: DoctorOptions) -> DoctorReport:
	checks: list[DoctorCheckResult] = []

	sources_res, sources = _check_sources(opts.sources_path)
	checks.append(sources_res)

	index_res = _check_source_indexes(sources, deep=opts.deep)
	checks.append(index_res)

	trust_res = _check_trust_store(opts.trust_store_path)
	checks.append(trust_res)

	lock_res, lock_entries = _check_lock(opts.lock_path, sources=sources, deep=opts.deep)
	checks.append(lock_res)

	lock_entries_for_checks = lock_entries if lock_res.status != "fatal" else None

	vendor_res = _check_vendor_consistency(lock_entries_for_checks, vendor_dir=opts.vendor_dir, deep=opts.deep)
	checks.append(vendor_res)

	cache_res = _check_cache_consistency(lock_entries_for_checks, cache_dir=opts.cache_dir, deep=opts.deep)
	checks.append(cache_res)

	fatal_count = sum(1 for c in checks if c.status == "fatal")
	degraded_count = sum(1 for c in checks if c.status == "degraded")
	info_count = sum(1 for c in checks if c.status == "info")
	ok = fatal_count == 0 and degraded_count == 0
	return DoctorReport(ok=ok, checks=checks, fatal_count=fatal_count, degraded_count=degraded_count, info_count=info_count)


def _check_sources(path: Path) -> tuple[DoctorCheckResult, SourcesV0 | None]:
	if not path.exists():
		return (
			DoctorCheckResult(
				check_id="sources",
				status="degraded",
				summary=f"sources file missing: {path}",
				findings=[
					DriftError(reason_code="SOURCES_MISSING", message="sources file missing", mode="doctor", artifact_path=str(path))
				],
			),
			None,
		)

	try:
		sources = load_sources_v0(path)
	except json.JSONDecodeError as err:
		return (
			DoctorCheckResult(
				check_id="sources",
				status="fatal",
				summary="sources file parse error",
				findings=[
					DriftError(
						reason_code="SOURCES_PARSE_ERROR",
						message=str(err),
						mode="doctor",
						artifact_path=str(path),
					)
				],
			),
			None,
		)
	except Exception as err:
		return (
			DoctorCheckResult(
				check_id="sources",
				status="fatal",
				summary="sources file schema invalid",
				findings=[
					DriftError(
						reason_code="SOURCES_SCHEMA_INVALID",
						message=str(err),
						mode="doctor",
						artifact_path=str(path),
					)
				],
			),
			None,
		)

	findings: list[DriftError] = []
	seen: set[str] = set()
	for s in sources.sources:
		if s.source_id in seen:
			findings.append(
				DriftError(
					reason_code="SOURCES_DUPLICATE_ID",
					message=f"duplicate source id '{s.source_id}'",
					mode="doctor",
					source_id=s.source_id,
					artifact_path=str(path),
				)
			)
		seen.add(s.source_id)
		if not s.path.exists():
			findings.append(
				DriftError(
					reason_code="SOURCES_PATH_MISSING",
					message="source path does not exist",
					mode="doctor",
					source_id=s.source_id,
					artifact_path=str(s.path),
				)
			)
		elif not s.path.is_dir():
			findings.append(
				DriftError(
					reason_code="SOURCES_PATH_NOT_DIR",
					message="source path is not a directory",
					mode="doctor",
					source_id=s.source_id,
					artifact_path=str(s.path),
				)
			)

	if findings:
		return (
			DoctorCheckResult(
				check_id="sources",
				status="fatal",
				summary="sources file has fatal issues",
				findings=findings,
			),
			sources,
		)

	return (
		DoctorCheckResult(check_id="sources", status="ok", summary="sources ok", findings=[], data={"source_count": len(sources.sources)}),
		sources,
	)


def _check_source_indexes(sources: SourcesV0 | None, *, deep: bool) -> DoctorCheckResult:
	if sources is None:
		return DoctorCheckResult(check_id="indexes", status="info", summary="indexes skipped (no sources)", findings=[])

	findings: list[DriftError] = []
	pkg_count = 0
	for src in sorted(sources.sources, key=lambda s: (s.priority, s.source_id)):
		if not src.path.exists() or not src.path.is_dir():
			continue
		index_path = src.path / "index.json"
		if not index_path.exists():
			findings.append(
				DriftError(
					reason_code="INDEX_MISSING",
					message="missing index.json for source",
					mode="doctor",
					source_id=src.source_id,
					index_path=str(index_path),
				)
			)
			continue
		try:
			index_obj = load_index(index_path)
		except json.JSONDecodeError as err:
			findings.append(
				DriftError(
					reason_code="INDEX_PARSE_ERROR",
					message=str(err),
					mode="doctor",
					source_id=src.source_id,
					index_path=str(index_path),
				)
			)
			continue
		except Exception as err:
			findings.append(
				DriftError(
					reason_code="INDEX_SCHEMA_INVALID",
					message=str(err),
					mode="doctor",
					source_id=src.source_id,
					index_path=str(index_path),
				)
			)
			continue

		pkgs = index_obj.get("packages") or {}
		if not isinstance(pkgs, dict):
			findings.append(
				DriftError(
					reason_code="INDEX_SCHEMA_INVALID",
					message="index packages must be an object",
					mode="doctor",
					source_id=src.source_id,
					index_path=str(index_path),
				)
			)
			continue

		for package_id, raw in pkgs.items():
			pkg_count += 1
			if not isinstance(raw, dict):
				findings.append(
					DriftError(
						reason_code="INDEX_INVALID_ENTRY",
						message="index entry must be an object",
						mode="doctor",
						identity=DriftIdentity(package_id=package_id),
						source_id=src.source_id,
						index_path=str(index_path),
					)
				)
				continue
			ver = raw.get("package_version")
			target = raw.get("target")
			sha = raw.get("sha256")
			filename = raw.get("filename")
			if not isinstance(ver, str) or not isinstance(target, str) or not isinstance(sha, str) or not isinstance(filename, str):
				findings.append(
					DriftError(
						reason_code="INDEX_INVALID_ENTRY",
						message="index entry missing required fields",
						mode="doctor",
						identity=DriftIdentity(package_id=package_id, version=str(ver) if ver is not None else None, target=str(target) if target is not None else None),
						source_id=src.source_id,
						index_path=str(index_path),
					)
				)
				continue
			path_str = raw.get("path") if isinstance(raw.get("path"), str) and raw.get("path") else filename
			try:
				normalized = _normalize_rel_path(path_str, what=f"index path for package_id '{package_id}'")
			except ValueError as err:
				findings.append(
					DriftError(
						reason_code="INDEX_INVALID_ENTRY",
						message=str(err),
						mode="doctor",
						identity=DriftIdentity(package_id=package_id, version=ver, target=target),
						source_id=src.source_id,
						index_path=str(index_path),
					)
				)
				continue

			if deep:
				artifact = src.path / normalized
				if not artifact.exists():
					findings.append(
						DriftError(
							reason_code="INDEX_MISSING_PACKAGE_FILE",
							message="missing package file referenced by index",
							mode="doctor",
							identity=DriftIdentity(package_id=package_id, version=ver, target=target),
							source_id=src.source_id,
							index_path=str(index_path),
							artifact_path=str(artifact),
						)
					)
					continue
				got_sha = f"sha256:{sha256_hex(artifact.read_bytes())}"
				if sha != got_sha:
					findings.append(
						DriftError(
							reason_code="INDEX_SHA_MISMATCH",
							message="sha256 mismatch between index and package bytes",
							mode="doctor",
							identity=DriftIdentity(package_id=package_id, version=ver, target=target),
							source_id=src.source_id,
							index_path=str(index_path),
							artifact_path=str(artifact),
							sha256_expected=sha,
							sha256_got=got_sha,
						)
					)
					continue
				try:
					ident = read_identity_v0(artifact)
				except PackageFormatError as err:
					findings.append(
						DriftError(
							reason_code="INDEX_PKG_FORMAT_INVALID",
							message=str(err),
							mode="doctor",
							identity=DriftIdentity(package_id=package_id, version=ver, target=target),
							source_id=src.source_id,
							index_path=str(index_path),
							artifact_path=str(artifact),
						)
					)
					continue
				if ident.package_id != package_id or ident.package_version != ver or ident.target != target:
					findings.append(
						DriftError(
							reason_code="INDEX_IDENTITY_MISMATCH",
							message="package identity mismatch for package referenced by index",
							mode="doctor",
							identity=DriftIdentity(package_id=package_id, version=ver, target=target),
							claimed_identity=DriftIdentity(package_id=package_id, version=ver, target=target),
							observed_identity=DriftIdentity(package_id=ident.package_id, version=ident.package_version, target=ident.target),
							source_id=src.source_id,
							index_path=str(index_path),
							artifact_path=str(artifact),
						)
					)

	status: DoctorStatus = "ok"
	summary = "indexes ok"
	if findings:
		status = "fatal"
		summary = "indexes have fatal issues"
	return DoctorCheckResult(
		check_id="indexes",
		status=status,
		summary=summary,
		findings=findings,
		data={"source_count": len(sources.sources), "package_entry_count": pkg_count, "deep": deep},
	)


def _check_trust_store(path: Path) -> DoctorCheckResult:
	if not path.exists():
		return DoctorCheckResult(
			check_id="trust",
			status="degraded",
			summary=f"trust store missing: {path}",
			findings=[DriftError(reason_code="TRUST_MISSING", message="trust store missing", mode="doctor", artifact_path=str(path))],
		)
	try:
		obj = json.loads(path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as err:
		return DoctorCheckResult(
			check_id="trust",
			status="fatal",
			summary="trust store parse error",
			findings=[DriftError(reason_code="TRUST_PARSE_ERROR", message=str(err), mode="doctor", artifact_path=str(path))],
		)
	if not isinstance(obj, dict) or obj.get("format") != "drift-trust" or obj.get("version") != 1:
		return DoctorCheckResult(
			check_id="trust",
			status="fatal",
			summary="trust store schema invalid",
			findings=[DriftError(reason_code="TRUST_SCHEMA_INVALID", message="unsupported trust store format/version (expected drift-trust v1)", mode="doctor", artifact_path=str(path))],
		)

	findings: list[DriftError] = []
	keys = obj.get("keys")
	namespaces = obj.get("namespaces")
	revoked = obj.get("revoked")
	if not isinstance(keys, dict) or not isinstance(namespaces, dict):
		findings.append(
			DriftError(reason_code="TRUST_SCHEMA_INVALID", message="trust store keys/namespaces must be objects", mode="doctor", artifact_path=str(path))
		)
		return DoctorCheckResult(check_id="trust", status="fatal", summary="trust store schema invalid", findings=findings)

	# v1: revoked is a flat list of kid strings.
	if revoked is not None and not isinstance(revoked, list):
		findings.append(
			DriftError(reason_code="TRUST_SCHEMA_INVALID", message="trust store revoked must be a list of kid strings (v1)", mode="doctor", artifact_path=str(path))
		)

	for kid, k in keys.items():
		if not isinstance(kid, str) or not isinstance(k, dict):
			findings.append(
				DriftError(reason_code="TRUST_KEY_INVALID", message="trust store key entries must be objects", mode="doctor", artifact_path=str(path))
			)
			continue
		if k.get("algo") != "ed25519":
			findings.append(
				DriftError(reason_code="TRUST_KEY_INVALID", message="unsupported key algo", mode="doctor", artifact_path=str(path))
			)
			continue
		pub_b64 = k.get("pubkey")
		if not isinstance(pub_b64, str):
			findings.append(
				DriftError(reason_code="TRUST_KEY_INVALID", message="missing pubkey", mode="doctor", artifact_path=str(path))
			)
			continue
		try:
			pub_raw = b64_decode(pub_b64.strip())
		except Exception as err:
			findings.append(
				DriftError(reason_code="TRUST_KEY_INVALID", message=f"pubkey base64 decode failed: {err}", mode="doctor", artifact_path=str(path))
			)
			continue
		if len(pub_raw) != 32:
			findings.append(
				DriftError(reason_code="TRUST_KEY_INVALID", message="ed25519 pubkey must be 32 bytes", mode="doctor", artifact_path=str(path))
			)
			continue
		derived = compute_ed25519_kid(pub_raw)
		if kid != derived:
			findings.append(
				DriftError(
					reason_code="TRUST_KEY_INVALID",
					message="kid does not match derived kid from pubkey",
					mode="doctor",
					artifact_path=str(path),
				)
			)

	for ns, kids in namespaces.items():
		if not isinstance(ns, str) or not ns:
			findings.append(
				DriftError(reason_code="TRUST_NAMESPACE_INVALID", message="namespace keys must be non-empty strings", mode="doctor", artifact_path=str(path))
			)
			continue
		if " " in ns or "\t" in ns or "\n" in ns:
			findings.append(
				DriftError(reason_code="TRUST_NAMESPACE_INVALID", message="namespace must not contain whitespace", mode="doctor", artifact_path=str(path))
			)
		if "*" in ns and not ns.endswith(".*") and ns != "*":
			findings.append(
				DriftError(reason_code="TRUST_NAMESPACE_INVALID", message="wildcard namespaces must end with '.*' (or be '*')", mode="doctor", artifact_path=str(path))
			)
		# v1: namespace values are role-tagged objects, not flat lists.
		# Shape: `{"authors": ["<kid>", ...], "certifiers": ["<kid>", ...]}`.
		if not isinstance(kids, dict):
			findings.append(
				DriftError(reason_code="TRUST_NAMESPACE_INVALID", message="namespace values must be role-tagged objects with 'authors'/'certifiers' lists (v1)", mode="doctor", artifact_path=str(path))
			)
			continue
		for role_key in ("authors", "certifiers"):
			role_list = kids.get(role_key, [])
			if not isinstance(role_list, list) or any(not isinstance(k, str) or not k for k in role_list):
				findings.append(
					DriftError(reason_code="TRUST_NAMESPACE_INVALID", message=f"namespace {role_key!r} must be a list of kid strings", mode="doctor", artifact_path=str(path))
				)

	status: DoctorStatus = "ok"
	summary = "trust store ok"
	if findings:
		status = "fatal" if any(f.reason_code in ("TRUST_SCHEMA_INVALID", "TRUST_PARSE_ERROR") for f in findings) else "degraded"
		summary = "trust store has issues"
	return DoctorCheckResult(
		check_id="trust",
		status=status,
		summary=summary,
		findings=findings,
		data={"key_count": len(keys), "namespace_count": len(namespaces)},
	)


def _check_lock(path: Path, *, sources: SourcesV0 | None, deep: bool) -> tuple[DoctorCheckResult, dict[str, LockEntry] | None]:
	if not path.exists():
		return DoctorCheckResult(check_id="lock", status="info", summary=f"lockfile missing: {path}", findings=[]), None
	try:
		entries = load_lock_entries_v0(path)
	except json.JSONDecodeError as err:
		return DoctorCheckResult(
			check_id="lock",
			status="fatal",
			summary="lockfile parse error",
			findings=[DriftError(reason_code="LOCK_PARSE_ERROR", message=str(err), mode="doctor", artifact_path=str(path))],
		), None
	except Exception as err:
		return DoctorCheckResult(
			check_id="lock",
			status="fatal",
			summary="lockfile schema invalid",
			findings=[DriftError(reason_code="LOCK_SCHEMA_INVALID", message=str(err), mode="doctor", artifact_path=str(path))],
		), None

	sources_by_id: dict[str, Path] = {}
	if sources is not None:
		for s in sources.sources:
			sources_by_id[s.source_id] = s.path

	findings: list[DriftError] = []
	for package_id in sorted(entries.keys()):
		e = entries[package_id]
		identity = DriftIdentity(package_id=e.package_id, version=e.package_version, target=e.target)
		if sources is not None and e.source_id not in sources_by_id:
			findings.append(
				DriftError(
					reason_code="LOCK_SOURCE_UNKNOWN",
					message="lock pins package to unknown source_id",
					mode="doctor",
					identity=identity,
					source_id=e.source_id,
					artifact_path=str(path),
					signer_kids=list(e.sig_kids),
				)
			)
			continue
		try:
			rel = _normalize_rel_path(e.path, what=f"lock path for package_id '{package_id}'")
		except ValueError as err:
			findings.append(
				DriftError(
					reason_code="LOCK_PATH_INVALID",
					message=str(err),
					mode="doctor",
					identity=identity,
					source_id=e.source_id,
					artifact_path=str(path),
					signer_kids=list(e.sig_kids),
				)
			)
			continue

		if deep and sources is not None:
			repo = sources_by_id.get(e.source_id)
			if repo is None:
				continue
			artifact = repo / rel
			if not artifact.exists():
				findings.append(
					DriftError(
						reason_code="LOCK_ARTIFACT_MISSING",
						message="missing locked package file",
						mode="doctor",
						identity=identity,
						source_id=e.source_id,
						artifact_path=str(artifact),
						signer_kids=list(e.sig_kids),
					)
				)
				continue
			got_sha = f"sha256:{sha256_hex(artifact.read_bytes())}"
			if got_sha != e.pkg_sha256:
				findings.append(
					DriftError(
						reason_code="LOCK_SHA_MISMATCH",
						message="sha256 mismatch for locked package",
						mode="doctor",
						identity=identity,
						source_id=e.source_id,
						artifact_path=str(artifact),
						sha256_expected=e.pkg_sha256,
						sha256_got=got_sha,
						signer_kids=list(e.sig_kids),
					)
				)
				continue
			try:
				ident = read_identity_v0(artifact)
			except PackageFormatError as err:
				findings.append(
					DriftError(
						reason_code="LOCK_PKG_FORMAT_INVALID",
						message=str(err),
						mode="doctor",
						identity=identity,
						source_id=e.source_id,
						artifact_path=str(artifact),
						signer_kids=list(e.sig_kids),
					)
				)
				continue
			obs = e.observed_identity
			if ident.package_id != obs.package_id or ident.package_version != obs.package_version or ident.target != obs.target:
				findings.append(
					DriftError(
						reason_code="LOCK_IDENTITY_MISMATCH",
						message="package identity mismatch for locked package",
						mode="doctor",
						identity=identity,
						claimed_identity=DriftIdentity(package_id=obs.package_id, version=obs.package_version, target=obs.target),
						observed_identity=DriftIdentity(package_id=ident.package_id, version=ident.package_version, target=ident.target),
						source_id=e.source_id,
						artifact_path=str(artifact),
						signer_kids=list(e.sig_kids),
					)
				)
				continue
			if e.sig_sha256 is not None:
				sig_path = repo / (rel + ".sig")
				if not sig_path.exists():
					findings.append(
						DriftError(
							reason_code="LOCK_SIGNATURE_MISSING",
							message="missing signature sidecar for locked package",
							mode="doctor",
							identity=identity,
							source_id=e.source_id,
							artifact_path=str(artifact),
							signer_kids=list(e.sig_kids),
						)
					)
					continue
				got_sig_sha = f"sha256:{sha256_hex(sig_path.read_bytes())}"
				if got_sig_sha != e.sig_sha256:
					findings.append(
						DriftError(
							reason_code="LOCK_SIG_SHA_MISMATCH",
							message="signature sha256 mismatch for locked package",
							mode="doctor",
							identity=identity,
							source_id=e.source_id,
							artifact_path=str(artifact),
							sha256_expected=e.sig_sha256,
							sha256_got=got_sig_sha,
							signer_kids=list(e.sig_kids),
						)
					)

	status: DoctorStatus = "ok"
	summary = "lockfile ok"
	if findings:
		status = "fatal"
		summary = "lockfile has fatal issues"
	return DoctorCheckResult(
		check_id="lock",
		status=status,
		summary=summary,
		findings=findings,
		data={"package_count": len(entries), "deep": deep},
	), entries


def _check_vendor_consistency(lock_entries: dict[str, LockEntry] | None, *, vendor_dir: Path, deep: bool) -> DoctorCheckResult:
	if not deep:
		return DoctorCheckResult(
			check_id="vendor_consistency",
			status="info",
			summary="vendor checks skipped (use --deep)",
			findings=[],
			data={"deep": deep},
		)
	if lock_entries is None:
		return DoctorCheckResult(
			check_id="vendor_consistency",
			status="info",
			summary="vendor checks skipped (lock missing/invalid)",
			findings=[],
			data={"deep": deep},
		)

	if not vendor_dir.exists():
		return DoctorCheckResult(
			check_id="vendor_consistency",
			status="degraded",
			summary="vendor directory missing",
			findings=[
				DriftError(
					reason_code="VENDOR_DIR_MISSING",
					message="vendor directory missing",
					mode="doctor",
					artifact_path=str(vendor_dir),
				)
			],
			data={"deep": deep, "package_count": len(lock_entries), "vendor_dir": str(vendor_dir)},
		)

	findings: list[DriftError] = []
	for package_id in sorted(lock_entries.keys()):
		entry = lock_entries[package_id]
		identity = DriftIdentity(package_id=entry.package_id, version=entry.package_version, target=entry.target)
		artifact = vendor_dir / entry.path
		if not artifact.exists():
			findings.append(
				DriftError(
					reason_code="VENDOR_MISSING_ARTIFACT",
					message="missing vendored package for locked entry",
					mode="doctor",
					identity=identity,
					source_id=entry.source_id,
					artifact_path=str(artifact),
					signer_kids=list(entry.sig_kids),
				)
			)
			continue
		got_sha = f"sha256:{sha256_hex(artifact.read_bytes())}"
		if got_sha != entry.pkg_sha256:
			findings.append(
				DriftError(
					reason_code="VENDOR_SHA_MISMATCH",
					message="vendored package sha256 differs from lock",
					mode="doctor",
					identity=identity,
					source_id=entry.source_id,
					artifact_path=str(artifact),
					sha256_expected=entry.pkg_sha256,
					sha256_got=got_sha,
					signer_kids=list(entry.sig_kids),
				)
			)

	status: DoctorStatus = "ok"
	summary = "vendor consistency ok"
	if findings:
		status = "degraded"
		summary = "vendor consistency issues detected"
	return DoctorCheckResult(
		check_id="vendor_consistency",
		status=status,
		summary=summary,
		findings=findings,
		data={"package_count": len(lock_entries), "vendor_dir": str(vendor_dir), "deep": deep},
	)


def _check_cache_consistency(lock_entries: dict[str, LockEntry] | None, *, cache_dir: Path, deep: bool) -> DoctorCheckResult:
	if not deep:
		return DoctorCheckResult(
			check_id="cache_consistency",
			status="info",
			summary="cache checks skipped (use --deep)",
			findings=[],
			data={"deep": deep},
		)
	if lock_entries is None:
		return DoctorCheckResult(
			check_id="cache_consistency",
			status="info",
			summary="cache checks skipped (lock missing/invalid)",
			findings=[],
			data={"deep": deep},
		)

	if not cache_dir.exists():
		return DoctorCheckResult(
			check_id="cache_consistency",
			status="degraded",
			summary="cache directory missing",
			findings=[
				DriftError(
					reason_code="CACHE_MISSING",
					message="cache directory missing",
					mode="doctor",
					artifact_path=str(cache_dir),
				)
			],
			data={"deep": deep, "package_count": len(lock_entries), "cache_dir": str(cache_dir)},
		)

	index_path = cache_dir / "index.json"
	if not index_path.exists():
		return DoctorCheckResult(
			check_id="cache_consistency",
			status="degraded",
			summary="cache index missing",
			findings=[
				DriftError(
					reason_code="CACHE_INDEX_MISSING",
					message="cache index missing",
					mode="doctor",
					artifact_path=str(index_path),
				)
			],
			data={"deep": deep, "package_count": len(lock_entries), "cache_dir": str(cache_dir)},
		)

	try:
		index_obj = load_index(index_path)
	except Exception as err:
		return DoctorCheckResult(
			check_id="cache_consistency",
			status="degraded",
			summary="cache index unreadable",
			findings=[
				DriftError(
					reason_code="CACHE_INDEX_INVALID",
					message=str(err),
					mode="doctor",
					artifact_path=str(index_path),
				)
			],
			data={"deep": deep, "package_count": len(lock_entries), "cache_dir": str(cache_dir)},
		)

	pkgs = index_obj.get("packages") or {}
	findings: list[DriftError] = []
	for package_id in sorted(lock_entries.keys()):
		entry = lock_entries[package_id]
		identity = DriftIdentity(package_id=entry.package_id, version=entry.package_version, target=entry.target)
		raw = pkgs.get(package_id)
		if not isinstance(raw, dict):
			findings.append(
				DriftError(
					reason_code="LOCK_CACHE_MISSING_ENTRY",
					message="cache index missing entry for locked package",
					mode="doctor",
					identity=identity,
					source_id=entry.source_id,
					artifact_path=str(index_path),
					signer_kids=list(entry.sig_kids),
				)
			)
			continue
		cache_path_raw = raw.get("path") if isinstance(raw.get("path"), str) and raw.get("path") else raw.get("filename")
		if not isinstance(cache_path_raw, str):
			findings.append(
				DriftError(
					reason_code="LOCK_CACHE_MISSING_ENTRY",
					message="cache index missing path for locked package",
					mode="doctor",
					identity=identity,
					source_id=entry.source_id,
					artifact_path=str(index_path),
					signer_kids=list(entry.sig_kids),
				)
			)
			continue
		try:
			cache_rel = _normalize_rel_path(cache_path_raw, what=f"cache path for package_id '{package_id}'")
		except ValueError as err:
			findings.append(
				DriftError(
					reason_code="CACHE_INDEX_INVALID",
					message=str(err),
					mode="doctor",
					identity=identity,
					source_id=entry.source_id,
					artifact_path=str(index_path),
					signer_kids=list(entry.sig_kids),
				)
			)
			continue

		cache_pkg = cache_dir / "pkgs" / cache_rel
		if not cache_pkg.exists():
			findings.append(
				DriftError(
					reason_code="CACHE_MISSING_ARTIFACT",
					message="cached package missing for locked entry",
					mode="doctor",
					identity=identity,
					source_id=entry.source_id,
					artifact_path=str(cache_pkg),
					signer_kids=list(entry.sig_kids),
				)
			)
			continue

		got_sha = f"sha256:{sha256_hex(cache_pkg.read_bytes())}"
		if got_sha != entry.pkg_sha256:
			findings.append(
				DriftError(
					reason_code="LOCK_CACHE_DIVERGENCE",
					message="cache package sha256 differs from lock",
					mode="doctor",
					identity=identity,
					source_id=entry.source_id,
					artifact_path=str(cache_pkg),
					sha256_expected=entry.pkg_sha256,
					sha256_got=got_sha,
					signer_kids=list(entry.sig_kids),
				)
			)

	status: DoctorStatus = "ok"
	summary = "cache consistency ok"
	if findings:
		status = "degraded"
		summary = "cache consistency issues detected"
	return DoctorCheckResult(
		check_id="cache_consistency",
		status=status,
		summary=summary,
		findings=findings,
		data={"package_count": len(lock_entries), "cache_dir": str(cache_dir), "deep": deep},
	)
