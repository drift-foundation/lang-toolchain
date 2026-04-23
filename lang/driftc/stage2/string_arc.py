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
from lang.driftc.core.function_id import function_symbol
from lang.driftc.core.function_id import FunctionId
from lang.driftc import debug as drift_debug
from . import mir_nodes as M
from . import ownership_ledger_events as _ledger_events
from . import ownership_ledger_reporter as _ledger_reporter
from .ownership_ledger import DropVerdict as _DropVerdict
from .drop_policy_compute import compute_drop_policy as _compute_drop_policy
from .drop_flags import is_flag_managed as _is_flag_managed


def insert_string_arc(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
	fn_infos: Mapping[FunctionId, FnInfo],
) -> M.MirFunc:
	is_destructor_method = "std.core.Destructible::destroy" in func.fn_id.name
	# Phase 3B step 1 (`drop_before_overwrite` swap): the ledger is
	# attached unconditionally by the driver and consulted as the
	# authoritative drop verdict at site 4.  Site 3 (`string_arc_return`)
	# remains observational pending its own swap.  Sites read the
	# canonical `DropPolicy.needs_drop` via `_compute_drop_policy` —
	# NOT the raw `TypeTable.has_drop` query that the 3A reporter
	# uses (the quarantined approximation in driftc.py).
	_ledger = getattr(func, "_ownership_ledger", None)
	def _ledger_needs_drop(local: str) -> bool:
		ty = local_types.get(local)
		if ty is None:
			return False
		try:
			return bool(type_table.has_drop(ty))
		except Exception:
			return False
	string_ty = type_table.ensure_string()
	local_types: Dict[str, TypeId] = func.local_types
	string_locals: Set[str] = {
		name for name in (list(func.params) + list(func.locals)) if local_types.get(name) == string_ty
	}
	storage_locals: Set[str] = set(func.params)
	for _blk in func.blocks.values():
		for _ins in _blk.instructions:
			local_name = getattr(_ins, "local", None)
			if isinstance(local_name, str):
				storage_locals.add(local_name)
	addr_taken_locals: Set[str] = set()
	for _blk in func.blocks.values():
		for _ins in _blk.instructions:
			if isinstance(_ins, M.AddrOfLocal):
				addr_taken_locals.add(_ins.local)
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
		if td.kind is TypeKind.ERROR:
			_type_needs_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.DIAGNOSTICVALUE:
			_type_needs_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.ARRAY and td.param_types:
			_type_needs_drop_cache[tid] = True
			return True
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

	# __borrow_tmp: Stage 1 (borrow_materialize.py) normally materialises rvalue
	# borrows into __tmp_borrow* locals that get scope-based drops.  This Stage 2
	# inclusion is a defensive fallback in case that assumption breaks.
	array_locals: Set[str] = {
		name
		for name in (list(func.params) + list(func.locals))
		if (not name.startswith("__")) or name.startswith("__match_binder_") or name.startswith("__borrow_tmp")
		if (tid := local_types.get(name)) is not None
		and type_table.get(tid).kind is TypeKind.ARRAY
	}

	def _is_destructible_tid(tid: TypeId | None) -> bool:
		if tid is None:
			return False
		return _type_needs_drop(tid)

	def _is_error_tid(tid: TypeId | None) -> bool:
		if tid is None:
			return False
		return type_table.get(tid).kind is TypeKind.ERROR

	_dtor_fns = getattr(type_table, "destructor_fns", None) or {}
	_nullsafe_drop_cache: Dict[TypeId, bool] = {}

	def _is_nullsafe_drop(tid: TypeId) -> bool:
		cached = _nullsafe_drop_cache.get(tid)
		if cached is not None:
			return cached
		if tid in _dtor_fns:
			_nullsafe_drop_cache[tid] = False
			return False
		td = type_table.get(tid)
		if td.kind is TypeKind.SCALAR:
			_nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.ARRAY:
			_nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.ERROR:
			_nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.INTERFACE:
			_nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.STRUCT:
			inst = type_table.get_struct_instance(tid)
			if inst is not None:
				safe = all(_is_nullsafe_drop(fty) for fty in inst.field_types if _type_needs_drop(fty))
				_nullsafe_drop_cache[tid] = safe
				return safe
		if td.kind is TypeKind.VARIANT:
			inst = type_table.get_variant_instance(tid)
			if inst is not None:
				safe = all(_is_nullsafe_drop(fty) for arm in inst.arms for fty in arm.field_types if _type_needs_drop(fty))
				_nullsafe_drop_cache[tid] = safe
				return safe
		_nullsafe_drop_cache[tid] = False
		return False

	# __borrow_tmp: defensive fallback — see comment above array_locals.
	destructible_locals: Set[str] = {
		name
		for name in (list(func.params) + list(func.locals))
		if (not name.startswith("__")) or name.startswith("__match_binder_") or name.startswith("__borrow_tmp") or _is_error_tid(local_types.get(name))
		if name not in string_locals
		if name not in array_locals
		if _is_destructible_tid(local_types.get(name))
	}
	nullsafe_destructible_locals: Set[str] = {name for name in destructible_locals if _is_nullsafe_drop(local_types[name])}

	def _is_string_value(val: str) -> bool:
		return _is_string_tid(local_types.get(val))

	def _is_local_name(val: str) -> bool:
		# Always False.  After the LoadLocal case in _iter_used_values
		# stopped yielding instr.local, only SSA value names ever flow
		# through the use/def/owned-defs analyses below.  Treating an SSA
		# value name as "a local" because the MIR temp counter happened
		# to issue a name string that also appears as a user storage
		# local was the root cause of a memcheck-visible leak: when a
		# user wrote `val t4 = call_returning_owned_string()` and the
		# call's SSA dest happened to be `t4`, the StoreLocal rewriter
		# excluded the dest from `owned_values` and inserted a spurious
		# retain via _ensure_owned, leaving the original +1 reference
		# from the call return unbalanced.
		return False

	def _ensure_owned(val: str, owned: Set[str], out: list[M.MInstr]) -> str:
		if not _is_string_value(val):
			return val
		if val in use_counts:
			use_counts[val] -= 1
			if use_counts[val] == 0 and val in owned and val not in live_out.get(block.name, set()):
				out.append(M.StringRelease(value=val))
				owned.discard(val)
				move_only_values.discard(val)
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
		zero = _new_temp()
		out.append(M.ZeroValue(dest=zero, ty=string_ty))
		local_types[zero] = string_ty
		out.append(M.StoreLocal(local=local, value=zero))
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
		zero = _new_temp()
		out.append(M.ZeroValue(dest=zero, ty=arr_ty))
		local_types[zero] = arr_ty
		out.append(M.StoreLocal(local=local, value=zero))
		out.append(M.ArrayDrop(elem_ty=elem_ty, array=tmp))
		local_types[tmp] = arr_ty

	def _drop_all_arrays(out: list[M.MInstr], *, skip_locals: Set[str] | None = None) -> None:
		skip = skip_locals or set()
		for local in sorted(array_locals):
			if local in skip:
				continue
			_drop_array_local(local, out)

	def _drop_destructible_local(local: str, out: list[M.MInstr]) -> None:
		if local not in destructible_locals:
			return
		ty = local_types.get(local)
		if ty is None:
			return
		tmp = _new_temp()
		out.append(M.LoadLocal(dest=tmp, local=local))
		zero = _new_temp()
		out.append(M.ZeroValue(dest=zero, ty=ty))
		local_types[zero] = ty
		out.append(M.StoreLocal(local=local, value=zero))
		out.append(M.DropValue(value=tmp, ty=ty))
		local_types[tmp] = ty

	def _drop_all_destructibles(
		out: list[M.MInstr],
		*,
		skip_locals: Set[str] | None = None,
		only_locals: Set[str] | None = None,
	) -> None:
		skip = skip_locals or set()
		only = only_locals
		for local in sorted(destructible_locals):
			if local in skip:
				continue
			if only is not None and local not in only:
				continue
			_drop_destructible_local(local, out)

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
			# Do not yield instr.local — that is a storage-local name, not
			# an SSA value name.  Yielding it conflated the storage namespace
			# with the SSA-value namespace and required _is_local_name() to
			# filter it back out, which silently mis-classified SSA values
			# whose MIR temp counter happened to land on a name already in
			# use as a user storage local (e.g. user `val t4 = call_ret`).
			# Storage-local liveness is tracked separately via store_defs /
			# assigned_in / assigned_out and via _release_local at scope exit.
			pass
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
		elif isinstance(instr, M.ErrorRaise):
			yield instr.error
		elif isinstance(instr, M.ErrorAttrsGetDV):
			yield instr.error
			yield instr.key
		elif isinstance(instr, M.ErrorCapturesGetDV):
			yield instr.error
			yield instr.frame
			yield instr.key
		elif isinstance(instr, M.ConstructDV):
			yield from instr.args
		elif isinstance(instr, M.DVAsInt):
			yield instr.dv
		elif isinstance(instr, M.DVAsBool):
			yield instr.dv
		elif isinstance(instr, M.DVAsFloat):
			yield instr.dv
		elif isinstance(instr, M.DVAsString):
			yield instr.dv
		elif isinstance(instr, M.DVAsObject):
			yield instr.dv
		elif isinstance(instr, M.DVGetField):
			yield instr.dv
			yield instr.key
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
			# Do not yield instr.local — same reason as LoadLocal above.
			# It is a storage-local name, not an SSA value.  MoveOut has
			# its own rewrite path that handles the storage-local move
			# semantics; it does not need the name to flow through the
			# SSA-value liveness analyses.  Yielding it would re-open the
			# namespace-collision class for explicit `move local` shapes.
			pass

	def _copy_span(dst: M.MInstr, src: M.MInstr) -> None:
		if hasattr(src, "span"):
			setattr(dst, "span", getattr(src, "span"))

	def _iter_term_used(term: M.MTerminator) -> Iterable[str]:
		if isinstance(term, M.Return) and term.value is not None:
			yield term.value
		elif isinstance(term, M.IfTerminator):
			yield term.cond

	def _seed_dest_types() -> None:
		"""Pre-seed missing destination types before ARC liveness/use analysis."""
		for bname in block_order:
			block = func.blocks[bname]
			for instr in block.instructions:
				dest = getattr(instr, "dest", None)
				if dest is None:
					continue
				if local_types.get(dest) is not None:
					continue
				if isinstance(instr, (M.ConstString, M.StringConcat, M.StringRetain, M.StringFromInt, M.StringFromBool, M.StringFromUint, M.StringFromFloat)):
					local_types[dest] = string_ty
					continue
				if isinstance(instr, M.AssignSSA):
					src_ty = local_types.get(instr.src)
					if src_ty is not None:
						local_types[dest] = src_ty
					continue
				if isinstance(instr, M.Phi):
					for incoming in instr.incoming.values():
						in_ty = local_types.get(incoming)
						if in_ty is not None:
							local_types[dest] = in_ty
							break
					continue
				if isinstance(instr, M.Call):
					info = fn_infos.get(instr.fn_id)
					if info is not None and info.signature is not None and info.signature.return_type_id is not None and not bool(getattr(info, "declared_can_throw", False)):
						local_types[dest] = info.signature.return_type_id
					else:
						sym = function_symbol(instr.fn_id)
						if sym in {
							"drift_string_from_cstr",
							"drift_string_from_utf8_bytes",
							"drift_string_from_int64",
							"drift_string_from_uint64",
							"drift_string_from_f64",
							"drift_string_from_bool",
							"drift_string_literal",
							"drift_string_concat",
							"drift_string_retain",
						}:
							local_types[dest] = string_ty
					continue
				if isinstance(instr, M.CallIndirect):
					if instr.user_ret_type is not None:
						local_types[dest] = instr.user_ret_type
					continue
				if isinstance(instr, M.CallIface):
					if instr.user_ret_type is not None:
						local_types[dest] = instr.user_ret_type
					continue

	def _block_succs(term: M.MTerminator | None) -> list[str]:
		if term is None:
			return []
		if isinstance(term, M.Goto):
			return [term.target]
		if isinstance(term, M.IfTerminator):
			return [term.then_target, term.else_target]
		return []

	def _block_preds() -> Dict[str, Set[str]]:
		preds: Dict[str, Set[str]] = {name: set() for name in block_order}
		for name in block_order:
			for succ in _block_succs(func.blocks[name].terminator):
				if succ in preds:
					preds[succ].add(name)
		return preds

	# Fill in missing destination types first so string-use liveness sees all
	# intermediate string temps (including conversion/call-produced values).
	_seed_dest_types()

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

	# Definite local assignment across CFG.
	preds = _block_preds()
	store_defs: Dict[str, Set[str]] = {}
	for name in block_order:
		stores: Set[str] = set()
		for instr in func.blocks[name].instructions:
			if isinstance(instr, M.StoreLocal):
				stores.add(instr.local)
		store_defs[name] = stores
	assigned_in: Dict[str, Set[str]] = {name: set() for name in block_order}
	assigned_out: Dict[str, Set[str]] = {name: set() for name in block_order}
	assigned_in[func.entry] = set(func.params)
	assigned_out[func.entry] = set(func.params) | store_defs.get(func.entry, set())
	changed = True
	while changed:
		changed = False
		for name in block_order:
			if name == func.entry:
				new_in = set(func.params)
			else:
				ps = preds.get(name, set())
				if not ps:
					new_in = set()
				else:
					it = iter(ps)
					new_in = set(assigned_out[next(it)])
					for p in it:
						new_in &= assigned_out[p]
			new_out = new_in | store_defs.get(name, set())
			if new_in != assigned_in[name] or new_out != assigned_out[name]:
				assigned_in[name] = new_in
				assigned_out[name] = new_out
				changed = True

	# Track locals that are definitely moved-out at each block boundary so
	# successor return blocks do not re-drop moved values.
	moved_in: Dict[str, Set[str]] = {name: set() for name in block_order}
	moved_out: Dict[str, Set[str]] = {name: set() for name in block_order}
	changed = True
	while changed:
		changed = False
		for name in block_order:
			if name == func.entry:
				new_in = set()
			else:
				ps = preds.get(name, set())
				if not ps:
					new_in = set()
				else:
					it = iter(ps)
					new_in = set(moved_out[next(it)])
					for p in it:
						new_in &= moved_out[p]
			cur = set(new_in)
			for instr in func.blocks[name].instructions:
				if isinstance(instr, M.StoreLocal):
					cur.discard(instr.local)
				elif isinstance(instr, M.MoveOut):
					cur.add(instr.local)
			new_out = cur
			if new_in != moved_in[name] or new_out != moved_out[name]:
				moved_in[name] = new_in
				moved_out[name] = new_out
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
			elif isinstance(instr, (M.Call, M.CallIndirect, M.CallIface)):
				# String-returning calls produce owned values that must be released
				# when their last use in the block is consumed.
				owned_defs.add(dest)
			elif isinstance(instr, M.PtrRead):
				if _is_string_tid(instr.elem_ty):
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
		moved_out_locals: Set[str] = set(moved_in.get(block.name, set()))
		explicitly_dropped_locals: Set[str] = set()
		load_local_src: Dict[str, str] = {}

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
			for local in func.locals:
				if local in func.params:
					continue
				if local not in nullsafe_destructible_locals:
					continue
				dest_ty = local_types.get(local)
				if dest_ty is None:
					continue
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=dest_ty))
				new_instrs.append(M.StoreLocal(local=local, value=zero))
				local_types[zero] = dest_ty
		# Count uses in this block for temp string values.
		use_counts: Dict[str, int] = {}
		producers: Dict[str, M.MInstr] = {}
		for instr in block.instructions:
			dest = getattr(instr, "dest", None)
			if isinstance(dest, str):
				producers[dest] = instr
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

		def _can_move_owned_once(val: str) -> bool:
			return val in owned_values and use_counts.get(val, 0) == 1

		def _is_string_creator(instr: M.MInstr | None) -> bool:
			if instr is None:
				return False
			if isinstance(instr, (M.ConstString, M.StringConcat, M.StringRetain, M.StringFromInt, M.StringFromBool, M.StringFromUint, M.StringFromFloat)):
				return True
			if isinstance(instr, M.Call):
				info = fn_infos.get(instr.fn_id)
				if info is not None and info.signature is not None and not bool(getattr(info, "declared_can_throw", False)) and info.signature.return_type_id == string_ty and local_types.get(getattr(instr, "dest", "")) == string_ty:
					return True
				sym = function_symbol(instr.fn_id)
				return sym in {
					"drift_string_from_cstr",
					"drift_string_from_utf8_bytes",
					"drift_string_from_int64",
					"drift_string_from_uint64",
					"drift_string_from_f64",
					"drift_string_from_bool",
					"drift_string_literal",
					"drift_string_concat",
					"drift_string_retain",
				}
			return False

		def _can_move_creator_return(val: str) -> bool:
			return use_counts.get(val, 0) <= 1 and _is_string_creator(producers.get(val))

		def _collect_return_source_locals(v: str, seen: Set[str] | None = None) -> Set[str]:
			seen_vals = seen or set()
			if v in seen_vals:
				return set()
			seen_vals.add(v)
			for prev in reversed(new_instrs):
				if isinstance(prev, M.AssignSSA) and prev.dest == v:
					return _collect_return_source_locals(prev.src, seen_vals)
				if isinstance(prev, M.LoadLocal) and prev.dest == v:
					return {prev.local}
			prod = producers.get(v)
			if prod is None:
				return set()
			locals_used: Set[str] = set()
			if isinstance(prod, M.ConstructStruct):
				for a in prod.args:
					locals_used |= _collect_return_source_locals(a, seen_vals)
			elif isinstance(prod, M.ConstructVariant):
				for a in prod.args:
					locals_used |= _collect_return_source_locals(a, seen_vals)
			elif isinstance(prod, M.ConstructResultOk):
				if prod.value is not None:
					locals_used |= _collect_return_source_locals(prod.value, seen_vals)
			elif isinstance(prod, M.ConstructIfaceValue):
				locals_used |= _collect_return_source_locals(prod.value, seen_vals)
			return locals_used

		initialized_destructibles: Set[str] = set(assigned_in.get(block.name, set())) & (destructible_locals - nullsafe_destructible_locals)
		for _instr_idx, instr in enumerate(block.instructions):
			if isinstance(instr, M.StoreLocal):
				moved_out_locals.discard(instr.local)
				explicitly_dropped_locals.discard(instr.local)
			if isinstance(instr, M.StoreLocal) and instr.local in array_locals:
				_drop_array_local(instr.local, new_instrs)
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.StoreLocal) and instr.local in nullsafe_destructible_locals:
				_drop_destructible_local(instr.local, new_instrs)
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.StoreLocal) and instr.local in destructible_locals:
				# Phase 3B step 1 — `drop_before_overwrite` swap.
				#
				# Status: **ledger-authoritative for deterministic
				# verdicts; legacy fallback retained for PathDependent /
				# unavailable ledger.**  Site-local authority is NOT
				# fully removed; that retirement is gated on either:
				#   (a) the drop-before-overwrite site gaining its own
				#       flag-guard pattern (so PathDependent verdicts
				#       can be resolved here, not at scope-exit only), OR
				#   (b) e2e observe demonstrating zero PathDependent
				#       verdicts at this site across all real Drift
				#       (today's data: 100 % verdict agreement at smoke
				#       + e2e, so PathDependent is currently unreached
				#       in practice — but the fallback is preserved
				#       defensively until that condition is pinned).
				#
				# Authoritative path:
				#   - For MustDrop / MustNotDrop verdicts, the ledger
				#     decides.  `compute_drop_policy(type_table, ty)
				#     .needs_drop` provides the canonical needs_drop
				#     axis — NOT the raw `TypeTable.has_drop` query
				#     (the quarantined 3A reporter approximation).
				#
				# Fallback path:
				#   - PathDependent verdict (lattice MaybeUninit at
				#     this point) → fall back to legacy
				#     `instr.local in initialized_destructibles`.
				#   - Ledger unavailable (no `_ownership_ledger`
				#     attached, e.g. ad-hoc test harness) → same
				#     fallback.
				#
				# The legacy `initialized_destructibles` set is
				# computed and preserved on every run for these two
				# fallback paths.  It is retired only when (a) or (b)
				# above holds, in a separate patch.
				#
				# Build-timing invariant: the ledger consulted here is
				# the PRE-`drop_flags` ledger (driver builds it before
				# the drop_flags pass runs).  Drop-before-overwrite
				# decisions only depend on per-local state at
				# StoreLocal points within the function body — none of
				# those points are mutated by drop_flags (which only
				# adds new blocks at Return terminators and inserts
				# flag-set/clear ops adjacent to existing
				# StoreLocal/MoveOut).  Pre-flag state is the correct
				# input for site 4.  See
				# `work/ownership-ledger/3b-invariants.md`.
				_local_ty = local_types.get(instr.local)
				_needs_drop = (
					bool(_compute_drop_policy(type_table, _local_ty).needs_drop)
					if _local_ty is not None
					else False
				)
				_verdict = (
					_ledger.verdict_at(
						(block.name, _instr_idx),
						instr.local,
						needs_drop=_needs_drop,
					)
					if _ledger is not None
					else None
				)
				_should_drop: bool
				_site_verdict_str: str
				_site_reason: str
				if _verdict is _DropVerdict.MUST_DROP:
					_should_drop = True
					_site_verdict_str = _ledger_events.VERDICT_MUST_DROP
					_site_reason = _ledger_events.REASON_NEEDS_DROP
				elif _verdict is _DropVerdict.MUST_NOT_DROP:
					_should_drop = False
					_site_verdict_str = _ledger_events.VERDICT_MUST_NOT_DROP
					_site_reason = _ledger_events.REASON_NOT_DROP_NEEDING
				else:
					# PathDependent OR ledger unavailable → legacy fallback.
					_should_drop = instr.local in initialized_destructibles
					_site_verdict_str = (
						_ledger_events.VERDICT_MUST_DROP
						if _should_drop
						else _ledger_events.VERDICT_MUST_NOT_DROP
					)
					_site_reason = (
						_ledger_events.REASON_NEEDS_DROP
						if _should_drop
						else _ledger_events.REASON_NOT_DROP_NEEDING
					)
				if _ledger is not None and drift_debug.enabled("ownership_ledger"):
					_ledger_reporter.check(
						_ledger,
						fn_name=func.name,
						site=_ledger_events.SITE_DROP_BEFORE_OVERWRITE,
						point=(block.name, _instr_idx),
						local=instr.local,
						site_verdict=_site_verdict_str,
						site_reason=_site_reason,
						needs_drop=_ledger_needs_drop,
						emit=_ledger_reporter.stderr_emit,
					)
				if _should_drop:
					_drop_destructible_local(instr.local, new_instrs)
				initialized_destructibles.add(instr.local)
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
				moved_out_locals.add(instr.local)
				# The local's storage is now zeroed (moved-out).  A
				# subsequent StoreLocal must NOT emit a
				# drop-before-overwrite for it — the "old value" is the
				# synthesized zero, and dropping that zero can fire
				# destructors on null payloads (e.g. variant with
				# droppable first-ctor: tag=0 dispatches to the ctor's
				# drop which reads zeroed reference fields → SEGV).
				# Clear the "initialized" marker so the next real
				# StoreLocal is treated as fresh initialization.
				#
				# Scope-of-effect: `initialized_destructibles` is
				# seeded from `destructible_locals - nullsafe_destructible_locals`
				# — i.e. it NEVER contains String or Array locals (those
				# go through the nullsafe path at line 824-827, which
				# always drops and re-adds regardless of prior state)
				# and it NEVER contains non-destructible locals.  The
				# only locals affected by this `discard` are the
				# non-nullsafe destructibles (variants with drop-
				# unsafe zero bytes, DVs, user-Destructible structs).
				# For each of those, dropping the zero bytes emitted
				# by the preceding ZeroValue is strictly unsafe — there
				# is no legitimate use case where the caller WANTS the
				# synthesized zero to be re-dropped.  Hence clearing
				# the marker cannot reintroduce a leak: if the next
				# StoreLocal is the last assignment, scope-exit drop
				# still runs; if no further store occurs, the local is
				# already moved-out and the zero bytes correctly stay
				# unowned.
				initialized_destructibles.discard(instr.local)
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
				load_local_src[instr.dest] = instr.local
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
					# `VariantGetField` for a String field is lowered in
					# LLVM codegen as `load field + drift_string_retain`
					# (copy-semantic transfer — see
					# `_classify_payload_extract_transfer` in
					# `llvm_codegen.py`).  The `dest` carries that +1
					# reference, matching `CopyValue`'s ownership shape,
					# NOT a borrowed view.  Treating it as borrowed
					# caused the subsequent `StoreLocal(..., dest)` in
					# the match-binder path to retain AGAIN via
					# `_ensure_owned`, producing an extra +1 that never
					# got released — observed as a 22-byte leak from
					# `drift_string_concat` in
					# `om_match_bind_string_heap_concat`'s
					# `scenario_value_producing_match`.
					#
					# Only `owned_values.add` — NOT `move_only_values`.
					# `_can_move_owned_once(val)` already checks
					# `val in owned AND use_counts == 1`, so a single-
					# consumer VariantGetField temp is moved without
					# retain.  Multi-consumer shapes (if any arise)
					# then go through the normal `_ensure_owned`
					# retain-for-additional-consumers path, releasing
					# the original +1 at the final use.  Adding to
					# `move_only_values` would bypass the single-use
					# guard and let the first consumer move the ref
					# while later consumers observed a consumed value.
					owned_values.add(instr.dest)
			elif isinstance(instr, M.ArrayIndexLoad):
				local_types[instr.dest] = instr.elem_ty
				if _is_string_tid(instr.elem_ty):
					owned_values.discard(instr.dest)
			elif isinstance(instr, M.ArrayElemTake):
				local_types[instr.dest] = instr.elem_ty
				if _is_string_tid(instr.elem_ty):
					owned_values.add(instr.dest)
					move_only_values.add(instr.dest)
			elif isinstance(instr, M.PtrRead):
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
				if val in move_only_values or _can_move_owned_once(val):
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
				if val in move_only_values or _can_move_owned_once(val):
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
				if val in move_only_values or _can_move_owned_once(val):
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
				if val in move_only_values or _can_move_owned_once(val):
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
					if e in move_only_values or _can_move_owned_once(e):
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
							if arg in move_only_values or _can_move_owned_once(arg):
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
							if arg in move_only_values or _can_move_owned_once(arg):
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
					if val in move_only_values or _can_move_owned_once(val):
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
					if val in move_only_values or _can_move_owned_once(val):
						_note_use(val, consume=True)
					else:
						val = _ensure_owned(val, owned_values, new_instrs)
						_note_use(val, consume=True)
				new_instrs.append(M.ConstructResultOk(dest=instr.dest, value=val))
				continue

			if isinstance(instr, M.ConstructError):
				event_fqn = instr.event_fqn
				if _is_string_value(event_fqn):
					if event_fqn in move_only_values or _can_move_owned_once(event_fqn):
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
					if key in move_only_values or _can_move_owned_once(key):
						_note_use(key, consume=True)
					else:
						key = _ensure_owned(key, owned_values, new_instrs)
						_note_use(key, consume=True)
				new_instrs.append(M.ErrorAddAttrDV(error=instr.error, key=key, value=instr.value))
				continue

			if isinstance(instr, M.ErrorAddLocalDV):
				frame = instr.frame
				if _is_string_value(frame):
					if frame in move_only_values or _can_move_owned_once(frame):
						_note_use(frame, consume=True)
					else:
						frame = _ensure_owned(frame, owned_values, new_instrs)
						_note_use(frame, consume=True)
				key = instr.key
				if _is_string_value(key):
					if key in move_only_values or _can_move_owned_once(key):
						_note_use(key, consume=True)
					else:
						key = _ensure_owned(key, owned_values, new_instrs)
						_note_use(key, consume=True)
				new_instrs.append(M.ErrorAddLocalDV(error=instr.error, frame=frame, key=key, value=instr.value))
				continue

			if isinstance(instr, M.ErrorRaise):
				new_instrs.append(instr)
				continue

			if isinstance(instr, M.ErrorCapturesGetDV):
				frame = instr.frame
				if _is_string_value(frame):
					if frame in move_only_values or _can_move_owned_once(frame):
						_note_use(frame, consume=True)
					else:
						frame = _ensure_owned(frame, owned_values, new_instrs)
						_note_use(frame, consume=True)
				key = instr.key
				if _is_string_value(key):
					if key in move_only_values or _can_move_owned_once(key):
						_note_use(key, consume=True)
					else:
						key = _ensure_owned(key, owned_values, new_instrs)
						_note_use(key, consume=True)
				new_instrs.append(M.ErrorCapturesGetDV(dest=instr.dest, error=instr.error, frame=frame, key=key))
				continue

			if isinstance(instr, M.DVGetField):
				key = instr.key
				if _is_string_value(key):
					if key in move_only_values or _can_move_owned_once(key):
						_note_use(key, consume=True)
					else:
						key = _ensure_owned(key, owned_values, new_instrs)
						_note_use(key, consume=True)
				new_instrs.append(M.DVGetField(dest=instr.dest, dv=instr.dv, key=key))
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
							if arg in move_only_values or _can_move_owned_once(arg):
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
						if arg in move_only_values or _can_move_owned_once(arg):
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
						if arg in move_only_values or _can_move_owned_once(arg):
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

			if isinstance(instr, M.DropValue) and _is_string_tid(instr.ty):
				new_instrs.append(instr)
				val = instr.value
				if _is_string_value(val) and not _is_local_name(val):
					_note_use(val, consume=True)
				continue
			if isinstance(instr, M.DropValue):
				src_local = load_local_src.get(instr.value)
				if src_local is not None and src_local in destructible_locals:
					explicitly_dropped_locals.add(src_local)

			new_instrs.append(instr)
			for val in _iter_used_values(instr):
				if _is_string_value(val) and not _is_local_name(val):
					_note_use(val, consume=False)

		if isinstance(block.terminator, M.Return):
			term = block.terminator
			val = term.value
			skip_cleanup_locals: Set[str] = set()
			skip_cleanup_locals |= moved_out_locals
			skip_cleanup_locals |= explicitly_dropped_locals
			if is_destructor_method and "self" in func.params:
				skip_cleanup_locals.add("self")
			can_move_from_skipped_local = False
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
						can_move_from_skipped_local = True
						break
				return_source_locals = _collect_return_source_locals(val)
				skip_cleanup_locals |= {loc for loc in return_source_locals if loc not in string_locals}
			if val is not None and (_is_string_value(val) or _can_move_creator_return(val)):
				if val in move_only_values or _can_move_owned_once(val) or _can_move_creator_return(val) or can_move_from_skipped_local:
					_note_use(val, consume=True)
				else:
					val = _ensure_owned(val, owned_values, new_instrs)
					_note_use(val, consume=True)
			_drop_all_arrays(new_instrs, skip_locals=skip_cleanup_locals)
			_release_all_locals(new_instrs, skip_locals=skip_cleanup_locals)
			initialized_at_return = assigned_in.get(block.name, set()) | store_defs.get(block.name, set()) | store_defs.get(func.entry, set())
			# Widen for variant locals that are conditionally initialized:
			# assigned on some predecessor paths but not all.  Variant
			# destroy on zeroinitializer (tag 0) is always a no-op, so
			# the PHI-provided zero for uninitialized paths is safe.
			# Only include locals not moved on ANY predecessor to avoid
			# double-free.
			_ret_preds = preds.get(block.name, set())
			if _ret_preds:
				_any_pred_moved: Set[str] = set()
				for _rp in _ret_preds:
					_any_pred_moved |= moved_out.get(_rp, set())
				for _rp in _ret_preds:
					_pred_assigned = assigned_out.get(_rp, set()) | store_defs.get(_rp, set())
					for _vl in _pred_assigned & destructible_locals - initialized_at_return - _any_pred_moved - skip_cleanup_locals:
						_vty = local_types.get(_vl)
						if _vty is not None and type_table.get(_vty).kind is TypeKind.VARIANT:
							initialized_at_return.add(_vl)
			# Phase 3B step 2 — `string_arc_return` swap (option 2:
			# site-3 skips locals managed by Phase 3C drop-flag
			# plumbing).  3C is the sole authority on scope-exit drops
			# for flagged locals: it has already inserted a
			# flag-guarded `MoveOut+DropValue` block reachable via the
			# original Return block's `IfTerminator(flag)`.  If site 3
			# also emitted a drop here, the flagged local would be
			# double-dropped on the path through 3C's drop block.
			# Filter flagged locals out of site-3's cleanup universe by
			# adding them to `skip_cleanup_locals`.
			#
			# Build-timing note (see `work/ownership-ledger/3b-invariants.md`):
			# the ledger we consulted in observe mode was the PRE-
			# `drop_flags` ledger, but the skip itself does not depend
			# on the ledger — it depends on the post-`drop_flags`
			# `func.locals` (where the `__drop_flag_<L>` markers
			# appear).  Detection via `_is_flag_managed`, which encodes
			# the flag-naming convention behind one helper rather than
			# scattering string matches.
			_flag_managed_at_return: Set[str] = {
				_dl for _dl in destructible_locals if _is_flag_managed(func, _dl)
			}
			skip_cleanup_locals |= _flag_managed_at_return
			if _ledger is not None and drift_debug.enabled("ownership_ledger"):
				# Site 3 observation: per-destructible-local verdict
				# at the return boundary.  Program point is
				# (block, len(original_instructions)) — the index a
				# hypothetical drop would land at if appended after the
				# block's last original instruction; the ledger reads
				# pre-state (post-state of that last instruction),
				# which is what `_drop_all_destructibles` is implicitly
				# deciding against.  Locals that 3C owns get a distinct
				# `REASON_DROP_FLAG_OWNED` record so observe triage can
				# see the responsibility split (without it, the missing
				# site-3 emission would surface as a bucket-5/6
				# regression in the next observe re-run).
				_ledger_point = (block.name, len(block.instructions))
				for _dl in sorted(destructible_locals):
					if _dl in _flag_managed_at_return:
						_ledger_reporter.check(
							_ledger,
							fn_name=func.name,
							site=_ledger_events.SITE_STRING_ARC_RETURN,
							point=_ledger_point,
							local=_dl,
							site_verdict=_ledger_events.VERDICT_MUST_NOT_DROP,
							site_reason=_ledger_events.REASON_DROP_FLAG_OWNED,
							needs_drop=_ledger_needs_drop,
							emit=_ledger_reporter.stderr_emit,
						)
						continue
					if _dl in skip_cleanup_locals:
						continue
					_dl_in_init = _dl in initialized_at_return
					_dl_verdict = (
						_ledger_events.VERDICT_MUST_DROP
						if _dl_in_init
						else _ledger_events.VERDICT_MUST_NOT_DROP
					)
					_dl_reason = (
						_ledger_events.REASON_NEEDS_DROP
						if _dl_in_init
						else _ledger_events.REASON_NOT_DROP_NEEDING
					)
					_ledger_reporter.check(
						_ledger,
						fn_name=func.name,
						site=_ledger_events.SITE_STRING_ARC_RETURN,
						point=_ledger_point,
						local=_dl,
						site_verdict=_dl_verdict,
						site_reason=_dl_reason,
						needs_drop=_ledger_needs_drop,
						emit=_ledger_reporter.stderr_emit,
					)
			_drop_all_destructibles(new_instrs, skip_locals=skip_cleanup_locals, only_locals=initialized_at_return)
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
