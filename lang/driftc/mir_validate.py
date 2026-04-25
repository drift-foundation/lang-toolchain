# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import re
from typing import Callable, Mapping

from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.checker import FnSignature
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable
from lang.driftc.stage2 import mir_nodes as M


def _canonicalize_forward_nominal_type_id(
	type_table: TypeTable,
	ty_id: TypeId,
	*,
	_seen: set[tuple[str | None, str]] | None = None,
) -> TypeId:
	td = type_table.get(ty_id)
	seen = _seen if _seen is not None else set()
	if td.kind is TypeKind.REF and td.param_types:
		inner = _canonicalize_forward_nominal_type_id(type_table, td.param_types[0], _seen=seen)
		if inner != td.param_types[0]:
			return type_table.ensure_ref_mut(inner) if td.ref_mut else type_table.ensure_ref(inner)
		return ty_id
	if td.kind is TypeKind.ARRAY and td.param_types:
		elem = _canonicalize_forward_nominal_type_id(type_table, td.param_types[0], _seen=seen)
		if elem != td.param_types[0]:
			return type_table.new_array(elem)
		return ty_id
	if td.kind is TypeKind.FNRESULT and len(td.param_types) == 2:
		ok_ty = _canonicalize_forward_nominal_type_id(type_table, td.param_types[0], _seen=seen)
		err_ty = _canonicalize_forward_nominal_type_id(type_table, td.param_types[1], _seen=seen)
		if ok_ty != td.param_types[0] or err_ty != td.param_types[1]:
			return type_table.new_fnresult(ok_ty, err_ty)
		return ty_id
	if td.kind is not TypeKind.FORWARD_NOMINAL:
		return ty_id
	alias_key = (td.module_id, td.name)
	if alias_key in seen:
		return ty_id
	alias_def = type_table.lookup_type_alias(module_id=td.module_id, name=td.name)
	if alias_def is not None:
		alias_params, alias_target, _loc = alias_def
		if not alias_params:
			resolved = resolve_opaque_type(
				alias_target,
				type_table,
				module_id=td.module_id,
				type_params=None,
				allow_generic_base=True,
			)
			if resolved != ty_id:
				return _canonicalize_forward_nominal_type_id(type_table, resolved, _seen=seen | {alias_key})
	resolved_nom = (
		type_table.get_nominal(kind=TypeKind.STRUCT, module_id=td.module_id, name=td.name)
		or type_table.get_nominal(kind=TypeKind.VARIANT, module_id=td.module_id, name=td.name)
		or type_table.get_nominal(kind=TypeKind.INTERFACE, module_id=td.module_id, name=td.name)
	)
	return resolved_nom if resolved_nom is not None else ty_id


def validate_mir_variant_field_invariants(
	funcs: Mapping[FunctionId, M.MirFunc],
	type_table: TypeTable,
) -> None:
	"""Ensure VariantGetField/VariantGetFieldAddr respect variant arm/field schema."""
	for fn_id, func in funcs.items():
		for block in func.blocks.values():
			for instr in block.instructions:
				if not isinstance(instr, (M.VariantGetField, M.VariantGetFieldAddr)):
					continue
				variant_ty = _canonicalize_forward_nominal_type_id(type_table, instr.variant_ty)
				td = type_table.get(variant_ty)
				if td.kind is not TypeKind.VARIANT:
					raise AssertionError(
						f"MIR invariant violation: {instr.__class__.__name__} variant_ty is not a variant in {function_symbol(fn_id)}"
					)
				inst = type_table.get_variant_instance(variant_ty)
				if inst is None:
					raise AssertionError(
						f"MIR invariant violation: unresolved variant instance in {instr.__class__.__name__} for {function_symbol(fn_id)}"
					)
				arm = next((a for a in inst.arms if a.name == instr.ctor), None)
				if arm is None:
					raise AssertionError(
						f"MIR invariant violation: {instr.__class__.__name__} ctor '{instr.ctor}' missing in {function_symbol(fn_id)}"
					)
				if instr.field_index < 0 or instr.field_index >= len(arm.field_types):
					raise AssertionError(
						f"MIR invariant violation: {instr.__class__.__name__} field index out of range in {function_symbol(fn_id)}"
					)
				expected_ty = _canonicalize_forward_nominal_type_id(type_table, arm.field_types[instr.field_index])
				actual_ty = _canonicalize_forward_nominal_type_id(type_table, instr.field_ty)
				if expected_ty != actual_ty:
					raise AssertionError(
						f"MIR invariant violation: {instr.__class__.__name__} field_ty mismatch in {function_symbol(fn_id)}"
					)


