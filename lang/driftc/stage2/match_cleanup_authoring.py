# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 site-2 — per-field match-cleanup re-authoring pass (patch 5).

HIR→MIR emits one `M.MatchCleanupHook` per arm's partial-move cleanup
point (inside `_lower_match`, at the program point legacy site 2
captured as `_cleanup_point`).  The hook carries the candidate
`(drop_tmp_local, field_index, field_ty)` triples — HIR→MIR pre-
allocates each `drop_tmp` local and registers it via
`_register_drop_local` so subsequent site-1 `M.CleanupHook`
candidate lists in the same scope include it.  The chain itself
(`VariantGetFieldAddr + LoadRef + StoreLocal + arm-end MoveOut +
DropValue`) is NOT emitted by HIR→MIR; this pass authors it based
on `field_verdict_at`.

Pass ordering (critical): runs AFTER the initial `build_ledger` but
BEFORE `cleanup_authoring` (site 1), with a ledger rebuild in
between.  This is required because site-1's `verdict_at` queries
must see the per-field StoreLocal / MoveOut transitions this pass
introduces.

Authority boundary (honest claim): the ledger decides emit-vs-skip
for carried candidates; HIR→MIR still decides the candidate SET via
the legacy `moved_field_indices` / `_needs_runtime_drop` filter
applied at hook-emission time.  Broadening to full unfiltered
consideration is a follow-up outside patch 5.

UNINIT contract: if a candidate's verdict is not MUST_DROP, this
pass emits no chain for it.  The pre-allocated `drop_tmp` local
remains registered in the function's locals / scope-stack but
receives no `StoreLocal` on any path.  Site-1 `verdict_at` at any
subsequent `CleanupHook` sees state `UNINIT` →
`classify(UNINIT, needs_drop=True) = MUST_NOT_DROP` → site 1 skips
emission for `drop_tmp`.  This is the property the design leans on;
pinned by a focused test in `test_match_cleanup_authoring.py`.

