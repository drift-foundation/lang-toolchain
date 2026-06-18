# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 site-1 — cleanup re-authoring pass for scope drops, with
observe parity.

All of HIR→MIR's scope-drop call sites (function-exit at HReturn /
HThrow, `lower_function_body` fall-through, `lower_block`
fall-through, lambda-block exits, and HBreak / HContinue) emit
`M.CleanupHook` markers via `_emit_scope_cleanup_hook(scope_index)`
instead of inline drops.  Post Bug 2 architecture flip (2026-05-15),
this pass runs AFTER `drop_flags` PLANNING (with a ledger rebuild
in between) and BEFORE `string_arc`.  It is the SOLE emitter of
cleanup drops: for each `CleanupHook` it consults the ledger's
`verdict_at` for every candidate (local, type) pair and emits real
`MoveOut + DropValue` sequences — guarded, edge-elaborated, or
unguarded — in candidate (reverse-decl RAII) order.

Pipeline order:

    build ledger
    drop_flags PLANNING (entry init, set/clear, metadata; no emit)
    rebuild ledger                                # mandatory
    cleanup_authoring                             # THIS PASS
    rebuild ledger
    string_arc

Authority: site 1's drop decisions are driven by `verdict_at`
across all emission sites, NOT by HIR→MIR's `_moved_locals` set.
Consume-via-intrinsic gap class (DROP_VALUE / RAW_WRITE /
PTR_WRITE / MAYBE_WRITE / REPLACE) is closed at the HIR→MIR
boundary via `_lower_owning_consume`, so cleanup_authoring's
verdict lookups see the intrinsic consumption as a real MoveOut
in MIR.

Emission shapes per candidate:

  * **Unguarded** — `MUST_DROP` or variant zero-tag `PathDependent`.
    Inline `MoveOut + DropValue` at the hook position.  If the local
    is flag-managed by `drop_flags`, a flag-clear `StoreLocal(flag,
    false)` follows.  Uniform invariant: "flag bit ≡ currently owns
    destructible storage."

  * **Per-arm edge-elaborated** — `PathDependent` non-variant +
    every hook candidate is flag-managed AND every predecessor edge
    of the hook block has a determinable LIVE/MOVED/UNINIT state
    (no `MAYBE_UNINIT`).  For each LIVE predecessor edge, emit
    `MoveOut + DropValue + flag-clear` at the edge — in-place at
    end of the predecessor if it has a single successor; via a
    fresh edge-split block if it has multiple successors.  The
    hook position emits nothing for elaborated candidates; the
    lattice merge becomes uniformly `MOVED_OUT`.  This is the
    primary path for the Bug 2 carrier (conditional move inside a
    loop body).

  * **Flag-guarded fallback** — `PathDependent` non-variant +
    flag-managed, when per-arm aborts (any predecessor edge is
    `MAYBE_UNINIT` — upstream merge unresolvable without
    recursion, which Phase 1 declines).  At the hook position:
    split the block, emit `LoadLocal(flag) → IfTerminator(flag,
    drop_blk, post_blk)`; `drop_blk` runs the canonical
    `MoveOut + DropValue + flag-clear` and goes to `post_blk`.

  * **Skip / tripwire** — `PathDependent` non-variant + NOT
    flag-managed.  Emits a `path_dependent_non_variant_skip`
    telemetry record and skips.  Post Bug 2 this fires only for
    shapes `drop_flags` planning did not select; useful as a
    regression detector if any real Drift code hits it.

Mutating-hook ledger discipline (K-review, 2026-05-15):

The pass mutates the CFG mid-loop (in-place inserts in
predecessor blocks, edge splits creating new blocks, hook removal
and drop insertion at the hook position).  Subsequent hook queries
against the original ledger would otherwise see stale `(block,
idx)` state — shifted indices in the same block, or missing
entries on freshly-created blocks (where `state_pre` falls back
to `UNINIT` and miscompiles).  The pass rebuilds the ledger after
every mutating hook step via `_rebuild_ledger()`.  Hooks are
tracked by object identity (`id(M.CleanupHook)`), NOT by
`(block, idx)`, so index shifts after a hook removal cannot
confuse the worklist.

Observe parity: every per-candidate decision is emitted as a
`[drift:ownership_ledger]` stderr line, gated on
`drift_debug.enabled("ownership_ledger")`.  Records carry
`site=scope_drop`.  Classification is hard-coded `agree` —
cleanup_authoring IS the ledger consultation, so by construction
the site verdict matches the ledger verdict at this program
point.  Reason tags: `needs_drop` / `not_drop_needing` /
`moved_unconditional` (existing) plus
`path_dependent_edge_elaborated_emit` (per-arm primary),
`path_dependent_flag_guarded_emit` (fallback),
`must_drop_flag_clear` (Carrier 7 uniform clear), and the
existing `path_dependent_non_variant_skip` tripwire.

