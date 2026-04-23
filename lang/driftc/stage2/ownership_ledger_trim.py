# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 step 3c — site-2 per-field drop VETO via
`LiveStateMap.field_verdict_at`.

Authority model: Tier 2 split via veto, NOT ledger-authored emission.
Site 2 (`match_cleanup`, inside HIR→MIR) still authors every per-field
drop using its inline legacy decisions (`moved_field_indices` /
`_needs_runtime_drop`).  The ledger cannot compel emission — if the
site didn't emit a drop, this pass cannot create one.  What the
ledger CAN do is veto: drops the site emitted whose `field_verdict_at`
comes back `MUST_NOT_DROP` are excised from MIR here.

Site 2 populates a side table on the function —
`func._match_cleanup_per_field_drops` — with one entry per emitted
drop: (scrut_local, field_path, cleanup_point, drop_local,
cleanup_fty).

After the ledger is built by the driver, `trim_match_cleanup_by_ledger`
queries `field_verdict_at` at each recorded cleanup point.  When the
ledger returns `MUST_NOT_DROP`, the drop chain for `drop_local` is
excised from MIR:

  - `StoreLocal(local=drop_local, value=...)` at the setup site.
  - Every `MoveOut(local=drop_local, ...)` (arm-end + any scope-exit
    fallback) paired with the `DropValue(value=moveout.dest, ...)`
    that immediately follows.

`VariantGetFieldAddr` / `LoadRef` scaffolding emitted alongside the
`StoreLocal` is left in place; it's side-effect-free at MIR level and
any dead-code cleanup downstream will handle it.  The drop-local name
is also removed from `func.locals` / `func.local_types` so the
emitted function looks like it had never registered the drop.

Build-timing: runs AFTER `build_ledger` in the driver, BEFORE
`string_arc` / `drop_flags`.  See `work/ownership-ledger/
3b-invariants.md` — the decisions trimmed here are per-field states at
points already stable pre-flag insertion.

Guardrail (K, 2026-04-23): 3a's `VariantGetFieldAddr` detection is
IMMEDIATE CONSERVATIVE — it marks the field MovedOut regardless of
whether downstream actually consumes it.  If that over-reporting ever
surfaces as real trims in e2e observe (i.e. the ledger removes drops
the site wanted to emit), halt 3c and tighten chain detection before
landing further.  Phase 4 step 3b observe re-run found zero
per_field_still_disagrees records, so today the trim pass is a no-op
for real Drift code.  The e2e-visible trim count is logged via
`drift_debug.enabled("ownership_ledger")`.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Set, Tuple

from lang.driftc.core.types_core import TypeId, TypeTable
from lang.driftc import debug as drift_debug
from . import mir_nodes as M
from .ownership_ledger import DropVerdict, LiveStateMap
from .drop_policy_compute import compute_drop_policy


# Side-table entry shape, kept in sync with hir_to_mir.py's
# `_match_cleanup_per_field_drops` init / append.
SideTableEntry = Tuple[str, Tuple[Tuple[str, int], ...], Tuple[str, int], str, TypeId]


def trim_match_cleanup_by_ledger(
	func: M.MirFunc,
	*,
	type_table: Optional[TypeTable] = None,
	needs_drop_fn: Optional[Callable[[TypeId], bool]] = None,
) -> int:
	"""
	Consult `func._ownership_ledger.field_verdict_at(...)` for every
	per-field drop site 2 recorded, and remove the drop chain for
	each field the ledger classifies `MUST_NOT_DROP`.

	Either `type_table` (production — canonical `DropPolicy.needs_drop`
	is computed) or `needs_drop_fn` (unit tests — direct injection)
	must be supplied.  `type_table` takes precedence when both are
	given.

	Returns the number of drop-locals trimmed.  A non-zero return
	means site-2 legacy disagreed with the ledger on at least one
	field — per K's guardrail, investigate before accepting.

	No-ops when the side table is empty or the ledger is unset
	(e.g. builds that skipped `build_ledger`).
	"""
	entries: List[SideTableEntry] = getattr(func, "_match_cleanup_per_field_drops", []) or []
	if not entries:
		return 0
	ledger: Optional[LiveStateMap] = getattr(func, "_ownership_ledger", None)
	if ledger is None:
		return 0
	if type_table is not None:
		def _needs_drop(ty: TypeId) -> bool:
			return bool(compute_drop_policy(type_table, ty).needs_drop)
	elif needs_drop_fn is not None:
		_needs_drop = needs_drop_fn
	else:
		raise ValueError("trim_match_cleanup_by_ledger requires type_table or needs_drop_fn")
	trimmed_drop_locals: Set[str] = set()
	for scrut_local, field_path, cleanup_point, drop_local, cleanup_fty in entries:
		if not scrut_local:
			continue
		verdict = ledger.field_verdict_at(
			cleanup_point,
			scrut_local,
			field_path,
			needs_drop=_needs_drop(cleanup_fty),
		)
		if verdict is DropVerdict.MUST_NOT_DROP:
			trimmed_drop_locals.add(drop_local)
	if not trimmed_drop_locals:
		return 0
	_excise_drop_chains(func, trimmed_drop_locals)
	if drift_debug.enabled("ownership_ledger"):
		import sys
		for name in sorted(trimmed_drop_locals):
			sys.stderr.write(
				"[drift:ownership_ledger_trim] "
				f"fn={func.name} drop_local={name} action=remove\n"
			)
	return len(trimmed_drop_locals)


def _excise_drop_chains(func: M.MirFunc, drop_locals: Set[str]) -> None:
	"""
	Remove every MIR instruction that materializes or destroys any
	local in `drop_locals`.

	The per-field drop chain for a single `drop_local` is:

	  setup (emitted by site 2 at the cleanup point):
	      tN  = VariantGetFieldAddr(...)
	      tN+1 = LoadRef(ptr=tN, ...)
	      StoreLocal(local=drop_local, value=tN+1)
	  destroy (emitted at arm-end + every scope-exit fallback):
	      tM  = MoveOut(local=drop_local, ...)
	      DropValue(value=tM, ...)

	We remove the `StoreLocal` plus every `MoveOut(local=drop_local)`
	paired with the adjacent `DropValue(value=moveout.dest)`.  The
	`VariantGetFieldAddr` + `LoadRef` setup and any stray temporary
	stays put — harmless without the `StoreLocal`, handled by
	downstream dead-instruction cleanup.
	"""
	for block in func.blocks.values():
		kept: List[M.MInstr] = []
		skip_next_dropvalue_for: Set[str] = set()
		for instr in block.instructions:
			if isinstance(instr, M.StoreLocal) and instr.local in drop_locals:
				continue
			if isinstance(instr, M.MoveOut) and instr.local in drop_locals:
				skip_next_dropvalue_for.add(instr.dest)
				continue
			if isinstance(instr, M.DropValue) and instr.value in skip_next_dropvalue_for:
				skip_next_dropvalue_for.discard(instr.value)
				continue
			kept.append(instr)
		block.instructions = kept
	for name in drop_locals:
		if name in func.locals:
			func.locals.remove(name)
		func.local_types.pop(name, None)
