# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Format-level validation for DMIR-PKG v0 container payloads.

These checks are **trust-agnostic**: they validate package bytes
against the format/interface invariants of `dmir_pkg_v0` and the
shape of `interface` / `payload` JSON inside each module.  No
signature or trust-store lookup happens here.

Both `provider_v0` (v0 trust path) and `provider_v1` (v1 trust
path) call into these helpers after their respective trust gates
accept the package.  When the v0 modules are deleted in a later
sub-boundary, this file remains the canonical home for these
checks.

Helpers:
  - `_validate_type_expr_obj` — recursive shape check for serialized
    TypeExpr fragments embedded in trait_metadata / impl_headers.
  - `_validate_trait_expr_obj` — recursive shape check for serialized
    trait expressions.
  - `validate_package_interfaces` — top-level: every module's
    `interface` must agree with its `payload`, exports must have
    signature metadata with the exported-entrypoint flag, consts/
    variants/exceptions/trait_metadata/impl_headers must round-trip.
  - `collect_external_exports` — aggregates export sets for the
    workspace parser.

These functions raise `ValueError` on malformed packages; callers
surface the error to the user.
"""

from __future__ import annotations

from pathlib import Path

from lang.driftc.core.function_id import function_id_from_obj, function_symbol
from lang.driftc.packages.dmir_pkg_v0 import LoadedPackage
from lang.driftc.traits.world import TraitKey


def _validate_type_expr_obj(obj: object, *, context: str) -> None:
	def _err(msg: str) -> ValueError:
		return ValueError(f"{context}: {msg}")

	if obj is None:
		return
	if not isinstance(obj, dict):
		raise _err("type expr must be an object")
	if "param" in obj:
		param = obj.get("param")
		if not isinstance(param, str) or not param:
			raise _err("type expr param must be a non-empty string")
		return
	name = obj.get("name")
	if not isinstance(name, str) or not name:
		raise _err("type expr name must be a non-empty string")
	module_id = obj.get("module")
	if module_id is not None and not isinstance(module_id, str):
		raise _err("type expr module must be a string")
	args = obj.get("args")
	if args is None:
		return
	if not isinstance(args, list):
		raise _err("type expr args must be a list")
	for idx, arg in enumerate(args):
		_validate_type_expr_obj(arg, context=f"{context}.args[{idx}]")


def _validate_trait_expr_obj(obj: object, *, context: str) -> None:
	def _err(msg: str) -> ValueError:
		return ValueError(f"{context}: {msg}")

	if obj is None:
		return
	if not isinstance(obj, dict):
		raise _err("trait expr must be an object")
	kind = obj.get("kind")
	if not isinstance(kind, str):
		raise _err("trait expr kind must be a string")
	if kind == "is":
		subject = obj.get("subject")
		if not isinstance(subject, str) or not subject:
			raise _err("trait expr subject must be a non-empty string")
		_validate_type_expr_obj(obj.get("trait"), context=f"{context}.trait")
		return
	if kind in {"and", "or"}:
		_validate_trait_expr_obj(obj.get("left"), context=f"{context}.left")
		_validate_trait_expr_obj(obj.get("right"), context=f"{context}.right")
		return
	if kind == "not":
		_validate_trait_expr_obj(obj.get("expr"), context=f"{context}.expr")
		return
	raise _err(f"unknown trait expr kind '{kind}'")


def validate_package_interfaces(pkg: LoadedPackage) -> None:
	"""Validate module interfaces against payload metadata.

	Pinned ABI boundary rule: any exported value must have a
	corresponding payload signature entry with
	`is_exported_entrypoint == True`.  This is a package-consumption
	guardrail: it rejects malformed/inconsistent packages early,
	before imports are resolved or IR is embedded.
	"""

	def _err(msg: str) -> ValueError:
		return ValueError(msg)

	pkg_id = pkg.manifest.get("package_id")
	if not isinstance(pkg_id, str) or not pkg_id:
		raise _err("package manifest missing package_id")

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
		traits = exports.get("traits", [])
		consts = exports.get("consts", [])
		if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
			raise _err(f"module '{mid}' interface exports.values must be a list of strings")
		if not isinstance(types, dict):
			raise _err(f"module '{mid}' interface exports.types must be an object")
		type_structs = types.get("structs")
		type_variants = types.get("variants")
		type_excs = types.get("exceptions")
		type_interfaces = types.get("interfaces")
		if not isinstance(type_structs, list) or not all(isinstance(t, str) for t in type_structs):
			raise _err(f"module '{mid}' interface exports.types.structs must be a list of strings")
		if not isinstance(type_variants, list) or not all(isinstance(t, str) for t in type_variants):
			raise _err(f"module '{mid}' interface exports.types.variants must be a list of strings")
		if not isinstance(type_excs, list) or not all(isinstance(t, str) for t in type_excs):
			raise _err(f"module '{mid}' interface exports.types.exceptions must be a list of strings")
		if not isinstance(type_interfaces, list) or not all(isinstance(t, str) for t in type_interfaces):
			raise _err(f"module '{mid}' interface exports.types.interfaces must be a list of strings")
		if not isinstance(traits, list) or not all(isinstance(t, str) for t in traits):
			raise _err(f"module '{mid}' interface exports.traits must be a list of strings")
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
		if len(set(type_interfaces)) != len(type_interfaces):
			raise _err(f"module '{mid}' interface exports.types.interfaces contains duplicates")
		if len(set(traits)) != len(traits):
			raise _err(f"module '{mid}' interface exports.traits contains duplicates")
		type_union = set(type_structs) | set(type_variants) | set(type_excs) | set(type_interfaces)
		if len(type_union) != (len(type_structs) + len(type_variants) + len(type_excs) + len(type_interfaces)):
			raise _err(f"module '{mid}' interface exports.types contains overlapping names across kinds")
		if len(set(consts)) != len(consts):
			raise _err(f"module '{mid}' interface exports.consts contains duplicates")

		payload_exports = mod.payload.get("exports")
		if not isinstance(payload_exports, dict):
			raise _err(f"module '{mid}' payload missing exports")
		payload_values = payload_exports.get("values")
		payload_types = payload_exports.get("types")
		payload_traits = payload_exports.get("traits")
		payload_consts = payload_exports.get("consts", [])
		if not isinstance(payload_values, list) or not isinstance(payload_consts, list) or not isinstance(payload_types, dict):
			raise _err(f"module '{mid}' payload exports must include values/types/consts")
		if not isinstance(payload_traits, list):
			raise _err(f"module '{mid}' payload exports must include traits list")
		payload_structs = payload_types.get("structs")
		payload_variants = payload_types.get("variants")
		payload_excs = payload_types.get("exceptions")
		payload_interfaces = payload_types.get("interfaces")
		if not isinstance(payload_structs, list) or not isinstance(payload_variants, list) or not isinstance(payload_excs, list) or not isinstance(payload_interfaces, list):
			raise _err(f"module '{mid}' payload exports.types must include structs/variants/exceptions/interfaces lists")
		payload_type_union = set(payload_structs) | set(payload_variants) | set(payload_excs) | set(payload_interfaces)
		if len(payload_type_union) != (len(payload_structs) + len(payload_variants) + len(payload_excs) + len(payload_interfaces)):
			raise _err(f"module '{mid}' payload exports.types contains overlapping names across kinds")
		if (
			sorted(payload_values) != sorted(values)
			or sorted(payload_structs) != sorted(type_structs)
			or sorted(payload_variants) != sorted(type_variants)
			or sorted(payload_excs) != sorted(type_excs)
			or sorted(payload_interfaces) != sorted(type_interfaces)
			or sorted(payload_traits) != sorted(traits)
			or sorted(payload_consts) != sorted(consts)
		):
			raise _err(f"module '{mid}' interface exports do not match payload exports")

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
		traits_reexp = iface_reexp.get("traits", {})
		if types_reexp is None:
			types_reexp = {}
		if consts_reexp is None:
			consts_reexp = {}
		if traits_reexp is None:
			traits_reexp = {}
		if not isinstance(types_reexp, dict):
			raise _err(f"module '{mid}' reexports.types must be an object")
		if not isinstance(consts_reexp, dict):
			raise _err(f"module '{mid}' reexports.consts must be an object")
		if not isinstance(traits_reexp, dict):
			raise _err(f"module '{mid}' reexports.traits must be an object")
		for kind, exported in (("structs", type_structs), ("variants", type_variants), ("exceptions", type_excs), ("interfaces", type_interfaces)):
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
		for local_name, target in traits_reexp.items():
			if not isinstance(local_name, str):
				raise _err(f"module '{mid}' reexports.traits has non-string key")
			if local_name not in traits:
				raise _err(f"module '{mid}' reexports.traits contains non-exported name '{local_name}'")
			if not isinstance(target, dict):
				raise _err(f"module '{mid}' reexports.traits target for '{local_name}' must be an object")
			tmod = target.get("module")
			tname = target.get("name")
			if not isinstance(tmod, str) or not tmod:
				raise _err(f"module '{mid}' reexports.traits target for '{local_name}' missing module")
			if not isinstance(tname, str) or not tname:
				raise _err(f"module '{mid}' reexports.traits target for '{local_name}' missing name")

		iface_sigs = mod.interface.get("signatures")
		if not isinstance(iface_sigs, dict):
			raise _err(f"module '{mid}' interface missing signatures table")
		payload_sigs = mod.payload.get("signatures")
		if not isinstance(payload_sigs, dict):
			raise _err(f"module '{mid}' payload missing signatures table")

		iface_trait_meta = mod.interface.get("trait_metadata")
		payload_trait_meta = mod.payload.get("trait_metadata")
		if iface_trait_meta is not None or payload_trait_meta is not None:
			if not isinstance(iface_trait_meta, list):
				raise _err(f"module '{mid}' interface trait_metadata must be a list")
			if not isinstance(payload_trait_meta, list):
				raise _err(f"module '{mid}' payload trait_metadata must be a list")
			if iface_trait_meta != payload_trait_meta:
				raise _err(f"module '{mid}' interface trait_metadata does not match payload")
			seen_traits: set[str] = set()
			seen_methods: set[tuple[str, str]] = set()
			for idx, entry in enumerate(iface_trait_meta):
				if not isinstance(entry, dict):
					raise _err(f"module '{mid}' trait_metadata[{idx}] must be an object")
				trait_id_obj = entry.get("trait_id")
				if not isinstance(trait_id_obj, dict):
					raise _err(f"module '{mid}' trait_metadata[{idx}] missing trait_id")
				trait_pkg = trait_id_obj.get("package_id")
				trait_mod = trait_id_obj.get("module")
				trait_name = trait_id_obj.get("name")
				if not isinstance(trait_pkg, str) or not trait_pkg:
					raise _err(f"module '{mid}' trait_metadata[{idx}] invalid trait_id.package_id")
				if not isinstance(trait_mod, str) or not trait_mod:
					raise _err(f"module '{mid}' trait_metadata[{idx}] invalid trait_id.module")
				if not isinstance(trait_name, str) or not trait_name:
					raise _err(f"module '{mid}' trait_metadata[{idx}] invalid trait_id.name")
				if trait_pkg != pkg_id:
					raise _err(f"module '{mid}' trait_metadata[{idx}] trait_id package_id mismatch")
				if trait_mod != mid:
					raise _err(f"module '{mid}' trait_metadata[{idx}] trait_id module mismatch")
				name = entry.get("name")
				if not isinstance(name, str) or not name:
					raise _err(f"module '{mid}' trait_metadata[{idx}] missing name")
				if name != trait_name:
					raise _err(f"module '{mid}' trait_metadata[{idx}] trait_id name mismatch")
				if name not in traits and name not in type_interfaces:
					raise _err(f"module '{mid}' trait_metadata[{idx}] refers to non-exported trait '{name}'")
				if name in seen_traits:
					raise _err(f"module '{mid}' trait_metadata contains duplicate trait '{name}'")
				seen_traits.add(name)
				methods = entry.get("methods")
				if not isinstance(methods, list):
					raise _err(f"module '{mid}' trait_metadata[{idx}] methods must be a list")
				_validate_trait_expr_obj(entry.get("require"), context=f"trait_metadata[{idx}].require")
				for midx, method in enumerate(methods):
					if not isinstance(method, dict):
						raise _err(f"module '{mid}' trait_metadata[{idx}].methods[{midx}] must be an object")
					mname = method.get("name")
					if not isinstance(mname, str) or not mname:
						raise _err(f"module '{mid}' trait_metadata[{idx}].methods[{midx}] missing name")
					type_params = method.get("type_params")
					if type_params is not None:
						if not isinstance(type_params, list):
							raise _err(
								f"module '{mid}' trait_metadata[{idx}].methods[{midx}].type_params must be a list"
							)
						if not all(isinstance(p, str) and p for p in type_params):
							raise _err(
								f"module '{mid}' trait_metadata[{idx}].methods[{midx}].type_params must be strings"
							)
					key = (name, mname)
					if key in seen_methods:
						raise _err(f"module '{mid}' trait_metadata duplicate method '{mname}' in trait '{name}'")
					seen_methods.add(key)
					params = method.get("params")
					if not isinstance(params, list):
						raise _err(f"module '{mid}' trait_metadata[{idx}].methods[{midx}] params must be a list")
					for pidx, param in enumerate(params):
						if not isinstance(param, dict):
							raise _err(
								f"module '{mid}' trait_metadata[{idx}].methods[{midx}].params[{pidx}] must be an object"
							)
						pname = param.get("name")
						if not isinstance(pname, str) or not pname:
							raise _err(
								f"module '{mid}' trait_metadata[{idx}].methods[{midx}].params[{pidx}] missing name"
							)
						_validate_type_expr_obj(
							param.get("type"),
							context=f"trait_metadata[{idx}].methods[{midx}].params[{pidx}].type",
						)
					_validate_type_expr_obj(
						method.get("return_type"),
						context=f"trait_metadata[{idx}].methods[{midx}].return_type",
					)
					_validate_trait_expr_obj(
						method.get("require"),
						context=f"trait_metadata[{idx}].methods[{midx}].require",
					)

		iface_impl_headers = mod.interface.get("impl_headers")
		payload_impl_headers = mod.payload.get("impl_headers")
		if iface_impl_headers is not None or payload_impl_headers is not None:
			if not isinstance(iface_impl_headers, list):
				raise _err(f"module '{mid}' interface impl_headers must be a list")
			if not isinstance(payload_impl_headers, list):
				raise _err(f"module '{mid}' payload impl_headers must be a list")
			if iface_impl_headers != payload_impl_headers:
				raise _err(f"module '{mid}' interface impl_headers does not match payload")
			seen_impls: set[tuple[str, int]] = set()
			for idx, impl in enumerate(iface_impl_headers):
				if not isinstance(impl, dict):
					raise _err(f"module '{mid}' impl_headers[{idx}] must be an object")
				impl_id = impl.get("impl_id")
				def_module = impl.get("def_module")
				if not isinstance(impl_id, int):
					raise _err(f"module '{mid}' impl_headers[{idx}] impl_id must be an int")
				if not isinstance(def_module, str) or not def_module:
					raise _err(f"module '{mid}' impl_headers[{idx}] def_module must be a string")
				decl_fp = impl.get("decl_fingerprint")
				if not isinstance(decl_fp, str) or not decl_fp:
					raise _err(f"module '{mid}' impl_headers[{idx}] missing decl_fingerprint")
				key = (def_module, impl_id)
				if key in seen_impls:
					raise _err(f"module '{mid}' impl_headers contains duplicate impl_id {impl_id} for '{def_module}'")
				seen_impls.add(key)
				trait_obj = impl.get("trait")
				if trait_obj is not None:
					if not isinstance(trait_obj, dict):
						raise _err(f"module '{mid}' impl_headers[{idx}] trait must be an object")
					tpkg = trait_obj.get("package_id")
					tmod = trait_obj.get("module")
					tname = trait_obj.get("name")
					if not isinstance(tpkg, str) or not tpkg:
						raise _err(f"module '{mid}' impl_headers[{idx}] trait must include package_id")
					if not isinstance(tmod, str) or not tmod or not isinstance(tname, str) or not tname:
						raise _err(f"module '{mid}' impl_headers[{idx}] trait must include module/name")
				type_params = impl.get("type_params")
				if type_params is not None and not isinstance(type_params, list):
					raise _err(f"module '{mid}' impl_headers[{idx}] type_params must be a list")
				_validate_type_expr_obj(impl.get("target"), context=f"impl_headers[{idx}].target")
				_validate_trait_expr_obj(impl.get("require"), context=f"impl_headers[{idx}].require")
				methods = impl.get("methods")
				if not isinstance(methods, list):
					raise _err(f"module '{mid}' impl_headers[{idx}] methods must be a list")
				for midx, method in enumerate(methods):
					if not isinstance(method, dict):
						raise _err(f"module '{mid}' impl_headers[{idx}].methods[{midx}] must be an object")
					mname = method.get("name")
					if not isinstance(mname, str) or not mname:
						raise _err(f"module '{mid}' impl_headers[{idx}].methods[{midx}] missing name")
					fn_symbol = method.get("fn_symbol")
					if not isinstance(fn_symbol, str) or not fn_symbol:
						raise _err(f"module '{mid}' impl_headers[{idx}].methods[{midx}] missing fn_symbol")
					fn_id_obj = method.get("fn_id")
					if not isinstance(fn_id_obj, dict):
						raise _err(f"module '{mid}' impl_headers[{idx}].methods[{midx}] missing fn_id")
					fn_id = function_id_from_obj(fn_id_obj)
					if fn_id is None:
						raise _err(f"module '{mid}' impl_headers[{idx}].methods[{midx}] invalid fn_id")
					if fn_symbol != function_symbol(fn_id):
						raise _err(
							f"module '{mid}' impl_headers[{idx}].methods[{midx}] fn_symbol mismatch"
						)
					if fn_symbol not in payload_sigs:
						raise _err(
							f"module '{mid}' impl_headers method '{mname}' missing signature entry '{fn_symbol}'"
						)

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

		extra = set(iface_sigs.keys()) - {f"{mid}::{v}" for v in values}
		if extra:
			raise _err(f"module '{mid}' interface contains non-export signature entries")


def collect_external_exports(packages: list[LoadedPackage]) -> dict[str, dict[str, object]]:
	"""Collect module export sets from loaded packages.

	Returns:
	  module_id -> {
	    "values": set[str],
	    "types": {"structs": set[str], "variants": set[str], "exceptions": set[str], "interfaces": set[str]},
	    "traits": set[str],
	    "consts": set[str],
	    "reexports": {"types": {"structs": dict, "variants": dict, "exceptions": dict, "interfaces": dict}, "consts": dict},
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
				out[mid] = {
					"values": set(),
					"types": {"structs": set(), "variants": set(), "exceptions": set(), "interfaces": set()},
					"traits": set(),
					"consts": set(),
				}
				continue
			values = exports.get("values")
			types = exports.get("types")
			traits = exports.get("traits")
			consts = exports.get("consts")
			reexports = mod.interface.get("reexports", {}) if isinstance(mod.interface, dict) else {}
			type_structs: list[str] = []
			type_variants: list[str] = []
			type_excs: list[str] = []
			type_interfaces: list[str] = []
			type_aliases: list[str] = []
			if isinstance(types, dict):
				if isinstance(types.get("structs"), list):
					type_structs = [str(x) for x in types.get("structs") if isinstance(x, str)]
				if isinstance(types.get("variants"), list):
					type_variants = [str(x) for x in types.get("variants") if isinstance(x, str)]
				if isinstance(types.get("exceptions"), list):
					type_excs = [str(x) for x in types.get("exceptions") if isinstance(x, str)]
				if isinstance(types.get("interfaces"), list):
					type_interfaces = [str(x) for x in types.get("interfaces") if isinstance(x, str)]
				if isinstance(types.get("aliases"), list):
					type_aliases = [str(x) for x in types.get("aliases") if isinstance(x, str)]
			_raw_ts = mod.interface.get("trait_scope") if isinstance(mod.interface, dict) else None
			_trait_scope_keys: list[TraitKey] | None = None
			if isinstance(_raw_ts, list):
				_trait_scope_keys = []
				for _entry in _raw_ts:
					if isinstance(_entry, dict) and "name" in _entry:
						_trait_scope_keys.append(TraitKey(package_id=_entry.get("package_id"), module=_entry.get("module"), name=_entry["name"]))
			out[mid] = {
				"values": set(values) if isinstance(values, list) else set(),
				"types": {
					"structs": set(type_structs),
					"variants": set(type_variants),
					"exceptions": set(type_excs),
					"interfaces": set(type_interfaces),
					"aliases": set(type_aliases),
				},
				"traits": set(traits) if isinstance(traits, list) else set(),
				"consts": set(consts) if isinstance(consts, list) else set(),
				"reexports": reexports if isinstance(reexports, dict) else {},
			}
			if _trait_scope_keys is not None:
				out[mid]["trait_scope"] = _trait_scope_keys
	return out