RAII timing: every emission point reflects the source-syntactic
scope-exit ordering.  Per-arm drops fire at end of each LIVE
predecessor (semantically equivalent to scope-exit for any
user-observable behavior; the merge is a no-op CFG join).
Marker is removed after authoring; downstream passes see only
canonical `MoveOut + DropValue` sequences.
"""

from __future__ import annotations

import sys
from typing import List, Optional

from lang.driftc.core.types_core import TypeId, TypeTable
from lang.driftc import debug as drift_debug
from . import mir_nodes as M
from . import cfg as _cfg
from . import ownership_ledger_events as _ledger_events
from .ledger_cache import (
	build_and_attach_ledger,
	mark_ledger_dirty,
	maybe_fresh_ledger,
)
from .ownership_ledger import DropVerdict, LiveState, LiveStateMap
from .drop_policy_compute import compute_drop_policy
from .string_arc import variant_zero_tag_drop_safe


# Patch-2 reason tag for the non-variant + PathDependent skip case.
# Site 1's legacy `_moved_locals` would have skipped here too; this
# tag makes the case visible in observe so we can detect any real
# Drift code that hits it.  Post Bug 2 architecture flip: this tag
# fires ONLY when the local is NOT flag-managed; flag-managed
# non-variant PathDependent now takes the guarded-emit branch.
_REASON_PATH_DEPENDENT_NON_VARIANT_SKIP = "path_dependent_non_variant_skip"

# Bug 2 architecture flip telemetry tags (2026-05-15).
_REASON_PATH_DEPENDENT_FLAG_GUARDED_EMIT = "path_dependent_flag_guarded_emit"
_REASON_MUST_DROP_FLAG_CLEAR = "must_drop_flag_clear"
# Per-arm edge elaboration (2026-05-15) — Bug 2 architecture: a
# non-variant PathDependent candidate at a CleanupHook is resolved
# by emitting MoveOut + DropValue + flag-clear on each predecessor
# edge where the local is LIVE.  No emission at the hook itself
# (the merge becomes uniformly MOVED_OUT, which the ledger handles
# cleanly).
_REASON_PATH_DEPENDENT_EDGE_ELABORATED_EMIT = "path_dependent_edge_elaborated_emit"


_KIND_UNGUARDED = "unguarded"
_KIND_GUARDED = "guarded"
_KIND_SKIP = "skip"
# `_KIND_EDGE_ELABORATED` is an internal marker used between the
# classification step and the emission step.  A candidate marked
# this way had its drop emitted on every LIVE predecessor edge;
# the hook position emits nothing for it.  Telemetry reports
# `path_dependent_edge_elaborated_emit`.
_KIND_EDGE_ELABORATED = "edge_elaborated"


def _build_predecessor_map(func: M.MirFunc) -> dict:
	"""Return `dict[target_block_name, list[(pred_block_name, edge_kind)]]`.

	Edge kind is one of:
	  - "goto"     — pred's terminator is `Goto(target)`.
	  - "if_then"  — pred's `IfTerminator.then_target == target`.
	  - "if_else"  — pred's `IfTerminator.else_target == target`.

	An `IfTerminator(cond, X, X)` (same target on both sides) appears
	twice (once as `if_then`, once as `if_else`).  Single-successor
	dedup happens at the in-place insertion site; do not collapse
	here so the edge-split path can disambiguate.
	"""
	pred_map: dict = {}
	for blk_name, blk in func.blocks.items():
		# Edge kinds come from the central MIR CFG-successor contract
		# (`MTerminator.successor_edges()` via stage2/cfg.py): Goto → "goto",
		# IfTerminator → "if_then"/"if_else".  `IfTerminator(cond, X, X)` yields
		# two edges to X (once per label), matching the prior behavior.
		for target, edge_kind in _cfg.terminator_successor_edges(blk.terminator):
			pred_map.setdefault(target, []).append((blk_name, edge_kind))
	return pred_map


def _is_multi_successor(blk: M.BasicBlock) -> bool:
	"""True iff the block's terminator has 2+ DISTINCT outgoing targets.
	`Goto`/`Return`/`Unreachable` are single-or-no-successor.  `IfTerminator(cond,
	X, X)` is treated as single-successor (in-place insertion before the terminator
	covers both edges identically).  Generalizes to any terminator via the central
	successor contract (a future multi-way terminator with ≥2 distinct targets is
	multi-successor)."""
	return len(set(_cfg.terminator_successors(blk.terminator))) >= 2


def _state_post_at_block_end(ledger: LiveStateMap, pred_name: str, pred_blk: M.BasicBlock, local: str) -> LiveState:
	"""State of `local` on every outgoing edge of `pred_blk` (i.e.,
	immediately before the terminator).  Falls back to `block_in`
	when the block has no instructions."""
	if local not in ledger.tracked_locals:
		return LiveState.LIVE
	if pred_blk.instructions:
		last_idx = len(pred_blk.instructions) - 1
		return ledger.state_post((pred_name, last_idx), local)
	return ledger.block_in.get(pred_name, {}).get(local, LiveState.UNINIT)


def author_cleanup(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
) -> int:
	"""Walk every block, replace each `M.CleanupHook` with the
	canonical drop sequences the ledger says are needed.

	Returns the number of `MoveOut + DropValue` pairs emitted across
	all hooks (telemetry; not used for control flow).

	No-op when the function has no `_ownership_ledger` attached
	(e.g. ad-hoc test harnesses that build MIR without the driver
	wiring).  Any `CleanupHook` instructions remain in place in that
	case — they would surface as a downstream pass error, which is
	the intended fail-loud signal.

	Bug 2 architecture flip (2026-05-15): this pass is the SOLE
	emitter of cleanup drops.  Three emit shapes:

	  - **Unguarded MUST_DROP** (or variant zero-tag PathDependent):
	    inline `MoveOut + DropValue` at the hook position.  If the
	    local is flag-managed (`_drop_flag_managed_locals`), the
	    sequence is followed by a flag clear `StoreLocal(flag,
	    false)` — uniform invariant: "flag bit ≡ currently owns
	    destructible storage."

	  - **Guarded non-variant PathDependent + flag-managed**: split
	    the block at the hook position; emit
	    `LoadLocal(flag) → IfTerminator(flag, drop_blk, post_blk)`;
	    drop_blk contains `MoveOut + DropValue + flag clear` and
	    `Goto(post_blk)`; post_blk continues with the rest of the
	    original block.  RAII timing: the drop fires at the
	    original CleanupHook position, NOT at next-overwrite or
	    function exit.

	  - **Skip** (non-variant PathDependent + not flag-managed):
	    `path_dependent_non_variant_skip` tripwire (unchanged
	    behaviour; signals any shape that drop_flags planning did
	    not select).
	"""
	# Pass-entry consumer site.  Use `maybe_fresh_ledger` (soft form)
	# because cleanup_authoring legitimately no-ops when no ledger is
	# attached — exercised by tests and by toolchain paths that skip
	# the ledger build entirely.  A *stale* ledger is still an
	# assertion (the soft form only soft-handles missing, not dirty).
	ledger: Optional[LiveStateMap] = maybe_fresh_ledger(func, "cleanup_authoring")
	if ledger is None:
		return 0
	emitted_drops = 0
	new_temp_counter = 0
	# MIR shares one string namespace across locals and SSA value-ids
	# (LocalId and ValueId are both strings).  Seed `used_temps` from
	# every name the function already references so a synthesised
	# `__cleanup_tN` cannot collide with a real local — the prefix
	# alone is not a guarantee (user code could legally name a local
	# `__cleanup_t1`).  Sources: instruction dests, func.locals,
	# func.local_types keys, and func.params.
	used_temps: set[str] = set()
	used_temps.update(func.params)
	used_temps.update(func.locals)
	used_temps.update(func.local_types.keys())
	for blk in func.blocks.values():
		for ins in blk.instructions:
			dest = getattr(ins, "dest", None)
			if isinstance(dest, str):
				used_temps.add(dest)

	def _new_temp() -> str:
		nonlocal new_temp_counter
		while True:
			new_temp_counter += 1
			name = f"__cleanup_t{new_temp_counter}"
			if name not in used_temps:
				used_temps.add(name)
				return name

	# Bug 2 metadata attached by drop_flags planning pass.
	flag_managed: set = set(getattr(func, "_drop_flag_managed_locals", set()) or set())
	flag_for: dict = dict(getattr(func, "_drop_flag_for_local", {}) or {})

	# Predecessor map for per-arm edge elaboration.  Snapshotted once
	# at pass entry: per-arm elaboration may add new blocks (edge
	# cleanup blocks), but those new blocks never become predecessors
	# of pre-existing hook blocks during this pass (they're always
	# inserted on an edge that ALREADY targeted the hook block, so
	# they replace the predecessor — see _try_per_arm_elaboration).
	# Subsequent hooks processed by the worklist still need accurate
	# predecessor info; we rebuild the entry for any block whose
	# predecessor set changed via edge split.
	pred_map = _build_predecessor_map(func)

	# Track block names allocated by elaboration so the worklist's
	# fresh-name generation does not collide.
	used_block_names: set = set(func.blocks.keys())

	def _alloc_block_name(base: str) -> str:
		if base not in used_block_names:
			used_block_names.add(base)
			return base
		i = 1
		while True:
			name = f"{base}_{i}"
			if name not in used_block_names:
				used_block_names.add(name)
				return name
			i += 1

	observe_on = drift_debug.enabled("ownership_ledger")

	# Worklist-style processing: each iteration processes the FIRST
	# unprocessed CleanupHook in a block.  Block splits add new blocks
	# to the worklist; multiple hooks per block are handled by re-
	# queuing the block until it has no more hooks.
	#
	# Track processed hooks by OBJECT IDENTITY (id of the
	# `M.CleanupHook` instance), NOT by `(block, idx)`.  Block-rewriting
	# shifts subsequent hook indices, so an idx-keyed dedup would
	# incorrectly match a NEW hook landing at a previously-processed
	# idx after a removal.  Object identity is stable across shifts.
	worklist: List[M.BasicBlock] = list(func.blocks.values())
	processed_hook_ids: set = set()

	def _rebuild_ledger() -> None:
		"""Rebuild the ledger to reflect post-mutation MIR.  K-review
		finding (2026-05-15): cleanup_authoring mutates the CFG during
		the worklist loop (block splits, in-place predecessor inserts,
		hook removal + drop insertion).  Subsequent hook queries
		against `(blk.name, idx)` would otherwise hit stale entries —
		shifted indices in the same block, or missing entries on newly
		split blocks (where `state_pre` falls back to UNINIT and
		`state_post` likewise).  Both miscompile.  Rebuild after every
		mutation that could shift indices or create new blocks."""
		nonlocal ledger
		ledger = build_and_attach_ledger(
			func,
			drop_policy=lambda _t: None,
			reason="cleanup_authoring.in_pass_rebuild",
		)

	while worklist:
		blk = worklist.pop(0)
		# Find the first unprocessed CleanupHook (by object id).
		hook_idx: Optional[int] = None
		for idx, ins in enumerate(blk.instructions):
			if isinstance(ins, M.CleanupHook):
				if id(ins) in processed_hook_ids:
					continue
				hook_idx = idx
				break
		if hook_idx is None:
			continue
		hook: M.CleanupHook = blk.instructions[hook_idx]
		processed_hook_ids.add(id(hook))

		# Classify each candidate.
		hook_point = (blk.name, hook_idx)
		decisions: List[tuple] = []  # list of (kind, local, ty, verdict, raw_state)
		for local, ty in hook.candidates:
			needs_drop_axis = bool(compute_drop_policy(type_table, ty).needs_drop)
			verdict = ledger.verdict_at(hook_point, local, needs_drop=needs_drop_axis)
			raw_state = ledger.state_pre(hook_point, local)
			if verdict is DropVerdict.MUST_DROP:
				kind = _KIND_UNGUARDED
			elif verdict is DropVerdict.PATH_DEPENDENT:
				if variant_zero_tag_drop_safe(ty, type_table):
					kind = _KIND_UNGUARDED
				elif local in flag_managed and local in flag_for:
					kind = _KIND_GUARDED
				else:
					kind = _KIND_SKIP
			else:
				kind = _KIND_SKIP
			decisions.append((kind, local, ty, verdict, raw_state))

		# Per-arm edge elaboration (Bug 2 architecture, 2026-05-15).
		# Activates only when EVERY candidate at this hook is non-
		# variant PathDependent + flag-managed (the `_KIND_GUARDED`
		# decisions above).  This conservative gate preserves
		# destructor order across all candidates at this hook: if any
		# candidate must emit at the hook position (MUST_DROP, variant
		# zero-tag, or non-flag-managed skip), per-arm is disabled and
		# everything emits at the hook in reverse-decl order — matches
		# the prior architecture.  Mixed-candidate hooks fall back to
		# the existing flag-guarded path for PD candidates.
		#
		# When all candidates are GUARDED, per-arm attempts to resolve
		# them via predecessor edge cleanup.  If every PD candidate can
		# be elaborated cleanly on every predecessor edge, ALL hook
		# candidates are demoted to `_KIND_EDGE_ELABORATED` and the
		# hook position emits nothing.  If ANY predecessor edge has a
		# candidate in `MAYBE_UNINIT` state (cannot determine LIVE vs
		# MOVED on that edge without recursion — Phase 1 declines),
		# elaboration aborts entirely and the GUARDED kinds stand
		# (existing flag-guarded path at the hook).
		all_pd_flag_managed = (
			len(decisions) > 0
			and all(d[0] == _KIND_GUARDED for d in decisions)
		)
		if all_pd_flag_managed:
			pd_candidates = [(local, ty) for (kind, local, ty, _, _) in decisions]
			resolved, _unresolved, new_blocks, edge_emitted = _try_per_arm_elaboration(
				func=func,
				hook_blk=blk,
				pd_candidates=pd_candidates,
				candidate_order=hook.candidates,
				ledger=ledger,
				pred_map=pred_map,
				flag_managed=flag_managed,
				flag_for=flag_for,
				new_temp=_new_temp,
				alloc_block_name=_alloc_block_name,
			)
			# Per-arm elaboration is atomic: it commits only when
			# ALL pd_candidates resolved (Phase A's gate).  When it
			# aborts, `resolved` is empty and `new_blocks` is empty
			# AND no pred blocks were mutated.  When it commits,
			# `resolved == {locals from pd_candidates}` and we apply
			# the new_blocks list.
			if resolved:
				for nb in new_blocks:
					func.blocks[nb.name] = nb
				if new_blocks:
					mark_ledger_dirty(func, "cleanup_authoring.per_arm_elaboration_block_insert")
				# Refresh predecessor map.  Edge splits added new
				# blocks whose predecessor entries are now part of
				# the hook block's incoming set; the original
				# predecessor for the SPLIT edge points to the new
				# edge block instead.  Cheap to rebuild and per-arm
				# activation is rare.
				pred_map = _build_predecessor_map(func)
				# Rebuild ledger — per-arm appended drops to pred
				# block instruction lists (or created new edge-split
				# blocks).  Subsequent hook queries against the
				# original ledger would otherwise see stale state at
				# pred-block ends and on the new blocks.
				_rebuild_ledger()
				emitted_drops += edge_emitted
				# Demote decisions.
				demoted: List[tuple] = []
				for kind, local, ty, v, r in decisions:
					if local in resolved:
						demoted.append((_KIND_EDGE_ELABORATED, local, ty, v, r))
					else:
						demoted.append((kind, local, ty, v, r))
				decisions = demoted

		# Emit observe-parity telemetry per candidate.
		if observe_on:
			for kind, local, ty, verdict, raw_state in decisions:
				_emit_decision_record(
					func=func,
					block_name=blk.name,
					idx=hook_idx,
					local=local,
					verdict=verdict,
					raw_state=raw_state,
					kind=kind,
					flag_managed=(local in flag_managed),
				)

		pre_hook = blk.instructions[:hook_idx]
		post_hook = blk.instructions[hook_idx + 1:]
		original_term = blk.terminator

		has_guarded = any(d[0] == _KIND_GUARDED for d in decisions)

		if not has_guarded:
			# Simple in-place rewrite: no block split needed.
			new_instrs: List[M.MInstr] = list(pre_hook)
			for kind, local, ty, _verdict, _raw_state in decisions:
				if kind != _KIND_UNGUARDED:
					continue
				tmp = _new_temp()
				new_instrs.append(M.MoveOut(dest=tmp, local=local, ty=ty))
				new_instrs.append(M.DropValue(value=tmp, ty=ty))
				func.local_types[tmp] = ty
				emitted_drops += 1
				# Uniform flag-clear invariant: if the local is
				# flag-managed, clear the flag after the drop so the
				# bit reflects "no longer owns storage."
				if local in flag_managed and local in flag_for:
					clear_dest = _new_temp()
					new_instrs.append(M.ConstBool(dest=clear_dest, value=False))
					new_instrs.append(M.StoreLocal(local=flag_for[local], value=clear_dest))
			new_instrs.extend(post_hook)
			# Mutating step: rebuild ledger BEFORE the rewrite so the
			# rebuild reflects the PRE-mutation MIR... wait, no — we
			# want POST-mutation state for the next hook query.  Apply
			# rewrite, THEN rebuild.
			block_mutated = (new_instrs != blk.instructions)
			blk.instructions = new_instrs
			if block_mutated:
				mark_ledger_dirty(func, "cleanup_authoring.replace_hook_with_drops")
				# Index shifts in this block invalidate ledger entries
				# at every (blk.name, idx) past the original hook_idx.
				# Subsequent hooks in this same block (or in any block
				# that queries state at this block's end via the
				# predecessor edge path) would otherwise read stale
				# state.  Rebuild to keep the ledger consistent.
				_rebuild_ledger()
			# Terminator unchanged.  Re-queue if more hooks remain in
			# this block (post_hook may contain another CleanupHook).
			if any(isinstance(i, M.CleanupHook) for i in blk.instructions):
				worklist.insert(0, blk)
			continue

		# At least one guarded emission — split the block.
		# Processing model: maintain a `current_blk` cursor and
		# `current_instrs` buffer.  Each guarded emission closes out
		# `current_blk` with an IfTerminator and starts a fresh
		# `post_blk`.  Unguarded emissions before/between guards
		# accumulate in the appropriate `current_instrs`.
		current_blk = blk
		current_instrs = list(pre_hook)
		new_blocks: List[M.BasicBlock] = []

		for kind, local, ty, _verdict, _raw_state in decisions:
			if kind == _KIND_SKIP:
				continue
			if kind == _KIND_UNGUARDED:
				tmp = _new_temp()
				current_instrs.append(M.MoveOut(dest=tmp, local=local, ty=ty))
				current_instrs.append(M.DropValue(value=tmp, ty=ty))
				func.local_types[tmp] = ty
				emitted_drops += 1
				if local in flag_managed and local in flag_for:
					clear_dest = _new_temp()
					current_instrs.append(M.ConstBool(dest=clear_dest, value=False))
					current_instrs.append(M.StoreLocal(local=flag_for[local], value=clear_dest))
				continue
			# kind == _KIND_GUARDED
			flag_local = flag_for[local]
			flag_load_dest = _new_temp()
			current_instrs.append(M.LoadLocal(dest=flag_load_dest, local=flag_local))

			# drop_blk is brand-new (not yet inserted into func.blocks);
			# mutations to it cannot affect the attached ledger.  The
			# block-list insert at line 571 (below) is the operation
			# that actually invalidates the ledger; mark_ledger_dirty
			# there.
			drop_blk = M.BasicBlock(name=_new_block_name(func, f"{blk.name}_cleanup_drop_{local}", new_blocks))
			tmp = _new_temp()
			drop_blk.instructions.append(M.MoveOut(dest=tmp, local=local, ty=ty))  # ledger-cache-safety-audit: allow new-block
			drop_blk.instructions.append(M.DropValue(value=tmp, ty=ty))  # ledger-cache-safety-audit: allow new-block
			func.local_types[tmp] = ty
			clear_dest = _new_temp()
			drop_blk.instructions.append(M.ConstBool(dest=clear_dest, value=False))  # ledger-cache-safety-audit: allow new-block
			drop_blk.instructions.append(M.StoreLocal(local=flag_local, value=clear_dest))  # ledger-cache-safety-audit: allow new-block

			post_blk = M.BasicBlock(name=_new_block_name(func, f"{blk.name}_cleanup_post_{local}", new_blocks))
			drop_blk.terminator = M.Goto(target=post_blk.name)  # ledger-cache-safety-audit: allow new-block

			# current_blk IS in func.blocks; the next two mutations
			# (instructions replacement + terminator) DO invalidate
			# the attached ledger.
			current_blk.instructions = list(current_instrs)
			mark_ledger_dirty(func, "cleanup_authoring.emit_guarded_drop")
			current_blk.terminator = M.IfTerminator(
				cond=flag_load_dest,
				then_target=drop_blk.name,
				else_target=post_blk.name,
			)

			new_blocks.append(drop_blk)
			new_blocks.append(post_blk)
			emitted_drops += 1

			current_blk = post_blk
			current_instrs = []

		# After all decisions: append the post-hook instructions and
		# restore the original terminator on the final `current_blk`.
		current_instrs.extend(post_hook)
		current_blk.instructions = current_instrs
		current_blk.terminator = original_term
		mark_ledger_dirty(func, "cleanup_authoring.emit_unguarded_drop_tail")

		# Register the new blocks.
		for nb in new_blocks:
			func.blocks[nb.name] = nb
		if new_blocks:
			mark_ledger_dirty(func, "cleanup_authoring.register_new_blocks")

		# Rebuild ledger after block-split mutation: the original
		# block's instructions/terminator changed, new blocks were
		# created (drop_blk / post_blk / chain), and a later hook in
		# `current_blk` (from post_hook) would otherwise be classified
		# against stale state at a brand-new (block_name, idx) where
		# the ledger has no entries (state_pre falls back to UNINIT —
		# wrong classification).
		_rebuild_ledger()
		# Refresh predecessor map for the same reason.
		pred_map = _build_predecessor_map(func)

		# The final post-block may contain another CleanupHook (from
		# post_hook).  Re-queue it so the worklist will process it.
		if any(isinstance(i, M.CleanupHook) for i in current_blk.instructions):
			worklist.insert(0, current_blk)

	return emitted_drops


def _new_block_name(func: M.MirFunc, base: str, pending: List[M.BasicBlock]) -> str:
	"""Allocate a fresh block name that does not collide with existing
	`func.blocks` keys NOR with blocks `pending` insertion (queued in
	this pass call)."""
	pending_names = {b.name for b in pending}
	if base not in func.blocks and base not in pending_names:
		return base
	i = 1
	while True:
		name = f"{base}_{i}"
		if name not in func.blocks and name not in pending_names:
			return name
		i += 1


def _try_per_arm_elaboration(
	*,
	func: M.MirFunc,
	hook_blk: M.BasicBlock,
	pd_candidates: list,           # [(local, ty), ...] — non-variant PD candidates
	candidate_order: list,         # hook.candidates (reverse-decl RAII order)
	ledger: LiveStateMap,
	pred_map: dict,
	flag_managed: set,
	flag_for: dict,
	new_temp,
	alloc_block_name,
) -> tuple:
	"""Two-phase per-arm cleanup elaboration.

	Phase A (probe, side-effect-free): for each predecessor edge of
	`hook_blk`, compute `state_post` of every PD candidate.
	Classify each (edge, candidate) as:
	  - LIVE                        → include in this edge's cleanup
	  - MOVED_OUT/UNINIT/TOMBSTONED → skip on this edge
	  - MAYBE_UNINIT                → unresolvable on this edge
	A candidate is RESOLVED iff NO predecessor edge classifies it as
	MAYBE_UNINIT.  If `len(resolved) < len(pd_candidates)`, return
	empty resolved set — caller falls back to the hook-position
	flag-guarded path.

	Phase B (apply, only if ALL candidates resolved): for each
	predecessor edge with non-empty cleanup, emit the sequence:
	  - For each candidate in `candidate_order` (preserving hook's
	    reverse-decl order) that is LIVE on this edge: emit
	    `MoveOut + DropValue + flag-clear`.
	  - Single-successor predecessor (Goto / IfTerminator same-target):
	    insert in-place at end of pred before terminator (deduplicated
	    across edge_kind for the same pred).
	  - Multi-successor predecessor (IfTerminator distinct targets):
	    split the specific edge — create a new block with the cleanup
	    sequence + Goto(hook_blk), rewrite pred's terminator's matching
	    target field.

	Returns `(resolved_locals_set, unresolved_locals_set, new_blocks_list, emitted_count)`.
	When elaboration commits, `unresolved_locals_set` is empty and
	`emitted_count` is the number of `MoveOut + DropValue` pairs
	emitted across all edge cleanups (used by `author_cleanup` for
	the telemetry running total).  When elaboration aborts,
	`resolved_locals_set` is empty AND no blocks are mutated.
	"""
	preds = pred_map.get(hook_blk.name, [])
	all_locals = {local for (local, _ty) in pd_candidates}
	if not preds:
		return set(), all_locals, [], 0

	# --- Phase A: probe ---
	# edge_state[(pred_name, edge_kind)] = {local: LiveState}
	edge_state: dict = {}
	for pred_name, edge_kind in preds:
		pred_blk = func.blocks.get(pred_name)
		if pred_blk is None:
			# Defensive: shouldn't happen but if pred_map is stale
			# (e.g. a block was removed elsewhere), treat as unresolved.
			return set(), all_locals, [], 0
		states: dict = {}
		for local, _ty in pd_candidates:
			states[local] = _state_post_at_block_end(ledger, pred_name, pred_blk, local)
		edge_state[(pred_name, edge_kind)] = states

	resolved: set = set()
	for local, _ty in pd_candidates:
		ok = True
		for ed in edge_state:
			s = edge_state[ed][local]
			if s is LiveState.MAYBE_UNINIT:
				ok = False
				break
		if ok:
			resolved.add(local)

	unresolved = all_locals - resolved
	if unresolved:
		# Order-preserving gate: ANY unresolved candidate aborts the
		# entire per-arm attempt for this hook (so destructor order is
		# preserved across all candidates).  See author_cleanup's
		# per-arm activation comment.
		return set(), all_locals, [], 0

	# --- Phase B: apply ---
	ty_by_local = {local: ty for (local, ty) in pd_candidates}
	new_blocks: list = []
	inplace_inserted: set = set()  # pred_name where in-place insert done
	emitted_pairs = 0

	for pred_name, edge_kind in preds:
		states = edge_state[(pred_name, edge_kind)]
		# Collect candidates LIVE on this edge in candidate (= RAII) order.
		edge_live_candidates: list = []
		for local, ty in candidate_order:
			if local not in resolved:
				continue
			if states.get(local) is LiveState.LIVE:
				edge_live_candidates.append((local, ty))
		if not edge_live_candidates:
			continue

		pred_blk = func.blocks[pred_name]

		def _emit_cleanup_into(seq_target: list) -> int:
			count = 0
			for local, ty in edge_live_candidates:
				tmp = new_temp()
				seq_target.append(M.MoveOut(dest=tmp, local=local, ty=ty))
				seq_target.append(M.DropValue(value=tmp, ty=ty))
				func.local_types[tmp] = ty
				count += 1
				if local in flag_managed and local in flag_for:
					clear_dest = new_temp()
					seq_target.append(M.ConstBool(dest=clear_dest, value=False))
					seq_target.append(M.StoreLocal(local=flag_for[local], value=clear_dest))
			return count

		if not _is_multi_successor(pred_blk):
			# Single-successor pred: in-place insert at end before
			# terminator.  Dedup so an `IfTerminator(c, X, X)` pred
			# doesn't get the cleanup inserted twice.
			if pred_name in inplace_inserted:
				continue
			inplace_inserted.add(pred_name)
			emitted_pairs += _emit_cleanup_into(pred_blk.instructions)
			continue

		# Multi-successor: split THIS specific edge.
		edge_blk_name = alloc_block_name(f"{pred_name}_edge_cleanup_to_{hook_blk.name}")
		edge_blk = M.BasicBlock(name=edge_blk_name)
		emitted_pairs += _emit_cleanup_into(edge_blk.instructions)
		edge_blk.terminator = M.Goto(target=hook_blk.name)  # ledger-cache-safety-audit: allow new-block
		new_blocks.append(edge_blk)

		term = pred_blk.terminator
		assert isinstance(term, M.IfTerminator), (
			f"multi-successor pred {pred_name} expected IfTerminator, "
			f"got {type(term).__name__}"
		)
		if edge_kind == "if_then":
			term.then_target = edge_blk_name
		elif edge_kind == "if_else":
			term.else_target = edge_blk_name
		else:
			raise AssertionError(
				f"unexpected edge_kind={edge_kind!r} for multi-successor "
				f"pred {pred_name}"
			)

	return resolved, set(), new_blocks, emitted_pairs


def _emit_decision_record(
	*,
	func: M.MirFunc,
	block_name: str,
	idx: int,
	local: str,
	verdict: DropVerdict,
	raw_state: LiveState,
	kind: str,
	flag_managed: bool,
) -> None:
	"""Observe parity emit.  One JSON line per per-candidate decision,
	format-compatible with the existing `[drift:ownership_ledger]`
	channel that legacy site-1 records flow through.

	Classification is hard-coded `agree` — cleanup_authoring IS the
	ledger consultation, so by construction the site verdict matches
	the ledger verdict at this program point.

	Reason tag mapping post Bug 2 architecture flip (2026-05-15):

	  - emit + MUST_DROP, not flag-managed → `needs_drop`
	  - emit + MUST_DROP, flag-managed → `must_drop_flag_clear`
	    (Carrier 7 — uniform flag-clear invariant)
	  - emit + PathDependent variant-widening → `needs_drop`
	  - emit + PathDependent non-variant, flag-managed, edge-
	    elaborated (per-arm Phase 1) →
	    `path_dependent_edge_elaborated_emit`
	  - emit + PathDependent non-variant, flag-managed (guarded
	    fallback) → `path_dependent_flag_guarded_emit`
	  - skip + PathDependent non-variant, not flag-managed →
	    `path_dependent_non_variant_skip` (tripwire — signals any
	    shape drop_flags planning did not select)
	  - skip + state=MOVED_OUT → `moved_unconditional`
	  - skip + other → `not_drop_needing`
	"""
	import json
	should_emit = kind != _KIND_SKIP
	site_verdict = _ledger_events.VERDICT_MUST_DROP if should_emit else _ledger_events.VERDICT_MUST_NOT_DROP
	if should_emit:
		if kind == _KIND_EDGE_ELABORATED:
			site_reason = _REASON_PATH_DEPENDENT_EDGE_ELABORATED_EMIT
		elif kind == _KIND_GUARDED:
			site_reason = _REASON_PATH_DEPENDENT_FLAG_GUARDED_EMIT
		elif verdict is DropVerdict.MUST_DROP and flag_managed:
			site_reason = _REASON_MUST_DROP_FLAG_CLEAR
		else:
			site_reason = _ledger_events.REASON_NEEDS_DROP
	elif verdict is DropVerdict.PATH_DEPENDENT:
		site_reason = _REASON_PATH_DEPENDENT_NON_VARIANT_SKIP
	elif raw_state is LiveState.MOVED_OUT:
		site_reason = _ledger_events.REASON_MOVED_UNCONDITIONAL
	else:
		site_reason = _ledger_events.REASON_NOT_DROP_NEEDING
	ledger_verdict_str = _verdict_to_str(verdict)
	payload = {
		"site": _ledger_events.SITE_SCOPE_DROP,
		"fn_name": func.name,
		"program_point": [block_name, idx],
		"local": local,
		"site_verdict": site_verdict,
		"site_reason": site_reason,
		"ledger_verdict": ledger_verdict_str,
		"raw_state": raw_state.value,
		"classification": "agree",
		"field_path": [],
	}
	sys.stderr.write("[drift:ownership_ledger] " + json.dumps(payload, sort_keys=True) + "\n")


def _verdict_to_str(v: DropVerdict) -> str:
	if v is DropVerdict.MUST_DROP:
		return _ledger_events.VERDICT_MUST_DROP
	if v is DropVerdict.MUST_NOT_DROP:
		return _ledger_events.VERDICT_MUST_NOT_DROP
	return "path_dependent"
