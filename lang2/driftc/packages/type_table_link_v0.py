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
  Int/Uint/Bool/Float/String/Void/Error/DiagnosticValue/Unknown.
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

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from lang2.driftc.core.generic_type_expr import GenericTypeExpr
from lang2.driftc.core.types_core import (
	NominalKey,
	TypeDef,
	TypeId,
	TypeKind,
	TypeTable,
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
	field_names: list[str] | None


@dataclass(frozen=True)
class DecodedTypeTable:
	defs: dict[TypeId, DecodedTypeDef]
	struct_schemas: dict[NominalKey, tuple[str, list[str]]]
	exception_schemas: dict[str, tuple[str, list[str]]]
	variant_schemas: dict[TypeId, VariantSchema]


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
	return GenericTypeExpr(name=name, args=args, param_index=param_index, module_id=module_id)


def decode_type_table_obj(obj: Mapping[str, Any]) -> DecodedTypeTable:
	"""
	Decode a `type_table` JSON object from a package payload.
	"""
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
		ref_mut = td_obj.get("ref_mut")
		field_names = td_obj.get("field_names")
		if not isinstance(kind_s, str) or not isinstance(name, str) or not isinstance(param_types, list):
			raise ValueError("invalid type_table.defs entry fields")
		if module_id is not None and not isinstance(module_id, str):
			raise ValueError("invalid type_table.defs module_id")
		if ref_mut is not None and not isinstance(ref_mut, bool):
			raise ValueError("invalid type_table.defs ref_mut")
		if field_names is not None and not isinstance(field_names, list):
			raise ValueError("invalid type_table.defs field_names")
		defs[tid] = DecodedTypeDef(
			kind=_decode_kind(kind_s),
			name=name,
			param_types=[int(x) for x in param_types],
			module_id=module_id,
			ref_mut=ref_mut,
			field_names=[str(x) for x in field_names] if field_names is not None else None,
		)

	struct_schemas_obj = obj.get("struct_schemas")
	struct_schemas: dict[NominalKey, tuple[str, list[str]]] = {}
	if struct_schemas_obj is not None:
		if not isinstance(struct_schemas_obj, list):
			raise ValueError("invalid type_table.struct_schemas")
		for entry in struct_schemas_obj:
			if not isinstance(entry, dict):
				raise ValueError("invalid struct_schemas entry")
			module_id = entry.get("module_id")
			name = entry.get("name")
			fields = entry.get("fields")
			if not isinstance(module_id, str) or not isinstance(name, str) or not isinstance(fields, list):
				raise ValueError("invalid struct_schemas entry fields")
			if not isinstance(fields, list):
				raise ValueError("invalid struct_schemas field list")
			key = NominalKey(module_id=module_id, name=name, kind=TypeKind.STRUCT)
			struct_schemas[key] = (name, [str(x) for x in fields])

	exc_schemas_obj = obj.get("exception_schemas")
	exception_schemas: dict[str, tuple[str, list[str]]] = {}
	if exc_schemas_obj is not None:
		if not isinstance(exc_schemas_obj, dict):
			raise ValueError("invalid type_table.exception_schemas")
		for k, v in exc_schemas_obj.items():
			if not isinstance(k, str) or not isinstance(v, list) or len(v) != 2:
				raise ValueError("invalid exception_schemas entry")
			fqn = str(v[0])
			fields = v[1]
			if not isinstance(fields, list):
				raise ValueError("invalid exception_schemas field list")
			exception_schemas[k] = (fqn, [str(x) for x in fields])

	variant_schemas_obj = obj.get("variant_schemas")
	variant_schemas: dict[TypeId, VariantSchema] = {}
	if variant_schemas_obj is not None:
		if not isinstance(variant_schemas_obj, dict):
			raise ValueError("invalid type_table.variant_schemas")
		for base_id_s, schema_obj in variant_schemas_obj.items():
			base_id = int(base_id_s)
			if not isinstance(schema_obj, dict):
				raise ValueError("invalid variant schema entry")
			schema_mid = schema_obj.get("module_id")
			name = schema_obj.get("name")
			type_params = schema_obj.get("type_params")
			arms_obj = schema_obj.get("arms")
			if not isinstance(schema_mid, str) or not isinstance(name, str) or not isinstance(type_params, list) or not isinstance(arms_obj, list):
				raise ValueError("invalid variant schema fields")
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
			variant_schemas[base_id] = VariantSchema(module_id=schema_mid, name=name, type_params=[str(x) for x in type_params], arms=arms)

	return DecodedTypeTable(
		defs=defs,
		struct_schemas=struct_schemas,
		exception_schemas=exception_schemas,
		variant_schemas=variant_schemas,
	)


def _builtin_type_id(host: TypeTable, td: DecodedTypeDef) -> TypeId | None:
	"""
	Map a package TypeDef to a canonical host builtin TypeId if it is a builtin.
	"""
	if td.kind is TypeKind.SCALAR:
		if td.name == "Int":
			return host.ensure_int()
		if td.name == "Uint":
			return host.ensure_uint()
		if td.name == "Bool":
			return host.ensure_bool()
		if td.name == "Float":
			return host.ensure_float()
		if td.name == "String":
			return host.ensure_string()
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
	Merge a package type table into `host` and build a pkg->host TypeId map.

	This mutates `host` (adds missing nominal definitions and schemas).
	"""
	pkg = decode_type_table_obj(pkg_tt_obj)

	# Build a deterministic mapping. For nominal user-defined types we key by
	# (kind,name) only; schema compatibility is checked separately.
	tid_map: dict[TypeId, TypeId] = {0: 0}

	# Pre-map and validate builtins.
	for tid in sorted(pkg.defs.keys()):
		td = pkg.defs[tid]
		b = _builtin_type_id(host, td)
		if b is not None:
			tid_map[tid] = b

	# Merge exception schemas (keyed by canonical event fqn). This is not a type,
	# but it participates in exception ctor validation and lowering.
	for fqn, schema in sorted(pkg.exception_schemas.items()):
		prev = host.exception_schemas.get(fqn)
		if prev is None:
			host.exception_schemas[fqn] = schema
		elif prev != schema:
			raise ValueError(f"exception schema collision for '{fqn}'")

	# Declare/validate structs deterministically by name.
	# Find the package TypeId for each declared struct name.
	pkg_struct_ids: dict[NominalKey, TypeId] = {}
	for tid, td in pkg.defs.items():
		if td.kind is TypeKind.STRUCT:
			if td.module_id is None:
				raise ValueError(f"package STRUCT '{td.name}' missing module_id")
			key = NominalKey(module_id=td.module_id, name=td.name, kind=TypeKind.STRUCT)
			pkg_struct_ids[key] = tid
	for key, (_n, field_names) in sorted(pkg.struct_schemas.items(), key=lambda kv: ((kv[0].module_id or ""), kv[0].name)):
		pkg_tid = pkg_struct_ids.get(key)
		if pkg_tid is None:
			raise ValueError(f"struct schema '{key.module_id}:{key.name}' missing STRUCT TypeDef in package type table")
		if pkg_tid in tid_map:
			continue
		if key in host.struct_schemas:
			# Declared in host already; verify schema shape matches.
			h_name, h_fields = host.struct_schemas[key]
			if h_fields != field_names:
				raise ValueError(f"struct field name mismatch for '{key.module_id}:{key.name}'")
			host_tid = host.require_nominal(kind=TypeKind.STRUCT, module_id=key.module_id, name=key.name)
			if host.get(host_tid).kind is not TypeKind.STRUCT:
				raise ValueError(f"type name '{key.module_id}:{key.name}' already defined as {host.get(host_tid).kind}")
			tid_map[pkg_tid] = host_tid
		else:
			host_tid = host.declare_struct(key.module_id or "", key.name, list(field_names))
			tid_map[pkg_tid] = host_tid

	# Declare/validate variant schemas deterministically by name.
	pkg_variant_ids: dict[NominalKey, TypeId] = {}
	for base_id, schema in pkg.variant_schemas.items():
		key = NominalKey(module_id=schema.module_id, name=schema.name, kind=TypeKind.VARIANT)
		pkg_variant_ids[key] = base_id
	for base_id, schema in sorted(pkg.variant_schemas.items(), key=lambda kv: (kv[1].module_id, kv[1].name)):
		if base_id in tid_map:
			continue
		key = NominalKey(module_id=schema.module_id, name=schema.name, kind=TypeKind.VARIANT)
		host_base = host.get_variant_base(module_id=schema.module_id, name=schema.name)
		if host_base is not None:
			host_schema = host.get_variant_schema(host_base)
			if host_schema is None or host_schema != schema:
				raise ValueError(f"variant schema collision for '{schema.module_id}:{schema.name}'")
			tid_map[base_id] = host_base
		else:
			host_base = host.declare_variant(schema.module_id, schema.name, schema.type_params, schema.arms)
			tid_map[base_id] = host_base

	# Helper: map any remaining TypeId recursively.
	def map_tid(tid: TypeId) -> TypeId:
		if tid in tid_map:
			return tid_map[tid]
		td = pkg.defs.get(tid)
		if td is None:
			raise ValueError(f"unknown TypeId {tid} referenced by package type table")
		b = _builtin_type_id(host, td)
		if b is not None:
			tid_map[tid] = b
			return b
		if td.kind is TypeKind.STRUCT:
			# Must have been declared in the struct schema pass.
			host_tid = tid_map.get(tid)
			if host_tid is None:
				if td.module_id is None:
					raise ValueError(f"package STRUCT '{td.name}' missing module_id")
				host_tid = host.require_nominal(kind=TypeKind.STRUCT, module_id=td.module_id, name=td.name)
				tid_map[tid] = host_tid
			return host_tid
		if td.kind is TypeKind.VARIANT:
			# Variant bases were declared in the schema pass; instantiations carry
			# type args in `param_types`.
			if td.module_id is None:
				raise ValueError(f"package VARIANT '{td.name}' missing module_id")
			base = host.require_nominal(kind=TypeKind.VARIANT, module_id=td.module_id, name=td.name)
			if base not in host.variant_schemas:
				raise ValueError(f"variant '{td.module_id}:{td.name}' not declared in host after import")
			if td.param_types:
				args = [map_tid(x) for x in td.param_types]
				inst = host.ensure_instantiated(base, args)
				tid_map[tid] = inst
				return inst
			# Non-generic uses the base id directly.
			tid_map[tid] = base
			return base
		if td.kind is TypeKind.OPTIONAL:
			inner = map_tid(td.param_types[0])
			out = host.new_optional(inner)
			tid_map[tid] = out
			return out
		if td.kind is TypeKind.ARRAY:
			elem = map_tid(td.param_types[0])
			out = host.new_array(elem)
			tid_map[tid] = out
			return out
		if td.kind is TypeKind.REF:
			inner = map_tid(td.param_types[0])
			out = host.new_ref(inner, is_mut=bool(td.ref_mut))
			tid_map[tid] = out
			return out
		if td.kind is TypeKind.FNRESULT:
			ok = map_tid(td.param_types[0])
			err = map_tid(td.param_types[1])
			out = host.ensure_fnresult(ok, err)
			tid_map[tid] = out
			return out
		if td.kind is TypeKind.FUNCTION:
			# v1: function types are not exposed; still remap structurally if present.
			params = [map_tid(x) for x in td.param_types[:-1]]
			ret = map_tid(td.param_types[-1])
			out = host.new_function(td.name, params, ret)
			tid_map[tid] = out
			return out
		if td.kind is TypeKind.SCALAR:
			# Unknown nominal scalar: keep as named scalar.
			out = host.ensure_named(td.name, module_id=td.module_id)
			tid_map[tid] = out
			return out
		raise ValueError(f"unsupported package type kind {td.kind} for TypeId {tid}")

	# Deterministically map all remaining TypeIds.
	for tid in sorted(pkg.defs.keys()):
		map_tid(tid)

	# Finalize struct field types now that all referenced types can be mapped.
	for key, pkg_tid in sorted(pkg_struct_ids.items(), key=lambda kv: ((kv[0].module_id or ""), kv[0].name)):
		host_tid = tid_map[pkg_tid]
		pkg_def = pkg.defs[pkg_tid]
		field_types = [map_tid(x) for x in pkg_def.param_types]
		h_td = host.get(host_tid)
		if h_td.kind is not TypeKind.STRUCT:
			raise ValueError(f"expected STRUCT for '{key.module_id}:{key.name}' after import")
		# If host struct already had field types defined, require exact match.
		if any(t != host.ensure_unknown() for t in h_td.param_types):
			if list(h_td.param_types) != field_types:
				raise ValueError(f"struct field type mismatch for '{name}'")
		else:
			host.define_struct_fields(host_tid, field_types)

	# Ensure non-generic variants have concrete instances available.
	host.finalize_variants()

	return tid_map