def validate_mir_basic_hygiene(funcs: Mapping[FunctionId, M.MirFunc]) -> None:
	"""Ensure MIR operands refer to defined SSA values and declared locals."""
	def _iter_values(value: object):
		if isinstance(value, str):
			yield value
		elif isinstance(value, (list, tuple)):
			for item in value:
				yield from _iter_values(item)

	for fn_id, func in funcs.items():
		defined_values: set[str] = set()
		defined_values.update(func.params)
		for block in func.blocks.values():
			for instr in block.instructions:
				dest = getattr(instr, "dest", None)
				if isinstance(dest, str):
					defined_values.add(dest)
		declared_locals = set(func.params) | set(func.locals)

		def _check_value(name: str, instr_name: str, field_name: str) -> None:
			if name not in defined_values:
				raise AssertionError(
					f"MIR invariant violation: undefined SSA operand '{name}' in {instr_name}.{field_name} for {function_symbol(fn_id)}"
				)

		for block in func.blocks.values():
			for instr in block.instructions:
				instr_name = instr.__class__.__name__
				if isinstance(instr, M.DropValue):
					_check_value(instr.value, instr_name, "value")
				elif isinstance(instr, M.CopyValue):
					_check_value(instr.value, instr_name, "value")
				elif isinstance(instr, M.StoreLocal):
					_check_value(instr.value, instr_name, "value")
					if instr.local not in declared_locals:
						raise AssertionError(
							f"MIR invariant violation: unknown local '{instr.local}' in {instr_name}.local for {function_symbol(fn_id)}"
						)
				elif isinstance(instr, M.MoveOut):
					if instr.local not in declared_locals:
						raise AssertionError(
							f"MIR invariant violation: unknown local '{instr.local}' in {instr_name}.local for {function_symbol(fn_id)}"
						)
				elif isinstance(instr, M.LoadRef):
					_check_value(instr.ptr, instr_name, "ptr")
				elif isinstance(instr, M.StoreRef):
					_check_value(instr.ptr, instr_name, "ptr")
					_check_value(instr.value, instr_name, "value")
				elif isinstance(instr, M.MoveFromRef):
					# Atomic ownership-transfer primitive; reads `ptr`,
					# writes into named `local`.  Both must be valid.
					_check_value(instr.ptr, instr_name, "ptr")
					if instr.local not in declared_locals:
						raise AssertionError(
							f"MIR invariant violation: unknown local '{instr.local}' in {instr_name}.local for {function_symbol(fn_id)}"
						)
				elif isinstance(instr, M.BinaryOpInstr):
					_check_value(instr.left, instr_name, "left")
					_check_value(instr.right, instr_name, "right")
				elif isinstance(instr, M.UnaryOpInstr):
					_check_value(instr.operand, instr_name, "operand")
				elif isinstance(instr, M.Call):
					for arg in instr.args:
						_check_value(arg, instr_name, "args")
				elif isinstance(instr, M.CallIndirect):
					_check_value(instr.callee, instr_name, "callee")
					for arg in instr.args:
						_check_value(arg, instr_name, "args")
				elif isinstance(instr, M.CallIface):
					_check_value(instr.iface, instr_name, "iface")
					for arg in instr.args:
						_check_value(arg, instr_name, "args")
				elif isinstance(instr, M.VariantTag):
					_check_value(instr.variant, instr_name, "variant")
				elif isinstance(instr, M.VariantGetField):
					_check_value(instr.variant, instr_name, "variant")
				elif isinstance(instr, M.VariantGetFieldAddr):
					_check_value(instr.variant_ref, instr_name, "variant_ref")
				elif isinstance(instr, M.StructGetField):
					_check_value(instr.subject, instr_name, "subject")
				elif isinstance(instr, M.AddrOfField):
					_check_value(instr.base_ptr, instr_name, "base_ptr")
				elif isinstance(instr, M.LoadField):
					_check_value(instr.subject, instr_name, "subject")
				elif isinstance(instr, M.StoreField):
					_check_value(instr.subject, instr_name, "subject")
					_check_value(instr.value, instr_name, "value")
				elif isinstance(instr, M.ArrayElemInit):
					for val in _iter_values((instr.array, instr.index, instr.value)):
						_check_value(val, instr_name, "operand")
				elif isinstance(instr, M.ArrayElemInitUnchecked):
					for val in _iter_values((instr.array, instr.index, instr.value)):
						_check_value(val, instr_name, "operand")
				elif isinstance(instr, M.ArrayElemAssign):
					for val in _iter_values((instr.array, instr.index, instr.value)):
						_check_value(val, instr_name, "operand")
				elif isinstance(instr, M.ArrayElemDrop):
					for val in _iter_values((instr.array, instr.index)):
						_check_value(val, instr_name, "operand")
				elif isinstance(instr, M.ArrayElemTake):
					for val in _iter_values((instr.array, instr.index)):
						_check_value(val, instr_name, "operand")
				elif isinstance(instr, M.ArrayDrop):
					_check_value(instr.array, instr_name, "array")
				elif isinstance(instr, M.ArrayDup):
					_check_value(instr.array, instr_name, "array")
				elif isinstance(instr, M.ArrayIndexLoad):
					for val in _iter_values((instr.array, instr.index)):
						_check_value(val, instr_name, "operand")
				elif isinstance(instr, M.ArrayIndexLoadUnchecked):
					for val in _iter_values((instr.array, instr.index)):
						_check_value(val, instr_name, "operand")
				elif isinstance(instr, M.ArrayIndexStore):
					for val in _iter_values((instr.array, instr.index, instr.value)):
						_check_value(val, instr_name, "operand")
			term = block.terminator
			if isinstance(term, M.Return) and term.value is not None:
				_check_value(term.value, "Return", "value")
			elif isinstance(term, M.IfTerminator):
				_check_value(term.cond, "IfTerminator", "cond")

