# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
String ARC insertion for MIR.

This pass inserts explicit StringRetain/StringRelease (and CopyValue) ops so
LLVM codegen does not need to guess ownership. It also expands MoveOut into
LoadLocal + ZeroValue + StoreLocal once retains/releases are inserted.

PIPELINE PRECONDITION (tripwire-deletion slice, 2026-07-18): in
production, `string_releases.materialize_lastuse_releases` MUST run
before `insert_string_arc` (the driver's cleanup_authoring loop does).
string_arc authors NO last-use releases of its own: the in-pass release
arm went corpus-zero when TLR-7 closed the family ladder, was
fail-closed through the 0.33.84 cert cycle (release-arm tripwire,
2026-07-16 — one production catch, TLR-8, fixed in-tree; zero firings
at cert), and was DELETED together with the 4a/4b dead-stake tripwires
after that clean cycle.  Bare `insert_string_arc` on MIR containing
family temps that drain non-consumingly silently UNDER-RELEASES (those
releases are the materialization pass's job).  Direct unit use is
valid only for tests that document why their MIR carries no such
temps (no family producers, or all consumed / live-out /
pre-materialized).
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping, Set

from lang.driftc.checker import FnInfo
from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable
from lang.driftc.core.function_id import function_symbol
from lang.driftc.core.function_id import FunctionId
from lang.driftc import debug as drift_debug
from . import mir_nodes as M
from . import cfg as _cfg
from . import ownership_ledger_events as _ledger_events
from . import ownership_ledger_reporter as _ledger_reporter
from .ledger_cache import mark_ledger_dirty, maybe_fresh_ledger
from .ownership_ledger import DropVerdict as _DropVerdict
from .drop_policy_compute import compute_drop_policy as _compute_drop_policy
from .drop_policy_compute import zero_storage_drop_safe as _zero_storage_drop_safe
from .drop_flags import is_flag_managed as _is_flag_managed


def variant_zero_tag_drop_safe(local_ty: TypeId, type_table: TypeTable) -> bool:
	"""COMPATIBILITY SHIM (string-arc-endgame-array-sweep,
	2026-07-19) — tests/back-compat only; NO production caller may
	remain (maintainer migration rule: no production decision through
	the misleading variant-only name).

	The production policy axis is
	`drop_policy_compute.zero_storage_drop_safe`, which additionally
	admits ARRAY (zeroed-header drop is a no-op).  This shim keeps
	the ORIGINAL variant-only semantics so existing test expectations
	stay meaningful; it dies with string_arc.  Historical rationale
	(Phase 4 site-3 sub-step 3) lives on the new predicate's
	docstring.
	"""
	td = type_table.get(local_ty)
	return td.kind is TypeKind.VARIANT


# ── R10 extraction (string-arc-endgame-r10-extraction, 2026-07-20) ──
#
# The shared string-ownership ANALYSES (iter_used_values,
# seed_string_dest_types, is_materialized_release_family_producer,
# build_fnwide_producers, compute_lastuse_release_points,
# recognize_materialized_releases, compute_string_temp_liveness,
# string_operand_dispositions + the DISPOSITION_* constants and
# DRIFT_STRING_HELPER_SYMBOLS) moved VERBATIM to
# `string_ownership_analysis.py` — the neutral library both this
# pass and `string_releases` consume.  Only the names the remaining
# emission code references are imported back here.  The dead
# `consumes_string_operand` wrapper (zero call sites) was deleted
# with the move; its contract prose lives with the library.
from .string_ownership_analysis import classify_string_array_locals
from .string_ownership_analysis import (
	DRIFT_STRING_HELPER_SYMBOLS,
	build_fnwide_producers,
	compute_string_temp_liveness,
	iter_used_values,
	recognize_materialized_releases,
	seed_string_dest_types,
)



def insert_string_arc(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
	fn_infos: Mapping[FunctionId, FnInfo],
) -> M.MirFunc:
	# PIPELINE PRECONDITION: run `materialize_lastuse_releases` first in
	# production — string_arc authors no last-use releases of its own
	# (the in-pass arm was deleted with the tripwire-deletion slice,
	# 2026-07-18; see the module doc).  Direct unit use only for tests
	# that document why their MIR carries no unmaterialized family
	# temps.
	# (`is_destructor_method` was the site-local destructor-self
	# skip flag; retired in Phase 4 site-3 sub-step 2.  The lattice
	# now models the destructor's runtime-owned `self` at the
	# Return terminator and the per-local ledger consultation in
	# the Return branch handles the skip.)
	# Phase 3B step 1 (`drop_before_overwrite` swap): the ledger is
	# attached unconditionally by the driver and consulted as the
	# authoritative drop verdict at site 4.  Site 3 (`string_arc_return`)
	# remains observational pending its own swap.  Sites read the
	# canonical `DropPolicy.needs_drop` via `_compute_drop_policy` —
	# NOT the raw `TypeTable.has_drop` query that the 3A reporter
	# uses (the quarantined approximation in driftc.py).
	# Pass-entry consumer site.  Use `maybe_fresh_ledger` (soft form)
	# because string_arc legitimately runs against MIR without an
	# attached ledger in pass-local testing.  A *stale* ledger still
	# asserts; that is the bug class we are catching.
	_ledger = maybe_fresh_ledger(func, "string_arc")
	# B-arch-0 differential stake audit (Scope B §11.2).  OFF by default:
	# `_audit is None` unless DRIFT_STRING_ARC_AUDIT=1, and every
	# recording site below is guarded on that — the disabled path is
	# behavior-identical (zero instructions, zero diagnostics, zero
	# allocations beyond this None).  `_audit_point` is the pre-MIR
	# program point of the instruction currently being rewritten (the
	# L_pre anchor); Return-boundary emissions use the established
	# site-3 convention (block, len(original_instructions)).
	_audit = (
		_ledger_reporter.StringArcAudit(func.name)
		if _ledger_reporter.string_arc_audit_enabled()
		else None
	)
	_audit_point: list = [("", 0)]
	def _ledger_needs_drop(local: str) -> bool:
		ty = local_types.get(local)
		if ty is None:
			return False
		try:
			return bool(type_table.has_drop(ty))
		except Exception:
			return False
	local_types: Dict[str, TypeId] = func.local_types
	# Slice B1: shared single-source classifier (string_ownership_analysis)
	# so string_arc and overwrite_cleanup classify String/Array locals
	# identically (a mismatch would leak or double-free).
	string_ty, string_locals, array_locals = classify_string_array_locals(
		func, type_table
	)
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
	# Cycle guard for _type_needs_drop: tids whose by-value field closure is
	# still being computed up the call stack. A directly-recursive value type is
	# rejected earlier by validate_no_recursive_value_types, but malformed/legacy
	# package metadata could still present one here; this prevents a raw Python
	# RecursionError on the back-edge (the result cache is written only AFTER the
	# recursion returns, so it cannot break the cycle on its own).
	_type_needs_drop_active: Set[TypeId] = set()
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
		if tid in _type_needs_drop_active:
			# Cycle back-edge: a directly-recursive value type should have been
			# rejected by validate_no_recursive_value_types; break the edge as
			# False (the correct least-fixpoint seed for the OR-of-fields below)
			# instead of recursing forever into a Python RecursionError. Do NOT
			# cache this provisional False — the outer in-progress call computes
			# and caches the real result.
			return False
		_type_needs_drop_active.add(tid)
		try:
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
		finally:
			_type_needs_drop_active.discard(tid)

	# __borrow_tmp: Stage 1 (borrow_materialize.py) normally materialises rvalue
	# borrows into __tmp_borrow* locals that get scope-based drops.  This Stage 2
	# inclusion is a defensive fallback in case that assumption breaks.
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
	# Cycle guard for _is_nullsafe_drop, mirroring _type_needs_drop_active: the
	# result cache is written only after recursion returns, so it cannot break a
	# self-loop in a malformed/legacy recursive value type on its own.
	_nullsafe_drop_active: Set[TypeId] = set()

	def _is_nullsafe_drop(tid: TypeId) -> bool:
		cached = _nullsafe_drop_cache.get(tid)
		if cached is not None:
			return cached
		if tid in _nullsafe_drop_active:
			# Cycle back-edge: a directly-recursive value type should have been
			# rejected before stage 2. True is the identity for the `all()`
			# aggregation below, so the back-edge does not alter the real
			# (non-cyclic) verdict; recurse no further (avoids RecursionError).
			# Not cached — the outer in-progress call caches the real result.
			return True
		_nullsafe_drop_active.add(tid)
		try:
			return _is_nullsafe_drop_body(tid)
		finally:
			_nullsafe_drop_active.discard(tid)

	def _is_nullsafe_drop_body(tid: TypeId) -> bool:
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

	def _ensure_owned(
		val: str,
		owned: Set[str],
		out: list[M.MInstr],
		site_class: str = _ledger_reporter.SITE_CLASS_VALUE_POSITION_RETAIN,
	) -> str:
		# Tripwire-deletion slice (2026-07-18): identity pass-through.
		# The terminal late-retain arm that lived here — the single
		# funnel for call_arg_retain, value_position_retain and
		# return_retain_site3 stakes — went corpus-zero when B-arch
		# migrated stake authoring into string_stakes (114,107 → 0),
		# was fail-closed by the shared dead-stake tripwire (slices
		# 4a/4b), and was DELETED after the clean 0.33.84 cert cycle
		# (zero firings; the certified run also exercised the
		# drift-workflows corpus).  Untyped values keep their
		# historical pass-through; proven-String values now join it —
		# staking is owned UPSTREAM by string_stakes, never here.  The
		# call sites keep the funnel shape so the site_class taxonomy
		# stays greppable until string_arc itself is deleted.
		return val

	def _param_is_string(tid: TypeId) -> bool:
		td = type_table.get(tid)
		return td.kind is TypeKind.SCALAR and td.name == "String"

	def _param_is_ref(tid: TypeId) -> bool:
		td = type_table.get(tid)
		return td.kind is TypeKind.REF

	def _release_local(local: str, out: list[M.MInstr], *, site_class: str) -> None:
		if local not in string_locals:
			return
		old = _new_temp()
		out.append(M.LoadLocal(dest=old, local=local))
		zero = _new_temp()
		out.append(M.ZeroValue(dest=zero, ty=string_ty))
		local_types[zero] = string_ty
		_zb = M.StoreLocal(local=local, value=zero)
		setattr(_zb, "synthetic_zero_back", True)  # Slice B1 provenance
		out.append(_zb)
		if _audit is not None:
			# Subject is the STORAGE LOCAL (what C1 quantifies over),
			# not the loaded temp.
			_audit.note(
				_ledger_reporter.STAKE_RELEASE, local, site_class,
				pre_point=_audit_point[0],
				post_point=(block.name, len(out)),
			)
		out.append(M.StringRelease(value=old))
		local_types[old] = string_ty

	def _release_all_locals(out: list[M.MInstr], *, skip_locals: Set[str] | None = None) -> None:
		skip = skip_locals or set()
		for local in sorted(string_locals):
			if local in skip:
				continue
			_release_local(local, out, site_class=_ledger_reporter.SITE_CLASS_SCOPE_EXIT_RELEASE)

	# `_drop_all_arrays` (Return-boundary array sweep) was deleted in B-U
	# (2026-07-19); `_drop_array_local` (R7 array overwrite drop) was
	# deleted in Slice B1 (2026-07-20) — that authority moved to
	# `overwrite_cleanup`.

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
		_zb = M.StoreLocal(local=local, value=zero)
		setattr(_zb, "synthetic_zero_back", True)  # Slice B1 provenance
		out.append(_zb)
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

	# TLR-2a: single-source alias — the shared occurrence iterator
	# lives at module level (iter_used_values) so the release-point
	# calculator counts EXACTLY the occurrences this pass counts.
	_iter_used_values = iter_used_values

	def _copy_span(dst: M.MInstr, src: M.MInstr) -> None:
		if hasattr(src, "span"):
			setattr(dst, "span", getattr(src, "span"))

	def _iter_term_used(term: M.MTerminator) -> Iterable[str]:
		# Central MIR terminator value-use contract (stage2/cfg.py →
		# MTerminator.value_uses()): Return→value, IfTerminator→cond,
		# SwitchTerminator→scrutinee.  Keeping this central means a value consumed
		# only by a (new) terminator stays live without editing this scanner.
		yield from _cfg.terminator_value_uses(term)

	def _seed_dest_types() -> None:
		"""Pre-seed missing destination types before ARC liveness/use
		analysis.  Delegates to the shared module-level
		`seed_string_dest_types` (TLR-2a) — single source with the
		TLR-2b pass."""
		seed_string_dest_types(
			[func.blocks[bname] for bname in block_order],
			local_types,
			fn_infos=fn_infos,
			type_table=type_table,
		)

	def _block_succs(term: M.MTerminator | None) -> list[str]:
		# Central MIR CFG-successor contract (stage2/cfg.py).
		return _cfg.terminator_successors(term)

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

	# TLR-7: the fn-wide producer map — ONE lookup authority shared with
	# the materialization pass (via `build_fnwide_producers`), consumed
	# by per-block recognition and the TLR shim classification so
	# cross-block family temps resolve identically everywhere.
	producers_fnwide = build_fnwide_producers(
		[func.blocks[bname] for bname in block_order]
	)

	# Per-block live-out for string temps — delegates to the shared
	# module-level fixpoint (TLR-2b): single liveness author with the
	# materialization pass.  (`_is_local_name` is always False — see its
	# definition — so the historical name-exclusion is a no-op the shared
	# helper drops.)
	live_out = compute_string_temp_liveness(
		func.blocks,
		block_order,
		local_types=local_types,
		string_ty=string_ty,
	)

	# Definite local assignment across CFG.
	preds = _block_preds()
	store_defs: Dict[str, Set[str]] = {}
	for name in block_order:
		stores: Set[str] = set()
		for instr in func.blocks[name].instructions:
			if isinstance(instr, M.StoreLocal):
				stores.add(instr.local)
			elif isinstance(instr, M.MoveFromRef):
				# MoveFromRef is a definite assignment to `local` — the
				# transferred bytes land in the local's storage.
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
				elif isinstance(instr, M.MoveFromRef):
					# Fresh assignment; local is no longer "moved-out."
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
			elif isinstance(instr, M.ExcGetParamsJson):
				# `drift_error_get_params_json` returns a RETAINED
				# DriftString per ABI spec §2.3 — caller owns and is
				# responsible for releasing.  Tracked as an owned
				# string-result alongside the StringConcat / Call class.
				# TLR-5: this prepass is the ONLY owned registration for
				# Exc* dests (no rewrite-loop re-add arm).  Recognized
				# temps stay owned here since Sub-slice A1 deleted the
				# per-block subtraction — output-neutral under the T4
				# proof (see the MoveOut arm).
				owned_defs.add(dest)
			elif isinstance(instr, M.ExcGetContextJson):
				# Same retained-string contract (and TLR-5 prepass-only
				# suppression coverage) as ExcGetParamsJson above.
				owned_defs.add(dest)
			elif isinstance(instr, M.ErrorEventFqn):
				# Slice 3 DV→JSON: codegen retains the extracted
				# event_fqn String so the dest is independently owned.
				owned_defs.add(dest)
			elif isinstance(instr, (M.Call, M.CallIndirect, M.CallIface)):
				# String-returning calls produce owned values that must be released
				# when their last use in the block is consumed.
				# TLR-4: this fn-wide prepass is the ONLY owned
				# registration for call dests (no rewrite-loop re-add
				# arm).  Recognized temps stay owned here since
				# Sub-slice A1 deleted the per-block subtraction —
				# output-neutral under the T4 proof (see the MoveOut
				# arm).
				owned_defs.add(dest)
			elif isinstance(instr, M.PtrRead):
				if _is_string_tid(instr.elem_ty):
					owned_defs.add(dest)
			elif isinstance(instr, M.RawBufferRead):
				# `mem.read<T>(&mut buf, i)` moves the slot's value out;
				# slot becomes uninitialized.  For refcount-bearing T
				# (String) the +1 stake travels with the read result —
				# treat it as already-owned, identical to `ArrayElemTake`
				# / `PtrRead`.  Without this, the subsequent
				# `StoreLocal(local, dest)` in
				# `val v = mem.read<String>(...)` inserts a spurious
				# `StringRetain` (refcount → 2), and the final drop
				# leaves refcount at 1 → the original allocation leaks
				# (carrier C7 in `test_typebox_take.py`; web-team
				# `ctx_take<T>` is the motivating use case).
				if _is_string_tid(instr.elem_ty):
					owned_defs.add(dest)
					move_only_defs.add(dest)
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
				_zb = M.StoreLocal(local=local, value=zero)
				setattr(_zb, "synthetic_zero_back", True)  # Slice B1 provenance
				new_instrs.append(_zb)
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
				_zb = M.StoreLocal(local=local, value=zero)
				setattr(_zb, "synthetic_zero_back", True)  # Slice B1 provenance
				new_instrs.append(_zb)
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
				_zb = M.StoreLocal(local=local, value=zero)
				setattr(_zb, "synthetic_zero_back", True)  # Slice B1 provenance
				new_instrs.append(_zb)
				local_types[zero] = dest_ty
		# TLR-2b recognition — BEFORE use counting (the load-bearing
		# ordering: StringRelease is a use per `_iter_used_values`, so an
		# unrecognized materialized release would inflate its temp's
		# count and move the last-use point).  In-contract
		# pre-materialized releases are excluded from occurrence counts
		# entirely; ANY out-of-contract input release fails closed inside
		# the shared analysis (`unexpected input release`).  Fast path:
		# blocks without input releases (every unit-test run without the
		# materialization pass; post-pass blocks with no qualified temps)
		# skip the analysis.
		recognized_released: Set[str] = (
			recognize_materialized_releases(
				block,
				local_types=local_types,
				fn_infos=fn_infos,
				type_table=type_table,
				live_out_names=live_out.get(block.name, set()),
				producers_fnwide=producers_fnwide,
			)
			if any(isinstance(_i, M.StringRelease) for _i in block.instructions)
			else set()
		)
		# Sub-slice A1 (string-arc-endgame-array-sweep, 2026-07-18):
		# the TLR-2b `owned_values -= recognized_released` prepass
		# subtraction and the ConstString/StringFrom*/Concat re-add
		# guards were DELETED together with their consistency-only
		# role.  Owned state of a recognized temp is output-neutral
		# under the T4 proof (see the MoveOut arm): it may propagate
		# and steer branches, but every affected branch is
		# output-equivalent, so no branch can author another
		# instruction or release.  The recognition machinery itself
		# (prescan exclusion + copy-through arm) is untouched.
		# Count uses in this block for temp string values.
		use_counts: Dict[str, int] = {}
		producers: Dict[str, M.MInstr] = {}
		for instr in block.instructions:
			if isinstance(instr, M.StringRelease) and instr.value in recognized_released:
				# TLR-2b prescan-exclusion: contributes no occurrence
				# (symmetric with the rewrite loop's recognition arm,
				# which copies it through with no `_note_use`).
				continue
			dest = getattr(instr, "dest", None)
			if isinstance(dest, str):
				producers[dest] = instr
			# String ZeroValue and element-extraction dests carry their
			# type on the INSTRUCTION; register it before use counting
			# so these temps participate in ownership tracking at all
			# (`use_counts` skips untyped values) even when upstream
			# metadata omitted them from func.local_types.  Originally
			# added to keep a metadata gap from false-firing the
			# slice-4a store_value tripwire; the tripwire is deleted
			# (2026-07-18) but the visibility this pre-scan provides is
			# still what keeps these dests owned/moved correctly.
			if isinstance(instr, M.ZeroValue) and _is_string_tid(instr.ty) and instr.dest not in local_types:
				local_types[instr.dest] = instr.ty
			elif (
				isinstance(instr, (M.ArrayIndexLoad, M.ArrayIndexLoadUnchecked))
				and _is_string_tid(instr.elem_ty)
				and instr.dest not in local_types
			):
				local_types[instr.dest] = instr.elem_ty
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
			# No last-use release arm here (deleted 2026-07-18 after the
			# clean 0.33.84 cert cycle held its tripwire at zero
			# firings): every last-use release of a family temp is
			# authored by the string_releases pass and copied through by
			# the recognition arm above.  An owned temp draining to zero
			# non-consumingly leaves only bookkeeping state, which is
			# output-neutral (see the MoveOut arm proof).

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
				return sym in DRIFT_STRING_HELPER_SYMBOLS
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

		for _instr_idx, instr in enumerate(block.instructions):
			if _audit is not None:
				_audit_point[0] = (block.name, _instr_idx)
			if isinstance(instr, M.StringRelease) and instr.value in recognized_released:
				# TLR-2b recognition arm: copy the pre-materialized
				# release through verbatim with NO `_note_use` — its
				# occurrence was never counted (prescan-exclusion), so
				# no decrement may happen either; an uncounted decrement
				# would skew `_can_move_owned_once` decisions.  The
				# audit note here keeps `materialized_lastuse_release`
				# author-independent (same event, new author) and
				# `events` constant.
				if _audit is not None:
					_audit.note(
						_ledger_reporter.STAKE_RELEASE, instr.value,
						_ledger_reporter.SITE_CLASS_MATERIALIZED_LASTUSE_RELEASE,
						pre_point=_audit_point[0],
						post_point=(block.name, len(new_instrs)),
					)
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.StoreLocal):
				moved_out_locals.discard(instr.local)
				explicitly_dropped_locals.discard(instr.local)
			if isinstance(instr, M.MoveFromRef):
				# MoveFromRef defines `local` (transferred ownership);
				# clear any prior moved/dropped state.
				moved_out_locals.discard(instr.local)
				explicitly_dropped_locals.discard(instr.local)
			if isinstance(instr, M.StoreLocal) and instr.local in array_locals:
				# R7 array overwrite drop MOVED to overwrite_cleanup
				# (Slice B1, 2026-07-20). string_arc keeps no per-array
				# bookkeeping here — just pass the store through.
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.StoreLocal) and instr.local in nullsafe_destructible_locals:
				_drop_destructible_local(instr.local, new_instrs)
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.StoreLocal) and instr.local in destructible_locals:
				# Phase 4 (post-3c) — `drop_before_overwrite` promoted
				# to **Tier 1: pure ledger authority.**  The legacy
				# `initialized_destructibles` dataflow fallback is
				# RETIRED.  Site 4 authors nothing of its own; the
				# verdict comes from `_ledger.verdict_at(...)` with
				# `_compute_drop_policy(type_table, ty).needs_drop` as
				# the sole type-level needs_drop axis.
				#
				# Proof obligation: the ledger MUST resolve every
				# StoreLocal point here to either `MUST_DROP` or
				# `MUST_NOT_DROP`.  `PathDependent` (lattice
				# `MaybeUninit` at this point) is unreached at smoke
				# + e2e today (100 % verdict agreement across 1031
				# cases); if it ever appears, the `RuntimeError`
				# below fires loudly so the regression is investigated
				# before it silently reintroduces split authority.
				# Same behavior if the ledger is unset — any caller
				# that hits site 4 MUST attach a ledger first.
				#
				# Build-timing invariant: the ledger consulted here is
				# the POST-`drop_flags` ledger (driver rebuilds it
				# between `drop_flags` and `string_arc`).  drop_flags
				# inserts drop-flag init instructions (`ConstBool` +
				# `StoreLocal(__drop_flag_*)`) at block heads which
				# shift every subsequent index, so the
				# pre-drop_flags ledger's `(block, idx)` keys do not
				# line up with the indices string_arc walks here.
				# Pinned by
				# `lang/tests/driver/test_if_join_drop_destructor_uniform_move.py`.
				if _ledger is None:
					raise RuntimeError(
						f"drop_before_overwrite invoked without an "
						f"attached ownership ledger (fn={func.name}, "
						f"block={block.name}, local={instr.local}); "
						f"Tier-1 site requires `func._ownership_ledger` "
						f"to be set by the driver before `string_arc`."
					)
				_local_ty = local_types.get(instr.local)
				_needs_drop = (
					bool(_compute_drop_policy(type_table, _local_ty).needs_drop)
					if _local_ty is not None
					else False
				)
				_verdict = _ledger.verdict_at(
					(block.name, _instr_idx),
					instr.local,
					needs_drop=_needs_drop,
				)
				if _verdict is _DropVerdict.MUST_DROP:
					_should_drop = True
					_site_verdict_str = _ledger_events.VERDICT_MUST_DROP
					_site_reason = _ledger_events.REASON_NEEDS_DROP
				elif _verdict is _DropVerdict.MUST_NOT_DROP:
					_should_drop = False
					_site_verdict_str = _ledger_events.VERDICT_MUST_NOT_DROP
					_site_reason = _ledger_events.REASON_NOT_DROP_NEEDING
				else:
					# PathDependent — proof-obligation tripwire.  The
					# observe re-run said the lattice never yields
					# MaybeUninit at drop_before_overwrite points.  If
					# it ever does, this raise signals the regression.
					raise RuntimeError(
						f"drop_before_overwrite: ledger returned "
						f"PathDependent at (fn={func.name}, "
						f"block={block.name}, idx={_instr_idx}, "
						f"local={instr.local}).  Tier-1 promotion "
						f"retired the `initialized_destructibles` "
						f"fallback — if PathDependent is now reachable, "
						f"either tighten the lattice or restore a "
						f"flag-guarded path here before re-landing."
					)
				if drift_debug.enabled("ownership_ledger"):
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
					if _audit is not None:
						_audit.note(
							_ledger_reporter.STAKE_RELEASE, instr.local,
							_ledger_reporter.SITE_CLASS_DROP_BEFORE_OVERWRITE_SITE4,
							pre_point=(block.name, _instr_idx),
							post_point=(block.name, len(new_instrs)),
						)
					_drop_destructible_local(instr.local, new_instrs)
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.MoveOut):
				# Emit load + zero-store, but keep ownership of the moved value.
				if _audit is not None:
					# Snapshot the cleanup-drop pairing from the SOURCE
					# stream now — finalize runs after every block was
					# rewritten, so the pre_point index stops aligning.
					_nxt = (
						block.instructions[_instr_idx + 1]
						if _instr_idx + 1 < len(block.instructions)
						else None
					)
					_audit.note(
						_ledger_reporter.STAKE_MOVEOUT_EXPANSION, instr.local,
						_ledger_reporter.SITE_CLASS_MOVEOUT_EXPANSION,
						pre_point=(block.name, _instr_idx),
						post_point=(block.name, len(new_instrs)),
						moveout_feeds_drop=(
							isinstance(_nxt, M.DropValue)
							and _nxt.value == instr.dest
						),
					)
				new_instrs.append(M.LoadLocal(dest=instr.dest, local=instr.local))
				local_types[instr.dest] = instr.ty
				if _is_string_tid(instr.ty):
					# TLR-8: MoveOut is family (the dest inherits the
					# storage local's +1 verbatim).  The
					# `recognized_released` re-add guard that lived here
					# (TLR-6/8 teeth) was deleted 2026-07-18 together
					# with the release arm it protected: a re-added
					# recognized temp cannot cause a second release.
					# The re-owned state MAY propagate (AssignSSA copies
					# owned membership) and affect branch selection
					# (`_can_move_owned_once` reads it), but every
					# affected branch is output-equivalent —
					# `_ensure_owned` is identity, the store paths are
					# unconditional, and `_note_use` only changes
					# bookkeeping — so no branch can author another
					# instruction or release; recognition copies the
					# pass-materialized release through without
					# consulting `owned_values`.
					owned_values.add(instr.dest)
					move_only_values.add(instr.dest)
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=instr.ty))
				_zb = M.StoreLocal(local=instr.local, value=zero)
				setattr(_zb, "synthetic_zero_back", True)  # Slice B1 provenance
				new_instrs.append(_zb)
				local_types[zero] = instr.ty
				moved_out_locals.add(instr.local)
				# Post-MoveOut the storage is zeroed.  A subsequent
				# StoreLocal must NOT re-drop the zero bytes (tag=0
				# can dispatch to a ctor whose drop reads zeroed
				# reference fields → SEGV).  Tier-1 site 4 handles
				# this correctly via the ledger: the MoveOut
				# transfers state to `MOVED_OUT`, so `verdict_at` at
				# the next StoreLocal returns `MUST_NOT_DROP` and no
				# drop-before-overwrite is emitted.
				continue

			if isinstance(instr, M.ConstString):
				# Re-add unconditional since Sub-slice A1 (guard
				# deleted with the prepass subtraction — see its
				# comment above): recognized-temp owned state is
				# output-neutral.
				owned_values.add(instr.dest)
			elif isinstance(instr, (M.StringFromInt, M.StringFromBool, M.StringFromUint, M.StringFromFloat, M.StringConcat)):
				# TLR-3/5 family members; unconditional re-add since
				# Sub-slice A1 (same as the ConstString arm).
				owned_values.add(instr.dest)
			elif isinstance(instr, M.StringRetain):
				owned_values.add(instr.dest)
			elif isinstance(instr, M.ZeroValue):
				# Zeroed String bytes are a valid OWNED empty value —
				# retain AND release of a zeroed String are both runtime
				# no-ops — so a fresh input-stream ZeroValue dest is
				# owned/no-stake-needed.  Without this, the store paths'
				# `_ensure_owned` emitted a dead retain of zeroed bytes;
				# the single wild carrier was the `captures(move <String>)`
				# env-slot ZERO-BACK (`StoreRef(env_field, ZeroValue)`) in
				# hidden-lambda prologues — the last invisible-stake
				# residual (c2_invisible_stake / store_value_retain
				# singletons, reconciled 2026-07-13, PROGRESS).
				if _is_string_tid(instr.ty):
					owned_values.add(instr.dest)
			elif isinstance(instr, M.CopyValue):
				# TLR-6: CopyValue is family.  Its `recognized_released`
				# re-add guard was deleted 2026-07-18 with the release
				# arm (see the MoveOut arm for the full argument): the
				# re-owned state cannot author another instruction or
				# release — every branch it can reach is
				# output-equivalent.
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
					# have their additional-consumer retains staked
					# upstream by string_stakes, with the original +1
					# released at the final use.  Adding to
					# `move_only_values` would bypass the single-use
					# guard and let the first consumer move the ref
					# while later consumers observed a consumed value.
					owned_values.add(instr.dest)
			elif isinstance(instr, (M.ArrayIndexLoad, M.ArrayIndexLoadUnchecked)):
				# OWNED AT EXTRACTION (B-arch-1d contract; see the
				# `# owned-at-extraction:` markers in llvm_codegen.py and
				# string_stakes.py, enforced by
				# test_extraction_retain_contract.py): codegen lowers a
				# String element load as load + drift_string_retain, so
				# the dest arrives with its own +1 — string_arc must MOVE
				# it (single use); additional-consumer retains are staked
				# upstream by string_stakes, exactly like VariantGetField
				# above.  The
				# pre-contract `discard` view classification orphaned the
				# codegen +1 whenever a consumer re-staked (the 1d leak
				# shape); it was corpus-latent on the CLI path and
				# surfaced through the in-process pipeline's explicit
				# bounds-check shape (`idx_ok`/`__idx_tmp` stores of
				# ArrayIndexLoadUnchecked dests) when the slice-4a
				# tripwire made the fallback fail-closed.  NOT
				# move_only_values — same multi-consumer rationale as the
				# VariantGetField arm.
				local_types[instr.dest] = instr.elem_ty
				if _is_string_tid(instr.elem_ty):
					owned_values.add(instr.dest)
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
			elif isinstance(instr, M.RawBufferRead):
				# See the matching `RawBufferRead` branch in the
				# `owned_defs` pass above for the rationale.  Mirrors
				# `ArrayElemTake` / `PtrRead` — `mem.read<T>` is a
				# move-out-of-storage primitive; the read result owns
				# the +1 stake for refcount-bearing element types.
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
				# R2 overwrite release MOVED to overwrite_cleanup
				# (Slice B1); string_arc keeps only its consume
				# bookkeeping for the stored value.
				val = instr.value
				# Store staking is owned UPSTREAM by string_stakes; the
				# 4a proven-String tripwire branch was deleted
				# 2026-07-18 after the clean 0.33.84 cert cycle, which
				# left the move arm and the historical pass-through
				# IDENTICAL — the store consumes its source exactly
				# once, retain-free, for moved, owned-single-use and
				# untyped values alike.
				new_instrs.append(M.StoreLocal(local=instr.local, value=val))
				_note_use(val, consume=True)
				continue

			if isinstance(instr, M.MoveFromRef) and _is_string_tid(instr.inner_ty):
				# Ownership-transfer store: the source `*ptr` stake
				# moves into `local` atomically (codegen handles the
				# load + tombstone-write + transfer).  string_arc must
				# NOT insert a `StringRetain` here — the value is
				# transferred, not copied.
				#
				# Release any prior owned value at the destination so
				# we don't leak it.  For a freshly-UNINIT local (the
				# canonical match-cleanup-authoring use case),
				# `_release_local` issues a release on the zero bytes
				# loaded from the slot — `drift_string_release(null)`
				# is a runtime no-op, so this is safe regardless of
				# whether the local was previously written.
				# R2 overwrite release MOVED to overwrite_cleanup (B1).
				new_instrs.append(instr)
				_note_use(instr.ptr, consume=True)
				continue

			if isinstance(instr, M.StoreRef) and _is_string_tid(instr.inner_ty):
				# R2 overwrite release MOVED to overwrite_cleanup (B1).
				val = instr.value
				# Retain-free single consume for every value class (see
				# the StoreLocal arm — 4a tripwire branch deleted
				# 2026-07-18, arms were identical without it).
				new_instrs.append(M.StoreRef(ptr=instr.ptr, value=val, inner_ty=instr.inner_ty))
				_note_use(val, consume=True)
				continue

			if isinstance(instr, M.ArrayIndexStore) and _is_string_tid(instr.elem_ty):
				# R2 overwrite release MOVED to overwrite_cleanup (B1).
				val = instr.value
				# Retain-free single consume for every value class (see
				# the StoreLocal arm — 4a tripwire branch deleted
				# 2026-07-18, arms were identical without it).
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
								args.append(_ensure_owned(arg, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_VALUE_POSITION_RETAIN))
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
								args.append(_ensure_owned(arg, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_VALUE_POSITION_RETAIN))
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

			if isinstance(instr, M.ExcSetParamsJson):
				# `drift_error_set_params_json` takes ownership of
				# `json_text` per ABI spec §2.3 — runtime releases the
				# prior params_json and stores the input.  ARC must
				# treat `json_text` as consumed (not a non-consuming
				# read).
				json_val = instr.json_text
				if _is_string_value(json_val):
					if json_val in move_only_values or _can_move_owned_once(json_val):
						_note_use(json_val, consume=True)
					else:
						json_val = _ensure_owned(json_val, owned_values, new_instrs)
						_note_use(json_val, consume=True)
				new_instrs.append(M.ExcSetParamsJson(error=instr.error, json_text=json_val))
				continue

			if isinstance(instr, M.ExcAppendContextFrame):
				# `drift_error_append_context_frame` takes ownership of
				# `frame_json` per ABI spec §2.3 — runtime splices it
				# into the merged context_json.  Same consume pattern
				# as ExcSetParamsJson.
				frame_val = instr.frame_json
				if _is_string_value(frame_val):
					if frame_val in move_only_values or _can_move_owned_once(frame_val):
						_note_use(frame_val, consume=True)
					else:
						frame_val = _ensure_owned(frame_val, owned_values, new_instrs)
						_note_use(frame_val, consume=True)
				new_instrs.append(M.ExcAppendContextFrame(error=instr.error, frame_json=frame_val))
				continue

			if isinstance(instr, M.ErrorRaise):
				new_instrs.append(instr)
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
								args.append(_ensure_owned(arg, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_CALL_ARG_RETAIN))
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
							args.append(_ensure_owned(arg, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_CALL_ARG_RETAIN))
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
							args.append(_ensure_owned(arg, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_CALL_ARG_RETAIN))
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
			# Phase 4 site-3 sub-step 2 — destructor-method `self`
			# skip is now ledger-authored.  The lattice transitions
			# `self` to MOVED_OUT at the end of every Return-
			# terminator block in a destructor method, so the
			# per-local ledger consultation below folds it into
			# `skip_cleanup_locals` without any site-local guard.
			# The legacy
			# `if is_destructor_method and "self" in func.params:
			#     skip_cleanup_locals.add("self")` line is retired.
			# Phase 4 sub-step 1 — returned-value source suppression
			# is now ledger-authored.  The legacy alias-walk and
			# `_collect_return_source_locals` composite walk that
			# used to populate `skip_cleanup_locals` here are
			# retired; the lattice's Return-as-move (incl.
			# composite constructors: ConstructStruct /
			# ConstructVariant / ConstructResultOk /
			# ConstructIfaceValue) transitions the source locals
			# to `MOVED_OUT`, and the per-local ledger consultation
			# a few lines below folds every `MUST_NOT_DROP`
			# verdict into `skip_cleanup_locals`.
			#
			# The alias walk is still needed for
			# `can_move_from_skipped_local` (string-ownership
			# transfer at the return value — a separate concern
			# from cleanup).
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
				# For STRING return-source locals, the legacy alias-walk
				# skip is preserved here.  The Phase 4 sub-step 1
				# ledger consultation below is limited to
				# `destructible_locals`; strings have their own
				# parallel ownership-tracking machinery
				# (`_release_all_locals`), and folding them into
				# `skip_cleanup_locals` via the generic consultation
				# breaks that machinery in subtle ways (caught by the
				# package-consumer memcheck regression
				# `test_pkg_map_literal_string_leak`).  The ARRAY half
				# of this skip was removed in the review-closure round
				# of string-arc-endgame-array-sweep (2026-07-19): with
				# the Return-boundary sweep gone, array membership in
				# `skip_cleanup_locals` had no downstream consumer
				# (arrays are excluded from `destructible_locals`;
				# `_release_all_locals` and the boundary audit
				# intersect strings) — scope-exit array drops are
				# cleanup_authoring's, and a returned array is
				# Return-as-move at the ledger there.  Once strings
				# move to ledger authority on a future track, this can
				# collapse into the consultation.
				for prev in reversed(new_instrs):
					if isinstance(prev, M.LoadLocal) and prev.dest == alias:
						can_move_from_skipped_local = True
						if prev.local in string_locals:
							skip_cleanup_locals.add(prev.local)
						break
			# Phase 4 sub-step 1 — ledger consultation for returned-
			# value source suppression on DESTRUCTIBLES.  Every
			# destructible local whose `verdict_at` at the return
			# cursor returns `MUST_NOT_DROP` joins
			# `skip_cleanup_locals`.  The lattice's Return-as-move
			# (including composite constructors) transitions the
			# source local(s) of the returned value to `MOVED_OUT`
			# at the LoadLocal index, so the verdict is MustNotDrop
			# and the return-source is correctly skipped without any
			# site-local alias walk.
			#
			# Scope note (2026-04-23, post-sub-step-3 memcheck
			# diagnosis; array half retired 2026-07-19): the
			# consultation is intentionally restricted to
			# `destructible_locals`.  STRINGS have their own
			# parallel ownership-tracking machinery
			# (`_release_all_locals` plus `moved_out_locals` /
			# `owned_values`) that pre-dates the ledger; folding
			# string MUST_NOT_DROP verdicts into
			# `skip_cleanup_locals` interferes with that machinery
			# in subtle ways (caught by the 0.27.145 memcheck
			# regression).  Strings remain on legacy authority on
			# this track; their swap to ledger authority is
			# separate work.  ARRAYS are no longer anyone's here:
			# scope-exit array drops are cleanup_authoring's
			# (string-arc-endgame-array-sweep), and returned arrays
			# are Return-as-move at its ledger.
			# Destructor `self` and variant zero-tag widening
			# remain site-local (sub-steps 2 and 3).
			if _ledger is not None:
				_ledger_point = (block.name, len(block.instructions))
				# **Authority boundary** (post-2026-04-25 site-3 String
				# migration ATTEMPT + revert; array half retired
				# 2026-07-19).  This consultation covers DESTRUCTIBLES
				# only.  STRINGS remain under `string_arc.py`'s
				# post-rewrite alias-walk authority (the String-only
				# LoadLocal walk above); scope-exit ARRAY drops are
				# cleanup_authoring's.
				#
				# **Why Strings are NOT here** (wording refreshed with the
				# R10 slice, 2026-07-20 — the historical "retain-wrap at
				# return" model this note used to describe is RETIRED:
				# copy stakes are materialized UPSTREAM by
				# `string_stakes`, `_ensure_owned` is an identity
				# pass-through, and `return_retain_site3` is
				# structurally extinct and fail-closed in the
				# reporter).  Strings keep their own scope-exit
				# machinery (`_release_all_locals` + the elision fold
				# below + the String-only alias-walk skip above)
				# because their release decisions still key off THIS
				# pass's post-rewrite bookkeeping (`moved_out_locals`,
				# `owned_values`, move approvals) rather than the
				# pre-rewrite ledger snapshot alone.  Folding String
				# MUST_NOT_DROP verdicts into the generic consultation
				# below remains the separate R3/R4 migration — with
				# the 0.27.145 re-proof made against the CURRENT
				# upstream-stake model (see
				# STRING-ARC-ENDGAME-RESUME-CHECKPOINT.md R4).
				#
				# Arc<T> and other refcounted types whose
				# clone/destroy are MIR-first (visible to the ledger
				# at build time) flow through this consultation
				# correctly via `destructible_locals`.
				#
				# Architectural rule (Share Slice 1 / 0.31.14 close-out;
				# see `doc/history.md` 2026-04-26): ledger authority
				# is valid only for ownership effects visible in the
				# MIR snapshot used to build the ledger.  Any late
				# pass that creates/releases refcount stakes remains
				# its own authority unless we rebuild/extend the
				# ledger after that pass or move those effects
				# earlier.  `string_arc` is the canonical late-rewrite
				# authority for refcounted-builtin return-source
				# cleanup; future shared-owner types whose `Share`
				# impl synthesises late refcount mutations must
				# follow the same containment pattern.
				for _local in destructible_locals:
					if _local in skip_cleanup_locals:
						continue
					_local_ty = local_types.get(_local)
					if _local_ty is None:
						continue
					_needs_drop_axis = bool(
						_compute_drop_policy(type_table, _local_ty).needs_drop
					)
					_verdict = _ledger.verdict_at(
						_ledger_point,
						_local,
						needs_drop=_needs_drop_axis,
					)
					if _verdict is _DropVerdict.MUST_NOT_DROP:
						skip_cleanup_locals.add(_local)
			if _audit is not None:
				# Return-boundary emissions anchor at the established
				# site-3 convention point.
				_audit_point[0] = (block.name, len(block.instructions))
			if val is not None and (_is_string_value(val) or _can_move_creator_return(val)):
				if val in move_only_values or _can_move_owned_once(val) or _can_move_creator_return(val) or can_move_from_skipped_local:
					_note_use(val, consume=True)
				else:
					val = _ensure_owned(val, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_RETURN_RETAIN_SITE3)
					_note_use(val, consume=True)
			# RELEASE ELISION (2026-07-11 slice; B-arch-1 prerequisite):
			# String locals whose return-boundary ledger verdict is
			# MUST_NOT_DROP are elided from the scope-exit release sweep.
			# This is the strings analog of the Phase 4 destructible
			# consultation above, unblocked by B-arch-1: with every
			# copy-stake ledger-visible (C2 = 0), the 0.27.145 failure
			# class — a WRONG MOVED_OUT verdict on a retain-wrapped
			# return source — is structurally gone, and every
			# MUST_NOT_DROP string slot at this boundary holds ZEROED
			# bytes at runtime (UNINIT: never written on the path;
			# MOVED_OUT: the MoveOut expansion zero-stores; TOMBSTONED:
			# `_emit_tombstone_value` for String IS `_emit_zero_value`,
			# proven 2026-07-11) — the elided release was a null-safe
			# no-op quad (Load+Zero+Store+Release).
			#
			# Guardrails (review-pinned):
			# - DropPolicy-backed needs_drop axis (String is
			#   needs_drop=True despite structural Copy — cheap-copy,
			#   NOT drop-free; verified against the Copy-shortcut
			#   hazard before landing).
			# - PATH_DEPENDENT keeps today's unconditional null-safe
			#   release (no string drop-flag machinery in this slice).
			# - No attached ledger → legacy behavior (loop guarded).
			# - Arrays, site 4/drop_before_overwrite, and C3
			#   flag-guarded cleanup MoveOuts untouched.
			if _ledger is not None:
				_string_needs_drop = bool(
					_compute_drop_policy(type_table, string_ty).needs_drop
				)
				_ledger_point_str = (block.name, len(block.instructions))
				for _sl in sorted(string_locals):
					if _sl in skip_cleanup_locals:
						continue
					_sv = _ledger.verdict_at(
						_ledger_point_str,
						_sl,
						needs_drop=_string_needs_drop,
					)
					if _sv is _DropVerdict.MUST_NOT_DROP:
						skip_cleanup_locals.add(_sl)
			# NO Return-boundary array sweep here (B-U,
			# string-arc-endgame-array-sweep, 2026-07-19): the sweep,
			# its MUST_NOT_DROP elision fold, and the Slice-3
			# measurement notes were DELETED after the bijection
			# measurement proved the residual population was exactly
			# two classes — 3,696 proven no-ops over storage already
			# zeroed by complete flag-guarded cleanup (the
			# `{blk}_cleanup_post_{local}` shapes), and 924 live
			# close-error-arm drops now AUTHORED by cleanup_authoring's
			# unguarded zero-storage branch at their CleanupHook
			# (verified corpus-exact: +924 events/moveout/zero-safe,
			# sweep counters 4,620 → 3,696 → 0).  cleanup_authoring is
			# the SOLE authority for scope-exit array drops; the
			# array OVERWRITE drop moved to `overwrite_cleanup` in
			# Slice B1 (`_drop_array_local` deleted, 2026-07-20);
			# the entry-block array zero-init above SURVIVES (the
			# zero-safety proof depends on it — endgame-inventory
			# item).
			_release_all_locals(new_instrs, skip_locals=skip_cleanup_locals)
			if _audit is not None:
				_audit.note_return_boundary(
					(block.name, len(block.instructions)),
					string_locals=string_locals,
					skipped=(skip_cleanup_locals & string_locals),
				)
			initialized_at_return = assigned_in.get(block.name, set()) | store_defs.get(block.name, set()) | store_defs.get(func.entry, set())
			# Phase 4 site-3 sub-step 3 — variant zero-tag widening,
			# now ledger-driven.  The legacy widening used site-3
			# dataflow (`assigned_out` / `store_defs` / `moved_out`)
			# to detect "assigned on some predecessor paths but not
			# all"; the lattice answers the same question via
			# `MAYBE_UNINIT → PathDependent`.  Site 3 keeps one
			# explicit policy axis: `zero_storage_drop_safe(ty,
			# table)` — types whose zeroed-storage drop is a no-op
			# (variants via tag-0 dispatch; arrays via the zeroed
			# header — though arrays are excluded from
			# `destructible_locals` and cannot reach this loop; the
			# widened call satisfies the no-variant-only-name
			# migration rule).  Live paths get their drop; uninit
			# paths drop the PHI-zero storage harmlessly.
			# Carrier (0.27.145 fix): pinned by
			# `lang/tests/codegen/e2e/scope_drop_conditional_move/`
			# + `lang/tests/memcheck/test_scope_drop_conditional_move.py`.
			if _ledger is not None:
				for _local in destructible_locals:
					if _local in initialized_at_return or _local in skip_cleanup_locals:
						continue
					_local_ty = local_types.get(_local)
					if _local_ty is None or not _zero_storage_drop_safe(_local_ty, type_table):
						continue
					_verdict = _ledger.verdict_at(
						_ledger_point,
						_local,
						needs_drop=True,
					)
					if _verdict is _DropVerdict.PATH_DEPENDENT:
						initialized_at_return.add(_local)
			# Phase 3B step 2 — `string_arc_return` swap (option 2:
			# site-3 skips locals managed by drop-flag plumbing).
			# Since the Arm B flag retirement (2026-07-20) this set
			# contains ONLY zero-storage-UNSAFE destructibles
			# (String-bearing structs etc.): cleanup_authoring is the
			# sole authority on their scope-exit drops (flag-guarded /
			# edge-elaborated), and a site-3 drop here would
			# double-drop on the path through the authored drop block.
			# Zero-storage-SAFE variants no longer appear here — their
			# cleanup is authored UNGUARDED at the hooks, whose MoveOut
			# transitions the rebuilt ledger to MOVED_OUT before the
			# Return, so the generic consultation ABOVE already added
			# them to `skip_cleanup_locals` (the ledger verdict, not
			# the flag, is what suppresses site 3 — pinned by
			# lang/tests/memcheck/test_variant_flag_retirement.py).
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
			mark_ledger_dirty(func, "string_arc.rewrite_return_terminator")
		elif block.terminator is not None:
			for val in _iter_term_used(block.terminator):
				if _is_string_value(val) and not _is_local_name(val):
					_note_use(val, consume=False)

		block.instructions = new_instrs
		mark_ledger_dirty(func, "string_arc.rewrite_block")

	if _audit is not None:
		# L_post: a fresh ledger over the pass OUTPUT.  Built directly
		# (never attached) so the driver's ledger-cache sequencing and
		# dirty-bit state are exactly as in the non-audit path.
		from .ownership_ledger import build_ledger as _build_ledger
		try:
			_l_post = _build_ledger(func, drop_policy=lambda tid: _compute_drop_policy(type_table, tid))
		except Exception:
			# Audit must never break a compile (observational contract),
			# but a missing L_post must never pass silently either:
			# finalize hard-counts it as post_ledger_build_failed and
			# force-emits the per-fn record; the corpus gate fails on
			# any nonzero count (review finding, B-arch-0 acceptance).
			_l_post = None
		_audit.finalize(
			l_pre=_ledger,
			l_post=_l_post,
			needs_drop=_ledger_needs_drop,
			# C3 agree-class ladder inputs (Slice 2 Part 2): the func for
			# STRUCTURAL flag-guard verification (terminators + pred
			# LoadLocals survive this pass's rewrites) and the same
			# zero-safety predicate cleanup_authoring used to author the
			# unguarded cleanup arm.
			func=func,
			zero_safe_ty=lambda _tid: _zero_storage_drop_safe(_tid, type_table),
		)

	return func


__all__ = ["insert_string_arc"]
