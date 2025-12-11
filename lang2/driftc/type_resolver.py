# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Minimal type resolver for lang2.

Given a module-like object (or any iterable of function decls) with declared
parameter/return types and throws clauses, build:
  - a shared TypeTable
  - a mapping of function name -> FnSignature with TypeIds populated

This is intentionally shallow: it only resolves declared types (Int/Bool/String/
Error/FnResult<...>) and does not perform expression-level type checking. It
exists to feed real TypeIds into the checker pipeline so legacy string/tuple
type shims can be retired.
"""

from __future__ import annotations

from typing import Iterable, Tuple

from lang2.checker import FnSignature
from lang2.core.types_core import TypeId, TypeKind, TypeTable


def resolve_program_signatures(func_decls: Iterable[object]) -> tuple[TypeTable, dict[str, FnSignature]]:
	"""
	Resolve declared types on function declarations into TypeIds.

	Each decl is expected to expose:
	  - name: str
	  - params: iterable with a .type annotation (string/tuple/etc.)
	  - return_type: declared return type
	  - throws or throws_events: optional iterable of event names
	  - loc (optional): carried into FnSignature.loc
	  - is_extern / is_intrinsic (optional): flags
	  - is_method: bool (True when declared inside an `implement Type` block)
	  - self_mode: optional str ("value", "ref", "ref_mut") for methods
	  - impl_target: optional TypeExpr for the nominal type the impl targets
	  - module: optional str module name
	"""
	table = TypeTable()

	# Seed some common scalars; unknowns/new scalars are created on demand.
	int_ty = table.ensure_int()
	bool_ty = table.ensure_bool()
	str_ty = table.ensure_string()
	err_ty = table.new_error("Error")
	uint_ty = table.ensure_uint()

	signatures: dict[str, FnSignature] = {}

	for decl in func_decls:
		name = getattr(decl, "name")
		decl_loc = getattr(decl, "loc", None)
		is_extern = bool(getattr(decl, "is_extern", False))
		is_intrinsic = bool(getattr(decl, "is_intrinsic", False))
		module_name = getattr(decl, "module", None)

		# Params
		raw_params = []
		param_names: list[str] = []
		param_type_ids: list[TypeId] = []
		for p in getattr(decl, "params", []):
			raw_ty = getattr(p, "type", None)
			raw_params.append(raw_ty)
			param_names.append(getattr(p, "name", f"p{len(param_names)}"))
			param_type_ids.append(_resolve_type(raw_ty, table, int_ty, bool_ty, str_ty, err_ty))

		# Return
		raw_ret = getattr(decl, "return_type", None)
		return_type_id = _resolve_type(raw_ret, table, int_ty, bool_ty, str_ty, err_ty)
		error_type_id = None
		ret_def = table.get(return_type_id)
		if ret_def.kind is TypeKind.FNRESULT and len(ret_def.param_types) >= 2:
			error_type_id = ret_def.param_types[1]

		throws = _throws_from_decl(decl)
		declared_can_throw = True if throws else None
		if declared_can_throw is None and ret_def.kind is TypeKind.FNRESULT:
			declared_can_throw = True

		is_method = bool(getattr(decl, "is_method", False))
		self_mode = getattr(decl, "self_mode", None)
		impl_target_type_id: TypeId | None = None
		if is_method and getattr(decl, "impl_target", None) is not None:
			impl_target_type_id = _resolve_type(decl.impl_target, table, int_ty, bool_ty, str_ty, err_ty)

		signatures[name] = FnSignature(
			name=name,
			method_name=getattr(decl, "method_name", None) or name,
			loc=decl_loc,
			param_type_ids=param_type_ids,
			return_type_id=return_type_id,
			error_type_id=error_type_id,
			declared_can_throw=declared_can_throw,
			is_extern=is_extern,
			is_intrinsic=is_intrinsic,
			# Legacy/raw fields for compatibility
			param_types=raw_params,
			return_type=raw_ret,
			throws_events=throws,
			param_names=param_names if param_names else None,
			is_method=is_method,
			self_mode=self_mode,
			impl_target_type_id=impl_target_type_id,
			module=module_name,
		)

	return table, signatures


def _resolve_type(raw: object, table: TypeTable, int_ty: TypeId, bool_ty: TypeId, str_ty: TypeId, err_ty: TypeId) -> TypeId:
	"""
	Map a raw type annotation into a TypeId using the provided TypeTable.

	Supported shapes:
	  - "Int", "Bool", "String", "Error" (strings)
	  - tuples of the form ("FnResult", ok, err) or (ok, err)
	  - strings containing "FnResult" (treated as FnResult<Int, Error>)
	  - strings containing "Array<...>" (treated as Array<inner>)
	Unknown shapes are registered as scalar/unknown to keep resolution total.
	"""
	if raw is None:
		return table.new_unknown("Unknown")
	if isinstance(raw, TypeId):
		return raw
	# Accept duck-typed TypeExpr from the parser adapter.
	if hasattr(raw, "name") and hasattr(raw, "args"):
		name = getattr(raw, "name")
		args = getattr(raw, "args")
		if name == "FnResult":
			ok = _resolve_type(args[0] if args else None, table, int_ty, bool_ty, str_ty, err_ty)
			err = _resolve_type(args[1] if len(args) > 1 else err_ty, table, int_ty, bool_ty, str_ty, err_ty)
			return table.new_fnresult(ok, err)
		if name == "Array":
			elem = _resolve_type(args[0] if args else None, table, int_ty, bool_ty, str_ty, err_ty)
			return table.new_array(elem)
		if name == "Uint":
			return table.ensure_uint()
		if name == "Int":
			return int_ty
		if name == "Bool":
			return bool_ty
		if name == "String":
			return str_ty
		if name == "Error":
			return err_ty
		# Generic with unknown name: register as scalar to keep total.
		return table.new_scalar(str(name))
	if isinstance(raw, str):
		if raw == "Int":
			return int_ty
		if raw == "Bool":
			return bool_ty
		if raw == "String":
			return str_ty
		if raw == "Error":
			return err_ty
		if raw == "Uint":
			return table.ensure_uint()
		if "FnResult" in raw:
			return table.new_fnresult(int_ty, err_ty)
		if raw.startswith("Array<") and raw.endswith(">"):
			inner = raw[len("Array<"):-1]
			elem_ty = _resolve_type(inner, table, int_ty, bool_ty, str_ty, err_ty)
			return table.new_array(elem_ty)
		return table.new_scalar(raw)
	if isinstance(raw, tuple):
		# Tuple forms: ('FnResult', ok, err) or (ok, err)
		if len(raw) >= 3 and raw[0] == "FnResult":
			ok = _resolve_type(raw[1], table, int_ty, bool_ty, str_ty, err_ty)
			err = _resolve_type(raw[2], table, int_ty, bool_ty, str_ty, err_ty)
			return table.new_fnresult(ok, err)
		if len(raw) == 2:
			ok = _resolve_type(raw[0], table, int_ty, bool_ty, str_ty, err_ty)
			err = _resolve_type(raw[1], table, int_ty, bool_ty, str_ty, err_ty)
			return table.new_fnresult(ok, err)
		return table.new_unknown(str(raw))
	return table.new_unknown(str(raw))


def _throws_from_decl(decl: object) -> Tuple[str, ...]:
	throws = getattr(decl, "throws", None)
	if throws is None:
		throws = getattr(decl, "throws_events", None)
	if throws is None:
		return ()
	return tuple(throws)


__all__ = ["resolve_program_signatures"]
