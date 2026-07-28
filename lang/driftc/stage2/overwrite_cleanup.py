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

PLACEMENT: runs LAST in the ownership pipeline (dedicated driver
bucket, after ownership normalization, the unified Return authority,
and the audit L_post build).  This pass adds ONLY the old-value
release/drop, immediately BEFORE each eligible store, preserving
old-value-before-new-store order and the store's span.  Running after
normalization keeps the plan-window recognition from ever seeing these
releases, and needs NO ledger (R2/R7 are pure structural type checks).

PROVENANCE (review 2026-07-20): a `StoreLocal(String|Array, zeroval)`
is NOT categorically a non-overwrite — an INPUT-stream
`ZeroValue(String) -> StoreLocal` into a live slot IS a real overwrite.
We must skip ONLY the PIPELINE-synthesized zero-back stores (ownership
normalization's R1 entry init + R5 MoveOut expansion, the unified
Return authority's bands, and this pass's own plan phase) — each is
marked `synthetic_zero_back=True` at authoring; this pass skips exactly
the marked stores, never inferring provenance from value shape, temp
name, or adjacency.  (The marks are stripped again at the END of this
pass, after every consumer has run.)

RETAIN-BEFORE-RELEASE / self-alias (`x = x`): the store-VALUE copy
stake (retain) is materialized upstream by
`string_stakes.materialize_call_arg_stakes` (pre-normalization), so a
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
destructible cleanups emit HERE, driven by the frozen `CleanupPlan`
`destructible_planner` builds at the pre-normalization ledger-A slot (a
mandatory, non-`None` plan; an empty frozen plan for functions with no
destructible decisions).  Site-3 destructible Return cleanup is the
unified Return authority's (`return_cleanup_emitter`, S5); this pass
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
from .drop_flag_guard import build_guarded_drop_blocks
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
	"""True iff the ownership pipeline marked this as one of its OWN
	synthetic zero-back stores (explicit provenance — never a shape
	guess)."""
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
	pipeline-synthesized zero-back is explicitly NOT eligible."""
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

	Runs after ownership normalization.  Mutates `func` in place and returns it.

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

	# Fresh-temp generator that cannot collide with normalization's
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
	# authored inline by the legacy string_arc emit HERE, driven by the plan
	# `destructible_planner` froze at the pre-normalization ledger-A slot.
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
	# id(StoreLocal) -> (store_obj, local, ty, flag_local) for
	# FLAG_GUARDED (zero-storage-unsafe PathDependent) site-4 anchors —
	# authored by a block split (drop iff the runtime drop flag is set),
	# NOT the inline canonical sequence.
	guarded_site4: Dict[int, Tuple[M.MInstr, str, TypeId, str]] = {}
	# Emitter-local authoring side table: (store id, DropValue) per authored
	# drop, in emission order. Keeps plan/validation identity OUT of the MIR
	# nodes (no dynamic `plan_authored_for` attribute); the pre-commit
	# bijection reads this. A LIST (not a dict) so a duplicate authoring for
	# one store is detectable rather than silently overwritten.
	emitted_drops: List[Tuple[int, M.DropValue]] = []
	# FLAG_GUARDED authoring side table: (guarded store id, authored
	# DropValue) — one per split, read by the guarded-emission validator.
	guarded_authored: List[Tuple[int, M.DropValue]] = []

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
		phase.stage(dec)                       # preflight validate (every disposition)
		if pl.guarded:                         # FLAG_GUARDED: block-split below
			# Consumption-side flag validation (item 3).  `phase.stage(dec)`
			# already ran the plan's `_check_type_bindings` (build+consume),
			# which enforces that EVERY frozen binding's local still carries
			# its frozen type in `func.local_types` — including the drop
			# flag's Bool binding the planner froze.  Here we additionally
			# pin that the binding was actually FROZEN (a future planner that
			# forgot to bind the flag fails closed BEFORE any block split,
			# never a runtime miscompile).
			bool_ty = type_table.ensure_bool()
			_bindings = dict(dec.type_bindings)
			if _bindings.get(pl.flag_local) != bool_ty:
				raise PlanContractError(
					f"overwrite_cleanup (fn '{func.name}'): FLAG_GUARDED site4 "
					f"decision for '{pl.local}' at {dec.coord.block}:"
					f"{dec.coord.orig_index} has no Bool type binding for its "
					f"drop flag '{pl.flag_local}' (bound "
					f"{_bindings.get(pl.flag_local)!r})."
				)
			guarded_site4[id(dec.obj)] = (dec.obj, pl.local, pl.ty, pl.flag_local)
		elif pl.emit:                          # UNCONDITIONAL: inline canonical drop
			_register_emit(dec, is_site4=True, pl=pl)
		else:                                  # NO_DROP: emits nothing
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

	# ── FLAG_GUARDED site-4: block-split guarded drops ──
	# Zero-storage-UNSAFE PathDependent overwrites: the drop-before-overwrite
	# must run IFF the local's runtime drop flag says the slot is live (the
	# earlier branch that MAY have moved it out cleared the flag; dropping the
	# moved-out zeroed storage would be a use-after-free / phantom drop).  The
	# split shape is authored by the SHARED `drop_flag_guard` primitive so it
	# cannot drift from the site-1 (cleanup_authoring) guarded authority.
	if guarded_site4:
		_emit_guarded_site4(
			func, guarded_site4, local_types, _new_temp, guarded_authored
		)
		# Dirty marking happens per split inside `_emit_guarded_site4`,
		# immediately after each origin replacement + drop/post registration —
		# same discipline as cleanup_authoring's split.
		_validate_guarded_site4_emission(func, guarded_site4, guarded_authored)
		# The same-block postflight contract is resolved by the EmitterPhase:
		# `phase.commit()` (below) derives this phase's relocations from its
		# PRIVATE staged set, requires each relocated anchor to be an
		# INSTRUCTION anchor moved into a block THIS PHASE created, proves the
		# original decision order across the split control-flow chain, and only
		# THEN publishes the relocation map — transactionally.  The emitter
		# performs the split; authorization + validation live on the phase.
		site4_emitted_count += len(guarded_site4)

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
		# corpus-wide): the count moved from the legacy `_audit.note(...
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
	# emission validator, the planner's pre-normalization tripwire, the
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
	"""Reproduce the legacy `_release_local`: load the old value,
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


