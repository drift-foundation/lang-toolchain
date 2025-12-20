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

	Pinned ABI boundary rule: any exported value must have a corresponding payload
	signature entry with `is_exported_entrypoint == True`.

	This is a package-consumption guardrail: it rejects malformed/inconsistent
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
		consts = exports.get("consts", [])
		if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
			raise _err(f"module '{mid}' interface exports.values must be a list of strings")
		if not isinstance(types, dict):
			raise _err(f"module '{mid}' interface exports.types must be an object")
		type_structs = types.get("structs")
		type_variants = types.get("variants")
		type_excs = types.get("exceptions")
		if not isinstance(type_structs, list) or not all(isinstance(t, str) for t in type_structs):
			raise _err(f"module '{mid}' interface exports.types.structs must be a list of strings")
		if not isinstance(type_variants, list) or not all(isinstance(t, str) for t in type_variants):
			raise _err(f"module '{mid}' interface exports.types.variants must be a list of strings")
		if not isinstance(type_excs, list) or not all(isinstance(t, str) for t in type_excs):
			raise _err(f"module '{mid}' interface exports.types.exceptions must be a list of strings")
		if not isinstance(consts, list) or not all(isinstance(c, str) for c in consts):
			raise _err(f"module '{mid}' interface exports.consts must be a list of strings")
		if len(set(values)) != len(values):
			raise _err(f"module '{mid}' interface exports.values contains duplicates")
		if len(set(type_structs)) != len(type_structs):
			raise _err(f"module '{mid}' interface exports.types.structs contains duplicates")
		if len(set(type_variants)) != len(type_variants):
			raise _err(f"module '{mid}' interface exports.types.variants contains duplicates")
		if len(set(type_excs)) != len(type_excs):
			raise _err(f"module '{mid}' interface exports.types.exceptions contains duplicates")
		type_union = set(type_structs) | set(type_variants) | set(type_excs)
		if len(type_union) != (len(type_structs) + len(type_variants) + len(type_excs)):
			raise _err(f"module '{mid}' interface exports.types contains overlapping names across kinds")
		if len(set(consts)) != len(consts):
			raise _err(f"module '{mid}' interface exports.consts contains duplicates")

		# Payload must agree with interface exports exactly.
		payload_exports = mod.payload.get("exports")
		if not isinstance(payload_exports, dict):
			raise _err(f"module '{mid}' payload missing exports")
		payload_values = payload_exports.get("values")
		payload_types = payload_exports.get("types")
		payload_consts = payload_exports.get("consts", [])
		if not isinstance(payload_values, list) or not isinstance(payload_consts, list) or not isinstance(payload_types, dict):
			raise _err(f"module '{mid}' payload exports must include values/types/consts")
		payload_structs = payload_types.get("structs")
		payload_variants = payload_types.get("variants")
		payload_excs = payload_types.get("exceptions")
		if not isinstance(payload_structs, list) or not isinstance(payload_variants, list) or not isinstance(payload_excs, list):
			raise _err(f"module '{mid}' payload exports.types must include structs/variants/exceptions lists")
		payload_type_union = set(payload_structs) | set(payload_variants) | set(payload_excs)
		if len(payload_type_union) != (len(payload_structs) + len(payload_variants) + len(payload_excs)):
			raise _err(f"module '{mid}' payload exports.types contains overlapping names across kinds")
		if (
			sorted(payload_values) != sorted(values)
			or sorted(payload_structs) != sorted(type_structs)
			or sorted(payload_variants) != sorted(type_variants)
			or sorted(payload_excs) != sorted(type_excs)
			or sorted(payload_consts) != sorted(consts)
		):
			raise _err(f"module '{mid}' interface exports do not match payload exports")

		# Re-export metadata must match between interface and payload.
		#
		# Values are re-exported by materialized trampoline functions, but types and
		# (future) consts may be re-exported as aliases. This metadata is required
		# so import resolution can bind to the defining module identity without
		# duplicating nominal type definitions.
		iface_reexp = mod.interface.get("reexports", {})
		payload_reexp = mod.payload.get("reexports", {})
		if iface_reexp is None:
			iface_reexp = {}
		if payload_reexp is None:
			payload_reexp = {}
		if not isinstance(iface_reexp, dict):
			raise _err(f"module '{mid}' interface reexports must be an object")
		if not isinstance(payload_reexp, dict):
			raise _err(f"module '{mid}' payload reexports must be an object")
		if iface_reexp != payload_reexp:
			raise _err(f"module '{mid}' interface reexports do not match payload reexports")

		types_reexp = iface_reexp.get("types", {})
		consts_reexp = iface_reexp.get("consts", {})
		if types_reexp is None:
			types_reexp = {}
		if consts_reexp is None:
			consts_reexp = {}
		if not isinstance(types_reexp, dict):
			raise _err(f"module '{mid}' reexports.types must be an object")
		if not isinstance(consts_reexp, dict):
			raise _err(f"module '{mid}' reexports.consts must be an object")
		for kind, exported in (("structs", type_structs), ("variants", type_variants), ("exceptions", type_excs)):
			kind_map = types_reexp.get(kind, {})
			if kind_map is None:
				kind_map = {}
			if not isinstance(kind_map, dict):
				raise _err(f"module '{mid}' reexports.types.{kind} must be an object")
			for local_name, target in kind_map.items():
				if not isinstance(local_name, str):
					raise _err(f"module '{mid}' reexports.types.{kind} has non-string key")
				if local_name not in exported:
					raise _err(f"module '{mid}' reexports.types.{kind} contains non-exported name '{local_name}'")
				if not isinstance(target, dict):
					raise _err(f"module '{mid}' reexports.types.{kind} target for '{local_name}' must be an object")
				tmod = target.get("module")
				tname = target.get("name")
				if not isinstance(tmod, str) or not tmod:
					raise _err(f"module '{mid}' reexports.types.{kind} target for '{local_name}' missing module")
				if not isinstance(tname, str) or not tname:
					raise _err(f"module '{mid}' reexports.types.{kind} target for '{local_name}' missing name")
		for local_name, target in consts_reexp.items():
			if not isinstance(local_name, str):
				raise _err(f"module '{mid}' reexports.consts has non-string key")
			if local_name not in consts:
				raise _err(f"module '{mid}' reexports.consts contains non-exported name '{local_name}'")
			if not isinstance(target, dict):
				raise _err(f"module '{mid}' reexports.consts target for '{local_name}' must be an object")
			tmod = target.get("module")
			tname = target.get("name")
			if not isinstance(tmod, str) or not tmod:
				raise _err(f"module '{mid}' reexports.consts target for '{local_name}' missing module")
			if not isinstance(tname, str) or not tname:
				raise _err(f"module '{mid}' reexports.consts target for '{local_name}' missing name")

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

		# Exported const payloads must be present in both interface and payload.
		#
		# Consts are compile-time values; consumers import them without executing
		# any code. Therefore, the interface must fully describe their type and
		# literal value.
		payload_consts_tbl = mod.payload.get("consts", {})
		iface_consts_tbl = mod.interface.get("consts", {})
		if not isinstance(payload_consts_tbl, dict):
			raise _err(f"module '{mid}' payload consts table must be an object")
		if not isinstance(iface_consts_tbl, dict):
			raise _err(f"module '{mid}' interface consts table must be an object")
		if set(payload_consts_tbl.keys()) != set(consts):
			raise _err(f"module '{mid}' payload consts table does not match exports.consts")
		if set(iface_consts_tbl.keys()) != set(consts):
			raise _err(f"module '{mid}' interface consts table does not match exports.consts")
		for c in consts:
			p_entry = payload_consts_tbl.get(c)
			i_entry = iface_consts_tbl.get(c)
			if not isinstance(p_entry, dict) or not isinstance(i_entry, dict):
				raise _err(f"exported const '{c}' has invalid const table entry")
			if p_entry != i_entry:
				raise _err(f"exported const '{c}' interface entry does not match payload")
			ty_id = p_entry.get("type_id")
			val = p_entry.get("value")
			if not isinstance(ty_id, int):
				raise _err(f"exported const '{c}' missing integer type_id")
			if not isinstance(val, (bool, int, float, str)):
				raise _err(f"exported const '{c}' has unsupported literal value kind")

		payload_tt = mod.payload.get("type_table")
		if not isinstance(payload_tt, dict):
			raise _err(f"module '{mid}' payload missing type_table")

		# Exported exception schemas must be present and match.
		payload_exc = payload_tt.get("exception_schemas")
		if not isinstance(payload_exc, dict):
			payload_exc = {}
		expected_exc: dict[str, list[str]] = {}
		for t in type_excs:
			fqn = f"{mid}:{t}"
			raw = payload_exc.get(fqn)
			if not isinstance(raw, list) or len(raw) != 2 or not isinstance(raw[1], list):
				raise _err(f"module '{mid}' payload has invalid exception schema for '{fqn}'")
			expected_exc[fqn] = list(raw[1])

		iface_exc = mod.interface.get("exception_schemas", {})
		if expected_exc:
			if not isinstance(iface_exc, dict):
				raise _err(f"module '{mid}' interface exception_schemas must be an object")
			for fqn, fields in expected_exc.items():
				got = iface_exc.get(fqn)
				if got is None:
					raise _err(f"exported exception '{fqn}' is missing interface schema")
				if not isinstance(got, list) or list(got) != list(fields):
					raise _err(f"exported exception '{fqn}' interface schema does not match payload")
			extra_exc = set(iface_exc.keys()) - set(expected_exc.keys())
			if extra_exc:
				raise _err(f"module '{mid}' interface contains non-export exception schemas")
		else:
			if iface_exc not in ({}, None) and isinstance(iface_exc, dict) and iface_exc:
				raise _err(f"module '{mid}' interface contains non-export exception schemas")

		# Exported variant schemas must be present and match.
		payload_var = payload_tt.get("variant_schemas")
		if not isinstance(payload_var, dict):
			payload_var = {}
		expected_var: dict[str, dict] = {}
		for raw in payload_var.values():
			if not isinstance(raw, dict):
				continue
			if raw.get("module_id") != mid:
				continue
			name = raw.get("name")
			if not isinstance(name, str) or not name:
				continue
			if name not in type_variants:
				continue
			expected_var[name] = raw
		missing_vars = set(type_variants) - set(expected_var.keys())
		if missing_vars:
			raise _err(f"module '{mid}' payload missing variant schema(s) for: {', '.join(sorted(missing_vars))}")

		iface_var = mod.interface.get("variant_schemas", {})
		if expected_var:
			if not isinstance(iface_var, dict):
				raise _err(f"module '{mid}' interface variant_schemas must be an object")
			for name, schema in expected_var.items():
				got = iface_var.get(name)
				if got is None:
					raise _err(f"exported variant '{name}' is missing interface schema")
				if got != schema:
					raise _err(f"exported variant '{name}' interface schema does not match payload")
			extra_var = set(iface_var.keys()) - set(expected_var.keys())
			if extra_var:
				raise _err(f"module '{mid}' interface contains non-export variant schemas")
		else:
			if iface_var not in ({}, None) and isinstance(iface_var, dict) and iface_var:
				raise _err(f"module '{mid}' interface contains non-export variant schemas")

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


