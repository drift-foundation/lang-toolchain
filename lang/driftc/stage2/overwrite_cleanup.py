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

B2+C S4 (2026-07-21): the null-safe + site-4 drop-before-overwrite
destructible cleanups now emit HERE, driven by the frozen `CleanupPlan`
`destructible_planner` builds at the pre-string_arc ledger-A slot (a
mandatory, non-`None` plan; an empty frozen plan for functions with no
destructible decisions).  Site-3 destructible Return cleanup remains
string_arc's authority until the unified Return authority (S5); this pass
only PRESERVES + postflight-validates the plan's site-3 anchors.
"""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable
from . import mir_nodes as M
from . import ownership_ledger_reporter as _ledger_reporter
from .cleanup_plan import CleanupPlan, PlanContractError
from .cleanup_payloads import NullsafePayload, Site4Payload
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
	plan: CleanupPlan,
) -> M.MirFunc:
	"""Author instruction-local overwrite releases/drops in `func`.

	Runs after `string_arc`.  Mutates `func` in place and returns it.

	B2+C S4 (2026-07-21): `plan` (a frozen destructible `CleanupPlan` from
	`destructible_planner`) is MANDATORY.  A SEPARATE EmitterPhase runs
	AFTER the R2/R7 rewrite + `_validate` and emits the null-safe + site-4
	drop-before-overwrite destructible cleanups string_arc formerly
	authored inline.  R2/R7 preserve every destructible StoreLocal object,
	so the plan's identity anchors survive into this phase.  A function
	with no destructible decisions is carried by an explicit EMPTY frozen
	plan — a `None` plan is a fail-closed internal error (a missing plan
	must NEVER silently mean "skip cleanup").
	"""
	# Fail closed: the plan is a required, frozen `CleanupPlan`.  The no-op
	# `plan=None` path is GONE — a missing plan can never mean skipped
	# destructible cleanup (a silent double-free/leak).
	if plan is None:
		raise PlanContractError(
			f"overwrite_cleanup (fn '{func.name}'): a frozen destructible "
			f"CleanupPlan is REQUIRED (pass an explicit empty plan for a "
			f"function with no destructible decisions); plan is None"
		)
	if not isinstance(plan, CleanupPlan):
		raise PlanContractError(
			f"overwrite_cleanup (fn '{func.name}'): plan must be a "
			f"CleanupPlan, got {type(plan).__name__}"
		)
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

	# ── Structural POST-rewrite BIJECTION validation (finding #2) ──
	# R2/R7 authoring + this bijection validation are UNCHANGED and run
	# FIRST — they preserve every destructible StoreLocal object, so the
	# plan phase below finds its identity anchors intact.
	_validate(func, type_table, inventory)

	# ── B2+C S4 (2026-07-21): consume the FROZEN destructible plan ──
	# The null-safe + site-4 drop-before-overwrite cleanups formerly
	# authored inline by string_arc emit HERE, driven by the plan
	# `destructible_planner` froze at the pre-string_arc ledger-A slot.
	# SEPARATE EmitterPhase: preflight stage → rewrite → postflight commit,
	# so a decision is consumed only once its anchor re-validates against
	# the mutated MIR.  The drop lands IMMEDIATELY BEFORE its store
	# (retain-before-release: Load old → Zero → Store(zero) → Drop old),
	# so the StoreLocal anchor survives and the postflight passes.
	site4_emitted_count = 0
	phase = plan.begin_phase(func)
	# id(original StoreLocal) -> (store_obj, local, ty, is_site4); only
	# EMITTING anchors are listed.  MUST_NOT_DROP site-4 anchors are staged
	# (so they are consumed) but emit nothing.
	emit_anchors: Dict[int, Tuple[M.MInstr, str, TypeId, bool]] = {}
	# MUST_NOT_DROP site-4 store ids — staged/consumed but MUST author no
	# drop sequence (proven below).
	must_not_drop_ids: Set[int] = set()
	# Emitter-local authoring side table: (store id, DropValue) per authored
	# drop, in emission order. Keeps plan/validation identity OUT of the MIR
	# nodes (no dynamic `plan_authored_for` attribute); the pre-commit
	# bijection reads this. A LIST (not a dict) so a duplicate authoring for
	# one store is detectable rather than silently overwritten.
	emitted_drops: List[Tuple[int, M.DropValue]] = []

	def _register_emit(dec, *, is_site4: bool, pl) -> None:
		# Fail closed on a duplicate identity key: the plain dict would
		# else silently overwrite one emitting decision with another
		# sharing the same anchor object.
		if id(dec.obj) in emit_anchors:
			raise PlanContractError(
				f"overwrite_cleanup (fn '{func.name}'): duplicate emitting "
				f"anchor identity for site {dec.site!r} at "
				f"{dec.coord.block}:{dec.coord.orig_index} — two emitting "
				f"decisions claim the same StoreLocal object"
			)
		emit_anchors[id(dec.obj)] = (dec.obj, pl.local, pl.ty, is_site4)

	for dec in plan.decisions_for_site("nullsafe"):
		pl = dec.payload
		# Reject a wrong payload/site combination fail-closed.
		if not isinstance(pl, NullsafePayload):
			raise PlanContractError(
				f"overwrite_cleanup (fn '{func.name}'): nullsafe decision at "
				f"{dec.coord.block}:{dec.coord.orig_index} carries a "
				f"{type(pl).__name__} payload (expected NullsafePayload)"
			)
		phase.stage(dec)                       # preflight validate
		_register_emit(dec, is_site4=False, pl=pl)
	for dec in plan.decisions_for_site("site4"):
		pl = dec.payload
		if not isinstance(pl, Site4Payload):
			raise PlanContractError(
				f"overwrite_cleanup (fn '{func.name}'): site4 decision at "
				f"{dec.coord.block}:{dec.coord.orig_index} carries a "
				f"{type(pl).__name__} payload (expected Site4Payload)"
			)
		phase.stage(dec)                       # preflight validate (even MUST_NOT_DROP)
		if pl.emit:                            # MUST_DROP only (the 14)
			_register_emit(dec, is_site4=True, pl=pl)
		else:                                  # MUST_NOT_DROP: emits nothing
			must_not_drop_ids.add(id(dec.obj))
	for block in func.blocks.values():
		new_instrs: List[M.MInstr] = []
		block_changed = False
		for instr in block.instructions:
			hit = emit_anchors.get(id(instr))
			if hit is not None:
				_store_obj, d_local, d_ty, is_site4 = hit
				tmp = _new_temp()
				new_instrs.append(M.LoadLocal(dest=tmp, local=d_local))
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=d_ty))
				local_types[zero] = d_ty
				zb = M.StoreLocal(local=d_local, value=zero)
				setattr(zb, "synthetic_zero_back", True)
				new_instrs.append(zb)
				drop = M.DropValue(value=tmp, ty=d_ty)
				# Record authoring identity in the emitter-local side table,
				# NOT on the MIR node.
				emitted_drops.append((id(instr), drop))
				new_instrs.append(drop)
				local_types[tmp] = d_ty
				if is_site4:
					site4_emitted_count += 1
				block_changed = True
			new_instrs.append(instr)
		if block_changed:
			block.instructions = new_instrs
			# Real dirty mark within the audit's proximity window of
			# the actual mutation (no allow marker).
			mark_ledger_dirty(func, "overwrite_cleanup.plan_overwrite")

	# ── Pre-commit BIJECTION: emitting decision ↔ canonical drop sequence ──
	# `phase.commit()` proves the STORE anchors survived, but not that each
	# emitting decision produced EXACTLY ONE canonical cleanup.  Prove that
	# separately (null-safe + site-4 MUST_DROP), and that no MUST_NOT_DROP
	# store authored anything, BEFORE consuming.
	_validate_plan_emission(func, emit_anchors, must_not_drop_ids, emitted_drops)

	phase.mark_rewritten()
	phase.commit()                             # postflight fresh-validate + consume
	plan.assert_sites_consumed({"nullsafe", "site4"})
	# Item-1 postflight: the site-3 Return anchors are NOT consumed here (the
	# S5 Return authority owns them).  Prove they SURVIVED the null-safe /
	# site-4 insertions above — a replaced/moved/dropped Return fails closed.
	plan.validate_unconsumed(func)

	if _ledger_reporter.string_arc_audit_enabled():
		if overwrite_release_count:
			_ledger_reporter.record_counted_only(
				_ledger_reporter.SITE_CLASS_OVERWRITE_RELEASE,
				overwrite_release_count,
			)
		# Preserve string_arc's former per-MUST_DROP site-4 note (14
		# corpus-wide): the count moved from string_arc's `_audit.note(...
		# SITE_CLASS_DROP_BEFORE_OVERWRITE_SITE4 ...)` to this counted-only
		# recorder, keeping the aggregate `events` + site_class total.
		# Null-safe has NO counter (unmeasured) — emit nothing for it.
		if site4_emitted_count:
			_ledger_reporter.record_counted_only(
				_ledger_reporter.SITE_CLASS_DROP_BEFORE_OVERWRITE_SITE4,
				site4_emitted_count,
			)
	# ── B2+C S8 debt (2): strip transient MIR attributes ──
	# `ow_authored_for` (host-process object ids) and `synthetic_zero_back`
	# (migration provenance) are validation-only metadata.  Every consumer
	# has now run — the R2 recognition skip and `_validate` above, the plan
	# emission validator, the planner's pre-string_arc tripwire, the
	# Return-emitter's own pre-commit checks, and the audit L_post (built
	# before this pass) — so neither attribute may survive into output MIR.
	_strip_transient_attrs(func)
	return func


def _strip_transient_attrs(func: M.MirFunc) -> None:
	"""Remove `ow_authored_for` / `synthetic_zero_back` from every
	instruction — no object ids or migration provenance in output MIR
	(SLICE-B §10 debt 2).  Attribute-only: instruction objects, order,
	and operands are untouched (no ledger impact)."""
	for block in func.blocks.values():
		for ins in block.instructions:
			for attr in ("ow_authored_for", "synthetic_zero_back"):
				if hasattr(ins, attr):
					delattr(ins, attr)


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
	# Occurrence count per instruction identity in the OUTPUT stream —
	# `pos` alone would silently collapse a duplicated object to its last
	# position, and a vanished object would surface as a raw KeyError.
	occurrences: Dict[int, int] = {}
	blocks_instrs: Dict[str, List[M.MInstr]] = {}
	for bn, block in func.blocks.items():
		blocks_instrs[bn] = block.instructions
		for i, ins in enumerate(block.instructions):
			pos[id(ins)] = (bn, i)
			occurrences[id(ins)] = occurrences.get(id(ins), 0) + 1
			tag = getattr(ins, "ow_authored_for", None)
			if tag is not None:
				authored.setdefault(tag, []).append((bn, i, ins))

	# Rewritten-site survival (B1 debt item 1): every inventoried eligible
	# store must occur EXACTLY ONCE in the output.  A store the authoring
	# loop dropped (vanished) or aliased into the stream twice (duplicated)
	# is a contained AssertionError here — never an uncaught KeyError from
	# `pos[id(store)]` nor a silent position collapse below.
	for sid, (kind, store) in inventory.items():
		n = occurrences.get(id(store), 0)
		if n != 1:
			raise AssertionError(
				f"overwrite_cleanup validation (fn '{func.name}'): inventoried "
				f"{kind} store for '{_subject(store)}' occurs {n} time(s) in "
				f"the rewritten output (expected exactly once — "
				f"{'vanished store' if n == 0 else 'duplicated store'})."
			)

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


def _validate_plan_emission(
	func: M.MirFunc,
	emit_anchors: Dict[int, Tuple[M.MInstr, str, "TypeId", bool]],
	must_not_drop_ids: Set[int],
	emitted_drops: List[Tuple[int, M.DropValue]],
) -> None:
	"""BIJECTION between the EMITTING plan decisions and the canonical
	destructible drop sequences this pass authored (item 3).  Each emitting
	decision (null-safe + site-4 MUST_DROP) must produce EXACTLY ONE
	canonical sequence — `LoadLocal(tmp, local) -> ZeroValue(zero, ty) ->
	StoreLocal(local, zero)[synthetic_zero_back] -> DropValue(tmp, ty)` —
	immediately before its store, with the full operand/type linkage.  A
	MUST_NOT_DROP site-4 store must author NOTHING.  Authoring identity comes
	from the emitter-local `emitted_drops` side table (store id -> DropValue),
	NOT any MIR attribute.  Missing / duplicate / orphan emission and any
	broken link fail closed via `PlanContractError`."""
	# Index each instruction's (block, position) by object identity.
	pos: Dict[int, Tuple[str, int]] = {}
	blocks_instrs: Dict[str, List[M.MInstr]] = {}
	for bn, block in func.blocks.items():
		blocks_instrs[bn] = block.instructions
		for i, ins in enumerate(block.instructions):
			pos[id(ins)] = (bn, i)

	# 1) Side-table bijection: authored store ids must equal emitting store
	#    ids exactly, one drop each (orphan = authored non-emitter; missing =
	#    emitter with no authored drop; duplicate = a store authored twice).
	authored: Dict[int, List[M.DropValue]] = {}
	for sid_a, drop_a in emitted_drops:
		authored.setdefault(sid_a, []).append(drop_a)
	authored_ids = set(authored)
	emit_ids = set(emit_anchors)
	orphans = authored_ids - emit_ids
	if orphans:
		raise PlanContractError(
			f"overwrite_cleanup plan-emission (fn '{func.name}'): "
			f"{len(orphans)} authored destructible drop(s) target no emitting "
			f"decision (orphan authoring)."
		)
	missing = emit_ids - authored_ids
	if missing:
		raise PlanContractError(
			f"overwrite_cleanup plan-emission (fn '{func.name}'): "
			f"{len(missing)} emitting decision(s) received no authored drop "
			f"(suppressed authoring)."
		)
	dup = [sid for sid, drops in authored.items() if len(drops) != 1]
	if dup:
		raise PlanContractError(
			f"overwrite_cleanup plan-emission (fn '{func.name}'): "
			f"{len(dup)} emitting decision(s) authored more than one drop "
			f"(duplicate authoring)."
		)
	# 2) A MUST_NOT_DROP site-4 store must have authored nothing.
	bad_mnd = must_not_drop_ids & authored_ids
	if bad_mnd:
		raise PlanContractError(
			f"overwrite_cleanup plan-emission (fn '{func.name}'): "
			f"{len(bad_mnd)} MUST_NOT_DROP site-4 store(s) authored a drop "
			f"sequence (MUST_NOT_DROP must emit nothing)."
		)
	# 3) Structural + full operand/type linkage for each emitting decision.
	for sid, (store, local, ty, _is_site4) in emit_anchors.items():
		# item 4: a removed/absent store anchor fails as a plan-contract
		# error, NOT a raw KeyError.
		if id(store) not in pos:
			raise PlanContractError(
				f"overwrite_cleanup plan-emission (fn '{func.name}'): the "
				f"emitting store for '{local}' is absent from the function "
				f"after authoring (removed/replaced anchor)."
			)
		bn, si = pos[id(store)]
		instrs = blocks_instrs[bn]
		if si < 4:
			raise PlanContractError(
				f"overwrite_cleanup plan-emission (fn '{func.name}', block "
				f"'{bn}'[{si}]): destructible store for '{local}' has no room "
				f"for a canonical drop sequence immediately before it."
			)
		load, zv, zb, drop = instrs[si - 4], instrs[si - 3], instrs[si - 2], instrs[si - 1]
		ok = (
			isinstance(load, M.LoadLocal) and load.local == local
			and isinstance(zv, M.ZeroValue) and zv.ty == ty
			and isinstance(zb, M.StoreLocal)
			and zb.local == local and zb.value == zv.dest
			and getattr(zb, "synthetic_zero_back", False) is True
			and isinstance(drop, M.DropValue)
			and drop.value == load.dest and drop.ty == ty
			# identity linkage via the side table, not a MIR attribute:
			and drop is authored[sid][0]
		)
		if not ok:
			raise PlanContractError(
				f"overwrite_cleanup plan-emission (fn '{func.name}', block "
				f"'{bn}'[{si}]): destructible store for '{local}' lacks a "
				f"correctly-linked canonical drop sequence immediately before "
				f"it (operand/type mismatch)."
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