def validate_mir_call_invariants(funcs: Mapping[FunctionId, M.MirFunc]) -> None:
	"""Ensure MIR call instructions carry explicit can_throw flags and stable ids."""
	for fn_id, func in funcs.items():
		for block in func.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, M.Call):
					if not isinstance(instr.fn_id, FunctionId):
						raise AssertionError(f"MIR Call missing fn_id in {function_symbol(fn_id)}")
					if not isinstance(instr.can_throw, bool):
						raise AssertionError(f"MIR Call missing can_throw in {function_symbol(fn_id)}")
				elif isinstance(instr, M.CallIndirect):
					if not isinstance(instr.can_throw, bool):
						raise AssertionError(
							f"MIR CallIndirect missing can_throw in {function_symbol(fn_id)}"
						)
				elif isinstance(instr, M.CallIface):
					if not isinstance(instr.can_throw, bool):
						raise AssertionError(
							f"MIR CallIface missing can_throw in {function_symbol(fn_id)}"
						)


def validate_mir_call_types(
	funcs: Mapping[FunctionId, M.MirFunc],
	signatures_by_id: Mapping[FunctionId, FnSignature],
	type_table: TypeTable,
) -> None:
	"""Ensure no call-related MIR types carry TypeVar into codegen."""
	def _no_typevars(ty_id: TypeId) -> bool:
		return not type_table.has_typevar(ty_id)
	for fn_id, func in funcs.items():
		for block in func.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, M.Call):
					sig = signatures_by_id.get(instr.fn_id)
					if sig is None or sig.param_type_ids is None or sig.return_type_id is None:
						raise AssertionError(
							f"MIR invariant violation: unresolved call signature for {function_symbol(instr.fn_id)}"
						)
					if sig.type_params or getattr(sig, "impl_type_params", None):
						continue
					if any(type_table.has_typevar(t) for t in sig.param_type_ids):
						raise AssertionError(
							f"MIR invariant violation: unresolved typevars in call params for {function_symbol(instr.fn_id)}"
						)
					if type_table.has_typevar(sig.return_type_id):
						raise AssertionError(
							f"MIR invariant violation: unresolved typevars in call return for {function_symbol(instr.fn_id)}"
						)
				elif isinstance(instr, (M.CallIndirect, M.CallIface)):
					if any(not _no_typevars(t) for t in instr.param_types):
						raise AssertionError(
							f"MIR invariant violation: unresolved typevars in call param types for {function_symbol(fn_id)}"
						)
					if not _no_typevars(instr.user_ret_type):
						raise AssertionError(
							f"MIR invariant violation: unresolved typevars in call return type for {function_symbol(fn_id)}"
						)