def _emit_guarded_site4(
	func: M.MirFunc,
	guarded_site4: Dict[int, Tuple[M.MInstr, str, "TypeId", str]],
	local_types: Dict[str, "TypeId"],
	new_temp,
	authored_out: List[Tuple[int, M.DropValue]],
) -> "Set[str]":
	"""Author the FLAG_GUARDED (zero-storage-UNSAFE PathDependent) site-4
	drop-before-overwrite cleanups by splitting each store's block.

	For each guarded store the drop runs IFF the local's runtime drop
	flag is set:

	    origin:  <pre-store instrs> ; LoadLocal(flag) ; If(flag, drop, post)
	    drop:    LoadLocal(old) ; ZeroValue ; StoreLocal(zero)[sz] ;
	             DropValue(old) ; StoreLocal(flag, false) ; Goto(post)
	    post:    <store + drop_flags' flag-set + rest> ; original terminator

	The split shape (LoadLocal(flag)/IfTerminator/flag-clear/Goto) is the
	SHARED `drop_flag_guard.build_guarded_drop_blocks` primitive — the same
	one the site-1 scope-drop authority uses — so the guarded emission
	cannot diverge between the two sites.  The site-4-specific
	`drop_sequence` (retain-before-release Load→Zero→Store(zero)→Drop) is
	supplied here; the primitive appends the flag-clear + Goto.

	Worklist form: each split removes one guarded store and may relocate
	other still-pending guarded stores into the fresh `post_blk` (found
	again on the next pass), so multiple guarded stores in one block are
	handled without index bookkeeping."""
	remaining = dict(guarded_site4)
	created_blocks: Set[str] = set()
	# Bounded: one split per guarded store; +len slack covers post_blk
	# re-scans.  A non-converging worklist is a fail-closed contract error.
	guard_budget = 2 * len(guarded_site4) + 8
	while remaining:
		guard_budget -= 1
		if guard_budget < 0:
			raise PlanContractError(
				f"overwrite_cleanup guarded site-4 (fn '{func.name}'): "
				f"block-split worklist failed to converge "
				f"({len(remaining)} store(s) still pending)."
			)
		located = None
		for bn, block in func.blocks.items():
			for i, ins in enumerate(block.instructions):
				if id(ins) in remaining:
					located = (bn, i, id(ins))
					break
			if located is not None:
				break
		if located is None:
			raise PlanContractError(
				f"overwrite_cleanup guarded site-4 (fn '{func.name}'): "
				f"{len(remaining)} FLAG_GUARDED store(s) vanished before their "
				f"guarded drop could be authored (removed/replaced anchor)."
			)
		bn, idx, sid = located
		_store_obj, local, ty, flag_local = remaining.pop(sid)
		block = func.blocks[bn]
		pre_instrs = list(block.instructions[:idx])
		# tail = the overwriting store + drop_flags' following StoreLocal(flag,
		# true) + the rest of the block; the store keeps its object identity so
		# the EmitterPhase commit re-locates its anchor by id().
		tail_instrs = list(block.instructions[idx:])
		original_term = block.terminator

		authored_holder: List[M.DropValue] = []

		def _site4_drop_seq(buf, _local=local, _ty=ty, _holder=authored_holder):
			tmp = new_temp()
			buf.append(M.LoadLocal(dest=tmp, local=_local))
			zero = new_temp()
			buf.append(M.ZeroValue(dest=zero, ty=_ty))
			local_types[zero] = _ty
			zb = M.StoreLocal(local=_local, value=zero)
			setattr(zb, "synthetic_zero_back", True)
			buf.append(zb)
			drop = M.DropValue(value=tmp, ty=_ty)
			buf.append(drop)
			local_types[tmp] = _ty
			_holder.append(drop)

		origin_blk, drop_blk, post_blk = build_guarded_drop_blocks(
			func,
			origin_block_name=bn,
			pre_instrs=pre_instrs,
			tail_instrs=tail_instrs,
			original_term=original_term,
			flag_local=flag_local,
			drop_sequence=_site4_drop_seq,
			new_temp=new_temp,
			pending=[],
			label=local,
		)
		# Replace the origin block in place (preserves dict order/key) and
		# register the two fresh blocks.
		func.blocks[bn] = origin_blk
		func.blocks[drop_blk.name] = drop_blk  # ledger-cache-safety-audit: allow new-block
		func.blocks[post_blk.name] = post_blk  # ledger-cache-safety-audit: allow new-block
		mark_ledger_dirty(func, "overwrite_cleanup.guarded_site4_split")
		# `post_blk` is where every relocated anchor (the guarded store, any
		# later store, a block-terminating Return) now lives; `drop_blk` holds
		# only freshly-authored nodes.  `bn` reuses the ORIGINAL name (its
		# pre-split anchors keep the same expected block), so it is NOT a
		# relocation target and is not declared here.
		created_blocks.add(drop_blk.name)
		created_blocks.add(post_blk.name)
		authored_out.append((sid, authored_holder[0]))
	return created_blocks