Variant zero-tag widening (parity with `cleanup_authoring.py`):
`field_verdict_at` returning `PathDependent` does NOT widen at this
site.  Field types here are arbitrary destructibles; the widening
predicate (`variant_zero_tag_drop_safe`) is variant-specific and
almost never applies.  Default for `PathDependent` = SKIP chain
emission (matches legacy trim-pass behavior — `PathDependent` never
appeared in the trim path because legacy site 2 only emitted on
statically-known-live fields).
"""

from __future__ import annotations

import sys
from typing import Dict, List, Optional, Tuple

from lang.driftc.core.types_core import TypeId, TypeTable
from lang.driftc import debug as drift_debug
from . import mir_nodes as M
from .ledger_cache import mark_ledger_dirty, maybe_fresh_ledger
from .ownership_ledger import DropVerdict, LiveState, LiveStateMap
from .drop_policy_compute import compute_drop_policy


ProgramPoint = Tuple[str, int]


def author_match_cleanup(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
) -> int:
	"""Walk every block, expand each `M.MatchCleanupHook` into the
	canonical per-field cleanup chain the ledger deems necessary.

	Returns the number of `(StoreLocal + arm-end MoveOut + DropValue)`
	chains emitted across all hooks (telemetry; not control flow).

	No-op when the function has no `_ownership_ledger` attached
	(e.g. test harnesses that build MIR without driver wiring).  Any
	`MatchCleanupHook` instructions remain in place in that case — a
	downstream pass would surface them as a fail-loud signal.
	"""
	# Pass-entry consumer site.  Use `maybe_fresh_ledger` (soft form)
	# because test harnesses legitimately build MIR without driver
	# wiring (see docstring above).  A *stale* ledger still asserts.
	ledger: Optional[LiveStateMap] = maybe_fresh_ledger(func, "match_cleanup_authoring")
	if ledger is None:
		return 0

	# Seed temp-name namespace — MIR strings are shared across locals
	# and SSA value ids.  Mirrors cleanup_authoring's `_new_temp`.
	used_temps: set[str] = set()
	used_temps.update(func.params)
	used_temps.update(func.locals)
	used_temps.update(func.local_types.keys())
	for blk in func.blocks.values():
		for ins in blk.instructions:
			dest = getattr(ins, "dest", None)
			if isinstance(dest, str):
				used_temps.add(dest)

	temp_counter = 0

	def _new_temp() -> str:
		nonlocal temp_counter
		while True:
			temp_counter += 1
			name = f"__match_ca_t{temp_counter}"
			if name not in used_temps:
				used_temps.add(name)
				return name

	# Pass 1: collect per-hook authoring decisions.  Authoring is
	# decided from the PRE-hook ledger state (field_state_pre at the
	# hook program point in the original instruction list).
	hook_decisions: Dict[ProgramPoint, List[Tuple[str, int, TypeId, bool, LiveState, DropVerdict]]] = {}
	hook_tail: Dict[ProgramPoint, Tuple[str, int, str, TypeId, str]] = {}
	for blk in func.blocks.values():
		for idx, ins in enumerate(blk.instructions):
			if not isinstance(ins, M.MatchCleanupHook):
				continue
			point: ProgramPoint = (blk.name, idx)
			decisions: List[Tuple[str, int, TypeId, bool, LiveState, DropVerdict]] = []
			for cand in ins.candidates:
				drop_tmp, field_idx, field_ty = cand
				needs_drop = bool(compute_drop_policy(type_table, field_ty).needs_drop)
				field_path = ((ins.ctor, int(field_idx)),)
				raw_state = ledger.field_state_pre(point, ins.arm_scrut_local, field_path)
				verdict = ledger.field_verdict_at(
					point,
					ins.arm_scrut_local,
					field_path,
					needs_drop=needs_drop,
				)
				should_emit = verdict is DropVerdict.MUST_DROP
				decisions.append((drop_tmp, int(field_idx), field_ty, should_emit, raw_state, verdict))
			hook_decisions[point] = decisions
			hook_tail[point] = (
				ins.arm_end_block,
				ins.arm_end_index,
				ins.arm_scrut_ptr_local,
				ins.variant_ty,
				ins.ctor,
			)

	if not hook_decisions:
		return 0

	# Pass 2: build per-block insertion / replacement plans.
	# `hook_replace[(blk, idx)]` — instructions that replace the hook
	#     at that index (the StoreLocal chain for MUST_DROP cands).
	# `arm_end_insert[(blk, idx)]` — instructions inserted BEFORE the
	#     instruction at that index (the arm-end MoveOut+DropValue
	#     pairs for MUST_DROP cands).
	hook_replace: Dict[ProgramPoint, List[M.MInstr]] = {}
	arm_end_insert: Dict[ProgramPoint, List[M.MInstr]] = {}
	emitted = 0

	observe_on = drift_debug.enabled("ownership_ledger")

	for point, decisions in hook_decisions.items():
		(ae_block, ae_idx, arm_scrut_ptr, variant_ty, ctor) = hook_tail[point]
		hook_chain: List[M.MInstr] = []
		tail_chain: List[M.MInstr] = []
		for drop_tmp, field_idx, field_ty, should_emit, raw_state, verdict in decisions:
			if observe_on:
				_emit_decision_record(
					func=func,
					point=point,
					drop_tmp=drop_tmp,
					ctor=ctor,
					field_idx=field_idx,
					raw_state=raw_state,
					verdict=verdict,
					should_emit=should_emit,
				)
			if not should_emit:
				continue
			slot_addr = _new_temp()
			move_dest = _new_temp()
			hook_chain.append(
				M.VariantGetFieldAddr(
					dest=slot_addr,
					variant_ref=arm_scrut_ptr,
					variant_ty=variant_ty,
					ctor=ctor,
					field_index=field_idx,
					field_ty=field_ty,
				)
			)
			# Atomic ownership transfer from the variant slot into
			# `drop_tmp`.  Codegen lowers this into:
			#   1. load *slot_addr → loaded_value
			#   2. tombstone bytes for `field_ty` written back to *slot_addr
			#   3. transfer loaded_value into drop_tmp's storage
			# `string_arc` recognises `MoveFromRef` as a TRANSFER
			# (no `StringRetain` insertion); the tail-chain `MoveOut +
			# DropValue` then releases the transferred stake exactly
			# once.  Pre-`MoveFromRef` this was `LoadRef + StoreLocal`,
			# which `string_arc.StoreLocal` rewrote with a retain —
			# the net release count came out to zero, leaking the
			# slot's original +1 (carrier:
			# `lang/tests/memcheck/test_partial_move_copy_binder_string_slot_leak.py`).
			hook_chain.append(
				M.MoveFromRef(local=drop_tmp, ptr=slot_addr, inner_ty=field_ty)
			)
			func.local_types[slot_addr] = type_table.ensure_ref_mut(field_ty)
			func.local_types[move_dest] = field_ty
			tail_chain.append(M.MoveOut(dest=move_dest, local=drop_tmp, ty=field_ty))
			tail_chain.append(M.DropValue(value=move_dest, ty=field_ty))
			emitted += 1
		hook_replace[point] = hook_chain
		ae_key: ProgramPoint = (ae_block, ae_idx)
		arm_end_insert.setdefault(ae_key, []).extend(tail_chain)

	# Pass 3: rewrite blocks.  Iterate original instructions in order;
	# for every index, prepend arm-end insertions keyed at that index,
	# then either replace a hook with its authored chain or keep the
	# instruction.  Indices in `hook_replace` / `arm_end_insert` refer
	# to the ORIGINAL instruction list; rebuild semantics preserve
	# their meaning because we never touch indices outside the
	# rewrite.
	for blk in func.blocks.values():
		new_instrs: List[M.MInstr] = []
		for idx, ins in enumerate(blk.instructions):
			key: ProgramPoint = (blk.name, idx)
			if key in arm_end_insert:
				new_instrs.extend(arm_end_insert[key])
			if isinstance(ins, M.MatchCleanupHook):
				new_instrs.extend(hook_replace.get(key, []))
				continue
			new_instrs.append(ins)
		# Arm-end insertions at the end-of-block index (i.e. immediately
		# before the terminator) — `arm_end_index = len(block.instructions)`
		# captured by HIR→MIR when the arm-end drainage would fire at
		# the trailing position.  The iterator above never visits this
		# index because it's past the last instruction.
		trailing_key: ProgramPoint = (blk.name, len(blk.instructions))
		if trailing_key in arm_end_insert:
			new_instrs.extend(arm_end_insert[trailing_key])
		blk.instructions = new_instrs
		mark_ledger_dirty(func, "match_cleanup_authoring.emit_arm_drops")
	return emitted


def _emit_decision_record(
	*,
	func: M.MirFunc,
	point: ProgramPoint,
	drop_tmp: str,
	ctor: str,
	field_idx: int,
	raw_state: LiveState,
	verdict: DropVerdict,
	should_emit: bool,
) -> None:
	"""Patch-5 observe parity emit (telemetry shift lands in step 6 of
	the slice order; this stub is wired today for symmetry with
	`cleanup_authoring._emit_decision_record` and to make the
	disagreement-free contract visible in the observe stream from the
	first slice).  Classification is hard-coded `agree` — match
	cleanup authoring IS the ledger consultation, so by construction
	the site verdict matches the ledger verdict at this program point.
	"""
	import json
	action = "emit" if should_emit else "skip"
	record = {
		"channel": "ownership_ledger",
		"site": "match_cleanup_per_field",
		"fn": func.name,
		"block": point[0],
		"idx": point[1],
		"drop_tmp": drop_tmp,
		"ctor": ctor,
		"field_index": field_idx,
		"raw_state": raw_state.value,
		"verdict": verdict.value,
		"action": action,
		"classification": "agree",
	}
	sys.stderr.write("[drift:ownership_ledger] " + json.dumps(record) + "\n")
