# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Type parameter substitution helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.core.types_core import TypeId, TypeKind, TypeParamId, TypeTable


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
				return table.ensure_struct_instantiated(inst.base_id, list(new_args))
			return type_id
	if td.kind is TypeKind.VARIANT:
		inst = table.get_variant_instance(type_id)
		if inst is not None:
			new_args = [apply_subst(a, subst, table) for a in inst.type_args]
			if new_args != inst.type_args:
				return table.ensure_instantiated(inst.base_id, list(new_args))
			return type_id
	if not td.param_types:
		return type_id
	new_params = [apply_subst(p, subst, table) for p in td.param_types]
	if new_params == td.param_types:
		return type_id
	if td.kind is TypeKind.ARRAY:
		return table.new_array(new_params[0])
	if td.kind is TypeKind.OPTIONAL:
		return table.new_optional(new_params[0])
	if td.kind is TypeKind.REF:
		return table.ensure_ref_mut(new_params[0]) if td.ref_mut else table.ensure_ref(new_params[0])
	if td.kind is TypeKind.FNRESULT:
		return table.ensure_fnresult(new_params[0], new_params[1] if len(new_params) > 1 else table.ensure_error())
	if td.kind is TypeKind.FUNCTION:
		if not new_params:
			return type_id
		return table.new_function(td.name, new_params[:-1], new_params[-1])
	if td.kind is TypeKind.VARIANT:
		if td.param_types:
			base = table.get_variant_base(module_id=td.module_id or "", name=td.name)
			if base is not None:
				return table.ensure_instantiated(base, list(new_params))
		return type_id
	return type_id


__all__ = ["Subst", "apply_subst"]
