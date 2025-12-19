# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Compiler-private iterator intrinsics (MVP).

lang2 does not have real modules/traits yet, but the language surface already
includes `for x in expr { ... }`. To keep that surface stable without
committing to a public iterator ABI, the compiler treats a small subset of
method calls as *intrinsics*:

  - `Array<T>.iter() -> __ArrayIter_<T>`
  - `__ArrayIter_<T>.next() -> Optional<T>`

This module is the *single* source of truth for the internal iterator type
shape so that:

  - the typed checker can assign consistent TypeIds,
  - MIR lowering can reliably generate field accesses and control-flow,
  - codegen sees stable structs within one compilation unit,
  - we do not duplicate the "how do we name/declare __ArrayIter_<T>" logic in
    multiple places.

Important: everything here is compiler-private. The `__ArrayIter_<T>` name is
not a stable public ABI and can change once real traits/modules exist.
"""

from __future__ import annotations

from lang2.driftc.core.types_core import TypeId, TypeKind, TypeTable


def type_key_for_typeid(ty_id: TypeId, table: TypeTable) -> str:
	"""
	Return a stable (within a compilation unit) string key for a TypeId.

	This is used solely to build readable and unique names for compiler-private
	types like `__ArrayIter_<TKey>`. It intentionally does *not* promise ABI
	stability across compiler versions.
	"""
	td = table.get(ty_id)
	if td.kind in (TypeKind.SCALAR, TypeKind.STRUCT, TypeKind.VARIANT):
		if td.param_types:
			inner = "_".join(type_key_for_typeid(t, table) for t in td.param_types)
			return f"{td.name}_{inner}"
		return td.name
	if td.kind is TypeKind.ARRAY and td.param_types:
		return f"Array_{type_key_for_typeid(td.param_types[0], table)}"
	if td.kind is TypeKind.REF and td.param_types:
		pfx = "RefMut" if td.ref_mut else "Ref"
		return f"{pfx}_{type_key_for_typeid(td.param_types[0], table)}"
	return td.kind.name


def ensure_array_iter_struct(array_ty: TypeId, table: TypeTable) -> TypeId:
	"""
	Ensure the internal `__ArrayIter_<T>` struct type exists for `Array<T>`.

	Internal layout (compiler-private):
	  struct __ArrayIter_<T> { arr: Array<T>, idx: Int }

	The MIR lowering relies on this exact field order:
	  - field 0: arr (Array<T>)
	  - field 1: idx (Int)
	"""
	td = table.get(array_ty)
	if td.kind is not TypeKind.ARRAY or not td.param_types:
		raise ValueError("ensure_array_iter_struct requires Array<T>")
	elem_ty = td.param_types[0]
	key = type_key_for_typeid(elem_ty, table)
	name = f"__ArrayIter_{key}"
	struct_id = table.declare_struct("lang.core", name, ["arr", "idx"])
	table.define_struct_fields(struct_id, [array_ty, table.ensure_int()])
	return struct_id


def is_array_iter_struct(ty_id: TypeId, table: TypeTable) -> bool:
	"""Return True if `ty_id` is a compiler-private array iterator struct."""
	td = table.get(ty_id)
	return td.kind is TypeKind.STRUCT and td.name.startswith("__ArrayIter_")


__all__ = [
	"ensure_array_iter_struct",
	"is_array_iter_struct",
	"type_key_for_typeid",
]