def validate_mir_array_copy_invariants(
	funcs: Mapping[FunctionId, M.MirFunc],
	type_table: TypeTable,
) -> None:
	"""Ensure array ops observe CopyValue/MoveOut invariants for Copy elements."""
	def _copy_status_can_be_unknown(tid: TypeId) -> bool:
		seen: set[TypeId] = set()

		def _scan(cur: TypeId) -> bool:
			if cur in seen:
				return False
			seen.add(cur)
			td = type_table.get(cur)
			if td.kind in {TypeKind.TYPEVAR, TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL}:
				return True
			if td.kind is TypeKind.STRUCT:
				inst = type_table.get_struct_instance(cur)
				if inst is None:
					return True
				return any(_scan(f) for f in inst.field_types)
			if td.kind is TypeKind.VARIANT:
				inst = type_table.get_variant_instance(cur)
				if inst is None:
					return True
				for arm in inst.arms:
					if any(_scan(f) for f in arm.field_types):
						return True
				return False
			if td.kind is TypeKind.ARRAY and td.param_types:
				return _scan(td.param_types[0])
			for child in td.param_types:
				if _scan(child):
					return True
			return False

		return _scan(tid)

	for fn_id, func in funcs.items():
		defs: dict[M.ValueId, M.MirInstr] = {}
		for block in func.blocks.values():
			for instr in block.instructions:
				dest = getattr(instr, "dest", None)
				if isinstance(dest, str):
					defs[dest] = instr
		for block in func.blocks.values():
			instrs = block.instructions
			for idx, instr in enumerate(instrs):
				if isinstance(instr, M.ArrayIndexLoad):
					elem_ty = instr.elem_ty
					copy_status = type_table.copy_status(elem_ty)
					if copy_status is None:
						if _copy_status_can_be_unknown(elem_ty):
							continue
						raise AssertionError(
							f"MIR invariant violation: unresolved Copy status for array element type in {function_symbol(fn_id)}"
						)
					if copy_status and not type_table.is_bitcopy(elem_ty):
						if idx + 1 >= len(instrs):
							raise AssertionError(
								f"MIR invariant violation: ArrayIndexLoad in {function_symbol(fn_id)} must be followed by CopyValue for Copy element type"
							)
						next_instr = instrs[idx + 1]
						if not isinstance(next_instr, M.CopyValue) or next_instr.value != instr.dest:
							raise AssertionError(
								f"MIR invariant violation: ArrayIndexLoad in {function_symbol(fn_id)} must be immediately wrapped in CopyValue for Copy element type"
							)
				if isinstance(instr, (M.ArrayElemInit, M.ArrayElemInitUnchecked, M.ArrayElemAssign, M.ArrayIndexStore)):
					elem_ty = instr.elem_ty
					copy_status = type_table.copy_status(elem_ty)
					if copy_status is None:
						if _copy_status_can_be_unknown(elem_ty):
							continue
						raise AssertionError(
							f"MIR invariant violation: unresolved Copy status for array element type in {function_symbol(fn_id)}"
						)
					if copy_status and not type_table.is_bitcopy(elem_ty):
						src = defs.get(instr.value)
						if isinstance(src, (M.CopyValue, M.MoveOut, M.ArrayElemTake)):
							continue
						if isinstance(
							src,
							(
								M.LoadLocal,
								M.LoadRef,
								M.ArrayIndexLoad,
								M.StructGetField,
								M.VariantGetField,
							),
						):
							raise AssertionError(
								f"MIR invariant violation: array store in {function_symbol(fn_id)} must use CopyValue or MoveOut for Copy element type"
							)


def validate_mir_array_alloc_invariants(funcs: Mapping[FunctionId, M.MirFunc]) -> None:
	"""Ensure ArrayAlloc length is zero in v1 (length must be set via ArraySetLen)."""
	for fn_id, func in funcs.items():
		defs: dict[M.ValueId, M.MirInstr] = {}
		for block in func.blocks.values():
			for instr in block.instructions:
				dest = getattr(instr, "dest", None)
				if isinstance(dest, str):
					defs[dest] = instr
		for block in func.blocks.values():
			for instr in block.instructions:
				if not isinstance(instr, M.ArrayAlloc):
					continue
				src = defs.get(instr.length)
				if isinstance(src, (M.ConstUint, M.ConstInt, M.ConstUint64)) and src.value == 0:
					continue
				raise AssertionError(
					f"MIR invariant violation: ArrayAlloc in {function_symbol(fn_id)} must use length=0 in v1"
				)


def validate_mir_concrete_layout_types(
	funcs: Mapping[FunctionId, M.MirFunc],
	type_table: TypeTable,
) -> None:
	"""Ensure layout-sensitive MIR types are concrete (no TypeVar/ForwardNominal/Unknown)."""
	def _is_unresolved(ty_id: TypeId) -> bool:
		td = type_table.get(ty_id)
		return td.kind in (TypeKind.TYPEVAR, TypeKind.FORWARD_NOMINAL, TypeKind.UNKNOWN)

	def _check_type(fn_id: FunctionId, ty_id: TypeId, label: str) -> None:
		if _is_unresolved(ty_id):
			raise AssertionError(
				f"MIR invariant violation: unresolved layout type {type_table.get(ty_id).name} "
				f"in {label} for {function_symbol(fn_id)}"
			)

	for fn_id, func in funcs.items():
		for block in func.blocks.values():
			for instr in block.instructions:
				if isinstance(
					instr,
					(
						M.ArrayLit,
						M.ArrayAlloc,
						M.ArrayElemInit,
						M.ArrayElemInitUnchecked,
						M.ArrayElemAssign,
						M.ArrayElemDrop,
						M.ArrayElemTake,
						M.ArrayDrop,
						M.ArrayDup,
						M.ArrayIndexLoad,
						M.ArrayIndexLoadUnchecked,
						M.ArrayIndexStore,
					),
				):
					_check_type(fn_id, instr.elem_ty, instr.__class__.__name__)
					continue
				if isinstance(
					instr,
					(
						M.RawBufferAlloc,
						M.RawBufferDealloc,
						M.RawBufferPtrAt,
						M.RawBufferWrite,
						M.RawBufferRead,
					),
				):
					_check_type(fn_id, instr.raw_ty, instr.__class__.__name__)
					if hasattr(instr, "elem_ty"):
						_check_type(fn_id, instr.elem_ty, instr.__class__.__name__)
					continue
				if isinstance(instr, (M.PtrFromRef, M.PtrOffset, M.PtrIsNull)):
					_check_type(fn_id, instr.ptr_ty, instr.__class__.__name__)
					if hasattr(instr, "elem_ty"):
						_check_type(fn_id, instr.elem_ty, instr.__class__.__name__)
					continue
				if isinstance(instr, (M.PtrRead,)):
					_check_type(fn_id, instr.elem_ty, instr.__class__.__name__)
					continue
				if isinstance(instr, (M.PtrWrite,)):
					_check_type(fn_id, instr.elem_ty, instr.__class__.__name__)
					continue
				if isinstance(instr, (M.ConstructStruct, M.StructGetField)):
					_check_type(fn_id, instr.struct_ty, instr.__class__.__name__)
					continue
				if isinstance(instr, (M.ConstructVariant,)):
					_check_type(fn_id, instr.variant_ty, instr.__class__.__name__)
					continue
				if isinstance(instr, (M.ConstructIface, M.ConstructIfaceValue)):
					_check_type(fn_id, instr.iface_ty, instr.__class__.__name__)
					continue
				if isinstance(instr, (M.ZeroValue, M.CopyValue, M.MoveOut, M.DropValue)):
					_check_type(fn_id, instr.ty, instr.__class__.__name__)
					continue
				if isinstance(instr, (M.LoadRef, M.StoreRef, M.MoveFromRef)):
					_check_type(fn_id, instr.inner_ty, instr.__class__.__name__)
					continue


