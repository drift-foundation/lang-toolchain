# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Instruction-local OVERWRITE cleanup authoring (Slice B1,
string-arc-endgame-cleanup-authority, 2026-07-20).

Emits the release/drop of the OLD value at an overwriting store, for
the two instruction-local families measured in Slice B:

  R2 — String overwrite releases:
       StoreLocal / MoveFromRef into a String local, and StoreRef /
       ArrayIndexStore of a String pointee/element.  Emits
       `StringRelease` of the prior value (233,519 corpus).
  R7 — Array overwrite drops:
       StoreLocal into an Array local.  Emits `ArrayDrop` of the
       prior array (143,008 corpus).

PLACEMENT: runs AFTER `string_arc` (dedicated driver bucket).
string_arc keeps its own `_note_use` bookkeeping for these stores;
this pass adds ONLY the old-value release/drop, immediately BEFORE
each eligible store, preserving old-value-before-new-store order and
the store's span.  Running after string_arc keeps its
recognition/occurrence-counting walk from ever seeing these releases,
and needs NO ledger (R2/R7 are pure structural type checks).

PROVENANCE (review 2026-07-20): a `StoreLocal(String|Array, zeroval)`
is NOT categorically a non-overwrite — an INPUT-stream
`ZeroValue(String) -> StoreLocal` into a live slot IS a real overwrite
(string_arc recognizes fresh input ZeroValue Strings as valid owned
empty values).  We must skip ONLY the zero-back stores string_arc
ITSELF synthesized (entry init, `_release_local`, `_drop_*`, MoveOut
expansion) — those were absent from the input the old R2/R7 arms
walked.  string_arc marks each such store `synthetic_zero_back=True`;
this pass skips exactly the marked stores, never inferring provenance
from value shape, temp name, or adjacency.

RETAIN-BEFORE-RELEASE / self-alias (`x = x`): the store-VALUE copy
stake (retain) is materialized upstream by
`string_stakes.materialize_call_arg_stakes` BEFORE string_arc, so a
release here can never drop the shared refcount below the retain.

COMPLETENESS: an INDEPENDENT pre-rewrite inventory of eligible sites
is taken before authoring; the rewritten MIR is validated
STRUCTURALLY afterward (exactly one canonical cleanup immediately
before each inventoried store, with the correct local/ptr/array/type
relationships) — a missing, duplicate, or mismatched cleanup is a
fail-closed AssertionError.

COUNTER: R2 releases fold `overwrite_release` into the process
aggregate via the reporter's strict counted-only recorder (env-gated).
R7 array drops carry no counter (as before this slice).

