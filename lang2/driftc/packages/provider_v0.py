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

	def _err(msg: str) -> ValueError:
		return ValueError(msg)

	for mid, mod in pkg.modules_by_id.items():
		if not isinstance(mod.interface, dict):
			raise _err(f"module '{mid}' interface is not a JSON object")
		if mod.interface.get("format") != "drift-module-interface":
			raise _err(f"module '{mid}' has unsupported interface format")
		if mod.interface.get("version") != 0:
			raise _err(f"module '{mid}' has unsupported interface version")
		if mod.interface.get("module_id") != mid:
			raise _err(f"module '{mid}' interface module_id mismatch")

		exports = mod.interface.get("exports")
		if not isinstance(exports, dict):
			raise _err(f"module '{mid}' interface missing exports")
		values = exports.get("values")
		types = exports.get("types")
		if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
			raise _err(f"module '{mid}' interface exports.values must be a list of strings")
		if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
			raise _err(f"module '{mid}' interface exports.types must be a list of strings")
		if len(set(values)) != len(values):
			raise _err(f"module '{mid}' interface exports.values contains duplicates")
		if len(set(types)) != len(types):
			raise _err(f"module '{mid}' interface exports.types contains duplicates")

		# Payload must agree with interface exports exactly.
		payload_exports = mod.payload.get("exports")
		if not isinstance(payload_exports, dict):
			raise _err(f"module '{mid}' payload missing exports")
		payload_values = payload_exports.get("values")
		payload_types = payload_exports.get("types")
		if not isinstance(payload_values, list) or not isinstance(payload_types, list):
			raise _err(f"module '{mid}' payload exports must include values/types lists")
		if sorted(payload_values) != sorted(values) or sorted(payload_types) != sorted(types):
			raise _err(f"module '{mid}' interface exports do not match payload exports")

		iface_sigs = mod.interface.get("signatures")
		if not isinstance(iface_sigs, dict):
			raise _err(f"module '{mid}' interface missing signatures table")

		payload_sigs = mod.payload.get("signatures")
		if not isinstance(payload_sigs, dict):
			raise _err(f"module '{mid}' payload missing signatures table")

		# Tightened ABI boundary invariants.
		for v in values:
			sym = f"{mid}::{v}"
			if "__impl" in sym:
				raise _err(f"exported value '{v}' must not reference private symbols")
			if sym not in iface_sigs:
				raise _err(f"exported value '{v}' is missing interface signature metadata")
			if sym not in payload_sigs:
				raise _err(f"exported value '{v}' is missing payload signature metadata")
			iface_sd = iface_sigs.get(sym)
			payload_sd = payload_sigs.get(sym)
			if not isinstance(iface_sd, dict) or not isinstance(payload_sd, dict):
				raise _err(f"exported value '{v}' has invalid signature metadata")
			if iface_sd != payload_sd:
				raise _err(f"exported value '{v}' interface signature does not match payload signature")
			if not bool(payload_sd.get("is_exported_entrypoint", False)):
				raise _err(f"exported value '{v}' is missing exported entrypoint signature metadata")
			if bool(payload_sd.get("is_method", False)):
				raise _err(f"exported value '{v}' must not be a method")

		# Forbid extra interface signature entries (strict interface).
		extra = set(iface_sigs.keys()) - {f"{mid}::{v}" for v in values}
		if extra:
			raise _err(f"module '{mid}' interface contains non-export signature entries")


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