def validate_mir_wrapping_u64_invariants(
	funcs: Mapping[FunctionId, M.MirFunc],
	type_table: TypeTable,
) -> None:
	"""Ensure wrapping u64 ops only consume Uint64-typed values."""
	def _scalar_type(name: str, fallback: Callable[[], TypeId]) -> TypeId:
		# Prefer existing scalar ids from the shared table to avoid creating
		# fresh TypeIds that won't match MIR-local annotations.
		existing = type_table.get_nominal(kind=TypeKind.SCALAR, module_id=None, name=name)
		return existing if existing is not None else fallback()

	uint64_ty = _scalar_type("Uint64", type_table.ensure_uint64)
	int_ty = _scalar_type("Int", type_table.ensure_int)
	uint_ty = _scalar_type("Uint", type_table.ensure_uint)
	bool_ty = _scalar_type("Bool", type_table.ensure_bool)
	byte_ty = _scalar_type("Byte", type_table.ensure_byte)
	string_ty = _scalar_type("String", type_table.ensure_string)
	float_ty = _scalar_type("Float", type_table.ensure_float)

	for fn_id, func in funcs.items():
		local_types = dict(getattr(func, "local_types", {}) or {})
		value_types: dict[M.ValueId, TypeId] = {}

		def ty_for(val: M.ValueId | None) -> TypeId | None:
			if val is None:
				return None
			return value_types.get(val) or local_types.get(val)

		def set_value(val: M.ValueId | None, ty: TypeId | None) -> None:
			if val is None or ty is None:
				return
			if value_types.get(val) != ty:
				value_types[val] = ty

		for _ in range(3):
			changed = False
			for block in func.blocks.values():
				for instr in block.instructions:
					dest = getattr(instr, "dest", None)
					if isinstance(instr, M.ConstUint64):
						if value_types.get(dest) != uint64_ty:
							value_types[dest] = uint64_ty
							changed = True
					elif isinstance(instr, M.ConstUint):
						if value_types.get(dest) != uint_ty:
							value_types[dest] = uint_ty
							changed = True
					elif isinstance(instr, M.ConstInt):
						if value_types.get(dest) != int_ty:
							value_types[dest] = int_ty
							changed = True
					elif isinstance(instr, M.ConstBool):
						if value_types.get(dest) != bool_ty:
							value_types[dest] = bool_ty
							changed = True
					elif isinstance(instr, M.ConstString):
						if value_types.get(dest) != string_ty:
							value_types[dest] = string_ty
							changed = True
					elif isinstance(instr, M.ConstFloat):
						if value_types.get(dest) != float_ty:
							value_types[dest] = float_ty
							changed = True
					elif isinstance(instr, M.CastScalar):
						if value_types.get(dest) != instr.dst_ty:
							value_types[dest] = instr.dst_ty
							changed = True
					elif isinstance(instr, M.IntFromUint):
						if value_types.get(dest) != int_ty:
							value_types[dest] = int_ty
							changed = True
					elif isinstance(instr, M.UintFromInt):
						if value_types.get(dest) != uint_ty:
							value_types[dest] = uint_ty
							changed = True
					elif isinstance(instr, M.LoadLocal):
						ty = local_types.get(instr.local)
						if ty is not None and value_types.get(dest) != ty:
							value_types[dest] = ty
							changed = True
					elif isinstance(instr, M.StoreLocal):
						src_ty = ty_for(instr.value)
						if src_ty is not None and local_types.get(instr.local) != src_ty:
							local_types[instr.local] = src_ty
							changed = True
					elif isinstance(instr, M.AssignSSA):
						src_ty = ty_for(instr.src)
						if src_ty is not None and value_types.get(dest) != src_ty:
							value_types[dest] = src_ty
							changed = True
					elif isinstance(instr, M.UnaryOpInstr):
						operand_ty = ty_for(instr.operand)
						if operand_ty is not None and value_types.get(dest) != operand_ty:
							value_types[dest] = operand_ty
							changed = True
					elif isinstance(instr, M.BinaryOpInstr):
						left_ty = ty_for(instr.left)
						right_ty = ty_for(instr.right)
						if left_ty is not None and left_ty == right_ty and value_types.get(dest) != left_ty:
							value_types[dest] = left_ty
							changed = True
					elif isinstance(instr, M.StringLen):
						if value_types.get(dest) != int_ty:
							value_types[dest] = int_ty
							changed = True
					elif isinstance(instr, M.StringByteAt):
						if value_types.get(dest) != byte_ty:
							value_types[dest] = byte_ty
							changed = True
					elif isinstance(instr, M.CopyValue):
						ty = instr.ty
						if value_types.get(dest) != ty:
							value_types[dest] = ty
							changed = True
					elif isinstance(instr, M.MoveOut):
						ty = instr.ty
						if value_types.get(dest) != ty:
							value_types[dest] = ty
							changed = True
					elif isinstance(instr, M.LoadRef):
						ty = instr.inner_ty
						if value_types.get(dest) != ty:
							value_types[dest] = ty
							changed = True
					elif isinstance(instr, M.PtrRead):
						ty = instr.elem_ty
						if value_types.get(dest) != ty:
							value_types[dest] = ty
							changed = True
					elif isinstance(instr, M.RawBufferRead):
						ty = instr.elem_ty
						if value_types.get(dest) != ty:
							value_types[dest] = ty
							changed = True
					elif isinstance(instr, M.ArrayIndexLoad):
						ty = instr.elem_ty
						if value_types.get(dest) != ty:
							value_types[dest] = ty
							changed = True
					elif isinstance(instr, M.ArrayIndexLoadUnchecked):
						ty = instr.elem_ty
						if value_types.get(dest) != ty:
							value_types[dest] = ty
							changed = True
					elif isinstance(instr, M.StructGetField):
						ty = instr.field_ty
						if value_types.get(dest) != ty:
							value_types[dest] = ty
							changed = True
					elif isinstance(instr, M.VariantGetField):
						ty = instr.field_ty
						if value_types.get(dest) != ty:
							value_types[dest] = ty
							changed = True
					elif isinstance(instr, M.WrappingAddU64):
						if value_types.get(dest) != uint64_ty:
							value_types[dest] = uint64_ty
							changed = True
					elif isinstance(instr, M.WrappingMulU64):
						if value_types.get(dest) != uint64_ty:
							value_types[dest] = uint64_ty
							changed = True
			if not changed:
				break

		for block in func.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, (M.WrappingAddU64, M.WrappingMulU64)):
					left_ty = ty_for(instr.left)
					right_ty = ty_for(instr.right)
					if left_ty != uint64_ty or right_ty != uint64_ty:
						raise AssertionError(
							f"MIR invariant violation: wrapping op requires Uint64 operands in {function_symbol(fn_id)}"
						)