def collect_external_exports(packages: list[LoadedPackage]) -> dict[str, dict[str, object]]:
	"""
	Collect module export sets from loaded packages.

	Returns:
	  module_id -> {
	    "values": set[str],
	    "types": {"structs": set[str], "variants": set[str], "exceptions": set[str]},
	    "consts": set[str],
	    "reexports": {"types": {"structs": dict, "variants": dict, "exceptions": dict}, "consts": dict},
	  }
	"""
	mod_to_pkg: dict[str, Path] = {}
	out: dict[str, dict[str, object]] = {}
	for pkg in packages:
		for mid, mod in pkg.modules_by_id.items():
			prev = mod_to_pkg.get(mid)
			if prev is None:
				mod_to_pkg[mid] = pkg.path
			elif prev != pkg.path:
				raise ValueError(f"module '{mid}' provided by multiple packages: '{prev}' and '{pkg.path}'")
			exports = mod.interface.get("exports")
			if not isinstance(exports, dict):
				out[mid] = {"values": set(), "types": {"structs": set(), "variants": set(), "exceptions": set()}, "consts": set()}
				continue
			values = exports.get("values")
			types = exports.get("types")
			consts = exports.get("consts")
			reexports = mod.interface.get("reexports", {}) if isinstance(mod.interface, dict) else {}
			type_structs: list[str] = []
			type_variants: list[str] = []
			type_excs: list[str] = []
			if isinstance(types, dict):
				if isinstance(types.get("structs"), list):
					type_structs = [str(x) for x in types.get("structs") if isinstance(x, str)]
				if isinstance(types.get("variants"), list):
					type_variants = [str(x) for x in types.get("variants") if isinstance(x, str)]
				if isinstance(types.get("exceptions"), list):
					type_excs = [str(x) for x in types.get("exceptions") if isinstance(x, str)]
			out[mid] = {
				"values": set(values) if isinstance(values, list) else set(),
				"types": {"structs": set(type_structs), "variants": set(type_variants), "exceptions": set(type_excs)},
				"consts": set(consts) if isinstance(consts, list) else set(),
				"reexports": reexports if isinstance(reexports, dict) else {},
			}
	return out
