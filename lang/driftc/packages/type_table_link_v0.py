# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Link-time TypeTable unification for package consumption (v0).

Goal
----
When consuming packages, we must not require identical TypeId assignment across
independently-produced artifacts. Instead, we:

1) Merge imported type definitions into the host `TypeTable` deterministically.
2) Build a `pkg_type_id -> host_type_id` mapping.
3) Remap all TypeId references in:
   - package signatures
   - package MIR nodes
   - schema tables (struct/exception/variant schemas)

This makes package consumption scale without pinning everything to a single
`type_table_fingerprint`.

Pinned rules (MVP)
------------------
- Builtins are toolchain-owned and must unify to the host builtins:
  Int/Uint/Uint64/Bool/Float/String/Byte/Void/Error/DiagnosticValue/Unknown.
- Packages may import new user-defined nominal types into the host TypeTable
  as long as there are no semantic collisions.
- Collisions are hard errors:
  - same nominal identity but different schema
  - attempts to redefine builtins / reserved namespaces
- Merge is deterministic: inputs with the same content yield the same host
  TypeIds independent of package discovery order.

Notes
-----
This module operates on the *encoded* type table object stored in provisional
package payloads (`payload["type_table"]`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

_DEBUG_LINK = os.environ.get("DRIFT_DEBUG_TYPEID_DIVERGENCE") == "1"

from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.function_id import FunctionId, function_id_from_obj
from lang.driftc.core.types_core import (
	NominalKey,
	TypeParamId,
	TypeDef,
	TypeId,
	TypeKind,
	TypeTable,
	InterfaceMethodSchema,
	InterfaceParamSchema,
	StructFieldSchema,
	VariantArmSchema,
	VariantFieldSchema,
	VariantSchema,
)


@dataclass(frozen=True)
class DecodedTypeDef:
	kind: TypeKind
	name: str
	param_types: list[TypeId]
	module_id: str | None
	ref_mut: bool | None
	fn_throws: bool
	field_names: list[str] | None
	type_param_id: TypeParamId | None


@dataclass(frozen=True)
class DecodedTypeTable:
	package_id: str
	defs: dict[TypeId, DecodedTypeDef]
	struct_schemas: dict[NominalKey, tuple[list[StructFieldSchema], list[str], TypeId | None]]
	struct_instances: dict[TypeId, tuple[TypeId, list[TypeId]]]
	interface_schemas: dict[NominalKey, tuple[list[str], list["InterfaceMethodSchema"], list[GenericTypeExpr], TypeId | None]]
	interface_instances: dict[TypeId, tuple[TypeId, list[TypeId]]]
	exception_schemas: dict[str, tuple[str, list[str]]]
	variant_schemas: dict[TypeId, VariantSchema]
	provided_nominals: set[tuple[TypeKind, str, str]]
	type_aliases: list[tuple[str, str, list[str], GenericTypeExpr]]
	# Pre-computed canonical keys from the producer. Required — the linker
	# rejects packages without canonical_keys. None only during decode before
	# the field is populated.
	canonical_keys: dict[TypeId, object] | None = None


def _decode_kind(name: str) -> TypeKind:
	try:
		return TypeKind[name]
	except KeyError as err:
		raise ValueError(f"unknown TypeKind '{name}' in package type table") from err


def _decode_generic_type_expr(obj: Any) -> GenericTypeExpr:
	"""
	Decode a GenericTypeExpr as encoded by `provisional_dmir_v0.encode_type_table`.
	"""
	if not isinstance(obj, dict):
		raise ValueError("invalid GenericTypeExpr encoding")
	name = obj.get("name")
	if not isinstance(name, str):
		raise ValueError("invalid GenericTypeExpr.name")
	module_id = obj.get("module_id")
	if module_id is not None and not isinstance(module_id, str):
		raise ValueError("invalid GenericTypeExpr.module_id")
	args_obj = obj.get("args")
	args: list[GenericTypeExpr] = []
	if args_obj is not None:
		if not isinstance(args_obj, list):
			raise ValueError("invalid GenericTypeExpr.args")
		args = [_decode_generic_type_expr(a) for a in args_obj]
	param_index = obj.get("param_index")
	if param_index is not None and not isinstance(param_index, int):
		raise ValueError("invalid GenericTypeExpr.param_index")
	if not name and param_index is None:
		raise ValueError("invalid GenericTypeExpr.name")
	if name != "fn":
		fn_throws = False
	else:
		if "fn_throws" in obj:
			fn_throws = obj.get("fn_throws")
			if fn_throws is None or not isinstance(fn_throws, bool):
				raise ValueError("invalid GenericTypeExpr.fn_throws")
		else:
			fn_throws = True
	return GenericTypeExpr(name=name, args=args, param_index=param_index, module_id=module_id, fn_throws=fn_throws)


def _decode_alias_target(obj: Any) -> GenericTypeExpr:
	"""Decode a type alias target expression (name-based type params, no param_index)."""
	if not isinstance(obj, dict):
		raise ValueError("invalid type alias target encoding")
	name = obj.get("name", "")
	if not isinstance(name, str):
		raise ValueError("invalid type alias target name")
	module_id = obj.get("module_id")
	if module_id is not None and not isinstance(module_id, str):
		raise ValueError("invalid type alias target module_id")
	args_obj = obj.get("args")
	args: list[GenericTypeExpr] = []
	if args_obj is not None:
		if not isinstance(args_obj, list):
			raise ValueError("invalid type alias target args")
		args = [_decode_alias_target(a) for a in args_obj]
	fn_throws = name == "fn"
	return GenericTypeExpr(name=name, args=args, param_index=None, module_id=module_id, fn_throws=fn_throws)


def decode_type_table_obj(obj: Mapping[str, Any]) -> DecodedTypeTable:
	"""
	Decode a `type_table` JSON object from a package payload.
	"""
	pkg_id = obj.get("package_id")
	if not isinstance(pkg_id, str) or not pkg_id:
		raise ValueError("package type_table missing package_id")
	defs_obj = obj.get("defs")
	if not isinstance(defs_obj, dict):
		raise ValueError("package type_table missing defs")
	defs: dict[TypeId, DecodedTypeDef] = {}
	for tid_s, td_obj in defs_obj.items():
		try:
			tid = int(tid_s)
		except Exception as err:
			raise ValueError("invalid TypeId key in package type_table.defs") from err
		if not isinstance(td_obj, dict):
			raise ValueError("invalid type_table.defs entry")
		kind_s = td_obj.get("kind")
		name = td_obj.get("name")
		param_types = td_obj.get("param_types")
		module_id = td_obj.get("module_id")
		type_param_obj = td_obj.get("type_param_id")
		ref_mut = td_obj.get("ref_mut")
		fn_throws = td_obj.get("fn_throws", True)
		field_names = td_obj.get("field_names")
		if not isinstance(kind_s, str) or not isinstance(name, str) or not isinstance(param_types, list):
			raise ValueError("invalid type_table.defs entry fields")
		if module_id is not None and not isinstance(module_id, str):
			raise ValueError("invalid type_table.defs module_id")
		if module_id == "":
			raise ValueError("invalid type_table.defs module_id")
		if ref_mut is not None and not isinstance(ref_mut, bool):
			raise ValueError("invalid type_table.defs ref_mut")
		type_param_id: TypeParamId | None = None
		if type_param_obj is not None:
			if not isinstance(type_param_obj, dict):
				raise ValueError("invalid type_table.defs type_param_id")
			owner_obj = type_param_obj.get("owner")
			index_obj = type_param_obj.get("index")
			owner = function_id_from_obj(owner_obj)
			if owner is None or not isinstance(index_obj, int):
				raise ValueError("invalid type_table.defs type_param_id")
			type_param_id = TypeParamId(owner=owner, index=index_obj)
		if kind_s == "FUNCTION":
			if "fn_throws" in td_obj:
				fn_throws = td_obj.get("fn_throws")
				if fn_throws is None or not isinstance(fn_throws, bool):
					raise ValueError("invalid type_table.defs fn_throws")
			else:
				fn_throws = True
		else:
			fn_throws = False
		if field_names is not None and not isinstance(field_names, list):
			raise ValueError("invalid type_table.defs field_names")
		if kind_s == "TYPEVAR" and type_param_id is None:
			raise ValueError("invalid type_table.defs type_param_id")
		defs[tid] = DecodedTypeDef(
			kind=_decode_kind(kind_s),
			name=name,
			param_types=[int(x) for x in param_types],
			module_id=module_id,
			ref_mut=ref_mut,
			fn_throws=fn_throws,
			field_names=[str(x) for x in field_names] if field_names is not None else None,
			type_param_id=type_param_id,
		)

	struct_schemas_obj = obj.get("struct_schemas")
	struct_schemas: dict[NominalKey, tuple[list[StructFieldSchema], list[str], TypeId | None]] = {}
	if struct_schemas_obj is not None:
		if not isinstance(struct_schemas_obj, list):
			raise ValueError("invalid type_table.struct_schemas")
		for entry in struct_schemas_obj:
			if not isinstance(entry, dict):
				raise ValueError("invalid struct_schemas entry")
			base_id_obj = entry.get("base_id")
			base_id: TypeId | None = None
			if base_id_obj is None:
				raise ValueError("invalid struct_schemas entry base_id")
			if not isinstance(base_id_obj, int):
				raise ValueError("invalid struct_schemas entry base_id")
			base_id = base_id_obj
			type_id_obj = entry.get("type_id")
			if not isinstance(type_id_obj, dict):
				raise ValueError("invalid struct_schemas entry type_id")
			type_pkg = type_id_obj.get("package_id")
			type_mod = type_id_obj.get("module")
			type_name = type_id_obj.get("name")
			if not isinstance(type_pkg, str) or not type_pkg:
				raise ValueError("invalid struct_schemas entry type_id.package_id")
			if not isinstance(type_mod, str) or not type_mod:
				raise ValueError("invalid struct_schemas entry type_id.module")
			if not isinstance(type_name, str) or not type_name:
				raise ValueError("invalid struct_schemas entry type_id.name")
			module_id = entry.get("module_id")
			name = entry.get("name")
			fields = entry.get("fields")
			type_params_obj = entry.get("type_params")
			if not isinstance(module_id, str) or not isinstance(name, str) or not isinstance(fields, list):
				raise ValueError("invalid struct_schemas entry fields")
			if module_id != type_mod or name != type_name:
				raise ValueError("struct_schemas entry does not match type_id")
			field_schemas: list[StructFieldSchema] = []
			for fobj in fields:
				if not isinstance(fobj, dict):
					raise ValueError("invalid struct schema field entry")
				fname = fobj.get("name")
				fty = fobj.get("type_expr")
				is_pub = fobj.get("is_pub", False)
				if not isinstance(fname, str):
					raise ValueError("invalid struct schema field name")
				if not isinstance(is_pub, bool):
					raise ValueError("invalid struct schema field is_pub")
				field_schemas.append(
					StructFieldSchema(
						name=fname,
						type_expr=_decode_generic_type_expr(fty),
						is_pub=is_pub,
					)
				)
			type_params: list[str] = []
			if type_params_obj is not None:
				if not isinstance(type_params_obj, list):
					raise ValueError("invalid struct_schemas entry type_params")
				for tp in type_params_obj:
					if not isinstance(tp, str):
						raise ValueError("invalid struct_schemas entry type_params")
					type_params.append(tp)
			key = NominalKey(package_id=type_pkg, module_id=type_mod, name=type_name, kind=TypeKind.STRUCT)
			struct_schemas[key] = (field_schemas, type_params, base_id)
	interface_schemas_obj = obj.get("interface_schemas")
	interface_schemas: dict[NominalKey, tuple[list[str], list[InterfaceMethodSchema], list[GenericTypeExpr], TypeId | None]] = {}
	if interface_schemas_obj is not None:
		if not isinstance(interface_schemas_obj, list):
			raise ValueError("invalid type_table.interface_schemas")
		for entry in interface_schemas_obj:
			if not isinstance(entry, dict):
				raise ValueError("invalid interface_schemas entry")
			base_id_obj = entry.get("base_id")
			if not isinstance(base_id_obj, int):
				raise ValueError("invalid interface_schemas entry base_id")
			base_id = base_id_obj
			type_id_obj = entry.get("type_id")
			if not isinstance(type_id_obj, dict):
				raise ValueError("invalid interface_schemas entry type_id")
			type_pkg = type_id_obj.get("package_id")
			type_mod = type_id_obj.get("module")
			type_name = type_id_obj.get("name")
			if not isinstance(type_pkg, str) or not type_pkg:
				raise ValueError("invalid interface_schemas entry type_id.package_id")
			if not isinstance(type_mod, str) or not type_mod:
				raise ValueError("invalid interface_schemas entry type_id.module")
			if not isinstance(type_name, str) or not type_name:
				raise ValueError("invalid interface_schemas entry type_id.name")
			module_id = entry.get("module_id")
			name = entry.get("name")
			type_params_obj = entry.get("type_params")
			parents_obj = entry.get("parents")
			methods_obj = entry.get("methods")
			if not isinstance(module_id, str) or not isinstance(name, str):
				raise ValueError("invalid interface_schemas entry fields")
			if module_id != type_mod or name != type_name:
				raise ValueError("interface_schemas entry does not match type_id")
			base_def = defs.get(base_id)
			# Defs are slimmed to MIR-reachable types; base_id may not be
			# present. Identity is validated by the linker via canonical_keys.
			if base_def is not None and base_def.kind is not TypeKind.INTERFACE:
				raise ValueError("interface_schemas entry base_id is not an INTERFACE TypeDef")
			type_params: list[str] = []
			if type_params_obj is not None:
				if not isinstance(type_params_obj, list):
					raise ValueError("invalid interface_schemas entry type_params")
				for tp in type_params_obj:
					if not isinstance(tp, str):
						raise ValueError("invalid interface_schemas entry type_params")
					type_params.append(tp)
			methods: list[InterfaceMethodSchema] = []
			if methods_obj is not None:
				if not isinstance(methods_obj, list):
					raise ValueError("invalid interface_schemas entry methods")
				for mobj in methods_obj:
					if not isinstance(mobj, dict):
						raise ValueError("invalid interface method schema")
					mname = mobj.get("name")
					if not isinstance(mname, str):
						raise ValueError("invalid interface method name")
					m_type_params_obj = mobj.get("type_params")
					m_type_params: list[str] = []
					if m_type_params_obj is not None:
						if not isinstance(m_type_params_obj, list):
							raise ValueError("invalid interface method type_params")
						for tp in m_type_params_obj:
							if not isinstance(tp, str):
								raise ValueError("invalid interface method type_params")
							m_type_params.append(tp)
					params_obj = mobj.get("params")
					if not isinstance(params_obj, list):
						raise ValueError("invalid interface method params")
					params: list[InterfaceParamSchema] = []
					for pobj in params_obj:
						if not isinstance(pobj, dict):
							raise ValueError("invalid interface method param")
						pname = pobj.get("name")
						pexpr = pobj.get("type_expr")
						if not isinstance(pname, str):
							raise ValueError("invalid interface method param name")
						params.append(InterfaceParamSchema(name=pname, type_expr=_decode_generic_type_expr(pexpr)))
					ret_expr = mobj.get("return_type")
					declared_nothrow = bool(mobj.get("declared_nothrow", False))
					is_unsafe = bool(mobj.get("is_unsafe", False))
					# Phase 3 of terminal-`throws`: round-trip both flags. Old
					# packages built before Phase 3 do not include these
					# fields; default to False so consumers can still load
					# them (forward compat).
					declared_throws = bool(mobj.get("declared_throws", False))
					declared_terminal_throws = bool(mobj.get("declared_terminal_throws", False))
					# Phase 1 v3: a `null` return_type indicates the
					# bare-terminal form. The decoded schema honestly carries
					# `return_type=None` rather than synthesizing a Void.
					decoded_return_type = (
						_decode_generic_type_expr(ret_expr) if ret_expr is not None else None
					)
					methods.append(
						InterfaceMethodSchema(
							name=mname,
							params=params,
							return_type=decoded_return_type,
							type_params=m_type_params,
							declared_nothrow=declared_nothrow,
							is_unsafe=is_unsafe,
							declared_throws=declared_throws,
							declared_terminal_throws=declared_terminal_throws,
						)
					)
			parents: list[GenericTypeExpr] = []
			if parents_obj is not None:
				if not isinstance(parents_obj, list):
					raise ValueError("invalid interface_schemas entry parents")
				for pobj in parents_obj:
					parents.append(_decode_generic_type_expr(pobj))
			key = NominalKey(package_id=type_pkg, module_id=type_mod, name=type_name, kind=TypeKind.INTERFACE)
			interface_schemas[key] = (type_params, methods, parents, base_id)

	exc_schemas_obj = obj.get("exception_schemas")
	exception_schemas: dict[str, tuple[str, list[str]]] = {}
	if exc_schemas_obj is not None:
		if not isinstance(exc_schemas_obj, dict):
			raise ValueError("invalid type_table.exception_schemas")
		for k, v in exc_schemas_obj.items():
			if not isinstance(k, str) or not isinstance(v, list) or len(v) != 2:
				raise ValueError("invalid exception_schemas entry")
			fqn = str(v[0])
			if k != fqn:
				raise ValueError("invalid exception_schemas key (must match event fqn)")
			fields = v[1]
			if not isinstance(fields, list):
				raise ValueError("invalid exception_schemas field list")
			exception_schemas[fqn] = (fqn, [str(x) for x in fields])

	variant_schemas_obj = obj.get("variant_schemas")
	variant_schemas: dict[TypeId, VariantSchema] = {}
	if variant_schemas_obj is not None:
		if not isinstance(variant_schemas_obj, dict):
			raise ValueError("invalid type_table.variant_schemas")
		for base_id_s, schema_obj in variant_schemas_obj.items():
			base_id = int(base_id_s)
			base_def = defs.get(base_id)
			# Defs are slimmed to MIR-reachable types; base_id may be absent.
			if base_def is not None:
				if base_def.kind is not TypeKind.VARIANT:
					raise ValueError("variant_schemas entry base_id is not a VARIANT TypeDef")
				if base_def.param_types:
					raise ValueError("variant base TypeDef must not carry param_types")
			if not isinstance(schema_obj, dict):
				raise ValueError("invalid variant schema entry")
			schema_mid = schema_obj.get("module_id")
			name = schema_obj.get("name")
			type_params = schema_obj.get("type_params")
			arms_obj = schema_obj.get("arms")
			tombstone_ctor = schema_obj.get("tombstone_ctor")
			if not isinstance(schema_mid, str) or not isinstance(name, str) or not isinstance(type_params, list) or not isinstance(arms_obj, list):
				raise ValueError("invalid variant schema fields")
			if tombstone_ctor is not None and not isinstance(tombstone_ctor, str):
				raise ValueError("invalid variant schema tombstone_ctor")
			arms: list[VariantArmSchema] = []
			for arm_obj in arms_obj:
				if not isinstance(arm_obj, dict):
					raise ValueError("invalid variant arm schema")
				arm_name = arm_obj.get("name")
				fields_obj = arm_obj.get("fields")
				if not isinstance(arm_name, str) or not isinstance(fields_obj, list):
					raise ValueError("invalid variant arm schema fields")
				fields: list[VariantFieldSchema] = []
				for fobj in fields_obj:
					if not isinstance(fobj, dict):
						raise ValueError("invalid variant field schema")
					fname = fobj.get("name")
					fty = fobj.get("type_expr")
					if not isinstance(fname, str):
						raise ValueError("invalid variant field name")
					fields.append(VariantFieldSchema(name=fname, type_expr=_decode_generic_type_expr(fty)))
				arms.append(VariantArmSchema(name=arm_name, fields=fields))
			if tombstone_ctor is not None and tombstone_ctor not in {arm.name for arm in arms}:
				raise ValueError("invalid variant schema tombstone_ctor (no matching arm)")
			if tombstone_ctor is not None:
				tomb_arm = next((arm for arm in arms if arm.name == tombstone_ctor), None)
				if tomb_arm is None:
					raise ValueError("invalid variant schema tombstone_ctor (no matching arm)")
				if tomb_arm.fields:
					raise ValueError("invalid variant schema tombstone_ctor (must have no payload)")
			if base_def is not None and (base_def.module_id != schema_mid or base_def.name != name):
				raise ValueError("variant schema does not match base VARIANT TypeDef")
			variant_schemas[base_id] = VariantSchema(
				module_id=schema_mid,
				name=name,
				type_params=[str(x) for x in type_params],
				arms=arms,
				tombstone_ctor=tombstone_ctor,
			)

	struct_instances_obj = obj.get("struct_instances")
	struct_instances: dict[TypeId, tuple[TypeId, list[TypeId]]] = {}
	if struct_instances_obj is not None:
		if not isinstance(struct_instances_obj, list):
			raise ValueError("invalid type_table.struct_instances")
		for entry in struct_instances_obj:
			if not isinstance(entry, dict):
				raise ValueError("invalid struct_instances entry")
			inst_id_obj = entry.get("inst_id")
			base_id_obj = entry.get("base_id")
			type_args_obj = entry.get("type_args")
			if not isinstance(inst_id_obj, int) or not isinstance(base_id_obj, int) or not isinstance(type_args_obj, list):
				raise ValueError("invalid struct_instances entry fields")
			# With slimmed defs, base/inst defs may be absent.
			base_def = defs.get(base_id_obj)
			inst_def = defs.get(inst_id_obj)
			if base_def is not None and base_def.kind is not TypeKind.STRUCT:
				raise ValueError("invalid struct_instances entry base_id")
			if inst_def is not None and inst_def.kind is not TypeKind.STRUCT:
				raise ValueError("invalid struct_instances entry inst_id")
			if base_def is not None and inst_def is not None:
				if inst_def.module_id != base_def.module_id or inst_def.name != base_def.name:
					raise ValueError("struct instance TypeDef does not match base identity")
			struct_instances[int(inst_id_obj)] = (int(base_id_obj), [int(x) for x in type_args_obj])

	interface_instances_obj = obj.get("interface_instances")
	interface_instances: dict[TypeId, tuple[TypeId, list[TypeId]]] = {}
	if interface_instances_obj is not None:
		if not isinstance(interface_instances_obj, list):
			raise ValueError("invalid type_table.interface_instances")
		for entry in interface_instances_obj:
			if not isinstance(entry, dict):
				raise ValueError("invalid interface_instances entry")
			inst_id_obj = entry.get("inst_id")
			base_id_obj = entry.get("base_id")
			type_args_obj = entry.get("type_args")
			if not isinstance(inst_id_obj, int) or not isinstance(base_id_obj, int) or not isinstance(type_args_obj, list):
				raise ValueError("invalid interface_instances entry fields")
			base_def = defs.get(base_id_obj)
			inst_def = defs.get(inst_id_obj)
			if base_def is not None and base_def.kind is not TypeKind.INTERFACE:
				raise ValueError("invalid interface_instances entry base_id")
			if inst_def is not None and inst_def.kind is not TypeKind.INTERFACE:
				raise ValueError("invalid interface_instances entry inst_id")
			if base_def is not None and inst_def is not None:
				if inst_def.module_id != base_def.module_id or inst_def.name != base_def.name:
					raise ValueError("interface instance TypeDef does not match base identity")
			interface_instances[int(inst_id_obj)] = (int(base_id_obj), [int(x) for x in type_args_obj])

	provided_nominals_obj = obj.get("provided_nominals")
	if provided_nominals_obj is None:
		raise ValueError("type_table.provided_nominals required (rebuild package)")
	if not isinstance(provided_nominals_obj, list):
		raise ValueError("invalid type_table.provided_nominals")
	provided_nominals: set[tuple[TypeKind, str, str]] = set()
	for entry in provided_nominals_obj:
		if not isinstance(entry, dict):
			raise ValueError("invalid provided_nominals entry")
		kind_s = entry.get("kind")
		module_id = entry.get("module_id")
		name = entry.get("name")
		if not isinstance(kind_s, str) or not isinstance(module_id, str) or not isinstance(name, str):
			raise ValueError("invalid provided_nominals entry")
		kind = _decode_kind(kind_s)
		if kind not in (TypeKind.STRUCT, TypeKind.VARIANT, TypeKind.SCALAR, TypeKind.INTERFACE):
			raise ValueError("invalid provided_nominals entry")
		if not module_id:
			raise ValueError("invalid provided_nominals entry")
		provided_nominals.add((kind, module_id, name))

	type_aliases_obj = obj.get("type_aliases")
	type_aliases: list[tuple[str, str, list[str], GenericTypeExpr]] = []
	if type_aliases_obj is not None:
		if not isinstance(type_aliases_obj, list):
			raise ValueError("invalid type_table.type_aliases")
		for entry in type_aliases_obj:
			if not isinstance(entry, dict):
				raise ValueError("invalid type_aliases entry")
			a_mid = entry.get("module_id")
			a_name = entry.get("name")
			a_params = entry.get("type_params")
			a_target = entry.get("target")
			if not isinstance(a_mid, str) or not isinstance(a_name, str) or not isinstance(a_params, list):
				raise ValueError("invalid type_aliases entry fields")
			type_aliases.append((a_mid, a_name, [str(p) for p in a_params], _decode_alias_target(a_target)))

	# Phase 9: decode pre-computed canonical keys if present.
	canonical_keys: dict[TypeId, object] | None = None
	canonical_keys_obj = obj.get("canonical_keys")
	if isinstance(canonical_keys_obj, dict):
		from lang.driftc.packages.provisional_dmir_v0 import _canonical_key_from_json
		canonical_keys = {}
		for ck_tid_s, ck_val in canonical_keys_obj.items():
			try:
				canonical_keys[int(ck_tid_s)] = _canonical_key_from_json(ck_val)
			except (ValueError, TypeError):
				continue  # skip malformed entries gracefully

	return DecodedTypeTable(
		package_id=pkg_id,
		defs=defs,
		struct_schemas=struct_schemas,
		struct_instances=struct_instances,
		interface_schemas=interface_schemas,
		interface_instances=interface_instances,
		exception_schemas=exception_schemas,
		variant_schemas=variant_schemas,
		provided_nominals=provided_nominals,
		type_aliases=type_aliases,
		canonical_keys=canonical_keys,
	)


def _canonical_builtin_name(name: str) -> str:
	if name == "u64":
		return "Uint64"
	return name


def _builtin_type_id(host: TypeTable, td: DecodedTypeDef) -> TypeId | None:
	"""
	Map a package TypeDef to a canonical host builtin TypeId if it is a builtin.
	"""
	if td.kind is TypeKind.SCALAR:
		name = _canonical_builtin_name(td.name)
		if name == "Int":
			return host.ensure_int()
		if name == "Uint":
			return host.ensure_uint()
		if name == "Uint64":
			return host.ensure_uint64()
		if name == "Byte":
			return host.ensure_byte()
		if name == "Bool":
			return host.ensure_bool()
		if name == "Float":
			return host.ensure_float()
		if name == "String":
			return host.ensure_string()
		if name == "Int32":
			return host.ensure_int32()
		if name == "Uint32":
			return host.ensure_uint32()
	if td.kind is TypeKind.VOID:
		return host.ensure_void()
	if td.kind is TypeKind.ERROR and td.name == "Error":
		return host.ensure_error()
	if td.kind is TypeKind.DIAGNOSTICVALUE and td.name == "DiagnosticValue":
		return host.ensure_diagnostic_value()
	if td.kind is TypeKind.UNKNOWN and td.name == "Unknown":
		return host.ensure_unknown()
	return None


def import_type_table_and_build_typeid_map(pkg_tt_obj: Mapping[str, Any], host: TypeTable) -> dict[TypeId, TypeId]:
	"""
	Backwards-compatible single-package wrapper for the deterministic multi-linker.

	Do not add new logic here. All production behavior must live in
	`import_type_tables_and_build_typeid_maps(...)` so package type linking has a
	single source of truth.
	"""
	return import_type_tables_and_build_typeid_maps([pkg_tt_obj], host)[0]


TypeKey = tuple
_CORE_NOMINAL_ALLOWLIST: set[tuple[TypeKind, str]] = {(TypeKind.VARIANT, "Optional")}


def _normalized_pkg_id_for_module(pkg_id: str, module_id: str | None) -> str:
	if module_id == "lang.core":
		return "lang.core"
	if isinstance(module_id, str) and module_id.startswith(("lang.", "std.")):
		return "std"
	return pkg_id


def compute_canonical_keys(table: TypeTable, package_id: str) -> dict[TypeId, TypeKey]:
	"""Compute canonical TypeKeys for all TypeIds in a TypeTable.

	Uses the same key format and normalization rules as the linker's
	key_for_tid(), but operates on a live TypeTable (producer-side).
	The returned dict can be serialized into the package payload so the
	consumer can use pre-computed keys instead of walking the TypeDef graph.
	"""
	memo: dict[TypeId, TypeKey] = {}

	def _is_builtin(td: TypeDef) -> bool:
		if td.kind is TypeKind.SCALAR and td.name in {
			"Int", "Uint", "Uint64", "Int32", "Uint32", "Bool", "Float", "String", "Byte",
		}:
			return True
		return False

	def key_for(tid: TypeId) -> TypeKey:
		if tid in memo:
			return memo[tid]
		# Guard against cycles.
		memo[tid] = ("cycle", tid)
		try:
			td = table.get(tid)
		except (KeyError, IndexError):
			k: TypeKey = ("unknown_tid", tid)
			memo[tid] = k
			return k

		mid = td.module_id or ""
		pkg_id = _normalized_pkg_id_for_module(package_id, td.module_id) if mid else ""

		# Builtins.
		if _is_builtin(td):
			k = ("builtin", td.kind.name, _canonical_builtin_name(td.name))
			memo[tid] = k
			return k
		if td.kind is TypeKind.VOID:
			k = ("builtin", "VOID", "Void")
			memo[tid] = k
			return k
		if td.kind is TypeKind.ERROR:
			k = ("builtin", "ERROR", "Error")
			memo[tid] = k
			return k
		if td.kind is TypeKind.DIAGNOSTICVALUE:
			k = ("builtin", "DIAGNOSTICVALUE", "DiagnosticValue")
			memo[tid] = k
			return k
		if td.kind is TypeKind.UNKNOWN:
			k = ("builtin", "UNKNOWN", "Unknown")
			memo[tid] = k
			return k

		# Struct instance.
		if td.kind is TypeKind.STRUCT:
			inst = table.struct_instances.get(tid)
			if inst is not None and inst.type_args:
				try:
					base_td = table.get(inst.base_id)
				except (KeyError, IndexError):
					pass
				else:
					base_mid = base_td.module_id or ""
					base_pkg_id = _normalized_pkg_id_for_module(package_id, base_td.module_id) if base_mid else ""
					base_key = ("nominal", TypeKind.STRUCT.name, base_pkg_id, base_mid, base_td.name)
					arg_keys = tuple(key_for(x) for x in inst.type_args)
					k = ("inst", base_key, arg_keys)
					memo[tid] = k
					return k

		# Interface instance.
		if td.kind is TypeKind.INTERFACE:
			inst = table.interface_instances.get(tid)
			if inst is not None and inst.type_args:
				try:
					base_td = table.get(inst.base_id)
				except (KeyError, IndexError):
					pass
				else:
					base_mid = base_td.module_id or ""
					base_pkg_id = _normalized_pkg_id_for_module(package_id, base_td.module_id) if base_mid else ""
					base_key = ("nominal", TypeKind.INTERFACE.name, base_pkg_id, base_mid, base_td.name)
					arg_keys = tuple(key_for(x) for x in inst.type_args)
					k = ("inst", base_key, arg_keys)
					memo[tid] = k
					return k

		# Nominal struct/scalar.
		if td.kind in (TypeKind.STRUCT, TypeKind.SCALAR):
			k = ("nominal", td.kind.name, pkg_id, mid, td.name)
			memo[tid] = k
			return k

		# FORWARD_NOMINAL.
		if td.kind is TypeKind.FORWARD_NOMINAL:
			# Try to resolve to concrete kind.
			resolved_kind: str | None = None
			for other_tid in table._defs:
				if other_tid == tid:
					continue
				try:
					other_td = table.get(other_tid)
				except (KeyError, IndexError):
					continue
				if other_td.module_id == td.module_id and other_td.name == td.name and other_td.kind in (TypeKind.STRUCT, TypeKind.INTERFACE, TypeKind.VARIANT):
					resolved_kind = other_td.kind.name
					break
			k = ("nominal", resolved_kind or td.kind.name, pkg_id, mid, td.name)
			memo[tid] = k
			return k

		# Interface nominal.
		if td.kind is TypeKind.INTERFACE:
			k = ("nominal", td.kind.name, pkg_id, mid, td.name)
			memo[tid] = k
			return k

		# Variant.
		if td.kind is TypeKind.VARIANT:
			base_key = ("nominal", TypeKind.VARIANT.name, pkg_id, mid, td.name)
			vinst = table.variant_instances.get(tid)
			if vinst is not None and vinst.type_args:
				arg_keys = tuple(key_for(x) for x in vinst.type_args)
				k = ("inst", base_key, arg_keys)
				memo[tid] = k
				return k
			if tid in table.variant_schemas and not td.param_types:
				memo[tid] = base_key
				return base_key
			if td.param_types:
				arg_keys = tuple(key_for(x) for x in td.param_types)
				k = ("inst", base_key, arg_keys)
				memo[tid] = k
				return k
			memo[tid] = base_key
			return base_key

		# TypeVar — use normalized package for the owner's module, not
		# the raw package_id, so lang.core owners inside std packages
		# get the correct "lang.core" package identity.
		if td.kind is TypeKind.TYPEVAR and td.type_param_id is not None:
			owner = td.type_param_id.owner
			owner_pkg = _normalized_pkg_id_for_module(package_id, owner.module) if owner.module else package_id
			k = ("typevar", owner_pkg, ("owner", owner.module, owner.name, owner.ordinal), td.type_param_id.index)
			memo[tid] = k
			return k

		# Structural / derived types.
		sub_keys = tuple(key_for(x) for x in td.param_types)
		if td.kind is TypeKind.ARRAY:
			k = ("array", sub_keys[0]) if sub_keys else ("array",)
		elif td.kind is TypeKind.REF:
			k = ("ref", bool(td.ref_mut), sub_keys[0]) if sub_keys else ("ref",)
		elif td.kind is TypeKind.FNRESULT:
			k = ("fnresult", sub_keys[0], sub_keys[1]) if len(sub_keys) >= 2 else ("fnresult",) + sub_keys
		elif td.kind is TypeKind.FUNCTION:
			k = ("function", bool(td.fn_throws), sub_keys)
		else:
			k = ("kind", td.kind.name, td.name, sub_keys, bool(td.ref_mut))
		memo[tid] = k
		return k

	for tid in table._defs:
		key_for(tid)
	return memo


def import_type_tables_and_build_typeid_maps(pkg_tt_objs: list[Mapping[str, Any]], host: TypeTable) -> list[dict[TypeId, TypeId]]:
	"""
	Two-phase, order-independent type linking for package consumption.

	Pinned determinism contract:
	- The resulting host TypeIds and per-package `TypeId -> TypeId` maps must not
	  depend on package discovery order, filesystem ordering, or `--package-root`
	  ordering.
	- Allocation is driven only by canonical type keys derived from package
	  contents (builtins, nominal identities, and structural constructors).
	"""
	pkgs = [decode_type_table_obj(obj) for obj in pkg_tt_objs]
	host.module_packages.setdefault("lang.core", "lang.core")
	# Module ownership is owned by the package resolver (or workspace), not by
	# type linking. Avoid inferring module→package mappings from TypeDefs.
	# Module ownership collision checks are performed by the resolver; linker
	# does not infer providers from schemas/TypeDefs.
	module_providers: dict[str, str] = {}
	for pkg in pkgs:
		provided = {(k, mid, name) for (k, mid, name) in pkg.provided_nominals}
		if pkg.canonical_keys is None:
			raise ValueError(
				f"package '{pkg.package_id}' missing canonical_keys; "
				f"rebuild with a compiler that emits canonical_keys (payload_version >= 1)"
			)
		for kind, module_id, name in provided:
			expected_pkg = _normalized_pkg_id_for_module(pkg.package_id, module_id)
			expected_key = ("nominal", kind.name, expected_pkg, module_id, name)
			found = any(ck == expected_key for ck in pkg.canonical_keys.values())
			if not found:
				raise ValueError(f"invalid provided_nominals entry ({kind.name} {module_id}:{name}, no matching canonical key)")
		for kind, module_id, name in provided:
			if module_id != "lang.core":
				prev = module_providers.get(module_id)
				if prev is None:
					module_providers[module_id] = pkg.package_id
				elif prev != pkg.package_id:
					# Tolerate when std/lang modules are claimed by multiple
					# user packages — these come from shared type table
					# accumulation in multi-package builds, not from real
					# ownership claims. The canonical provider is always the
					# std or lang.core package.
					if module_id.startswith(("std.", "lang.")):
						continue
					raise ValueError(f"module id collision for '{module_id}'")
				continue
			if pkg.package_id != "lang.core":
				raise ValueError("package cannot provide lang.core definitions")
			if kind in (TypeKind.STRUCT, TypeKind.SCALAR, TypeKind.VARIANT, TypeKind.INTERFACE):
				if (kind, name) not in _CORE_NOMINAL_ALLOWLIST:
					raise ValueError("unsupported lang.core nominal in package")
	for module_id, pkg_id in host.module_packages.items():
		prev = module_providers.get(module_id)
		if prev is not None and prev != pkg_id:
			raise ValueError(f"module id collision for '{module_id}'")
	# Populate host.module_packages deterministically from provided modules.
	for module_id, pkg_id in sorted(module_providers.items()):
		host.module_packages[module_id] = pkg_id

	# Phase A: compute canonical keys for every package TypeId.
	#
	# Keys must cover all types that can appear in signatures/MIR/schemas:
	# - builtins (by kind+name),
	# - nominal types (module_id, kind, name),
	# - derived/structural types (constructor + param keys),
	# - variant instantiations (base nominal key + arg keys).
	pkg_tid_to_key: list[dict[TypeId, TypeKey]] = []
	typevar_display_names: dict[TypeKey, tuple[int, str]] = {}
	for pkg in pkgs:
		memo: dict[TypeId, TypeKey] = {}

		def key_for_tid(tid: TypeId) -> TypeKey:
			if tid in memo:
				return memo[tid]
			# Primary path: canonical_keys (required for all packages).
			if tid in pkg.canonical_keys:
				k = pkg.canonical_keys[tid]
				memo[tid] = k
				# TypeVar display name tracking still needs the def.
				td = pkg.defs.get(tid)
				if td is not None and td.kind is TypeKind.TYPEVAR and td.type_param_id is not None:
					existing_entry = typevar_display_names.get(k)
					if existing_entry is None or tid < existing_entry[0]:
						typevar_display_names[k] = (tid, td.name)
				return k
			raise ValueError(
				f"TypeId {tid} not in canonical_keys for package "
				f"'{pkg.package_id}'; rebuild the package"
			)

		m: dict[TypeId, TypeKey] = {}
		for tid in pkg.defs.keys():
			m[tid] = key_for_tid(tid)
		# Include TypeIds from canonical_keys that aren't in slimmed defs.
		for tid, ck in pkg.canonical_keys.items():
			if tid not in m:
				m[tid] = ck
				memo[tid] = ck
		# Validate serialized canonical keys match defs-derived keys (debug mode).
		if os.environ.get("DRIFT_DEBUG_CANONICAL_KEYS") == "1":
			_ck_mismatches = 0
			for tid, linker_key in m.items():
				serialized_key = pkg.canonical_keys.get(tid)
				if serialized_key is not None and serialized_key != linker_key:
					_ck_mismatches += 1
					if _ck_mismatches <= 10:
						import sys
						print(
							f"CANONICAL_KEY MISMATCH tid={tid}: "
							f"serialized={serialized_key} linker={linker_key}",
							file=sys.stderr,
						)
			if _ck_mismatches > 0:
				import sys
				print(f"CANONICAL_KEY: {_ck_mismatches} mismatches in package {pkg.package_id}", file=sys.stderr)
		pkg_tid_to_key.append(m)

	# Phase A: merge/validate exception schemas (keyed by canonical event fqn).
	for pkg in pkgs:
		for fqn, schema in sorted(pkg.exception_schemas.items()):
			prev = host.exception_schemas.get(fqn)
			if prev is None:
				host.exception_schemas[fqn] = schema
			elif prev != schema:
				raise ValueError(f"exception schema collision for '{fqn}'")

	# Phase A: merge/validate variant schemas by nominal identity.
	merged_variant_schemas: dict[NominalKey, VariantSchema] = {}
	for pkg in pkgs:
		for _base_id, schema in pkg.variant_schemas.items():
			if schema.module_id == "lang.core" and pkg.package_id != "lang.core":
				if (TypeKind.VARIANT, schema.name) != (TypeKind.VARIANT, "Optional"):
					raise ValueError("package cannot provide lang.core definitions")
			pkg_id = _normalized_pkg_id_for_module(pkg.package_id, schema.module_id)
			key = NominalKey(package_id=pkg_id, module_id=schema.module_id, name=schema.name, kind=TypeKind.VARIANT)
			if schema.module_id == "lang.core":
				if (TypeKind.VARIANT, schema.name) not in _CORE_NOMINAL_ALLOWLIST:
					raise ValueError(f"unsupported lang.core nominal '{schema.name}' in package")
				if schema.name == "Optional":
					base_id = host.ensure_optional_base()
					canonical = host.variant_schemas.get(base_id)
					if canonical is None or canonical != schema:
						raise ValueError("lang.core Optional schema mismatch")
			prev = merged_variant_schemas.get(key)
			if prev is None:
				merged_variant_schemas[key] = schema
			elif prev != schema:
				raise ValueError(f"variant schema collision for '{schema.module_id}:{schema.name}'")

	# Phase A: merge/validate struct schemas with full field typing.
	merged_struct_schemas: dict[NominalKey, tuple[list[StructFieldSchema], list[str]]] = {}
	for pkg_idx, pkg in enumerate(pkgs):
		pkg_keys = pkg_tid_to_key[pkg_idx]
		for key, (field_schemas, type_params, base_id) in pkg.struct_schemas.items():
			# Validate base_id identity via canonical keys.
			ck = pkg.canonical_keys.get(base_id)
			expected_pkg = _normalized_pkg_id_for_module(pkg.package_id, key.module_id)
			expected_key = ("nominal", TypeKind.STRUCT.name, expected_pkg, key.module_id, key.name)
			if ck is None:
				raise ValueError(f"struct schema '{key.module_id}:{key.name}' base_id {base_id} missing canonical key")
			if ck != expected_key:
				raise ValueError(f"struct schema '{key.module_id}:{key.name}' base_id canonical key mismatch: {ck} != {expected_key}")
			prev = merged_struct_schemas.get(key)
			if prev is None:
				merged_struct_schemas[key] = (list(field_schemas), list(type_params))
			elif prev != (list(field_schemas), list(type_params)):
				raise ValueError(f"struct schema collision for '{key.module_id}:{key.name}'")

	# Phase A: merge/validate interface schemas by nominal identity.
	merged_interface_schemas: dict[NominalKey, tuple[list[str], list[InterfaceMethodSchema], list[GenericTypeExpr]]] = {}
	for pkg_idx, pkg in enumerate(pkgs):
		for key, (type_params, methods, parents, base_id) in pkg.interface_schemas.items():
			# Validate base_id identity via canonical keys.
			ck = pkg.canonical_keys.get(base_id)
			expected_pkg = _normalized_pkg_id_for_module(pkg.package_id, key.module_id)
			expected_key = ("nominal", TypeKind.INTERFACE.name, expected_pkg, key.module_id, key.name)
			if ck is None:
				raise ValueError(f"interface schema '{key.module_id}:{key.name}' base_id {base_id} missing canonical key")
			if ck != expected_key:
				raise ValueError(f"interface schema '{key.module_id}:{key.name}' base_id canonical key mismatch: {ck} != {expected_key}")
			prev = merged_interface_schemas.get(key)
			if prev is None:
				merged_interface_schemas[key] = (list(type_params), list(methods), list(parents))
			elif prev != (list(type_params), list(methods), list(parents)):
				raise ValueError(f"interface schema collision for '{key.module_id}:{key.name}'")

	# Phase B: allocate/import host TypeIds in canonical order (no discovery dependence).
	key_to_host: dict[TypeKey, TypeId] = {}
	typevar_param_ids: dict[TypeKey, TypeParamId] = {}
	typevar_owner = FunctionId(module="lang.__external", name="__pkg_typevar", ordinal=0)
	typevar_index = 0

	def ensure_builtin(k: TypeKey) -> TypeId:
		_, kind_s, name = k
		name = _canonical_builtin_name(name)
		kind = TypeKind[kind_s]
		if kind is TypeKind.SCALAR:
			if name == "Int":
				return host.ensure_int()
			if name == "Uint":
				return host.ensure_uint()
			if name == "Uint64":
				return host.ensure_uint64()
			if name == "Byte":
				return host.ensure_byte()
			if name == "Bool":
				return host.ensure_bool()
			if name == "Float":
				return host.ensure_float()
			if name == "String":
				return host.ensure_string()
			if name == "Int32":
				return host.ensure_int32()
			if name == "Uint32":
				return host.ensure_uint32()
		if kind is TypeKind.VOID:
			return host.ensure_void()
		if kind is TypeKind.ERROR and name == "Error":
			return host.ensure_error()
		if kind is TypeKind.DIAGNOSTICVALUE and name == "DiagnosticValue":
			return host.ensure_diagnostic_value()
		if kind is TypeKind.UNKNOWN and name == "Unknown":
			return host.ensure_unknown()
		raise ValueError(f"unsupported builtin type in package: {k!r}")

	# Declare nominal types deterministically.
	nominal_keys: list[NominalKey] = []
	nominal_keys.extend(list(merged_struct_schemas.keys()))
	nominal_keys.extend(list(merged_variant_schemas.keys()))
	nominal_keys.extend(list(merged_interface_schemas.keys()))
	for pkg in pkgs:
		# Collect non-builtin SCALAR nominals from provided_nominals;
		# fall back to canonical_keys extraction if not listed.
		_found_scalars = False
		for kind, mid, name in pkg.provided_nominals:
			if kind is TypeKind.SCALAR:
				_found_scalars = True
				nominal_keys.append(
					NominalKey(
						package_id=_normalized_pkg_id_for_module(pkg.package_id, mid),
						module_id=mid,
						name=name,
						kind=TypeKind.SCALAR,
					)
				)
		if not _found_scalars:
			# No SCALAR in provided_nominals — extract from canonical_keys.
			for _ck_tid, _ck_val in pkg.canonical_keys.items():
				if isinstance(_ck_val, tuple) and len(_ck_val) >= 5 and _ck_val[0] == "nominal" and _ck_val[1] == "SCALAR":
					nominal_keys.append(
						NominalKey(
							package_id=str(_ck_val[2]),
							module_id=str(_ck_val[3]),
							name=str(_ck_val[4]),
							kind=TypeKind.SCALAR,
						)
					)
	nominal_keys = sorted(
		set(nominal_keys),
		key=lambda nk: (nk.package_id or "", nk.module_id or "", nk.kind.name, nk.name),
	)

	for nk in nominal_keys:
		mid = nk.module_id or ""
		if nk.kind is TypeKind.STRUCT:
			field_schemas, type_params = merged_struct_schemas[nk]
			field_names = [f.name for f in field_schemas]
			prev = host.struct_schemas.get(nk)
			if prev is not None:
				_h_name, h_fields = prev
				if list(h_fields) != list(field_names):
					raise ValueError(f"struct field name mismatch for '{mid}:{nk.name}'")
				base_id = host.get_struct_base(module_id=mid, name=nk.name)
				if base_id is not None:
					schema = host.struct_bases.get(base_id)
					if schema is not None and list(schema.type_params) != list(type_params):
						raise ValueError(f"struct type parameter mismatch for '{mid}:{nk.name}'")
			else:
				host.declare_struct(mid, nk.name, list(field_names), list(type_params))
		elif nk.kind is TypeKind.VARIANT:
			schema = merged_variant_schemas[nk]
			if schema.module_id == "lang.core" and schema.name == "Optional":
				host.ensure_optional_base()
				continue
			host_base = host.get_variant_base(module_id=schema.module_id, name=schema.name)
			if host_base is not None:
				host_schema = host.get_variant_schema(host_base)
				if host_schema is None or host_schema != schema:
					raise ValueError(f"variant schema collision for '{schema.module_id}:{schema.name}'")
			else:
				host.declare_variant(schema.module_id, schema.name, schema.type_params, schema.arms, tombstone_ctor=schema.tombstone_ctor)
		elif nk.kind is TypeKind.INTERFACE:
			type_params, methods, parents = merged_interface_schemas[nk]
			base_id = host.get_interface_base(module_id=mid, name=nk.name)
			if base_id is not None:
				schema = host.interface_bases.get(base_id)
				if schema is not None and (
					list(schema.type_params) != list(type_params)
					or list(schema.methods) != list(methods)
					or list(schema.parents) != list(parents)
				):
					raise ValueError(f"interface type parameter mismatch for '{mid}:{nk.name}'")
			else:
				host.declare_interface(mid, nk.name, list(type_params))
			if base_id is None:
				base_id = host.get_interface_base(module_id=mid, name=nk.name)
			if base_id is not None:
				parent_base_ids: list[TypeId] = []
				for pexpr in parents:
					if pexpr.param_index is not None:
						raise ValueError(f"interface '{mid}:{nk.name}' parent cannot be a type parameter")
					parent_mod = pexpr.module_id or mid
					parent_base_ids.append(
						host.require_nominal(kind=TypeKind.INTERFACE, module_id=parent_mod, name=pexpr.name)
					)
				host.define_interface_schema_methods(
					base_id,
					list(methods),
					parents=list(parents),
					parent_base_ids=parent_base_ids,
				)
		elif nk.kind is TypeKind.SCALAR:
			host.declare_scalar(nk.module_id or "", nk.name)

	# Seed nominal keys into key_to_host.
	for nk in nominal_keys:
		mid = nk.module_id or ""
		if nk.kind is TypeKind.STRUCT:
			key_to_host[("nominal", TypeKind.STRUCT.name, nk.package_id or "", mid, nk.name)] = host.require_nominal(
				kind=TypeKind.STRUCT,
				module_id=mid,
				name=nk.name,
			)
		elif nk.kind is TypeKind.VARIANT:
			key_to_host[("nominal", TypeKind.VARIANT.name, nk.package_id or "", mid, nk.name)] = host.require_nominal(
				kind=TypeKind.VARIANT,
				module_id=mid,
				name=nk.name,
			)
		elif nk.kind is TypeKind.INTERFACE:
			key_to_host[("nominal", TypeKind.INTERFACE.name, nk.package_id or "", mid, nk.name)] = host.require_nominal(
				kind=TypeKind.INTERFACE,
				module_id=mid,
				name=nk.name,
			)
		elif nk.kind is TypeKind.SCALAR:
			key_to_host[("nominal", TypeKind.SCALAR.name, nk.package_id or "", mid, nk.name)] = host.require_nominal(
				kind=TypeKind.SCALAR,
				module_id=nk.module_id,
				name=nk.name,
			)

	# Populate struct base schemas with field type expressions before
	# remaining_keys processing — ensure_struct_instantiated needs
	# schema.fields to evaluate field types for generic instances.
	for nk in sorted(merged_struct_schemas.keys(), key=lambda k: (k.package_id or "", k.module_id or "", k.name)):
		mid = nk.module_id or ""
		host_tid = host.require_nominal(kind=TypeKind.STRUCT, module_id=mid, name=nk.name)
		field_schemas, _type_params = merged_struct_schemas[nk]
		host.define_struct_schema_fields(host_tid, list(field_schemas))

	all_keys: set[TypeKey] = set()
	for tid_keys in pkg_tid_to_key:
		all_keys.update(tid_keys.values())

	def depth_of_key(k: TypeKey, memo: dict[TypeKey, int]) -> int:
		if k in memo:
			return memo[k]
		tag = k[0]
		if tag in ("builtin", "nominal"):
			memo[k] = 0
			return 0
		if tag == "typevar":
			memo[k] = 0
			return 0
		if tag == "inst":
			base_key = k[1]
			arg_keys = k[2]
			d = 1 + max([depth_of_key(base_key, memo)] + [depth_of_key(x, memo) for x in arg_keys])
			memo[k] = d
			return d
		sub: list[TypeKey] = []
		if tag == "array":
			sub = [k[1]]
		elif tag == "ref":
			sub = [k[2]]
		elif tag == "fnresult":
			sub = [k[1], k[2]]
		elif tag == "function":
			sub = list(k[2])
		elif tag == "kind":
			sub = list(k[3])
		d = 1 + max([depth_of_key(x, memo) for x in sub], default=0)
		memo[k] = d
		return d

	depth_memo: dict[TypeKey, int] = {}
	remaining_keys = [k for k in all_keys if k not in key_to_host]
	remaining_keys.sort(key=lambda k: (depth_of_key(k, depth_memo), k))

	for k in remaining_keys:
		tag = k[0]
		if tag == "builtin":
			key_to_host[k] = ensure_builtin(k)
		elif tag == "nominal":
			# Must have been seeded by nominal_keys.
			if k not in key_to_host:
				_kind, kind_s, _pkg_id, mid, name = k
				if kind_s == TypeKind.FORWARD_NOMINAL.name:
					# Try to resolve to an existing concrete type first.
					# ensure_named checks for existing concrete types before
					# creating a new FORWARD_NOMINAL.
					key_to_host[k] = host.ensure_named(name, module_id=(mid or None))
				else:
					key_to_host[k] = host.require_nominal(kind=TypeKind[kind_s], module_id=(mid or None), name=name)
		elif tag == "array":
			key_to_host[k] = host.new_array(key_to_host[k[1]])
		elif tag == "ref":
			is_mut = bool(k[1])
			inner = key_to_host[k[2]]
			key_to_host[k] = host.ensure_ref_mut(inner) if is_mut else host.ensure_ref(inner)
		elif tag == "fnresult":
			ok = key_to_host[k[1]]
			err = key_to_host[k[2]]
			key_to_host[k] = host.ensure_fnresult(ok, err)
		elif tag == "function":
			can_throw = bool(k[1])
			pts = [key_to_host[x] for x in k[2]]
			if not pts:
				raise ValueError("invalid function type key (no return type)")
			key_to_host[k] = host.ensure_function(pts[:-1], pts[-1], can_throw=can_throw)
		elif tag == "kind":
			_kind, kind_s, _name, sub_keys, _ref_mut = k
			if kind_s == TypeKind.RAW_PTR.name:
				if not sub_keys:
					raise ValueError("invalid raw ptr type key (no inner type)")
				key_to_host[k] = host.new_ptr(key_to_host[sub_keys[0]])
			else:
				raise ValueError(f"unsupported type key in package linker: {k!r}")
		elif tag == "inst":
			base_tid = key_to_host[k[1]]
			args = [key_to_host[x] for x in list(k[2])]
			base_key = k[1]
			kind_s = base_key[1] if isinstance(base_key, tuple) and len(base_key) > 1 else ""
			if kind_s == TypeKind.STRUCT.name:
				if any(host.has_typevar(arg) for arg in args):
					key_to_host[k] = host.ensure_struct_template(base_tid, args)
				else:
					key_to_host[k] = host.ensure_struct_instantiated(base_tid, args)
			elif kind_s == TypeKind.INTERFACE.name:
				if any(host.has_typevar(arg) for arg in args):
					key_to_host[k] = host.ensure_interface_template(base_tid, args)
				else:
					key_to_host[k] = host.ensure_interface_instantiated(base_tid, args)
			else:
				if any(host.has_typevar(arg) for arg in args):
					key_to_host[k] = host.ensure_variant_template(base_tid, args)
				else:
					key_to_host[k] = host.ensure_variant_instantiated(base_tid, args)
		elif tag == "typevar":
			param_id = typevar_param_ids.get(k)
			if param_id is None:
				if isinstance(k[2], tuple) and k[2] and k[2][0] == "owner":
					_owner_tag, mod, name, ordinal = k[2]
					owner = FunctionId(module=str(mod), name=str(name), ordinal=int(ordinal))
					param_id = TypeParamId(owner=owner, index=int(k[3]))
					# Reuse the host's struct_type_param_ids for synthetic
					# __struct_ owners so that the same struct type param
					# from different packages maps to ONE TypeParamId
					# (and therefore one TypeVar with a stable display name).
					# Without this, the second package creates T0 instead of
					# reusing T, which breaks trait solver unification.
					if mod == "lang.__internal" and isinstance(name, str) and name.startswith("__struct_"):
						_struct_ref = name[len("__struct_"):]  # e.g. "std.sync::Handle"
						if "::" in _struct_ref:
							_struct_mod, _struct_name = _struct_ref.rsplit("::", 1)
							_host_base = host.get_struct_base(module_id=_struct_mod, name=_struct_name)
							if _host_base is not None:
								_host_tpids = host.struct_type_param_ids.get(_host_base, [])
								_tv_idx = int(k[3])
								if _tv_idx < len(_host_tpids):
									param_id = _host_tpids[_tv_idx]
				else:
					param_id = TypeParamId(typevar_owner, typevar_index)
					typevar_index += 1
				typevar_param_ids[k] = param_id
			display_entry = typevar_display_names.get(k)
			display_name = display_entry[1] if display_entry is not None else None
			key_to_host[k] = host.ensure_typevar(param_id, name=display_name)
		else:
			raise ValueError(f"unsupported type key in package linker: {k!r}")

	# Post-link FORWARD_NOMINAL resolution sweep.  After all packages'
	# types are allocated, scan key_to_host for FORWARD_NOMINAL nominal
	# keys that now have a concrete counterpart allocated from another
	# package.  Look up by (module_id, name) across all allocated nominal
	# keys.  This works from the linker's own nominal-key-derived index
	# rather than a module/name-only TypeTable lookup; if two packages
	# define the same (module_id, name), the first concrete entry wins
	# via setdefault — no global uniqueness is assumed.
	_concrete_kinds = frozenset({"STRUCT", "VARIANT", "INTERFACE", "SCALAR"})
	# Build index: (module_id, name) → host_tid for concrete nominals.
	_concrete_by_identity: dict[tuple[str | None, str], TypeId] = {}
	for _ck, _cv in key_to_host.items():
		if not isinstance(_ck, tuple) or len(_ck) < 5 or _ck[0] != "nominal":
			continue
		if _ck[1] in _concrete_kinds:
			_mid_ck, _name_ck = _ck[3], _ck[4]
			_concrete_by_identity.setdefault((_mid_ck, _name_ck), _cv)
	# Resolve FORWARD_NOMINAL keys via the index.
	for k in list(key_to_host):
		if not isinstance(k, tuple) or len(k) < 5 or k[0] != "nominal":
			continue
		if k[1] != TypeKind.FORWARD_NOMINAL.name:
			continue
		_fn_mid, _fn_name = k[3], k[4]
		concrete_tid = _concrete_by_identity.get((_fn_mid, _fn_name))
		if concrete_tid is not None:
			key_to_host[k] = concrete_tid
		elif _DEBUG_LINK:
			import sys
			print(f"[type_table_link] FORWARD_NOMINAL survived post-link sweep: key={k!r} host_tid={key_to_host[k]}", file=sys.stderr)

	# Finalize struct field types deterministically (names + types).
	host_default_pkg = getattr(host, "package_id", None)
	host_default_pkg_s = str(host_default_pkg) if isinstance(host_default_pkg, str) else ""

	def _host_pkg_for_module(module_id: str | None) -> str:
		if not module_id:
			return ""
		pkg_id = host.module_packages.get(module_id, host_default_pkg_s)
		return _normalized_pkg_id_for_module(str(pkg_id), module_id)

	host_key_memo: dict[TypeId, TypeKey] = {}

	def _host_type_key_for_tid(tid: TypeId) -> TypeKey:
		if tid in host_key_memo:
			return host_key_memo[tid]
		td = host.get(tid)
		if td.kind is TypeKind.SCALAR and td.module_id is None:
			builtin_name = _canonical_builtin_name(td.name)
			if builtin_name in {"Int", "Uint", "Uint64", "Byte", "Bool", "Float", "String", "Int32", "Uint32"}:
				k = ("builtin", TypeKind.SCALAR.name, builtin_name)
				host_key_memo[tid] = k
				return k
		if td.kind is TypeKind.VOID and td.name == "Void":
			k = ("builtin", TypeKind.VOID.name, "Void")
			host_key_memo[tid] = k
			return k
		if td.kind is TypeKind.ERROR and td.name == "Error":
			k = ("builtin", TypeKind.ERROR.name, "Error")
			host_key_memo[tid] = k
			return k
		if td.kind is TypeKind.DIAGNOSTICVALUE and td.name == "DiagnosticValue":
			k = ("builtin", TypeKind.DIAGNOSTICVALUE.name, "DiagnosticValue")
			host_key_memo[tid] = k
			return k
		if td.kind is TypeKind.UNKNOWN and td.name == "Unknown":
			k = ("builtin", TypeKind.UNKNOWN.name, "Unknown")
			host_key_memo[tid] = k
			return k
		mid = td.module_id or ""
		pkg_id = _host_pkg_for_module(td.module_id) if mid else ""
		if td.kind is TypeKind.STRUCT:
			inst = host.get_struct_instance(tid)
			if inst is not None:
				base_td = host.get(inst.base_id)
				base_mid = base_td.module_id or ""
				base_pkg_id = _host_pkg_for_module(base_td.module_id) if base_mid else ""
				base_key = ("nominal", TypeKind.STRUCT.name, base_pkg_id, base_mid, base_td.name)
				arg_keys = tuple(_host_type_key_for_tid(x) for x in inst.type_args)
				k = ("inst", base_key, arg_keys)
				host_key_memo[tid] = k
				return k
			k = ("nominal", TypeKind.STRUCT.name, pkg_id, mid, td.name)
			host_key_memo[tid] = k
			return k
		if td.kind is TypeKind.INTERFACE:
			inst = host.get_interface_instance(tid)
			if inst is not None:
				base_td = host.get(inst.base_id)
				base_mid = base_td.module_id or ""
				base_pkg_id = _host_pkg_for_module(base_td.module_id) if base_mid else ""
				base_key = ("nominal", TypeKind.INTERFACE.name, base_pkg_id, base_mid, base_td.name)
				arg_keys = tuple(_host_type_key_for_tid(x) for x in inst.type_args)
				k = ("inst", base_key, arg_keys)
				host_key_memo[tid] = k
				return k
			k = ("nominal", TypeKind.INTERFACE.name, pkg_id, mid, td.name)
			host_key_memo[tid] = k
			return k
		if td.kind is TypeKind.VARIANT:
			inst = host.get_variant_instance(tid)
			if inst is not None:
				base_td = host.get(inst.base_id)
				base_mid = base_td.module_id or ""
				base_pkg_id = _host_pkg_for_module(base_td.module_id) if base_mid else ""
				base_key = ("nominal", TypeKind.VARIANT.name, base_pkg_id, base_mid, base_td.name)
				arg_keys = tuple(_host_type_key_for_tid(x) for x in inst.type_args)
				k = ("inst", base_key, arg_keys)
				host_key_memo[tid] = k
				return k
			base_key = ("nominal", TypeKind.VARIANT.name, pkg_id, mid, td.name)
			if td.param_types:
				arg_keys = tuple(_host_type_key_for_tid(x) for x in td.param_types)
				k = ("inst", base_key, arg_keys)
				host_key_memo[tid] = k
				return k
			host_key_memo[tid] = base_key
			return base_key
		if td.kind in (TypeKind.SCALAR, TypeKind.FORWARD_NOMINAL):
			k = ("nominal", td.kind.name, pkg_id, mid, td.name)
			host_key_memo[tid] = k
			return k
		if td.kind is TypeKind.TYPEVAR:
			type_param = td.type_param_id
			if type_param is None:
				raise ValueError("host TYPEVAR missing type_param_id")
			owner = type_param.owner
			k = ("typevar", _host_pkg_for_module(owner.module), ("owner", owner.module, owner.name, owner.ordinal), type_param.index)
			host_key_memo[tid] = k
			return k
		sub_keys = tuple(_host_type_key_for_tid(x) for x in td.param_types)
		if td.kind is TypeKind.ARRAY:
			k = ("array", sub_keys[0])
		elif td.kind is TypeKind.REF:
			k = ("ref", bool(td.ref_mut), sub_keys[0])
		elif td.kind is TypeKind.FNRESULT:
			k = ("fnresult", sub_keys[0], sub_keys[1])
		elif td.kind is TypeKind.FUNCTION:
			k = ("function", bool(td.fn_throws), sub_keys)
		else:
			k = ("kind", td.kind.name, td.name, sub_keys, bool(td.ref_mut))
		host_key_memo[tid] = k
		return k

	def _is_unknown_type_key(k: object) -> bool:
		return isinstance(k, tuple) and len(k) == 3 and k[0] == "builtin" and k[1] == TypeKind.UNKNOWN.name and k[2] == "Unknown"

	def _type_key_compatible(lhs: object, rhs: object) -> bool:
		if _is_unknown_type_key(lhs) or _is_unknown_type_key(rhs):
			return True
		if type(lhs) is not type(rhs):
			return False
		if isinstance(lhs, tuple):
			if len(lhs) != len(rhs):
				return False
			return all(_type_key_compatible(a, b) for a, b in zip(lhs, rhs))
		return lhs == rhs

	# Import type aliases from packages before struct/variant finalization so
	# that field type expressions referencing aliases (e.g. HashMap<K, V>)
	# resolve during _eval_generic_type_expr.
	for pkg in pkgs:
		for a_mid, a_name, a_params, a_target in pkg.type_aliases:
			if host.lookup_type_alias(module_id=a_mid, name=a_name) is None:
				host.define_type_alias(module_id=a_mid, name=a_name, type_params=a_params, target=a_target)

	# Finalize non-generic struct field types (schema fields already populated
	# before remaining_keys processing above).
	for nk in sorted(merged_struct_schemas.keys(), key=lambda k: (k.package_id or "", k.module_id or "", k.name)):
		mid = nk.module_id or ""
		host_tid = host.require_nominal(kind=TypeKind.STRUCT, module_id=mid, name=nk.name)
		h_td = host.get(host_tid)
		if h_td.kind is not TypeKind.STRUCT:
			raise ValueError(f"expected STRUCT for '{mid}:{nk.name}' after import")
		field_schemas, type_params = merged_struct_schemas[nk]
		if not type_params:
			field_types = [
				host._eval_generic_type_expr(f.type_expr, [], module_id=mid) for f in field_schemas
			]
			if any(t != host.ensure_unknown() for t in h_td.param_types):
				host_fields_key = [_host_type_key_for_tid(t) for t in list(h_td.param_types)]
				new_fields_key = [_host_type_key_for_tid(t) for t in field_types]
				if len(host_fields_key) != len(new_fields_key) or any(
					not _type_key_compatible(a, b) for a, b in zip(host_fields_key, new_fields_key)
				):
					raise ValueError(f"struct field type mismatch for '{mid}:{nk.name}'")
			else:
				host.define_struct_fields(host_tid, field_types)

	# Fill in field types for synthetic struct types (e.g. hidden lambda capture
	# env structs) that have param_types in the package TypeDef but were not
	# handled by the struct_schemas path above.  These structs are created
	# dynamically during MIR lowering and don't have schema entries.
	for pkg in pkgs:
		for tid, td in pkg.defs.items():
			if td.kind is not TypeKind.STRUCT or not td.param_types:
				continue
			if td.field_names is None:
				continue
			mid = td.module_id or ""
			host_tid = host.get_nominal(kind=TypeKind.STRUCT, module_id=mid, name=td.name)
			if host_tid is None:
				_ts.stderr.write(f"[LINK-FIX] creating nominal for {mid}::{td.name}\n")
				host_tid = host.declare_struct(mid, td.name, list(td.field_names))
				# Register the key so Phase C TypeId mapping can find this struct.
				pkg_idx = pkgs.index(pkg)
				if pkg_idx < len(pkg_tid_to_key):
					pk = pkg_tid_to_key[pkg_idx].get(tid)
					if pk is not None:
						key_to_host[pk] = host_tid
			if host_tid is None:
				continue
			h_td = host.get(host_tid)
			if h_td.kind is not TypeKind.STRUCT:
				continue
			# Skip if host already has fields populated.
			if h_td.param_types and any(t != host.ensure_unknown() for t in h_td.param_types):
				continue
			# If the host struct was created without field names (from nominal
			# seeding), patch them from the package before defining fields.
			if not h_td.field_names and td.field_names:
				from dataclasses import replace as _replace
				new_td = _replace(h_td, field_names=list(td.field_names), param_types=[host.ensure_unknown() for _ in td.field_names])
				host._defs[host_tid] = new_td
				h_td = new_td
			# Map package field TypeIds to host TypeIds.
			pkg_idx = pkgs.index(pkg)
			pkg_key_map = pkg_tid_to_key[pkg_idx] if pkg_idx < len(pkg_tid_to_key) else {}
			mapped_fields: list[TypeId] = []
			all_mapped = True
			for ft in td.param_types:
				fk = pkg_key_map.get(ft)
				if fk is not None:
					host_ft = key_to_host.get(fk)
					if host_ft is not None:
						mapped_fields.append(host_ft)
						continue
				all_mapped = False
				break
			if all_mapped and len(mapped_fields) == len(td.field_names):
				h_td_check = host.get(host_tid)
				# Only patch if the host struct has placeholder (unknown) or
				# empty fields.  If it already has real fields that differ,
				# this is a genuine shape conflict that should not be silenced.
				has_real_fields = h_td_check.param_types and any(
					host.get(t).kind is not TypeKind.UNKNOWN for t in h_td_check.param_types
				)
				if not has_real_fields:
					host.define_struct_fields(host_tid, mapped_fields)

	# Ensure non-generic variants have concrete instances available.
	host.finalize_variants()

	# Phase C: per-package tid maps.
	out_maps: list[dict[TypeId, TypeId]] = []
	for tid_keys in pkg_tid_to_key:
		m: dict[TypeId, TypeId] = {}
		for tid, k in tid_keys.items():
			host_tid = key_to_host.get(k)
			if host_tid is None:
				raise ValueError(f"failed to map package TypeId {tid} to host TypeId (key {k!r})")
			m[tid] = host_tid
		out_maps.append(m)
	return out_maps
