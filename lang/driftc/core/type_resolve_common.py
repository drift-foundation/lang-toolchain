from __future__ import annotations

"""
Shared helpers for resolving raw/builtin type shapes into TypeIds.

This centralizes the knowledge of builtin names (`Int`, `Bool`, `String`,
`Uint`, `Float`, `Void`, `Error`, `DiagnosticValue`, `Array`, `Optional`, `FnResult`, `fn`) so resolver and checker code
stay in sync as the language evolves.
"""

from lang.driftc.core.types_core import TypeId, TypeKind, TypeParamId, TypeTable

_CORE_VARIANT_ALLOWLIST: set[str] = {"Optional"}


def _raw_can_throw(raw: object) -> bool:
	if hasattr(raw, "can_throw") and callable(getattr(raw, "can_throw")):
		return bool(raw.can_throw())
	val = getattr(raw, "fn_throws", True)
	if val is None:
		raise TypeError("fn_throws must be bool when provided")
	return bool(val)


def resolve_opaque_type(raw: object, table: TypeTable, *, module_id: str | None = None, type_params: dict[str, TypeParamId | TypeId] | None = None, allow_generic_base: bool = False, alias_stack: list[tuple[str | None, str]] | None = None) -> TypeId:
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
		if alias_stack is None:
			alias_stack = []
		if type_params and name in type_params and not args:
			param_val = type_params[name]
			if isinstance(param_val, TypeId):
				return param_val
			return table.ensure_typevar(param_val, name=name)
		alias_def = table.lookup_type_alias(module_id=origin_mod, name=str(name))
		if alias_def is not None:
			alias_params, alias_target, _loc = alias_def
			if len(args) != len(alias_params):
				return table.ensure_unknown()
			alias_key = (origin_mod, str(name))
			if alias_key in alias_stack:
				return table.ensure_unknown()
			local_params = dict(type_params or {})
			for idx, param_name in enumerate(alias_params):
				arg = args[idx] if idx < len(args) else None
				local_params[param_name] = resolve_opaque_type(
					arg,
					table,
					module_id=origin_mod,
					type_params=type_params,
					allow_generic_base=allow_generic_base,
					alias_stack=alias_stack + [alias_key],
				)
			return resolve_opaque_type(
				alias_target,
				table,
				module_id=origin_mod,
				type_params=local_params,
				allow_generic_base=allow_generic_base,
				alias_stack=alias_stack + [alias_key],
			)
		if name in {"&", "&mut"}:
			inner = resolve_opaque_type(
				args[0] if args else None,
				table,
				module_id=origin_mod,
				type_params=type_params,
				alias_stack=alias_stack,
			)
			return table.ensure_ref_mut(inner) if name == "&mut" else table.ensure_ref(inner)
		if name == "Ptr":
			if origin_mod != "std.mem":
				return table.ensure_unknown()
			inner = resolve_opaque_type(
				args[0] if args else None,
				table,
				module_id=origin_mod,
				type_params=type_params,
				alias_stack=alias_stack,
			)
			return table.new_ptr(inner, module_id=origin_mod)
		if name == "Void":
			return table.ensure_void()
		if not args:
			base = None
			if origin_mod is not None:
				base = table.get_variant_base(module_id=origin_mod, name=str(name))
				if base is None:
					base = table.get_struct_base(module_id=origin_mod, name=str(name))
				if base is None:
					base = table.get_interface_base(module_id=origin_mod, name=str(name))
			if base is None and name in _CORE_VARIANT_ALLOWLIST:
				base = table.get_variant_base(module_id="lang.core", name=str(name))
			if base is not None:
				if allow_generic_base:
					return base
				schema = table.get_variant_schema(base)
				if schema is not None and schema.type_params:
					return table.ensure_unknown()
				struct = table.get_struct_schema(base)
				if struct is not None and struct.type_params:
					return table.ensure_unknown()
				iface = table.get_interface_schema(base)
				if iface is not None and iface.type_params:
					return table.ensure_unknown()
				return base
		# Generic nominal instantiation (MVP: variants/structs/interfaces). Example: Optional<Int>.
		if args:
			base = None
			if origin_mod is not None:
				base = table.get_variant_base(module_id=origin_mod, name=str(name))
				if base is None:
					base = table.get_struct_base(module_id=origin_mod, name=str(name))
				if base is None:
					base = table.get_interface_base(module_id=origin_mod, name=str(name))
			if base is None:
				if name == "Optional":
					base = table.ensure_optional_base()
				elif name in _CORE_VARIANT_ALLOWLIST:
					base = table.get_variant_base(module_id="lang.core", name=str(name))
			if base is not None:
				arg_mod = module_id if module_id is not None else origin_mod
				arg_ids = [
					resolve_opaque_type(a, table, module_id=arg_mod, type_params=type_params, alias_stack=alias_stack)
					for a in list(args)
				]
				if base in table.variant_schemas:
					try:
						if any(table.has_typevar(a) for a in arg_ids):
							return table.ensure_variant_template(base, arg_ids)
						return table.ensure_variant_instantiated(base, arg_ids)
					except ValueError:
						return table.ensure_unknown()
				if base in table.struct_bases:
					try:
						if any(table.has_typevar(a) for a in arg_ids):
							return table.ensure_struct_template(base, arg_ids)
						return table.ensure_struct_instantiated(base, arg_ids)
					except ValueError:
						return table.ensure_unknown()
				if base in table.interface_bases:
					try:
						if any(table.has_typevar(a) for a in arg_ids):
							return table.ensure_interface_template(base, arg_ids)
						return table.ensure_interface_instantiated(base, arg_ids)
					except ValueError:
						return table.ensure_unknown()
			if name not in {"Array", "Optional", "FnResult", "fn"}:
				if origin_mod is not None:
					arg_mod = module_id if module_id is not None else origin_mod
					arg_ids = [
						resolve_opaque_type(a, table, module_id=arg_mod, type_params=type_params, alias_stack=alias_stack)
						for a in list(args)
					]
					return table._add(TypeKind.FORWARD_NOMINAL, name, arg_ids, module_id=origin_mod, register_named=False)
				return table.ensure_unknown()
		if name == "FnResult":
			ok = resolve_opaque_type(args[0] if args else None, table, module_id=origin_mod, type_params=type_params, alias_stack=alias_stack)
			err = resolve_opaque_type(
				args[1] if len(args) > 1 else table.ensure_error(),
				table,
				module_id=origin_mod,
				type_params=type_params,
				alias_stack=alias_stack,
			)
			return table.ensure_fnresult(ok, err)
		if name == "fn":
			if not args:
				return table.ensure_unknown()
			param_ids = [
				resolve_opaque_type(a, table, module_id=origin_mod, type_params=type_params, alias_stack=alias_stack)
				for a in list(args[:-1])
			]
			ret_id = resolve_opaque_type(args[-1], table, module_id=origin_mod, type_params=type_params, alias_stack=alias_stack)
			can_throw = _raw_can_throw(raw)
			return table.ensure_function(param_ids, ret_id, can_throw=can_throw)
		if name == "Array":
			elem = resolve_opaque_type(args[0] if args else None, table, module_id=origin_mod, type_params=type_params, alias_stack=alias_stack)
			return table.new_array(elem)
		if name == "DiagnosticValue":
			return table.ensure_diagnostic_value()
		if name == "Uint":
			return table.ensure_uint()
		if name == "Uint64":
			return table.ensure_uint64()
		if name == "Int":
			return table.ensure_int()
		if name == "Byte":
			return table.ensure_byte()
		if name == "Bool":
			return table.ensure_bool()
		if name == "String":
			return table.ensure_string()
		if name == "Float":
			return table.ensure_float()
		if name == "Error":
			return table.ensure_error()
		# Exception schemas are modeled as Error in type positions.
		# This allows helper signatures like err.ResultError without introducing
		# distinct nominal types for each exception.
		if origin_mod is not None:
			event_fqn = f"{origin_mod}:{name}"
			if event_fqn in table.exception_schemas:
				return table.ensure_error()
		# User-defined nominal types (structs/variants/interfaces) and unknown names.
		if origin_mod is not None:
			ty = table.get_nominal(kind=TypeKind.STRUCT, module_id=origin_mod, name=str(name))
			if ty is not None:
				if not args and ty in table.struct_bases:
					schema = table.struct_bases.get(ty)
					if schema is not None and schema.type_params and not allow_generic_base:
						return table.ensure_unknown()
				return ty
			ty = table.get_nominal(kind=TypeKind.VARIANT, module_id=origin_mod, name=str(name))
			if ty is not None:
				if args:
					type_args = [
						resolve_opaque_type(
							arg,
							table,
							module_id=origin_mod,
							type_params=type_params,
							allow_generic_base=allow_generic_base,
							alias_stack=alias_stack,
						)
						for arg in args
					]
					try:
						return table.ensure_variant_template(ty, type_args)
					except Exception:
						return table.ensure_unknown()
				if not args and ty in table.variant_schemas:
					schema = table.variant_schemas.get(ty)
					if schema is not None and schema.type_params and not allow_generic_base:
						return table.ensure_unknown()
				return ty
			ty = table.get_nominal(kind=TypeKind.INTERFACE, module_id=origin_mod, name=str(name))
			if ty is not None:
				if not args and ty in table.interface_bases:
					schema = table.interface_bases.get(ty)
					if schema is not None and schema.type_params and not allow_generic_base:
						return table.ensure_unknown()
				return ty
			# Toolchain-provided variants (e.g. lang.core Optional) are visible as
			# unqualified type references even in user modules. If the current module
			# does not define a variant of this name, prefer the toolchain base.
			if str(name) in _CORE_VARIANT_ALLOWLIST:
				core_base = table.get_variant_base(module_id="lang.core", name=str(name))
				if core_base is not None:
					if not allow_generic_base:
						schema = table.variant_schemas.get(core_base)
						if schema is not None and schema.type_params:
							return table.ensure_unknown()
					return core_base
			return table.ensure_named(str(name), module_id=origin_mod)
		# No module context: fall back to a forward nominal placeholder.
		if str(name) in _CORE_VARIANT_ALLOWLIST:
			core_base = table.get_variant_base(module_id="lang.core", name=str(name))
			if core_base is not None:
				if not allow_generic_base:
					schema = table.variant_schemas.get(core_base)
					if schema is not None and schema.type_params:
						return table.ensure_unknown()
				return core_base
		return table.ensure_named(str(name))

	# String forms.
	if isinstance(raw, str):
		def _split_top_level_comma(text: str) -> tuple[str, str] | None:
			depth = 0
			for idx, ch in enumerate(text):
				if ch == "<":
					depth += 1
				elif ch == ">":
					depth = max(depth - 1, 0)
				elif ch == "," and depth == 0:
					return text[:idx].strip(), text[idx + 1 :].strip()
			return None

		if type_params and raw in type_params:
			return table.ensure_typevar(type_params[raw], name=raw)
		alias_def = table.lookup_type_alias(module_id=module_id, name=str(raw))
		if alias_def is not None:
			alias_params, alias_target, _loc = alias_def
			if alias_params:
				return table.ensure_unknown()
			alias_key = (module_id, str(raw))
			if alias_stack is None:
				alias_stack = []
			if alias_key in alias_stack:
				return table.ensure_unknown()
			return resolve_opaque_type(
				alias_target,
				table,
				module_id=module_id,
				type_params=type_params,
				allow_generic_base=allow_generic_base,
				alias_stack=alias_stack + [alias_key],
			)
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
		if raw == "Uint64":
			return table.ensure_uint64()
		if raw == "Byte":
			return table.ensure_byte()
		if raw.startswith("Ptr<") and raw.endswith(">") and module_id == "std.mem":
			inner = raw[len("Ptr<"):-1]
			inner_ty = resolve_opaque_type(
				inner,
				table,
				module_id=module_id,
				type_params=type_params,
				allow_generic_base=allow_generic_base,
				alias_stack=alias_stack,
			)
			return table.new_ptr(inner_ty, module_id=module_id)
		if raw.startswith("FnResult<") and raw.endswith(">"):
			inner = raw[len("FnResult<"):-1]
			parts = _split_top_level_comma(inner)
			if parts is None:
				return table.ensure_unknown()
			ok_raw, err_raw = parts
			ok_ty = resolve_opaque_type(
				ok_raw,
				table,
				module_id=module_id,
				type_params=type_params,
				allow_generic_base=allow_generic_base,
				alias_stack=alias_stack,
			)
			err_ty = resolve_opaque_type(
				err_raw,
				table,
				module_id=module_id,
				type_params=type_params,
				allow_generic_base=allow_generic_base,
				alias_stack=alias_stack,
			)
			return table.ensure_fnresult(ok_ty, err_ty)
		if raw == "FnResult":
			return table.ensure_unknown()
		if raw.startswith("Array<") and raw.endswith(">"):
			inner = raw[len("Array<"):-1]
			elem_ty = resolve_opaque_type(inner, table, module_id=module_id, type_params=type_params, alias_stack=alias_stack)
			return table.new_array(elem_ty)
		if raw.startswith("Optional<") and raw.endswith(">"):
			inner = raw[len("Optional<"):-1]
			inner_ty = resolve_opaque_type(inner, table, module_id=module_id, type_params=type_params, alias_stack=alias_stack)
			base_id = table.ensure_optional_base()
			try:
				if table.has_typevar(inner_ty):
					return table.ensure_variant_template(base_id, [inner_ty])
				return table.ensure_variant_instantiated(base_id, [inner_ty])
			except ValueError:
				return table.ensure_unknown()
		# User-defined nominal types (e.g. structs/variants/interfaces) and unknown names.
		if module_id is not None:
			ty = table.get_nominal(kind=TypeKind.STRUCT, module_id=module_id, name=str(raw))
			if ty is not None:
				if ty in table.struct_bases and not allow_generic_base:
					schema = table.struct_bases.get(ty)
					if schema is not None and schema.type_params:
						return table.ensure_unknown()
				return ty
			ty = table.get_nominal(kind=TypeKind.VARIANT, module_id=module_id, name=str(raw))
			if ty is not None:
				if ty in table.variant_schemas and not allow_generic_base:
					schema = table.variant_schemas.get(ty)
					if schema is not None and schema.type_params:
						return table.ensure_unknown()
				return ty
			ty = table.get_nominal(kind=TypeKind.INTERFACE, module_id=module_id, name=str(raw))
			if ty is not None:
				if ty in table.interface_bases and not allow_generic_base:
					schema = table.interface_bases.get(ty)
					if schema is not None and schema.type_params:
						return table.ensure_unknown()
				return ty
			if raw in _CORE_VARIANT_ALLOWLIST:
				core_base = table.get_variant_base(module_id="lang.core", name=str(raw))
				if core_base is not None:
					if not allow_generic_base:
						schema = table.variant_schemas.get(core_base)
						if schema is not None and schema.type_params:
							return table.ensure_unknown()
					return core_base
			unique = (
				table.find_unique_nominal_by_name(kind=TypeKind.STRUCT, name=str(raw))
				or table.find_unique_nominal_by_name(kind=TypeKind.VARIANT, name=str(raw))
				or table.find_unique_nominal_by_name(kind=TypeKind.INTERFACE, name=str(raw))
			)
			if unique is not None:
				return unique
			return table.ensure_named(raw, module_id=module_id)

	# Tuple forms used by legacy call sites.
	if isinstance(raw, tuple):
		if len(raw) >= 3 and raw[0] == "FnResult":
			ok = resolve_opaque_type(
				raw[1],
				table,
				module_id=module_id,
				type_params=type_params,
				allow_generic_base=allow_generic_base,
				alias_stack=alias_stack,
			)
			err = resolve_opaque_type(
				raw[2],
				table,
				module_id=module_id,
				type_params=type_params,
				allow_generic_base=allow_generic_base,
				alias_stack=alias_stack,
			)
			return table.ensure_fnresult(ok, err)
		if len(raw) == 2:
			ok = resolve_opaque_type(
				raw[0],
				table,
				module_id=module_id,
				type_params=type_params,
				allow_generic_base=allow_generic_base,
				alias_stack=alias_stack,
			)
			err = resolve_opaque_type(
				raw[1],
				table,
				module_id=module_id,
				type_params=type_params,
				allow_generic_base=allow_generic_base,
				alias_stack=alias_stack,
			)
			return table.ensure_fnresult(ok, err)
		return table.ensure_unknown()

	return table.ensure_unknown()


__all__ = ["resolve_opaque_type"]
