# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Package provider (v0).

This module discovers package files, loads them using the DMIR-PKG v0 container,
and exposes minimal data needed by the workspace parser:
- which modules exist
- what symbols they export (values/types)

The provider is intentionally conservative:
- duplicate module_id across packages is a hard error (determinism),
- packages must pass integrity checks before any metadata is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lang.driftc.packages.dmir_pkg_v0 import LoadedPackage, load_dmir_pkg_v0, load_dmir_pkg_v0_from_bytes
from lang.driftc.packages.signature_v0 import verify_package_signatures
from lang.driftc.packages.trust_v0 import TrustStore
from lang.driftc.packages.package_validate import (
	collect_external_exports,
	validate_package_interfaces as _validate_package_interfaces,
)


def discover_package_files(package_roots: list[Path]) -> list[Path]:
	"""
	Discover package artifacts under package roots.

	Accepts both `.zdmp` (compressed, standard distribution form) and
	`.dmp` (uncompressed, build intermediate) files.  When both exist
	for the same stem in the same directory, `.zdmp` takes priority and
	the `.dmp` is excluded.

	The returned list is deterministic (sorted).

	Uses os.walk with followlinks=True so that symlinked directories
	(as created by drift deploy's staged/build package roots) are
	traversed correctly.
	"""
	import os

	out: set[Path] = set()
	for root in package_roots:
		if not root.exists():
			continue
		if root.is_file():
			if root.suffix in (".zdmp", ".dmp"):
				out.add(root)
			continue
		for dirpath, _dirnames, filenames in os.walk(root, followlinks=True):
			for fname in filenames:
				if fname.endswith(".zdmp") or fname.endswith(".dmp"):
					out.add(Path(dirpath) / fname)

	# Deduplicate: when both foo.zdmp and foo.dmp exist in the same
	# directory, keep only the .zdmp (published compressed form).
	# If the .zdmp turns out to be corrupt at load time, the loader
	# falls back to the .dmp sibling (see load_package_v0_with_policy).
	zdmp_stems: set[tuple[str, str]] = set()  # (dirpath, stem)
	for p in out:
		if p.suffix == ".zdmp":
			zdmp_stems.add((str(p.parent), p.stem))
	out = {
		p for p in out
		if p.suffix != ".dmp" or (str(p.parent), p.stem) not in zdmp_stems
	}

	return sorted(out)


def load_package_v0(path: Path) -> LoadedPackage:
	"""Load and verify a DMIR-PKG v0 artifact (integrity only).

	Handles both `.zdmp` (compressed) and `.dmp` (uncompressed) files.
	"""
	if path.suffix == ".zdmp":
		from lang.driftc.packages.zdmp import load_zdmp_cached
		raw_bytes = load_zdmp_cached(path, expected_sha256=None)
		return load_dmir_pkg_v0_from_bytes(raw_bytes, source_path=path)
	return load_dmir_pkg_v0(path)



@dataclass(frozen=True)
class PackageTrustPolicy:
	"""
	Trust policy used when loading packages from a package root.

	This is intentionally passed in from the driver (`driftc`), not hard-coded in
	the loader, because policy is a tooling concern (project trust store, CI
	settings, local unsigned roots, etc.).
	"""

	trust_store: TrustStore
	core_trust_store: TrustStore
	require_signatures: bool
	allow_unsigned_roots: list[Path]


def _read_sig_sha256(pkg_path: Path) -> str | None:
	"""
	Try to read the expected uncompressed sha256 from the .sig sidecar.

	Returns the hex digest string or None if the sidecar doesn't exist
	or can't be parsed.  Used to enable cache lookups for .zdmp files.
	"""
	sig_path = pkg_path.with_suffix(".sig")
	if not sig_path.exists():
		return None
	try:
		import json
		obj = json.loads(sig_path.read_text(encoding="utf-8"))
		sha_field = obj.get("package_sha256", "")
		if isinstance(sha_field, str) and sha_field.startswith("sha256:"):
			return sha_field.split("sha256:", 1)[1]
	except Exception:
		pass
	return None


def load_package_v0_with_policy(path: Path, *, policy: PackageTrustPolicy, pkg_bytes: bytes | None = None) -> LoadedPackage:
	"""
	Load a package and enforce signature/trust policy.

	Handles both `.zdmp` (compressed) and `.dmp` (uncompressed) files.
	For `.zdmp`: decompresses via cache, then parses + verifies against
	the uncompressed bytes (signature covers canonical uncompressed payload).
	If `.zdmp` decompression fails, falls back to a `.dmp` sibling if one exists.

	`pkg_bytes` is an optional optimization: callers that already read the bytes
	(for hashing) can provide them to avoid a second read.
	"""
	if path.suffix == ".zdmp":
		from lang.driftc.packages.zdmp import load_zdmp_cached
		try:
			import zstandard as _zstd
			_ZstdError: type = _zstd.ZstdError
		except (ImportError, AttributeError):
			_ZstdError = type(None)  # unreachable fallback
		try:
			expected_sha = _read_sig_sha256(path)
			raw_bytes = load_zdmp_cached(path, expected_sha256=expected_sha)
			pkg = load_dmir_pkg_v0_from_bytes(raw_bytes, source_path=path)
			data = raw_bytes
		except _ZstdError:
			# Zstd decompression failed (corrupt frame) — try .dmp sibling.
			# This handles the case where a stale/corrupt .zdmp exists
			# alongside a valid .dmp (e.g. after a partial deploy).
			# Intentionally narrow: sha/integrity mismatches and container
			# parse errors are real failures and must not be silenced.
			dmp_sibling = path.with_suffix(".dmp")
			if not dmp_sibling.exists():
				raise  # no fallback available, re-raise original error
			pkg = load_dmir_pkg_v0(dmp_sibling)
			data = pkg_bytes if pkg_bytes is not None else dmp_sibling.read_bytes()
			path = dmp_sibling  # use .dmp path for signature lookup below
	else:
		pkg = load_dmir_pkg_v0(path)
		data = pkg_bytes if pkg_bytes is not None else path.read_bytes()

	# Package identity fields (pinned): required for dependency resolution and for
	# driftc to enforce "single version per package id per build".
	pkg_id = pkg.manifest.get("package_id")
	pkg_ver = pkg.manifest.get("package_version")
	pkg_target = pkg.manifest.get("target")
	if not isinstance(pkg_id, str) or not pkg_id:
		raise ValueError("package manifest missing package_id")
	if not isinstance(pkg_ver, str) or not pkg_ver:
		raise ValueError("package manifest missing package_version")
	if not isinstance(pkg_target, str) or not pkg_target:
		raise ValueError("package manifest missing target")
	verify_package_signatures(
		pkg_path=path,
		pkg_bytes=data,
		pkg_manifest=pkg.manifest,
		trust=policy.trust_store,
		core_trust=policy.core_trust_store,
		require_signatures=policy.require_signatures,
		allow_unsigned_roots=policy.allow_unsigned_roots,
	)
	_validate_package_interfaces(pkg)
	return pkg


