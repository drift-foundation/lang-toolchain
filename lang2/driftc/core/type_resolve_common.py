from __future__ import annotations

"""
Shared helpers for resolving raw/builtin type shapes into TypeIds.

This centralizes the knowledge of builtin names (`Int`, `Bool`, `String`,
`Uint`, `Float`, `Void`, `Error`, `DiagnosticValue`, `Array`, `Optional`, `FnResult`) so resolver and checker code
stay in sync as the language evolves.
"""

from lang2.driftc.core.types_core import TypeId, TypeKind, TypeParamId, TypeTable


def resolve_opaque_type(
	raw: object,
	table: TypeTable,
	*,
	module_id: str | None = None,
	type_params: dict[str, TypeParamId] | None = None,
) -> TypeId:
	"""
	Map a raw type shape (TypeExpr-like, string, tuple, or TypeId) to a TypeId.

	Supported inputs:
	- TypeExpr-like objects exposing `name` and `args`.
	- Strings naming builtins (Int/Bool/String/Uint/Float/Void/Error/DiagnosticValue),
	  Array<...>, Optional<...>, and FnResult<ok, err>.
	- Tuples encoding FnResult, e.g. ("FnResult", ok, err) or (ok, err).
	Unknown shapes return the canonical Unknown TypeId for the table.
	"""
	if raw is None:
		return table.ensure_unknown()
	if isinstance(raw, TypeId):
		return raw

	# Parser TypeExpr-like object (duck typed on name/args).
	if hasattr(raw, "name") and hasattr(raw, "args"):
		name = getattr(raw, "name")
		args = getattr(raw, "args")
		origin_mod = getattr(raw, "module_id", None) or module_id
		if type_params and name in type_params and not args:
			return table.ensure_typevar(type_params[name], name=name)
		if name in {"&", "&mut"}:
			inner = resolve_opaque_type(
				args[0] if args else None,
				table,
				module_id=origin_mod,
				type_params=type_params,
			)
			return table.ensure_ref_mut(inner) if name == "&mut" else table.ensure_ref(inner)
		if name == "Void":
			return table.ensure_void()
		# Generic nominal instantiation (MVP: variants only). Example: Optional<Int>.
		if args:
			base = None
			if origin_mod is not None:
				base = table.get_variant_base(module_id=origin_mod, name=str(name))
				if base is None:
					base = table.get_struct_base(module_id=origin_mod, name=str(name))
			if base is None and table.get_variant_base(module_id="lang.core", name=str(name)) is not None:
				base = table.get_variant_base(module_id="lang.core", name=str(name))
			if base is not None:
				arg_ids = [
					resolve_opaque_type(a, table, module_id=origin_mod, type_params=type_params)
					for a in list(args)
				]
				if base in table.variant_schemas:
					return table.ensure_instantiated(base, arg_ids)
				if base in table.struct_bases:
					try:
						if any(table.get(a).kind is TypeKind.TYPEVAR for a in arg_ids):
							return table.ensure_struct_template(base, arg_ids)
						return table.ensure_struct_instantiated(base, arg_ids)
					except ValueError:
						return table.ensure_unknown()
		if name == "FnResult":
			ok = resolve_opaque_type(args[0] if args else None, table, module_id=origin_mod, type_params=type_params)
			err = resolve_opaque_type(
				args[1] if len(args) > 1 else table.ensure_error(),
				table,
				module_id=origin_mod,
				type_params=type_params,
			)
			return table.new_fnresult(ok, err)
		if name == "Array":
			elem = resolve_opaque_type(args[0] if args else None, table, module_id=origin_mod, type_params=type_params)
			return table.new_array(elem)
		if name == "DiagnosticValue":
			return table.ensure_diagnostic_value()
		if name == "Uint":
			return table.ensure_uint()
		if name == "Int":
			return table.ensure_int()
		if name == "Bool":
			return table.ensure_bool()
		if name == "String":
			return table.ensure_string()
		if name == "Float":
			return table.ensure_float()
		if name == "Error":
			return table.ensure_error()
		# User-defined nominal types (structs/variants) and unknown names.
		if origin_mod is not None:
			ty = table.get_nominal(kind=TypeKind.STRUCT, module_id=origin_mod, name=str(name))
			if ty is not None:
				return ty
			ty = table.get_nominal(kind=TypeKind.VARIANT, module_id=origin_mod, name=str(name))
			if ty is not None:
				return ty
			# Toolchain-provided variants (e.g. lang.core Optional) are visible as
			# unqualified type references even in user modules. If the current module
			# does not define a variant of this name, prefer the toolchain base.
			core_base = table.get_variant_base(module_id="lang.core", name=str(name))
			if core_base is not None:
				return core_base
			return table.ensure_named(str(name), module_id=origin_mod)
		# No module context: fall back to a scalar nominal placeholder.
		core_base = table.get_variant_base(module_id="lang.core", name=str(name))
		if core_base is not None:
			return core_base
		return table.ensure_named(str(name))

	# String forms.
	if isinstance(raw, str):
		if type_params and raw in type_params:
			return table.ensure_typevar(type_params[raw], name=raw)
		if raw == "Void":
			return table.ensure_void()
		if raw == "Int":
			return table.ensure_int()
		if raw == "Bool":
			return table.ensure_bool()
		if raw == "String":
			return table.ensure_string()
		if raw == "Float":
			return table.ensure_float()
		if raw == "Error":
			return table.ensure_error()
		if raw == "DiagnosticValue":
			return table.ensure_diagnostic_value()
		if raw == "Uint":
			return table.ensure_uint()
		if raw.startswith("FnResult<") and raw.endswith(">"):
			inner = raw[len("FnResult<"):-1]
			parts = inner.split(",", 1)
			if len(parts) == 2:
				ok_raw, err_raw = parts[0].strip(), parts[1].strip()
				ok_ty = resolve_opaque_type(ok_raw, table, module_id=module_id)
				err_ty = resolve_opaque_type(err_raw, table, module_id=module_id)
				return table.new_fnresult(ok_ty, err_ty)
			return table.ensure_unknown()
		if raw == "FnResult":
			return table.ensure_unknown()
		if raw.startswith("Array<") and raw.endswith(">"):
			inner = raw[len("Array<"):-1]
			elem_ty = resolve_opaque_type(inner, table, module_id=module_id, type_params=type_params)
			return table.new_array(elem_ty)
		if raw.startswith("Optional<") and raw.endswith(">"):
			inner = raw[len("Optional<"):-1]
			inner_ty = resolve_opaque_type(inner, table, module_id=module_id, type_params=type_params)
			base_id = table.get_variant_base(module_id="lang.core", name="Optional")
			if base_id is not None:
				return table.ensure_instantiated(base_id, [inner_ty])
			return table.ensure_unknown()
		# User-defined nominal types (e.g. structs) and unknown names.
		return table.ensure_named(raw, module_id=module_id)

	# Tuple forms used by legacy call sites.
	if isinstance(raw, tuple):
		if len(raw) >= 3 and raw[0] == "FnResult":
			ok = resolve_opaque_type(raw[1], table, module_id=module_id)
			err = resolve_opaque_type(raw[2], table, module_id=module_id)
			return table.new_fnresult(ok, err)
		if len(raw) == 2:
			ok = resolve_opaque_type(raw[0], table, module_id=module_id)
			err = resolve_opaque_type(raw[1], table, module_id=module_id)
			return table.new_fnresult(ok, err)
		return table.ensure_unknown()

	return table.ensure_unknown()


__all__ = ["resolve_opaque_type"]
