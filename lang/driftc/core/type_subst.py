# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Type parameter substitution helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeId, TypeKind, TypeParamId, TypeTable


@dataclass(frozen=True)
class Subst:
	"""Explicit, owner-scoped type parameter substitution."""

	owner: FunctionId
	args: List[TypeId]


def apply_subst(type_id: TypeId, subst: Subst, table: TypeTable) -> TypeId:
	"""Apply a substitution to a TypeId, returning a (possibly new) TypeId."""
	if type_id is None:
		return type_id
	td = table.get(type_id)
	if td.kind is TypeKind.TYPEVAR:
		param_id = td.type_param_id
		if param_id is None:
			return type_id
		if param_id.owner != subst.owner:
			return type_id
		idx = int(param_id.index)
		if idx < 0 or idx >= len(subst.args):
			return type_id
		return subst.args[idx]
	if td.kind is TypeKind.STRUCT:
		inst = table.get_struct_instance(type_id)
		if inst is not None:
			new_args = [apply_subst(a, subst, table) for a in inst.type_args]
			if new_args != inst.type_args:
				if any(table.has_typevar(a) for a in new_args):
					return table.ensure_struct_template(inst.base_id, list(new_args))
				return table.ensure_struct_instantiated(inst.base_id, list(new_args))
			return type_id
	if td.kind is TypeKind.VARIANT:
		inst = table.get_variant_instance(type_id)
		if inst is not None:
			new_args = [apply_subst(a, subst, table) for a in inst.type_args]
			if new_args != inst.type_args:
				if any(table.has_typevar(a) for a in new_args):
					return table.ensure_variant_template(inst.base_id, list(new_args))
				return table.ensure_variant_instantiated(inst.base_id, list(new_args))
			return type_id
		if td.param_types:
			base = table.get_variant_base(module_id=td.module_id or "", name=td.name)
			if base is None:
				raise AssertionError("variant type has param_types but no instance (compiler bug)")
			new_args = [apply_subst(p, subst, table) for p in td.param_types]
			if any(table.has_typevar(a) for a in new_args):
				return table.ensure_variant_template(base, list(new_args))
			return table.ensure_variant_instantiated(base, list(new_args))
	if td.kind is TypeKind.INTERFACE:
		inst = table.get_interface_instance(type_id)
		if inst is not None:
			new_args = [apply_subst(a, subst, table) for a in inst.type_args]
			if new_args != inst.type_args:
				if any(table.has_typevar(a) for a in new_args):
					return table.ensure_interface_template(inst.base_id, list(new_args))
				return table.ensure_interface_instantiated(inst.base_id, list(new_args))
			return type_id
	if td.kind is TypeKind.FORWARD_NOMINAL and td.param_types:
		new_params = [apply_subst(p, subst, table) for p in td.param_types]
		if new_params == td.param_types:
			return type_id
		resolved_base = (
			table.get_nominal(kind=TypeKind.VARIANT, module_id=td.module_id, name=td.name)
			or table.get_nominal(kind=TypeKind.STRUCT, module_id=td.module_id, name=td.name)
			or table.get_nominal(kind=TypeKind.INTERFACE, module_id=td.module_id, name=td.name)
		)
		if resolved_base is None:
			resolved_base = (
				table.find_unique_nominal_by_name(kind=TypeKind.VARIANT, name=td.name)
				or table.find_unique_nominal_by_name(kind=TypeKind.STRUCT, name=td.name)
				or table.find_unique_nominal_by_name(kind=TypeKind.INTERFACE, name=td.name)
			)
		if resolved_base is not None:
			has_typevar = any(table.has_typevar(a) for a in new_params)
			try:
				if resolved_base in getattr(table, "variant_schemas", {}):
					return table.ensure_variant_template(resolved_base, list(new_params)) if has_typevar else table.ensure_variant_instantiated(resolved_base, list(new_params))
				if resolved_base in getattr(table, "struct_bases", {}):
					return table.ensure_struct_template(resolved_base, list(new_params)) if has_typevar else table.ensure_struct_instantiated(resolved_base, list(new_params))
				if resolved_base in getattr(table, "interface_bases", {}):
					return table.ensure_interface_template(resolved_base, list(new_params)) if has_typevar else table.ensure_interface_instantiated(resolved_base, list(new_params))
			except (ValueError, KeyError):
				return type_id
		return type_id
	if not td.param_types:
		return type_id
	new_params = [apply_subst(p, subst, table) for p in td.param_types]
	if new_params == td.param_types:
		return type_id
	if td.kind is TypeKind.ARRAY:
		return table.new_array(new_params[0])
	if td.kind is TypeKind.REF:
		return table.ensure_ref_mut(new_params[0]) if td.ref_mut else table.ensure_ref(new_params[0])
	if td.kind is TypeKind.RAW_PTR:
		return table.new_ptr(new_params[0], module_id=td.module_id)
	if td.kind is TypeKind.FNRESULT:
		return table.ensure_fnresult(new_params[0], new_params[1] if len(new_params) > 1 else table.ensure_error())
	if td.kind is TypeKind.FUNCTION:
		if not new_params:
			return type_id
		return table.ensure_function(new_params[:-1], new_params[-1], can_throw=td.can_throw())
	return type_id


__all__ = ["Subst", "apply_subst"]
