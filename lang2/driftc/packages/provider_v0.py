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

from pathlib import Path

from lang2.driftc.packages.dmir_pkg_v0 import LoadedPackage, load_dmir_pkg_v0


def discover_package_files(package_roots: list[Path]) -> list[Path]:
	"""
	Discover package artifacts under package roots.

	MVP rule: any `*.dmp` file under a root is considered a package artifact.
	The returned list is deterministic.
	"""
	out: list[Path] = []
	for root in package_roots:
		if not root.exists():
			continue
		for p in sorted(root.rglob("*.dmp")):
			if p.is_file():
				out.append(p)
	return sorted(out)


def load_package_v0(path: Path) -> LoadedPackage:
	"""Load and verify a DMIR-PKG v0 artifact."""
	return load_dmir_pkg_v0(path)


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