def validate_mir_iface_init_invariants(
	funcs: Mapping[FunctionId, M.MirFunc],
	signatures_by_id: Mapping[FunctionId, FnSignature],
	type_table: TypeTable,
) -> None:
	"""Ensure interface values are initialized before DropValue."""
	def _is_iface(ty_id: TypeId) -> bool:
		return type_table.get(ty_id).kind is TypeKind.INTERFACE

	for fn_id, func in funcs.items():
		sig = signatures_by_id.get(fn_id)
		iface_params: set[M.ValueId] = set()
		iface_param_locals: set[M.LocalId] = set()
		if sig is not None and sig.param_type_ids:
			for idx, name in enumerate(func.params):
				ty_id = sig.param_type_ids[idx] if idx < len(sig.param_type_ids) else None
				local_iface = False
				local_ty = func.local_types.get(name)
				if local_ty is not None and _is_iface(local_ty):
					local_iface = True
				if (ty_id is not None and _is_iface(ty_id)) or local_iface:
					iface_params.add(name)
					iface_param_locals.add(name)
					m = re.match(r"^(.*)_\d+$", name)
					if m is not None:
						base_name = m.group(1)
						base_ty = func.local_types.get(base_name)
						if base_ty is not None and _is_iface(base_ty):
							iface_params.add(base_name)
							iface_param_locals.add(base_name)
			for local_name, local_ty in func.local_types.items():
				if not _is_iface(local_ty):
					continue
				m_local = re.match(r"^(.*)_\d+$", local_name)
				if m_local is None:
					continue
				base_name = m_local.group(1)
				if base_name in iface_param_locals:
					iface_params.add(local_name)
					iface_param_locals.add(local_name)
		elif func.params:
			for name in func.params:
				ty_id = func.local_types.get(name)
				if ty_id is not None and _is_iface(ty_id):
					iface_params.add(name)
					iface_param_locals.add(name)

		value_iface_type: dict[M.ValueId, bool] = {}
		defs: dict[M.ValueId, M.MirInstr] = {}
		for block in func.blocks.values():
			for instr in block.instructions:
				dest = getattr(instr, "dest", None)
				if not isinstance(dest, str):
					continue
				defs[dest] = instr
				ty_id: TypeId | None = None
				if isinstance(instr, (M.ConstructIface, M.ConstructIfaceValue)):
					ty_id = instr.iface_ty
				elif isinstance(instr, M.ZeroValue):
					ty_id = instr.ty
				elif isinstance(instr, (M.CopyValue, M.MoveOut)):
					ty_id = instr.ty
				elif isinstance(instr, M.Call):
					call_sig = signatures_by_id.get(instr.fn_id)
					if call_sig is not None:
						ty_id = call_sig.return_type_id
				elif isinstance(instr, (M.CallIndirect, M.CallIface)):
					ty_id = instr.user_ret_type
				if isinstance(instr, M.IfaceUpcast):
					value_iface_type[dest] = True
					continue
				if ty_id is not None and _is_iface(ty_id):
					value_iface_type[dest] = True

		# Build CFG.
		succs: dict[str, list[str]] = {}
		for name, block in func.blocks.items():
			term = block.terminator
			if isinstance(term, M.Goto):
				succs[name] = [term.target]
			elif isinstance(term, M.IfTerminator):
				succs[name] = [term.then_target, term.else_target]
			else:
				succs[name] = []
		preds: dict[str, list[str]] = {name: [] for name in func.blocks.keys()}
		for name, targets in succs.items():
			for tgt in targets:
				preds.setdefault(tgt, []).append(name)

		inited_in: dict[str, set[M.LocalId]] = {name: set() for name in func.blocks.keys()}
		out_inited: dict[str, set[M.LocalId]] = {name: set() for name in func.blocks.keys()}
		changed = True
		order = list(func.blocks.keys())
		while changed:
			changed = False
			for name in order:
				if preds.get(name):
					new_in = set.intersection(*(out_inited[p] for p in preds[name])) if preds[name] else set()
				else:
					new_in = set(iface_param_locals)
				if new_in != inited_in[name]:
					inited_in[name] = set(new_in)
					changed = True
				cur_locals = set(inited_in[name])
				cur_values: set[M.ValueId] = set(iface_params)

				for instr in func.blocks[name].instructions:
					if isinstance(instr, M.LoadLocal):
						if instr.local in cur_locals:
							cur_values.add(instr.dest)
						continue
					if isinstance(instr, M.LoadRef) and _is_iface(instr.inner_ty):
						cur_values.add(instr.dest)
						continue
					if isinstance(instr, M.MoveFromRef) and _is_iface(instr.inner_ty):
						# Atomic transfer: the loaded iface lands in `local`'s
						# storage.  Mark `local` as holding an initialized
						# iface so subsequent reads validate.  No SSA `dest`
						# (transfer is local-to-local at the storage level).
						cur_locals.add(instr.local)
						continue
					if isinstance(instr, M.StoreLocal):
						val_is_iface = instr.value in cur_values or instr.value in iface_params or value_iface_type.get(instr.value, False)
						if val_is_iface:
							if instr.value not in cur_values and instr.value not in iface_params:
								raise AssertionError(
									f"MIR invariant violation: storing uninitialized iface value in {function_symbol(fn_id)}"
								)
							cur_locals.add(instr.local)
						else:
							if instr.local in cur_locals:
								cur_locals.discard(instr.local)
						continue
					if isinstance(instr, M.IfaceUpcast):
						if instr.iface not in cur_values and instr.iface not in iface_params:
							raise AssertionError(
								f"MIR invariant violation: IfaceUpcast of uninitialized iface value in {function_symbol(fn_id)}"
							)
						cur_values.add(instr.dest)
						continue
					if isinstance(instr, M.CopyValue) and _is_iface(instr.ty):
						if instr.value not in cur_values and instr.value not in iface_params:
							raise AssertionError(
								f"MIR invariant violation: CopyValue of uninitialized iface value in {function_symbol(fn_id)}"
							)
						cur_values.add(instr.dest)
						continue
					if isinstance(instr, M.MoveOut) and _is_iface(instr.ty):
						local_iface = False
						local_ty = func.local_types.get(instr.local)
						if local_ty is not None and _is_iface(local_ty):
							local_iface = True
						if instr.local not in cur_locals and instr.local not in iface_param_locals and not local_iface:
							raise AssertionError(
								f"MIR invariant violation: MoveOut of uninitialized iface local '{instr.local}' in {function_symbol(fn_id)} block {name}"
							)
						cur_values.add(instr.dest)
						cur_locals.add(instr.local)
						continue
					if isinstance(instr, (M.ConstructIface, M.ConstructIfaceValue)):
						cur_values.add(instr.dest)
						continue
					if isinstance(instr, M.ZeroValue) and _is_iface(instr.ty):
						cur_values.add(instr.dest)
						continue
					if isinstance(instr, (M.CallIndirect, M.CallIface)) and _is_iface(instr.user_ret_type):
						if instr.dest is not None:
							cur_values.add(instr.dest)
						continue
					if isinstance(instr, M.Call):
						call_sig = signatures_by_id.get(instr.fn_id)
						if call_sig is not None and _is_iface(call_sig.return_type_id) and instr.dest is not None:
							cur_values.add(instr.dest)
						continue
					if isinstance(instr, M.DropValue) and _is_iface(instr.ty):
						allow_loaded_iface_param = False
						src = defs.get(instr.value)
						if isinstance(src, M.LoadLocal) and src.local in iface_param_locals:
							allow_loaded_iface_param = True
						if instr.value not in cur_values and instr.value not in iface_params and not allow_loaded_iface_param:
							raise AssertionError(
								f"MIR invariant violation: DropValue of uninitialized iface value in {function_symbol(fn_id)}"
							)
						continue
				if cur_locals != out_inited[name]:
					out_inited[name] = set(cur_locals)
					changed = True


