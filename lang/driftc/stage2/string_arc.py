# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
String ARC insertion for MIR.

This pass inserts explicit StringRetain/StringRelease (and CopyValue) ops so
LLVM codegen does not need to guess ownership. It also expands MoveOut into
LoadLocal + ZeroValue + StoreLocal once retains/releases are inserted.
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping, Set

from lang.driftc.checker import FnInfo
from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable
from lang.driftc.core.function_id import FunctionId
from lang.driftc import debug as drift_debug
from . import mir_nodes as M


def insert_string_arc(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
	fn_infos: Mapping[FunctionId, FnInfo],
) -> M.MirFunc:
	string_ty = type_table.ensure_string()
	local_types: Dict[str, TypeId] = func.local_types
	string_locals: Set[str] = {
		name for name in (list(func.params) + list(func.locals)) if local_types.get(name) == string_ty
	}
	_type_needs_drop_cache: Dict[TypeId, bool] = {}
	block_order = sorted(func.blocks.keys())

	used_ids: Set[str] = set(local_types.keys())
	arc_counter = 0

	def _new_temp() -> str:
		nonlocal arc_counter
		while True:
			arc_counter += 1
			name = f"__arc{arc_counter}"
			if name not in used_ids:
				used_ids.add(name)
				return name

	def _is_string_tid(tid: TypeId | None) -> bool:
		return tid == string_ty

	def _type_needs_drop(tid: TypeId) -> bool:
		cached = _type_needs_drop_cache.get(tid)
		if cached is not None:
			return cached
		td = type_table.get(tid)
		if hasattr(type_table, "is_destructible"):
			try:
				if bool(type_table.is_destructible(tid)):
					_type_needs_drop_cache[tid] = True
					return True
			except Exception:
				pass
		if td.kind is TypeKind.SCALAR:
			needs = td.name == "String"
			_type_needs_drop_cache[tid] = needs
			return needs
		if td.kind is TypeKind.REF:
			_type_needs_drop_cache[tid] = False
			return False
		if td.kind is TypeKind.ARRAY and td.param_types:
			needs = _type_needs_drop(td.param_types[0])
			_type_needs_drop_cache[tid] = needs
			return needs
		if td.kind is TypeKind.STRUCT:
			inst = type_table.get_struct_instance(tid)
			if inst is not None:
				needs = any(_type_needs_drop(fty) for fty in inst.field_types)
				_type_needs_drop_cache[tid] = needs
				return needs
		if td.kind is TypeKind.VARIANT:
			inst = type_table.get_variant_instance(tid)
			if inst is not None:
				needs = any(_type_needs_drop(fty) for arm in inst.arms for fty in arm.field_types)
				_type_needs_drop_cache[tid] = needs
				return needs
		if td.param_types:
			needs = any(_type_needs_drop(pt) for pt in td.param_types)
			_type_needs_drop_cache[tid] = needs
			return needs
		_type_needs_drop_cache[tid] = False
		return False

	array_locals: Set[str] = {
		name
		for name in (list(func.params) + list(func.locals))
		if (tid := local_types.get(name)) is not None
		and type_table.get(tid).kind is TypeKind.ARRAY
		and _type_needs_drop(type_table.get(tid).param_types[0])
	}

	def _is_destructible_tid(tid: TypeId | None) -> bool:
		if tid is None:
			return False
		if hasattr(type_table, "is_destructible"):
			try:
				return bool(type_table.is_destructible(tid))
			except Exception:
				return False
		return False

	destructible_locals: Set[str] = {
		name
		for name in (list(func.params) + list(func.locals))
		if _is_destructible_tid(local_types.get(name))
	}

	def _is_string_value(val: str) -> bool:
		return _is_string_tid(local_types.get(val))

	def _is_local_name(val: str) -> bool:
		return val in func.locals or val in func.params

	def _ensure_owned(val: str, owned: Set[str], out: list[M.MInstr]) -> str:
		if not _is_string_value(val):
			return val
		tmp = _new_temp()
		out.append(M.StringRetain(dest=tmp, value=val))
		local_types[tmp] = string_ty
		owned.add(tmp)
		return tmp

	def _param_is_string(tid: TypeId) -> bool:
		td = type_table.get(tid)
		return td.kind is TypeKind.SCALAR and td.name == "String"

	def _param_is_ref(tid: TypeId) -> bool:
		td = type_table.get(tid)
		return td.kind is TypeKind.REF

	def _release_local(local: str, out: list[M.MInstr]) -> None:
		if local not in string_locals:
			return
		old = _new_temp()
		out.append(M.LoadLocal(dest=old, local=local))
		out.append(M.StringRelease(value=old))
		local_types[old] = string_ty

	def _release_all_locals(out: list[M.MInstr], *, skip_locals: Set[str] | None = None) -> None:
		skip = skip_locals or set()
		for local in sorted(string_locals):
			if local in skip:
				continue
			_release_local(local, out)

	def _drop_array_local(local: str, out: list[M.MInstr]) -> None:
		if local not in array_locals:
			return
		arr_ty = local_types.get(local)
		if arr_ty is None:
			return
		td = type_table.get(arr_ty)
		if td.kind is not TypeKind.ARRAY or not td.param_types:
			return
		elem_ty = td.param_types[0]
		tmp = _new_temp()
		out.append(M.LoadLocal(dest=tmp, local=local))
		out.append(M.ArrayDrop(elem_ty=elem_ty, array=tmp))
		local_types[tmp] = arr_ty

	def _drop_all_arrays(out: list[M.MInstr], *, skip_locals: Set[str] | None = None) -> None:
		skip = skip_locals or set()
		for local in sorted(array_locals):
			if local in skip:
				continue
			_drop_array_local(local, out)

	def _drop_destructible_local(local: str, out: list[M.MInstr]) -> None:
		return

	def _drop_all_destructibles(out: list[M.MInstr], *, skip_locals: Set[str] | None = None) -> None:
		return

	def _iter_used_values(instr: M.MInstr) -> Iterable[str]:
		if isinstance(instr, M.StoreLocal):
			yield instr.value
		elif isinstance(instr, M.StoreRef):
			yield instr.ptr
			yield instr.value
		elif isinstance(instr, M.ArrayIndexStore):
			yield instr.array
			yield instr.index
			yield instr.value
		elif isinstance(instr, M.ArrayLit):
			yield from instr.elements
		elif isinstance(instr, M.ArrayAlloc):
			yield instr.length
			yield instr.cap
		elif isinstance(instr, M.ArrayElemInit):
			yield instr.array
			yield instr.index
			yield instr.value
		elif isinstance(instr, M.ArrayElemInitUnchecked):
			yield instr.array
			yield instr.index
			yield instr.value
		elif isinstance(instr, M.ArrayElemAssign):
			yield instr.array
			yield instr.index
			yield instr.value
		elif isinstance(instr, M.ArrayElemDrop):
			yield instr.array
			yield instr.index
		elif isinstance(instr, M.ArrayElemTake):
			yield instr.array
			yield instr.index
		elif isinstance(instr, M.ArrayDrop):
			yield instr.array
		elif isinstance(instr, M.ArrayDup):
			yield instr.array
		elif isinstance(instr, M.ArrayIndexLoad):
			yield instr.array
			yield instr.index
		elif isinstance(instr, M.ArraySetLen):
			yield instr.array
			yield instr.length
		elif isinstance(instr, M.ConstructStruct):
			yield from instr.args
		elif isinstance(instr, M.ConstructVariant):
			yield from instr.args
		elif isinstance(instr, M.StructGetField):
			yield instr.subject
		elif isinstance(instr, M.VariantGetField):
			yield instr.variant
		elif isinstance(instr, M.ConstructIfaceValue):
			yield instr.value
		elif isinstance(instr, M.LoadLocal):
			yield instr.local
		elif isinstance(instr, M.LoadRef):
			yield instr.ptr
		elif isinstance(instr, M.Call):
			yield from instr.args
		elif isinstance(instr, M.CallIndirect):
			yield instr.callee
			yield from instr.args
		elif isinstance(instr, M.CallIface):
			yield instr.iface
			yield from instr.args
		elif isinstance(instr, M.IfaceUpcast):
			yield instr.iface
		elif isinstance(instr, M.StringConcat):
			yield instr.left
			yield instr.right
		elif isinstance(instr, M.StringEq):
			yield instr.left
			yield instr.right
		elif isinstance(instr, M.StringCmp):
			yield instr.left
			yield instr.right
		elif isinstance(instr, M.StringLen):
			yield instr.value
		elif isinstance(instr, M.StringByteAt):
			yield instr.value
		elif isinstance(instr, M.StringRetain):
			yield instr.value
		elif isinstance(instr, M.StringRelease):
			yield instr.value
		elif isinstance(instr, M.CopyValue):
			yield instr.value
		elif isinstance(instr, M.DropValue):
			yield instr.value
		elif isinstance(instr, M.UnaryOpInstr):
			yield instr.operand
		elif isinstance(instr, M.BinaryOpInstr):
			yield instr.left
			yield instr.right
		elif isinstance(instr, M.AssignSSA):
			yield instr.src
		elif isinstance(instr, M.Phi):
			for val in instr.incoming.values():
				yield val
		elif isinstance(instr, M.ConstructResultOk):
			if instr.value is not None:
				yield instr.value
		elif isinstance(instr, M.ConstructResultErr):
			yield instr.error
		elif isinstance(instr, M.ResultOk):
			yield instr.result
		elif isinstance(instr, M.ResultErr):
			yield instr.result
		elif isinstance(instr, M.ResultIsErr):
			yield instr.result
		elif isinstance(instr, M.ConstructError):
			yield instr.code
			yield instr.event_fqn
			if instr.payload is not None:
				yield instr.payload
			if instr.attr_key is not None:
				yield instr.attr_key
		elif isinstance(instr, M.ErrorAddAttrDV):
			yield instr.error
			yield instr.key
			yield instr.value
		elif isinstance(instr, M.ErrorAddLocalDV):
			yield instr.error
			yield instr.frame
			yield instr.key
			yield instr.value
		elif isinstance(instr, M.ErrorAttrsGetDV):
			yield instr.error
			yield instr.key
		elif isinstance(instr, M.ErrorCapturesGetDV):
			yield instr.error
			yield instr.frame
			yield instr.key
		elif isinstance(instr, M.DVAsInt):
			yield instr.dv
		elif isinstance(instr, M.DVAsBool):
			yield instr.dv
		elif isinstance(instr, M.DVAsString):
			yield instr.dv
		elif isinstance(instr, M.ErrorEvent):
			yield instr.error
		elif isinstance(instr, M.StringFromInt):
			yield instr.value
		elif isinstance(instr, M.StringFromUint):
			yield instr.value
		elif isinstance(instr, M.StringFromBool):
			yield instr.value
		elif isinstance(instr, M.StringFromFloat):
			yield instr.value
		elif isinstance(instr, M.MoveOut):
			yield instr.local

	def _copy_span(dst: M.MInstr, src: M.MInstr) -> None:
		if hasattr(src, "span"):
			setattr(dst, "span", getattr(src, "span"))

	def _iter_term_used(term: M.MTerminator) -> Iterable[str]:
		if isinstance(term, M.Return) and term.value is not None:
			yield term.value
		elif isinstance(term, M.IfTerminator):
			yield term.cond

	def _block_succs(term: M.MTerminator | None) -> list[str]:
		if term is None:
			return []
		if isinstance(term, M.Goto):
			return [term.target]
		if isinstance(term, M.IfTerminator):
			return [term.then_target, term.else_target]
		return []

	# Compute per-block use/def for string temps (non-local value ids).
	use: Dict[str, Set[str]] = {}
	defs: Dict[str, Set[str]] = {}
	for name in block_order:
		block = func.blocks[name]
		block_use: Set[str] = set()
		block_def: Set[str] = set()
		seen_def: Set[str] = set()
		for instr in block.instructions:
			for val in _iter_used_values(instr):
				if not _is_string_value(val) or _is_local_name(val):
					continue
				if val not in seen_def:
					block_use.add(val)
			dest = getattr(instr, "dest", None)
			if dest is not None and _is_string_value(dest) and not _is_local_name(dest):
				block_def.add(dest)
				seen_def.add(dest)
		if block.terminator is not None:
			for val in _iter_term_used(block.terminator):
				if _is_string_value(val) and not _is_local_name(val) and val not in seen_def:
					block_use.add(val)
		use[name] = block_use
		defs[name] = block_def

	live_in: Dict[str, Set[str]] = {name: set() for name in block_order}
	live_out: Dict[str, Set[str]] = {name: set() for name in block_order}
	changed = True
	while changed:
		changed = False
		for name in block_order:
			block = func.blocks[name]
			succs = _block_succs(block.terminator)
			out = set()
			for succ in succs:
				out |= live_in.get(succ, set())
			new_in = use[name] | (out - defs[name])
			if new_in != live_in[name] or out != live_out[name]:
				live_in[name] = new_in
				live_out[name] = out
				changed = True

	owned_defs: Set[str] = set()
	move_only_defs: Set[str] = set()
	for name in block_order:
		for instr in func.blocks[name].instructions:
			dest = getattr(instr, "dest", None)
			if dest is None or not _is_string_value(dest) or _is_local_name(dest):
				continue
			if isinstance(instr, (M.ConstString, M.StringConcat, M.StringFromInt, M.StringFromBool, M.StringFromUint, M.StringFromFloat)):
				owned_defs.add(dest)
			elif isinstance(instr, (M.StringRetain, M.CopyValue)):
				owned_defs.add(dest)
			elif isinstance(instr, M.MoveOut):
				owned_defs.add(dest)
				move_only_defs.add(dest)
			elif isinstance(instr, M.ArrayElemTake):
				owned_defs.add(dest)
				move_only_defs.add(dest)

	for name in block_order:
		block = func.blocks[name]
		new_instrs: list[M.MInstr] = []
		owned_values: Set[str] = set(owned_defs)
		move_only_values: Set[str] = set(move_only_defs)

		# Initialize string locals in the entry block to avoid uninitialized releases.
		if block.name == func.entry:
			for local in func.locals:
				if local in func.params:
					continue
				if local_types.get(local) != string_ty:
					continue
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=string_ty))
				new_instrs.append(M.StoreLocal(local=local, value=zero))
				local_types[zero] = string_ty
			for local in func.locals:
				if local in func.params:
					continue
				if local not in array_locals:
					continue
				arr_ty = local_types.get(local)
				if arr_ty is None:
					continue
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=arr_ty))
				new_instrs.append(M.StoreLocal(local=local, value=zero))
				local_types[zero] = arr_ty

		# Count uses in this block for temp string values.
		use_counts: Dict[str, int] = {}
		for instr in block.instructions:
			for val in _iter_used_values(instr):
				if _is_string_value(val) and not _is_local_name(val):
					use_counts[val] = use_counts.get(val, 0) + 1
		if block.terminator is not None:
			for val in _iter_term_used(block.terminator):
				if _is_string_value(val) and not _is_local_name(val):
					use_counts[val] = use_counts.get(val, 0) + 1

		def _note_use(val: str, *, consume: bool) -> None:
			if val not in use_counts:
				return
			use_counts[val] -= 1
			if consume and val in owned_values:
				owned_values.discard(val)
				move_only_values.discard(val)
				return
			if use_counts[val] == 0 and val in owned_values and val not in live_out.get(block.name, set()):
				new_instrs.append(M.StringRelease(value=val))
				owned_values.discard(val)
				move_only_values.discard(val)

		for instr in block.instructions:
			if isinstance(instr, M.StoreLocal) and instr.local in array_locals:
				_drop_array_local(instr.local, new_instrs)
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.MoveOut):
				# Emit load + zero-store, but keep ownership of the moved value.
				new_instrs.append(M.LoadLocal(dest=instr.dest, local=instr.local))
				local_types[instr.dest] = instr.ty
				if _is_string_tid(instr.ty):
					owned_values.add(instr.dest)
					move_only_values.add(instr.dest)
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=instr.ty))
				new_instrs.append(M.StoreLocal(local=instr.local, value=zero))
				local_types[zero] = instr.ty
				continue

			if isinstance(instr, M.ConstString):
				owned_values.add(instr.dest)
			elif isinstance(instr, (M.StringFromInt, M.StringFromBool, M.StringFromUint, M.StringFromFloat, M.StringConcat)):
				owned_values.add(instr.dest)
			elif isinstance(instr, M.StringRetain):
				owned_values.add(instr.dest)
			elif isinstance(instr, M.CopyValue):
				if _is_string_tid(instr.ty):
					owned_values.add(instr.dest)
			elif isinstance(instr, M.LoadLocal):
				load_ty = local_types.get(instr.local)
				if load_ty is not None:
					local_types[instr.dest] = load_ty
				if _is_string_tid(local_types.get(instr.local)):
					owned_values.discard(instr.dest)
			elif isinstance(instr, M.LoadRef):
				local_types[instr.dest] = instr.inner_ty
				if _is_string_tid(instr.inner_ty):
					owned_values.discard(instr.dest)
			elif isinstance(instr, M.StructGetField):
				local_types[instr.dest] = instr.field_ty
				if _is_string_tid(instr.field_ty):
					owned_values.discard(instr.dest)
			elif isinstance(instr, M.VariantGetField):
				local_types[instr.dest] = instr.field_ty
				if _is_string_tid(instr.field_ty):
					owned_values.discard(instr.dest)
			elif isinstance(instr, M.ArrayIndexLoad):
				local_types[instr.dest] = instr.elem_ty
				if _is_string_tid(instr.elem_ty):
					owned_values.discard(instr.dest)
			elif isinstance(instr, M.ArrayElemTake):
				local_types[instr.dest] = instr.elem_ty
				if _is_string_tid(instr.elem_ty):
					owned_values.add(instr.dest)
					move_only_values.add(instr.dest)
			elif isinstance(instr, M.AssignSSA):
				if _is_string_value(instr.src):
					if instr.src in owned_values:
						owned_values.add(instr.dest)
					else:
						owned_values.discard(instr.dest)

			if isinstance(instr, M.StoreLocal) and _is_string_tid(local_types.get(instr.local)):
				_release_local(instr.local, new_instrs)
				val = instr.value
				if val in move_only_values:
					new_instrs.append(M.StoreLocal(local=instr.local, value=val))
					_note_use(val, consume=True)
				else:
					val = _ensure_owned(val, owned_values, new_instrs)
					new_instrs.append(M.StoreLocal(local=instr.local, value=val))
					_note_use(val, consume=True)
				continue

			if isinstance(instr, M.StoreRef) and _is_string_tid(instr.inner_ty):
				old = _new_temp()
				new_instrs.append(M.LoadRef(dest=old, ptr=instr.ptr, inner_ty=instr.inner_ty))
				new_instrs.append(M.StringRelease(value=old))
				local_types[old] = string_ty
				val = instr.value
				if val in move_only_values:
					new_instrs.append(M.StoreRef(ptr=instr.ptr, value=val, inner_ty=instr.inner_ty))
					_note_use(val, consume=True)
				else:
					val = _ensure_owned(val, owned_values, new_instrs)
					new_instrs.append(M.StoreRef(ptr=instr.ptr, value=val, inner_ty=instr.inner_ty))
					_note_use(val, consume=True)
				continue

			if isinstance(instr, M.ArrayIndexStore) and _is_string_tid(instr.elem_ty):
				old = _new_temp()
				new_instrs.append(
					M.ArrayIndexLoad(dest=old, elem_ty=instr.elem_ty, array=instr.array, index=instr.index)
				)
				new_instrs.append(M.StringRelease(value=old))
				local_types[old] = string_ty
				val = instr.value
				if val in move_only_values:
					new_instrs.append(
						M.ArrayIndexStore(elem_ty=instr.elem_ty, array=instr.array, index=instr.index, value=val)
					)
					_note_use(val, consume=True)
				else:
					val = _ensure_owned(val, owned_values, new_instrs)
					new_instrs.append(
						M.ArrayIndexStore(elem_ty=instr.elem_ty, array=instr.array, index=instr.index, value=val)
					)
					_note_use(val, consume=True)
				continue

			if isinstance(instr, (M.ArrayElemInit, M.ArrayElemInitUnchecked, M.ArrayElemAssign)) and _is_string_tid(instr.elem_ty):
				val = instr.value
				if val in move_only_values:
					new_instr = type(instr)(
						elem_ty=instr.elem_ty,
						array=instr.array,
						index=instr.index,
						value=val,
					)
					_copy_span(new_instr, instr)
					new_instrs.append(new_instr)
					_note_use(val, consume=True)
				else:
					val = _ensure_owned(val, owned_values, new_instrs)
					new_instr = type(instr)(
						elem_ty=instr.elem_ty,
						array=instr.array,
						index=instr.index,
						value=val,
					)
					_copy_span(new_instr, instr)
					new_instrs.append(new_instr)
					_note_use(val, consume=True)
				continue

			if isinstance(instr, M.ArrayLit) and _is_string_tid(instr.elem_ty):
				elems: list[str] = []
				for e in instr.elements:
					if e in move_only_values:
						elems.append(e)
						_note_use(e, consume=True)
					else:
						elems.append(_ensure_owned(e, owned_values, new_instrs))
						_note_use(e, consume=True)
				new_instr = M.ArrayLit(dest=instr.dest, elem_ty=instr.elem_ty, elements=elems)
				_copy_span(new_instr, instr)
				new_instrs.append(new_instr)
				continue

			if isinstance(instr, M.ConstructStruct):
				inst = type_table.get_struct_instance(instr.struct_ty)
				if inst is not None:
					args: list[str] = []
					for field_ty, arg in zip(inst.field_types, instr.args):
						if _is_string_tid(field_ty):
							if arg in move_only_values:
								args.append(arg)
								_note_use(arg, consume=True)
							else:
								args.append(_ensure_owned(arg, owned_values, new_instrs))
								_note_use(arg, consume=True)
						else:
							args.append(arg)
					new_instrs.append(M.ConstructStruct(dest=instr.dest, struct_ty=instr.struct_ty, args=args))
					continue

			if isinstance(instr, M.ConstructVariant):
				inst = type_table.get_variant_instance(instr.variant_ty)
				if inst is not None and instr.ctor in inst.arms_by_name:
					arm = inst.arms_by_name[instr.ctor]
					args: list[str] = []
					for field_ty, arg in zip(arm.field_types, instr.args):
						if _is_string_tid(field_ty):
							if arg in move_only_values:
								args.append(arg)
								_note_use(arg, consume=True)
							else:
								args.append(_ensure_owned(arg, owned_values, new_instrs))
								_note_use(arg, consume=True)
						else:
							args.append(arg)
					new_instrs.append(
						M.ConstructVariant(dest=instr.dest, variant_ty=instr.variant_ty, ctor=instr.ctor, args=args)
					)
					continue
			if isinstance(instr, M.ConstructIfaceValue):
				val = instr.value
				if _is_string_tid(instr.value_ty):
					if val in move_only_values:
						_note_use(val, consume=True)
					else:
						val = _ensure_owned(val, owned_values, new_instrs)
						_note_use(val, consume=True)
				new_instrs.append(
					M.ConstructIfaceValue(
						dest=instr.dest,
						iface_ty=instr.iface_ty,
						value=val,
						value_ty=instr.value_ty,
					)
				)
				continue

			if isinstance(instr, M.ConstructResultOk) and instr.value is not None:
				val = instr.value
				if _is_string_value(val):
					if val in move_only_values:
						_note_use(val, consume=True)
					else:
						val = _ensure_owned(val, owned_values, new_instrs)
						_note_use(val, consume=True)
				new_instrs.append(M.ConstructResultOk(dest=instr.dest, value=val))
				continue

			if isinstance(instr, M.ConstructError):
				event_fqn = instr.event_fqn
				if _is_string_value(event_fqn):
					if event_fqn in move_only_values:
						_note_use(event_fqn, consume=True)
					else:
						event_fqn = _ensure_owned(event_fqn, owned_values, new_instrs)
						_note_use(event_fqn, consume=True)
				new_instrs.append(
					M.ConstructError(
						dest=instr.dest,
						code=instr.code,
						event_fqn=event_fqn,
						payload=instr.payload,
						attr_key=instr.attr_key,
					)
				)
				continue

			if isinstance(instr, M.ErrorAddAttrDV):
				key = instr.key
				if _is_string_value(key):
					if key in move_only_values:
						_note_use(key, consume=True)
					else:
						key = _ensure_owned(key, owned_values, new_instrs)
						_note_use(key, consume=True)
				new_instrs.append(M.ErrorAddAttrDV(error=instr.error, key=key, value=instr.value))
				continue

			if isinstance(instr, M.ErrorAddLocalDV):
				frame = instr.frame
				if _is_string_value(frame):
					if frame in move_only_values:
						_note_use(frame, consume=True)
					else:
						frame = _ensure_owned(frame, owned_values, new_instrs)
						_note_use(frame, consume=True)
				key = instr.key
				if _is_string_value(key):
					if key in move_only_values:
						_note_use(key, consume=True)
					else:
						key = _ensure_owned(key, owned_values, new_instrs)
						_note_use(key, consume=True)
				new_instrs.append(M.ErrorAddLocalDV(error=instr.error, frame=frame, key=key, value=instr.value))
				continue

			if isinstance(instr, M.ErrorCapturesGetDV):
				frame = instr.frame
				if _is_string_value(frame):
					if frame in move_only_values:
						_note_use(frame, consume=True)
					else:
						frame = _ensure_owned(frame, owned_values, new_instrs)
						_note_use(frame, consume=True)
				key = instr.key
				if _is_string_value(key):
					if key in move_only_values:
						_note_use(key, consume=True)
					else:
						key = _ensure_owned(key, owned_values, new_instrs)
						_note_use(key, consume=True)
				new_instrs.append(M.ErrorCapturesGetDV(dest=instr.dest, error=instr.error, frame=frame, key=key))
				continue

			if isinstance(instr, M.Call):
				if drift_debug.enabled("ssa") and getattr(instr.fn_id, "module", None) == "main":
					import sys
					print(f"[drift:debug][arc] pre call fn={instr.fn_id} span={getattr(instr, 'span', None)}", file=sys.stderr)
				info = fn_infos.get(instr.fn_id)
				if info is not None and info.signature and info.signature.param_type_ids is not None:
					args: list[str] = []
					for ty_id, arg in zip(info.signature.param_type_ids, instr.args):
						if _param_is_ref(ty_id):
							args.append(arg)
							continue
						if _param_is_string(ty_id):
							if arg in move_only_values:
								args.append(arg)
								_note_use(arg, consume=True)
							else:
								args.append(_ensure_owned(arg, owned_values, new_instrs))
								_note_use(arg, consume=True)
						else:
							args.append(arg)
					new_call = M.Call(dest=instr.dest, fn_id=instr.fn_id, args=args, can_throw=instr.can_throw)
					_copy_span(new_call, instr)
					if drift_debug.enabled("ssa") and getattr(instr.fn_id, "module", None) == "main":
						import sys
						print(f"[drift:debug][arc] new call fn={new_call.fn_id} span={getattr(new_call, 'span', None)}", file=sys.stderr)
					new_instrs.append(new_call)
					continue
				if drift_debug.enabled("ssa") and getattr(instr.fn_id, "module", None) == "main":
					import sys
					print(f"[drift:debug][arc] keep call fn={instr.fn_id} span={getattr(instr, 'span', None)}", file=sys.stderr)
				new_instrs.append(instr)
				continue

			if isinstance(instr, M.CallIndirect):
				args: list[str] = []
				for ty_id, arg in zip(instr.param_types, instr.args):
					if _param_is_ref(ty_id):
						args.append(arg)
						continue
					if _param_is_string(ty_id):
						if arg in move_only_values:
							args.append(arg)
							_note_use(arg, consume=True)
						else:
							args.append(_ensure_owned(arg, owned_values, new_instrs))
							_note_use(arg, consume=True)
					else:
						args.append(arg)
				new_call = M.CallIndirect(
					dest=instr.dest,
					callee=instr.callee,
					args=args,
					param_types=instr.param_types,
					user_ret_type=instr.user_ret_type,
					can_throw=instr.can_throw,
				)
				_copy_span(new_call, instr)
				new_instrs.append(new_call)
				continue
			if isinstance(instr, M.CallIface):
				args: list[str] = []
				for ty_id, arg in zip(instr.param_types, instr.args):
					if _param_is_ref(ty_id):
						args.append(arg)
						continue
					if _param_is_string(ty_id):
						if arg in move_only_values:
							args.append(arg)
							_note_use(arg, consume=True)
						else:
							args.append(_ensure_owned(arg, owned_values, new_instrs))
							_note_use(arg, consume=True)
					else:
						args.append(arg)
				new_call = M.CallIface(
					dest=instr.dest,
					iface=instr.iface,
					args=args,
					param_types=instr.param_types,
					user_ret_type=instr.user_ret_type,
					can_throw=instr.can_throw,
					slot_index=instr.slot_index,
				)
				_copy_span(new_call, instr)
				new_instrs.append(new_call)
				continue

			new_instrs.append(instr)
			for val in _iter_used_values(instr):
				if _is_string_value(val) and not _is_local_name(val):
					_note_use(val, consume=False)

		if isinstance(block.terminator, M.Return):
			term = block.terminator
			val = term.value
			skip_cleanup_locals: Set[str] = set()
			if val is not None:
				alias = val
				while True:
					moved = False
					for prev in reversed(new_instrs):
						if isinstance(prev, M.AssignSSA) and prev.dest == alias:
							alias = prev.src
							moved = True
							break
					if not moved:
						break
				for prev in reversed(new_instrs):
					if isinstance(prev, M.LoadLocal) and prev.dest == alias:
						skip_cleanup_locals.add(prev.local)
						break
			if val is not None and _is_string_value(val):
				if val in move_only_values:
					_note_use(val, consume=True)
				else:
					val = _ensure_owned(val, owned_values, new_instrs)
					_note_use(val, consume=True)
			_drop_all_arrays(new_instrs, skip_locals=skip_cleanup_locals)
			_release_all_locals(new_instrs, skip_locals=skip_cleanup_locals)
			_drop_all_destructibles(new_instrs, skip_locals=skip_cleanup_locals)
			new_term = M.Return(value=val)
			if hasattr(term, "span"):
				setattr(new_term, "span", getattr(term, "span"))
			block.terminator = new_term
		elif block.terminator is not None:
			for val in _iter_term_used(block.terminator):
				if _is_string_value(val) and not _is_local_name(val):
					_note_use(val, consume=False)

		block.instructions = new_instrs

	return func


__all__ = ["insert_string_arc"]