R6 (site-3 destructible Return cleanup, site-4 drop-before-overwrite)
is NOT here — deferred to Slice B2; string_arc retains it.
"""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable
from . import mir_nodes as M
from . import ownership_ledger_reporter as _ledger_reporter
from .ledger_cache import mark_ledger_dirty
from .string_ownership_analysis import classify_string_array_locals


# Eligible-site kinds (independent inventory + structural validation).
_K_STORE_LOCAL = "store_local"      # R2 String StoreLocal
_K_MOVE_FROM_REF = "move_from_ref"  # R2 String MoveFromRef
_K_STORE_REF = "store_ref"          # R2 String StoreRef
_K_ARRAY_INDEX_STORE = "aistore"    # R2 String ArrayIndexStore
_K_ARRAY_LOCAL = "array_local"      # R7 Array StoreLocal


def _is_string_tid(type_table: TypeTable, tid: "TypeId | None", string_ty: TypeId) -> bool:
	if tid is None:
		return False
	if tid == string_ty:
		return True
	td = type_table.get(tid)
	return td.kind is TypeKind.SCALAR and td.name == "String"


def _is_synthetic_zero_back(instr: M.MInstr) -> bool:
	"""True iff string_arc marked this as one of its OWN synthetic
	zero-back stores (explicit provenance — never a shape guess)."""
	return bool(getattr(instr, "synthetic_zero_back", False))


def _eligible_kind(
	instr: M.MInstr,
	type_table: TypeTable,
	local_types: Dict[str, TypeId],
	string_ty: TypeId,
	string_locals: Set[str],
	array_locals: Set[str],
) -> "str | None":
	"""Return the eligible-site kind for `instr`, or None.  A
	string_arc-synthesized zero-back is explicitly NOT eligible."""
	if isinstance(instr, M.StoreLocal):
		if _is_synthetic_zero_back(instr):
			return None
		if _is_string_tid(type_table, local_types.get(instr.local), string_ty) and instr.local in string_locals:
			return _K_STORE_LOCAL
		if instr.local in array_locals:
			arr_ty = local_types.get(instr.local)
			td = type_table.get(arr_ty) if arr_ty is not None else None
			if td is not None and td.kind is TypeKind.ARRAY and td.param_types:
				return _K_ARRAY_LOCAL
		return None
	if isinstance(instr, M.MoveFromRef):
		if _is_string_tid(type_table, instr.inner_ty, string_ty) and instr.local in string_locals:
			return _K_MOVE_FROM_REF
		return None
	if isinstance(instr, M.StoreRef):
		if _is_string_tid(type_table, instr.inner_ty, string_ty):
			return _K_STORE_REF
		return None
	if isinstance(instr, M.ArrayIndexStore):
		if _is_string_tid(type_table, instr.elem_ty, string_ty):
			return _K_ARRAY_INDEX_STORE
		return None
	return None


def insert_overwrite_cleanup(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
) -> M.MirFunc:
	"""Author instruction-local overwrite releases/drops in `func`.

	Runs after `string_arc`.  Mutates `func` in place and returns it.
	"""
	string_ty, string_locals, array_locals = classify_string_array_locals(
		func, type_table
	)
	local_types: Dict[str, TypeId] = func.local_types

	def _kind(instr: M.MInstr) -> "str | None":
		return _eligible_kind(
			instr, type_table, local_types, string_ty, string_locals, array_locals
		)

	# ── Independent PRE-rewrite inventory (finding #2) ──
	# One entry per eligible input store, keyed by object identity so
	# the post-rewrite bijection check can match each back to its
	# authored cleanup regardless of index shifts.  Fail closed if the
	# SAME instruction object appears as more than one eligible site
	# (aliased into the stream) — the id-keyed dict would else silently
	# collapse it.
	inventory: Dict[int, Tuple[str, M.MInstr]] = {}
	for block in func.blocks.values():
		for instr in block.instructions:
			k = _kind(instr)
			if k is not None:
				if id(instr) in inventory:
					raise AssertionError(
						f"overwrite_cleanup (fn '{func.name}'): the same "
						f"eligible store object appears twice in the "
						f"instruction stream — cannot author a unique "
						f"cleanup for it."
					)
				inventory[id(instr)] = (k, instr)

	# Fresh-temp generator that cannot collide with string_arc's
	# already-emitted `__arc*` temps (they are in local_types now).
	used_ids: Set[str] = set(local_types.keys())
	counter = 0

	def _new_temp() -> str:
		nonlocal counter
		while True:
			counter += 1
			name = f"__ow{counter}"
			if name not in used_ids:
				used_ids.add(name)
				return name

	def _copy_span(dst: M.MInstr, src: M.MInstr) -> None:
		if hasattr(src, "span"):
			setattr(dst, "span", getattr(src, "span"))

	overwrite_release_count = 0
	mutated = False

	for block in func.blocks.values():
		new_instrs: List[M.MInstr] = []
		block_changed = False
		for instr in block.instructions:
			sid = id(instr)
			k = inventory.get(sid, (None, None))[0]
			if k == _K_STORE_LOCAL or k == _K_MOVE_FROM_REF:
				rel = _emit_local_release(new_instrs, instr.local, string_ty, _new_temp, local_types, _copy_span, instr)
				setattr(rel, "ow_authored_for", sid)
				new_instrs.append(instr)
				overwrite_release_count += 1
				block_changed = True
				continue
			if k == _K_STORE_REF:
				old = _new_temp()
				load = M.LoadRef(dest=old, ptr=instr.ptr, inner_ty=instr.inner_ty)
				_copy_span(load, instr)
				new_instrs.append(load)
				rel = M.StringRelease(value=old)
				setattr(rel, "ow_authored_for", sid)
				new_instrs.append(rel)
				local_types[old] = string_ty
				new_instrs.append(instr)
				overwrite_release_count += 1
				block_changed = True
				continue
			if k == _K_ARRAY_INDEX_STORE:
				old = _new_temp()
				load = M.ArrayIndexLoad(dest=old, elem_ty=instr.elem_ty, array=instr.array, index=instr.index)
				_copy_span(load, instr)
				new_instrs.append(load)
				rel = M.StringRelease(value=old)
				setattr(rel, "ow_authored_for", sid)
				new_instrs.append(rel)
				local_types[old] = string_ty
				new_instrs.append(instr)
				overwrite_release_count += 1
				block_changed = True
				continue
			if k == _K_ARRAY_LOCAL:
				arr_ty = local_types.get(instr.local)
				td = type_table.get(arr_ty)
				elem_ty = td.param_types[0]
				tmp = _new_temp()
				load = M.LoadLocal(dest=tmp, local=instr.local)
				_copy_span(load, instr)
				new_instrs.append(load)
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=arr_ty))
				local_types[zero] = arr_ty
				new_instrs.append(M.StoreLocal(local=instr.local, value=zero))
				drop = M.ArrayDrop(elem_ty=elem_ty, array=tmp)
				setattr(drop, "ow_authored_for", sid)
				new_instrs.append(drop)
				local_types[tmp] = arr_ty
				new_instrs.append(instr)
				block_changed = True
				continue
			new_instrs.append(instr)
		if block_changed:
			block.instructions = new_instrs
			# Real dirty mark within the audit's proximity window of
			# the actual mutation (no allow marker) — a changed block's
			# rewrite invalidates cached (block, idx) ledger state.
			mark_ledger_dirty(func, "overwrite_cleanup.block_rewrite")
			mutated = True

	# ── Structural POST-rewrite BIJECTION validation (finding #2) ──
	_validate(func, type_table, inventory)
	if _ledger_reporter.string_arc_audit_enabled() and overwrite_release_count:
		_ledger_reporter.record_counted_only(
			_ledger_reporter.SITE_CLASS_OVERWRITE_RELEASE,
			overwrite_release_count,
		)
	return func


def _emit_local_release(out, local, string_ty, new_temp, local_types, copy_span, src):
	"""Reproduce string_arc's `_release_local`: load the old value,
	zero the slot, release the old value — immediately before the
	overwriting store.  Span copied from the store.  Returns the
	authored `StringRelease` (the caller tags it with the site id)."""
	old = new_temp()
	load = M.LoadLocal(dest=old, local=local)
	copy_span(load, src)
	out.append(load)
	zero = new_temp()
	out.append(M.ZeroValue(dest=zero, ty=string_ty))
	local_types[zero] = string_ty
	out.append(M.StoreLocal(local=local, value=zero))
	rel = M.StringRelease(value=old)
	out.append(rel)
	local_types[old] = string_ty
	return rel


def _validate(func: M.MirFunc, type_table: TypeTable, inventory: Dict[int, Tuple[str, M.MInstr]]) -> None:
	"""BIJECTION between the pre-rewrite eligible-site inventory and the
	pass-authored cleanups (finding #2): every authored cleanup —
	identified independently by its `ow_authored_for` tag — must target
	exactly one inventoried site, exactly once (no orphan, no
	duplicate), sit IMMEDIATELY before that store, and carry the full
	canonical operand/type linkage.  Fail-closed on any deviation."""
	# 1) Collect every authored cleanup by its tag, and index each
	#    instruction's (block, position) for the immediate-precedence /
	#    linkage checks.
	authored: Dict[int, List[Tuple[str, int, M.MInstr]]] = {}
	pos: Dict[int, Tuple[str, int]] = {}
	blocks_instrs: Dict[str, List[M.MInstr]] = {}
	for bn, block in func.blocks.items():
		blocks_instrs[bn] = block.instructions
		for i, ins in enumerate(block.instructions):
			pos[id(ins)] = (bn, i)
			tag = getattr(ins, "ow_authored_for", None)
			if tag is not None:
				authored.setdefault(tag, []).append((bn, i, ins))

	authored_ids = set(authored)
	inv_ids = set(inventory)
	orphans = authored_ids - inv_ids
	if orphans:
		raise AssertionError(
			f"overwrite_cleanup validation (fn '{func.name}'): "
			f"{len(orphans)} authored cleanup(s) target no inventoried "
			f"eligible store (orphan authoring)."
		)
	missing = inv_ids - authored_ids
	if missing:
		raise AssertionError(
			f"overwrite_cleanup validation (fn '{func.name}'): "
			f"{len(missing)} inventoried eligible store(s) received no "
			f"authored cleanup."
		)
	for sid, occ in authored.items():
		if len(occ) != 1:
			raise AssertionError(
				f"overwrite_cleanup validation (fn '{func.name}'): "
				f"eligible store received {len(occ)} authored cleanups "
				f"(expected exactly one — duplicate authoring)."
			)

	# 2) Structural + full type linkage for each (site, cleanup) pair.
	for sid, (kind, store) in inventory.items():
		bn, si = pos[id(store)]
		instrs = blocks_instrs[bn]
		if not _cleanup_linkage_ok(instrs, si, kind, store, type_table):
			raise AssertionError(
				f"overwrite_cleanup validation (fn '{func.name}', "
				f"block '{bn}'[{si}]): {kind} store for '{_subject(store)}' "
				f"lacks a correctly-linked canonical cleanup immediately "
				f"before it (operand/type mismatch)."
			)


def _subject(instr: M.MInstr) -> str:
	if isinstance(instr, (M.StoreLocal, M.MoveFromRef)):
		return instr.local
	if isinstance(instr, M.StoreRef):
		return instr.ptr
	if isinstance(instr, M.ArrayIndexStore):
		return instr.array
	return "?"


def _elem_ty_of(type_table: TypeTable, local_ty: "TypeId | None") -> "TypeId | None":
	if local_ty is None:
		return None
	td = type_table.get(local_ty)
	if td.kind is TypeKind.ARRAY and td.param_types:
		return td.param_types[0]
	return None


def _cleanup_linkage_ok(instrs: List[M.MInstr], i: int, kind: str, store: M.MInstr, type_table: TypeTable) -> bool:
	"""Full operand + type linkage for the canonical cleanup that must
	immediately precede `instrs[i]` (the store).  The authored
	StringRelease/ArrayDrop is tagged `ow_authored_for == id(store)`."""
	sid = id(store)
	string_ty = type_table.ensure_string()

	def tagged(ins) -> bool:
		return getattr(ins, "ow_authored_for", None) == sid

	if kind in (_K_STORE_LOCAL, _K_MOVE_FROM_REF):
		if i < 4:
			return False
		load, zv, zstore, rel = instrs[i - 4], instrs[i - 3], instrs[i - 2], instrs[i - 1]
		return (
			isinstance(load, M.LoadLocal) and load.local == store.local
			and isinstance(zv, M.ZeroValue) and zv.ty == string_ty
			and isinstance(zstore, M.StoreLocal)
			and zstore.local == store.local and zstore.value == zv.dest
			and getattr(zstore, "synthetic_zero_back", False) is False
			and isinstance(rel, M.StringRelease) and rel.value == load.dest
			and tagged(rel)
		)
	if kind == _K_STORE_REF:
		if i < 2:
			return False
		load, rel = instrs[i - 2], instrs[i - 1]
		return (
			isinstance(load, M.LoadRef) and load.ptr == store.ptr
			and load.inner_ty == store.inner_ty
			and isinstance(rel, M.StringRelease) and rel.value == load.dest
			and tagged(rel)
		)
	if kind == _K_ARRAY_INDEX_STORE:
		if i < 2:
			return False
		load, rel = instrs[i - 2], instrs[i - 1]
		return (
			isinstance(load, M.ArrayIndexLoad) and load.array == store.array
			and load.index == store.index and load.elem_ty == store.elem_ty
			and isinstance(rel, M.StringRelease) and rel.value == load.dest
			and tagged(rel)
		)
	if kind == _K_ARRAY_LOCAL:
		if i < 4:
			return False
		load, zv, zstore, drop = instrs[i - 4], instrs[i - 3], instrs[i - 2], instrs[i - 1]
		return (
			isinstance(load, M.LoadLocal) and load.local == store.local
			and isinstance(zv, M.ZeroValue)
			and isinstance(zstore, M.StoreLocal)
			and zstore.local == store.local and zstore.value == zv.dest
			and getattr(zstore, "synthetic_zero_back", False) is False
			and isinstance(drop, M.ArrayDrop) and drop.array == load.dest
			and drop.elem_ty == _elem_ty_of(type_table, zv.ty)
			and tagged(drop)
		)
	return False