def validate_mir_call_byvalue_moves(
	funcs: Mapping[FunctionId, M.MirFunc],
	signatures_by_id: Mapping[FunctionId, FnSignature],
	type_table: TypeTable,
) -> None:
	"""Ensure non-Copy by-value call args originate from MoveOut (or non-local producers)."""
	def _copy_status_can_be_unknown(tid: TypeId) -> bool:
		seen: set[TypeId] = set()

		def _scan(cur: TypeId) -> bool:
			if cur in seen:
				return False
			seen.add(cur)
			td = type_table.get(cur)
			if td.kind in {TypeKind.TYPEVAR, TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL}:
				return True
			if td.kind is TypeKind.STRUCT:
				inst = type_table.get_struct_instance(cur)
				if inst is None:
					return True
				return any(_scan(f) for f in inst.field_types)
			if td.kind is TypeKind.VARIANT:
				inst = type_table.get_variant_instance(cur)
				if inst is None:
					return True
				for arm in inst.arms:
					if any(_scan(f) for f in arm.field_types):
						return True
				return False
			if td.kind is TypeKind.ARRAY and td.param_types:
				return _scan(td.param_types[0])
			for child in td.param_types:
				if _scan(child):
					return True
			return False

		return _scan(tid)

	for fn_id, func in funcs.items():
		defs: dict[M.ValueId, M.MirInstr] = {}
		for block in func.blocks.values():
			for instr in block.instructions:
				dest = getattr(instr, "dest", None)
				if isinstance(dest, str):
					defs[dest] = instr
		for block in func.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, M.Call):
					sig = signatures_by_id.get(instr.fn_id)
					if sig is None or sig.param_type_ids is None:
						continue
					for arg_val, param_ty in zip(instr.args, sig.param_type_ids):
						td = type_table.get(param_ty)
						if td.kind is TypeKind.REF:
							continue
						copy_status = type_table.copy_status(param_ty)
						if copy_status is None:
							if _copy_status_can_be_unknown(param_ty):
								continue
							raise AssertionError(
								f"MIR invariant violation: unresolved Copy status for call param type in {function_symbol(fn_id)}"
							)
						if copy_status:
							continue
						src = defs.get(arg_val)
						if isinstance(src, M.MoveOut):
							continue
						if isinstance(src, (M.LoadLocal, M.LoadRef)):
							raise AssertionError(
								f"MIR invariant violation: by-value arg must MoveOut non-Copy local in {function_symbol(fn_id)}"
							)
				elif isinstance(instr, (M.CallIndirect, M.CallIface)):
					for arg_val, param_ty in zip(instr.args, instr.param_types):
						td = type_table.get(param_ty)
						if td.kind is TypeKind.REF:
							continue
						copy_status = type_table.copy_status(param_ty)
						if copy_status is None:
							if _copy_status_can_be_unknown(param_ty):
								continue
							raise AssertionError(
								f"MIR invariant violation: unresolved Copy status for call param type in {function_symbol(fn_id)}"
							)
						if copy_status:
							continue
						src = defs.get(arg_val)
						if isinstance(src, M.MoveOut):
							continue
						if isinstance(src, (M.LoadLocal, M.LoadRef)):
							raise AssertionError(
								f"MIR invariant violation: by-value arg must MoveOut non-Copy local in {function_symbol(fn_id)}"
							)
