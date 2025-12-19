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

from lang2.driftc.packages.dmir_pkg_v0 import LoadedPackage, load_dmir_pkg_v0
from lang2.driftc.packages.signature_v0 import verify_package_signatures
from lang2.driftc.packages.trust_v0 import TrustStore


def discover_package_files(package_roots: list[Path]) -> list[Path]:
	"""
	Discover package artifacts under package roots.

	MVP rule: any `*.dmp` file under a root is considered a package artifact.
	The returned list is deterministic.
	"""
	out: set[Path] = set()
	for root in package_roots:
		if not root.exists():
			continue
		if root.is_file():
			if root.suffix == ".dmp":
				out.add(root)
			continue
		for p in sorted(root.rglob("*.dmp")):
			if p.is_file():
				out.add(p)
	return sorted(out)


def load_package_v0(path: Path) -> LoadedPackage:
	"""Load and verify a DMIR-PKG v0 artifact (integrity only)."""
	return load_dmir_pkg_v0(path)


def _validate_package_interfaces(pkg: LoadedPackage) -> None:
	"""
	Validate module interfaces against payload metadata.

	Pinned rule (ABI boundary): any exported value must have a corresponding
	payload signature entry with `is_exported_entrypoint == True`.

	This is a package consumption guardrail: it rejects malformed/inconsistent
	packages early, before imports are resolved or IR is embedded.
	"""
	for mid, mod in pkg.modules_by_id.items():
		exports = mod.interface.get("exports")
		if not isinstance(exports, dict):
			continue
		values = exports.get("values")
		if not isinstance(values, list):
			continue
		sigs = mod.payload.get("signatures")
		if not isinstance(sigs, dict):
			sigs = {}
		for v in values:
			if not isinstance(v, str):
				continue
			sym = f"{mid}::{v}"
			sd = sigs.get(sym)
			if not isinstance(sd, dict) or not bool(sd.get("is_exported_entrypoint", False)):
				raise ValueError(f"exported value '{v}' is missing exported entrypoint signature metadata")
			if bool(sd.get("is_method", False)):
				raise ValueError(f"exported value '{v}' must not be a method")


@dataclass(frozen=True)
class PackageTrustPolicy:
	"""
	Trust policy used when loading packages from a package root.

	This is intentionally passed in from the driver (`driftc`), not hard-coded in
	the loader, because policy is a tooling concern (project trust store, CI
	settings, local unsigned roots, etc.).
	"""

	trust_store: TrustStore
	require_signatures: bool
	allow_unsigned_roots: list[Path]


def load_package_v0_with_policy(path: Path, *, policy: PackageTrustPolicy, pkg_bytes: bytes | None = None) -> LoadedPackage:
	"""
	Load a package and enforce signature/trust policy.

	`pkg_bytes` is an optional optimization: callers that already read the bytes
	(for hashing) can provide them to avoid a second read.
	"""
	pkg = load_dmir_pkg_v0(path)
	data = pkg_bytes if pkg_bytes is not None else path.read_bytes()
	verify_package_signatures(
		pkg_path=path,
		pkg_bytes=data,
		pkg_manifest=pkg.manifest,
		trust=policy.trust_store,
		require_signatures=policy.require_signatures,
		allow_unsigned_roots=policy.allow_unsigned_roots,
	)
	_validate_package_interfaces(pkg)
	return pkg


def collect_external_exports(packages: list[LoadedPackage]) -> dict[str, dict[str, set[str]]]:
	"""
	Collect module export sets from loaded packages.

	Returns:
	  module_id -> { "values": set[str], "types": set[str] }
	"""
	mod_to_pkg: dict[str, Path] = {}
	out: dict[str, dict[str, set[str]]] = {}
	for pkg in packages:
		for mid, mod in pkg.modules_by_id.items():
			prev = mod_to_pkg.get(mid)
			if prev is None:
				mod_to_pkg[mid] = pkg.path
			elif prev != pkg.path:
				raise ValueError(f"module '{mid}' provided by multiple packages: '{prev}' and '{pkg.path}'")
			exports = mod.interface.get("exports")
			if not isinstance(exports, dict):
				out[mid] = {"values": set(), "types": set()}
				continue
			values = exports.get("values")
			types = exports.get("types")
			out[mid] = {
				"values": set(values) if isinstance(values, list) else set(),
				"types": set(types) if isinstance(types, list) else set(),
			}
	return out
