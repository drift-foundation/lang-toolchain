from __future__ import annotations

"""
Shared helpers for resolving raw/builtin type shapes into TypeIds.

This centralizes the knowledge of builtin names (`Int`, `Bool`, `String`,
`Uint`, `Float`, `Void`, `Error`, `DiagnosticValue`, `Array`, `Optional`, `FnResult`) so resolver and checker code
stay in sync as the language evolves.
"""

from lang2.driftc.core.types_core import TypeId, TypeTable


def resolve_opaque_type(raw: object, table: TypeTable) -> TypeId:
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
		if name in {"&", "&mut"}:
			inner = resolve_opaque_type(args[0] if args else None, table)
			return table.ensure_ref_mut(inner) if name == "&mut" else table.ensure_ref(inner)
		if name == "Void":
			return table.ensure_void()
		if name == "FnResult":
			ok = resolve_opaque_type(args[0] if args else None, table)
			err = resolve_opaque_type(args[1] if len(args) > 1 else table.ensure_error(), table)
			return table.new_fnresult(ok, err)
		if name == "Array":
			elem = resolve_opaque_type(args[0] if args else None, table)
			return table.new_array(elem)
		if name == "Optional":
			inner = resolve_opaque_type(args[0] if args else None, table)
			return table.new_optional(inner)
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
		# User-defined nominal types (e.g. structs) and unknown names.
		return table.ensure_named(str(name))

	# String forms.
	if isinstance(raw, str):
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
				ok_ty = resolve_opaque_type(ok_raw, table)
				err_ty = resolve_opaque_type(err_raw, table)
				return table.new_fnresult(ok_ty, err_ty)
			return table.ensure_unknown()
		if raw == "FnResult":
			return table.ensure_unknown()
		if raw.startswith("Array<") and raw.endswith(">"):
			inner = raw[len("Array<"):-1]
			elem_ty = resolve_opaque_type(inner, table)
			return table.new_array(elem_ty)
		if raw.startswith("Optional<") and raw.endswith(">"):
			inner = raw[len("Optional<"):-1]
			inner_ty = resolve_opaque_type(inner, table)
			return table.new_optional(inner_ty)
		# User-defined nominal types (e.g. structs) and unknown names.
		return table.ensure_named(raw)

	# Tuple forms used by legacy call sites.
	if isinstance(raw, tuple):
		if len(raw) >= 3 and raw[0] == "FnResult":
			ok = resolve_opaque_type(raw[1], table)
			err = resolve_opaque_type(raw[2], table)
			return table.new_fnresult(ok, err)
		if len(raw) == 2:
			ok = resolve_opaque_type(raw[0], table)
			err = resolve_opaque_type(raw[1], table)
			return table.new_fnresult(ok, err)
		return table.ensure_unknown()

	return table.ensure_unknown()


__all__ = ["resolve_opaque_type"]