def _validate_guarded_site4_emission(
	func: M.MirFunc,
	guarded_site4: Dict[int, Tuple[M.MInstr, str, "TypeId", str]],
	guarded_authored: List[Tuple[int, M.DropValue]],
) -> None:
	"""Fail-closed structural proof that every FLAG_GUARDED site-4 store
	produced EXACTLY ONE canonical guarded-drop split.

	For each guarded store:
	  * the store survives exactly once (in a `post_blk`);
	  * its authored `DropValue` sits in a `drop_blk` holding the canonical
	    guarded sequence `Load→Zero→Store(zero)[sz]→Drop→ConstBool(false)→
	    StoreLocal(flag,false)` and terminating in `Goto(post_blk)`;
	  * a UNIQUE origin block loads the flag and branches
	    `IfTerminator(flag, drop_blk, post_blk)`.
	Orphan / missing / duplicate authoring and any broken link raise
	`PlanContractError`."""
	authored: Dict[int, List[M.DropValue]] = {}
	for sid_a, drop_a in guarded_authored:
		authored.setdefault(sid_a, []).append(drop_a)
	g_ids = set(guarded_site4)
	a_ids = set(authored)
	orphans = a_ids - g_ids
	if orphans:
		raise PlanContractError(
			f"overwrite_cleanup guarded site-4 (fn '{func.name}'): "
			f"{len(orphans)} authored guarded drop(s) target no FLAG_GUARDED "
			f"decision (orphan authoring)."
		)
	missing = g_ids - a_ids
	if missing:
		raise PlanContractError(
			f"overwrite_cleanup guarded site-4 (fn '{func.name}'): "
			f"{len(missing)} FLAG_GUARDED decision(s) received no authored "
			f"guarded drop."
		)
	dup = [sid for sid, drops in authored.items() if len(drops) != 1]
	if dup:
		raise PlanContractError(
			f"overwrite_cleanup guarded site-4 (fn '{func.name}'): "
			f"{len(dup)} FLAG_GUARDED decision(s) authored more than one drop."
		)

	pos: Dict[int, Tuple[str, int]] = {}
	occ: Dict[int, int] = {}
	for bn, block in func.blocks.items():
		for i, ins in enumerate(block.instructions):
			pos[id(ins)] = (bn, i)
			occ[id(ins)] = occ.get(id(ins), 0) + 1

	for sid in g_ids:
		store_obj, local, ty, flag_local = guarded_site4[sid]
		drop = authored[sid][0]
		# 1) store survives exactly once.
		n = occ.get(id(store_obj), 0)
		if n != 1:
			raise PlanContractError(
				f"overwrite_cleanup guarded site-4 (fn '{func.name}'): "
				f"guarded store for '{local}' occurs {n} time(s) after the "
				f"split (expected exactly once)."
			)
		# 2) drop_blk canonical shape around the authored DropValue.
		if id(drop) not in pos:
			raise PlanContractError(
				f"overwrite_cleanup guarded site-4 (fn '{func.name}'): the "
				f"authored guarded drop for '{local}' is absent from the "
				f"function after the split."
			)
		dbn, didx = pos[id(drop)]
		drop_blk = func.blocks[dbn]
		di = drop_blk.instructions
		if didx < 3 or didx + 2 >= len(di):
			raise PlanContractError(
				f"overwrite_cleanup guarded site-4 (fn '{func.name}', block "
				f"'{dbn}'[{didx}]): guarded drop for '{local}' lacks room for "
				f"the canonical guarded sequence."
			)
		load, zv, zb = di[didx - 3], di[didx - 2], di[didx - 1]
		cb, fclear = di[didx + 1], di[didx + 2]
		shape_ok = (
			isinstance(load, M.LoadLocal) and load.local == local
			and isinstance(zv, M.ZeroValue) and zv.ty == ty
			and isinstance(zb, M.StoreLocal)
			and zb.local == local and zb.value == zv.dest
			and getattr(zb, "synthetic_zero_back", False) is True
			and isinstance(drop, M.DropValue)
			and drop.value == load.dest and drop.ty == ty
			and isinstance(cb, M.ConstBool) and cb.value is False
			and isinstance(fclear, M.StoreLocal)
			and fclear.local == flag_local and fclear.value == cb.dest
		)
		if not shape_ok:
			raise PlanContractError(
				f"overwrite_cleanup guarded site-4 (fn '{func.name}', block "
				f"'{dbn}'[{didx}]): guarded drop for '{local}' is not the "
				f"canonical Load→Zero→Store(zero)→Drop→flag-clear sequence "
				f"(operand/type mismatch)."
			)
		# 3) drop_blk ends in Goto(post) where the store lives.
		term = drop_blk.terminator
		if not isinstance(term, M.Goto):
			raise PlanContractError(
				f"overwrite_cleanup guarded site-4 (fn '{func.name}', block "
				f"'{dbn}'): guarded drop block does not terminate in Goto(post)."
			)
		post_name = term.target
		sbn, _sidx = pos[id(store_obj)]
		if sbn != post_name:
			raise PlanContractError(
				f"overwrite_cleanup guarded site-4 (fn '{func.name}'): guarded "
				f"store for '{local}' is in block '{sbn}', not the drop block's "
				f"Goto target '{post_name}'."
			)
		# 4) a UNIQUE origin branches If(flag, drop_blk, post_blk) after
		#    loading the flag.
		origins = [
			b for b in func.blocks.values()
			if isinstance(b.terminator, M.IfTerminator)
			and b.terminator.then_target == dbn
		]
		if len(origins) != 1:
			raise PlanContractError(
				f"overwrite_cleanup guarded site-4 (fn '{func.name}'): guarded "
				f"drop block '{dbn}' for '{local}' has {len(origins)} origin "
				f"IfTerminator(s) (expected exactly one)."
			)
		origin = origins[0]
		it = origin.terminator
		if it.else_target != post_name:
			raise PlanContractError(
				f"overwrite_cleanup guarded site-4 (fn '{func.name}'): origin "
				f"of '{dbn}' for '{local}' branches its else edge to "
				f"'{it.else_target}', not the post block '{post_name}'."
			)
		if (
			not origin.instructions
			or not isinstance(origin.instructions[-1], M.LoadLocal)
			or origin.instructions[-1].local != flag_local
			or origin.instructions[-1].dest != it.cond
		):
			raise PlanContractError(
				f"overwrite_cleanup guarded site-4 (fn '{func.name}'): origin "
				f"of '{dbn}' for '{local}' does not load the drop flag "
				f"'{flag_local}' immediately before the guard branch."
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
