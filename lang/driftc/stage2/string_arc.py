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
from .destructible_authority import (
	DropClassifier,
	classify_destructible_locals,
	compute_assigned_in,
	compute_return_move_state,
	compute_store_defs,
	flag_managed_at_return,
)
from .string_ownership_analysis import classify_string_array_locals
from .string_ownership_analysis import (
	DRIFT_STRING_HELPER_SYMBOLS,
	compute_recognized_releases,
	iter_used_values,
	seed_string_dest_types,
)



def insert_string_arc(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
	fn_infos: Mapping[FunctionId, FnInfo],
	audit_collector=None,
	r8_recognition=None,
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
	# B-arch-0 differential stake audit (Scope B §11.2).  DEFERRED-FINALIZE
	# contract (B2+C S5): string_arc NO LONGER creates, owns, or finalizes
	# the audit — it is only a NOTE PRODUCER.  `_audit` is the DRIVER-SUPPLIED
	# collector (a `StringArcAudit` when the driver has audit enabled, else
	# `None`); the driver is the sole lifecycle authority and runs the SINGLE
	# `finalize` AFTER the unified Return emitter has appended the string
	# releases + site-3 drops (so the deferred `l_post` sees them).  Every
	# recording site below is guarded on `_audit is not None`, so the
	# disabled path is behavior-identical (zero instructions, zero
	# diagnostics, zero allocations beyond this None).  string_arc NEVER
	# records the scope-exit releases (their emission moved to the unified
	# authority) nor the Return boundaries (`note_return_boundary` is gone);
	# the C1 ledger-A half is frozen at the plan slot and merged in the
	# driver's single finalize.  `_audit_point` is the pre-MIR program point
	# of the instruction currently being rewritten (the L_pre anchor for
	# MoveOut-expansion notes).
	_audit = audit_collector
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
	# DESTRUCTIBLE decision authority (Milestone A, 2026-07-20): the
	# type-level classification predicates moved VERBATIM to
	# `destructible_authority.DropClassifier`; the closure NAMES below are
	# rebound to its methods so every existing call site is unchanged.
	_clf = DropClassifier(type_table)
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

	# The type-level classification closures (`_type_needs_drop`,
	# `_is_destructible_tid`, `_is_error_tid`, `_is_nullsafe_drop`) moved
	# VERBATIM to `DropClassifier`; their only remaining call sites were the
	# `destructible_locals` / `nullsafe_destructible_locals` split, which now
	# consults `_clf` directly via `classify_destructible_locals`.
	destructible_locals, nullsafe_destructible_locals = classify_destructible_locals(
		func,
		_clf,
		local_types=local_types,
		string_locals=string_locals,
		array_locals=array_locals,
	)

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

	# B2+C S5 — the Return-boundary String scope-exit RELEASE emission
	# (`_release_local` / `_release_all_locals`) and the destructible DROP
	# emission (`_drop_destructible_local`) were DELETED here.  BOTH are now
	# authored by the unified Return authority (`return_cleanup_emitter`,
	# consuming the frozen `CleanupPlan`'s `string_release` + `site3`
	# decisions), which the driver runs AFTER this pass.  The scope-exit
	# release DECISION is `destructible_authority.string_return_releases`
	# (frozen into the plan); the destructible drop DECISION is
	# `site3_return_drops` (likewise).  string_arc keeps only the
	# Return-branch bookkeeping the (debug-gated) observe reporter reads and
	# the return-value string-liveness note; it PRESERVES the Return object
	# in place.  (`_drop_all_arrays` / `_drop_array_local` were deleted in
	# B-U / Slice B1; array + R2/R7 overwrite authority is
	# `overwrite_cleanup`'s.)

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

	# The CFG predecessor/successor helpers and the moved-out intersection
	# fixpoint that used to live here moved to `destructible_authority`
	# (`compute_return_move_state`, closed-authority follow-up).

	# Fill in missing destination types first so string-use liveness sees all
	# intermediate string temps (including conversion/call-produced values).
	_seed_dest_types()

	# B2+C S6 — string_arc no longer OWNS the R8 materialized-release
	# recognition (the `build_fnwide_producers` + `compute_string_temp_liveness`
	# + per-block `recognize_materialized_releases` invocation).  That
	# recognition is computed ONCE at the pre-string_arc planning window
	# over the ORIGINAL MIR and CONSUMED here as a frozen `R8Recognition`.
	# `_recognized` maps block name -> the recognized-released temp set the
	# rewrite loop's R5/MoveOut/copy-through arm reads.  When the driver
	# supplies the frozen recognition, consume it; when absent (bare
	# unit-test invocation), fall back to the SINGLE shared entry point
	# `compute_recognized_releases` (which is the ONLY place the three
	# underlying analyses are invoked — string_arc's own body names none of
	# them, pinned by `test_string_arc_no_longer_owns_r8_recognition`).  The
	# recognition is byte-identical to the former mid-rewrite computation:
	# nothing mutates the MIR between the plan window and here, and it reads
	# only pre-string_arc operand types.
	_recognized = (
		r8_recognition
		if r8_recognition is not None
		else compute_recognized_releases(func, type_table=type_table, fn_infos=fn_infos)
	)
	if _recognized.fn_name != func.name:
		raise AssertionError(
			f"string_arc: R8 recognition belongs to fn {_recognized.fn_name!r}, "
			f"not {func.name!r} (wrong-function recognition — fail closed)"
		)
	# A recognition vessel is CLOSED once supplied: its block-key set must
	# equal this function's block set (a missing block must never read as
	# "nothing recognized") and every value must be a frozenset.  Validated
	# BEFORE any rewrite; the fallback result passes by construction.
	_rec_blocks = set(_recognized.recognized_by_block.keys())
	_fn_blocks = set(func.blocks.keys())
	if _rec_blocks != _fn_blocks:
		raise AssertionError(
			f"string_arc: R8 recognition block set != function block set for "
			f"{func.name!r} (missing={sorted(_fn_blocks - _rec_blocks)}, "
			f"extra={sorted(_rec_blocks - _fn_blocks)}) — fail closed"
		)
	for _rb_name, _rb_vals in _recognized.recognized_by_block.items():
		if not isinstance(_rb_vals, frozenset):
			raise AssertionError(
				f"string_arc: R8 recognition for block {_rb_name!r} is "
				f"{type(_rb_vals).__name__}, not frozenset (malformed vessel)"
			)

	# Definite local assignment across CFG.  The `store_defs` /
	# `assigned_in` definite-assignment dataflow moved VERBATIM to
	# `destructible_authority` (Milestone A).
	store_defs = compute_store_defs(func)
	assigned_in = compute_assigned_in(func, store_defs)

	# Per-block moved-out / explicitly-dropped bookkeeping, computed ONCE
	# (closed-authority follow-up).  `move_state[b].moved_out` is the
	# block-END value of the moved-out intersection fixpoint (verbatim from
	# the loop that used to sit here); `.explicitly_dropped` is the
	# intra-block explicit-drop replay.  BOTH the inline Return skip below
	# and `site3_return_drops` consume this shared FROZEN result — neither
	# string_arc nor the standalone planner recomputes these sets.
	move_state = compute_return_move_state(
		func,
		destructible_locals=destructible_locals,
		string_ty=string_ty,
	)

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
		# The per-block `moved_out_locals` / `explicitly_dropped_locals` /
		# `load_local_src` bookkeeping that used to be rebuilt inline here
		# is now precomputed ONCE in `move_state` (compute_return_move_state);
		# this loop no longer maintains it.

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
		# count and move the last-use point).  B2+C S6: the recognition is
		# CONSUMED from the frozen `R8Recognition` (`for_block` returns the
		# empty set for blocks the plan-window fast path skipped — those
		# without any input release — matching the former inline gate).
		recognized_released: "Set[str]" = _recognized.for_block(block.name)
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
			# (moved-out / explicitly-dropped bookkeeping discards on
			# StoreLocal / MoveFromRef now live in
			# `compute_return_move_state`; not maintained inline here.)
			if isinstance(instr, M.StoreLocal) and instr.local in array_locals:
				# R7 array overwrite drop MOVED to overwrite_cleanup
				# (Slice B1, 2026-07-20). string_arc keeps no per-array
				# bookkeeping here — just pass the store through.
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.StoreLocal) and instr.local in nullsafe_destructible_locals:
				# B2+C S4 (2026-07-21): drop-before-overwrite for
				# destructibles (null-safe + site-4) MIGRATED to
				# `overwrite_cleanup`, driven by the frozen CleanupPlan
				# `destructible_planner` builds at the pre-string_arc
				# ledger-A slot.  The site-4 ledger verdict + tripwires
				# (missing-ledger + PathDependent) now fire at PLANNING
				# time in the planner, not here.  The store passes through
				# unchanged and is NOT reprocessed.
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.StoreLocal) and instr.local in destructible_locals:
				# B2+C S4 (2026-07-21): neutered — see the null-safe arm
				# above.  Site-4 drop-before-overwrite emission and its
				# audit note moved to `overwrite_cleanup`; the ledger
				# verdict + tripwires moved to `destructible_planner`.
				# The store passes through unchanged and is NOT reprocessed.
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
				# (moved-out bookkeeping for this MoveOut now lives in
				# `compute_return_move_state`; the ZeroValue+StoreLocal
				# emission above is unchanged.)
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
				# (load_local_src tracking for explicit-drop recognition
				# now lives in `compute_return_move_state`.)
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
			# (Non-string DropValue explicit-drop bookkeeping now lives in
			# `compute_return_move_state`; the instruction falls through to
			# the passthrough below unchanged.)

			new_instrs.append(instr)
			for val in _iter_used_values(instr):
				if _is_string_value(val) and not _is_local_name(val):
					_note_use(val, consume=False)

		if isinstance(block.terminator, M.Return):
			term = block.terminator
			val = term.value
			# TRANSITIONAL (delete with the unified Return authority, S5/D):
			# string_arc keeps building `skip_cleanup_locals` /
			# `initialized_at_return` inline ONLY for the String release
			# sweep (`_release_all_locals`), the boundary audit, and the
			# ownership_ledger observe reporter.  The DESTRUCTIBLE drop
			# decision is authored by `site3_return_drops`; both consume the
			# SAME `move_state` bookkeeping so they cannot diverge.
			_return_move_state = move_state[block.name]
			skip_cleanup_locals: Set[str] = set()
			skip_cleanup_locals |= _return_move_state.moved_out
			skip_cleanup_locals |= _return_move_state.explicitly_dropped
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
				# B2+C S5: the R4 return-source STRING skip contribution
				# (`skip_cleanup_locals.add(prev.local)`) was DELETED — the
				# R4 alias decision now lives in the frozen plan
				# (`destructible_authority.string_return_source_skip`, folded
				# into `string_return_releases`), and string_arc no longer
				# emits the scope-exit releases that skip fed.  The alias walk
				# STAYS only to compute `can_move_from_skipped_local` (the
				# return-value string-ownership transfer — a separate concern
				# from cleanup, and the never-released-twice R4 proof it backs
				# now holds at the unified Return authority).
				for prev in reversed(new_instrs):
					if isinstance(prev, M.LoadLocal) and prev.dest == alias:
						can_move_from_skipped_local = True
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
			# B2+C S5: the String RELEASE ELISION fold (MUST_NOT_DROP strings
			# dropped from the scope-exit release sweep) was DELETED here.  The
			# R3 elision decision now lives in the frozen plan
			# (`destructible_authority.string_return_releases`, which applies
			# the SAME DropPolicy-backed MUST_NOT_DROP elision over the ledger-A
			# snapshot at the original return coordinate), and the unified
			# Return authority emits exactly the surviving releases.  Its
			# guardrails (String needs_drop=True; PATH_DEPENDENT keeps the
			# null-safe release; no-ledger → legacy) moved with the decision.
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
			# B2+C S5: the scope-exit release EMISSION (`_release_all_locals`)
			# and the Return-boundary audit note (`note_return_boundary`) were
			# DELETED here.  Emission is the unified Return authority's; the C1
			# Return-boundary record + its released/skipped sets are frozen at
			# the plan slot into the driver-local `C1Contribution` and merged
			# in the driver's single deferred finalize.
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
			# appear).  Detection delegates to the authority's
			# `flag_managed_at_return` (canonical `drop_flags.is_flag_managed`),
			# so string_arc and the standalone planner share one
			# flag-managed set rather than each computing its own.
			_flag_managed_at_return: Set[str] = flag_managed_at_return(
				func, destructible_locals
			)
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
			# B2+C S5: the destructible site-3 drop EMISSION
			# (`site3_return_drops` + `_drop_destructible_local`) was DELETED
			# here.  The destructible drop DECISION is frozen into the plan by
			# the planner (via the SAME `site3_return_drops` authority) and the
			# unified Return authority emits the drop tail AFTER the string
			# release band, before the preserved Return.
			# PRESERVE the original Return object (item-1 site-3 anchor
			# survival): the frozen `CleanupPlan` holds this exact M.Return
			# as its site-3 + string_release TERM anchor, so string_arc must
			# NOT swap in a new object here.  Update `term.value` IN PLACE only
			# if it changed; the span already lives on `term`, so no new object
			# is created.
			if term.value is not val:
				term.value = val
			block.terminator = term
			mark_ledger_dirty(func, "string_arc.rewrite_return_terminator")
		elif block.terminator is not None:
			for val in _iter_term_used(block.terminator):
				if _is_string_value(val) and not _is_local_name(val):
					_note_use(val, consume=False)

		block.instructions = new_instrs
		mark_ledger_dirty(func, "string_arc.rewrite_block")

	# B2+C S5 DEFERRED FINALIZE: string_arc NEVER builds L_post nor calls
	# `finalize`.  The driver is the sole lifecycle authority — it builds the
	# single audit-only L_post over the FINAL MIR (after the unified Return
	# emitter has appended the string releases + site-3 drops) and runs the
	# ONE `StringArcAudit.finalize`, merging the frozen `C1Contribution`.
	# string_arc only PRODUCED notes into the driver-supplied `_audit`
	# collector (MoveOut expansions etc.); it authored NO scope-exit release
	# events and NO Return boundaries.  (Source pin:
	# `test_string_arc_no_self_finalize` asserts this module contains no
	# `_audit.finalize` call.)
	return func


__all__ = ["insert_string_arc"]
