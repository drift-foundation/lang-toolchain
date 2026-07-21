# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Standalone NON-EMITTING destructible cleanup planner (B2+C Milestone B).

Runs at the pre-mutation `string_arc` slot over the ORIGINAL post-
cleanup-authoring MIR and the attached fresh ledger A. It builds ONE
`CleanupPlan` of immutable payloads — site-3 Return drops, site-4
drop-before-overwrite verdicts, null-safe overwrites — finalizes it
against that original snapshot, and MUTATES NOTHING (no MIR change, no
ledger dirty-bit transition, no ledger build).

It reuses the shared `destructible_authority` (the SAME closed authority
`string_arc` delegates to) — no copied or re-approximated logic. The
site-4 missing-ledger / PATH_DEPENDENT and the site-3 widening tripwires
fire here at PLANNING time, exactly as in the emitter.

This module currently has NO production consumer. The temporary
env-gated driver census wiring used to validate the S2 population counts
was REMOVED after the gate; the planner is intentionally unwired until
S3+ production planning. When wired, the emitters will consume the
FROZEN PLAN via the S1 `EmitterPhase` postflight lifecycle — they must
NOT recompute the decisions.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from . import mir_nodes as M
from .ledger_cache import require_fresh_ledger
from .ownership_ledger import DropVerdict
from .string_ownership_analysis import classify_string_array_locals
from .destructible_authority import (
	DropClassifier,
	classify_destructible_locals,
	compute_assigned_in,
	compute_return_move_state,
	compute_store_defs,
	flag_managed_at_return,
	site3_return_drops,
	site4_verdict,
)
from .cleanup_plan import CleanupPlan, anchor_instr, anchor_term
from .cleanup_payloads import (
	NullsafePayload,
	Site3Drop,
	Site3ReturnPayload,
	Site4Payload,
)


class PlannerStop(RuntimeError):
	"""An unexpected-population STOP surfaced during planning (e.g. a
	`synthetic_zero_back` null-safe store at the pre-string_arc surface,
	where the measured baseline is zero)."""


def build_destructible_plan(
	func: "M.MirFunc", *, type_table
) -> "Tuple[CleanupPlan, Dict[str, int]]":
	"""Build + finalize the destructible `CleanupPlan` for `func`.

	Returns `(plan, census)`. Non-emitting; reads `func._ownership_ledger`
	(ledger A) via `require_fresh_ledger` (hard-required at this slot) and
	mutates neither MIR nor ledger.
	Raises (fail-closed) on the site-4 missing-ledger / PATH_DEPENDENT and
	site-3 widening tripwires, and on an unexpected marked-synthetic
	null-safe store (`PlannerStop`).
	"""
	# Ledger A is REQUIRED at the planner's pre-mutation slot — hard-assert
	# it is attached and fresh, independent of whether any site-4 candidate
	# exists (a `None` ledger would silently flow through site-3's no-ledger
	# path and make the missing-ledger tripwire population-dependent).
	ledger = require_fresh_ledger(func, "destructible_planner")
	local_types = func.local_types
	string_ty, string_locals, array_locals = classify_string_array_locals(func, type_table)
	clf = DropClassifier(type_table)
	destructible_locals, nullsafe_destructible_locals = classify_destructible_locals(
		func, clf,
		local_types=local_types,
		string_locals=string_locals,
		array_locals=array_locals,
	)
	store_defs = compute_store_defs(func)
	assigned_in = compute_assigned_in(func, store_defs)
	move_state = compute_return_move_state(
		func,
		destructible_locals=destructible_locals,
		string_ty=string_ty,
	)
	flag_managed = flag_managed_at_return(func, destructible_locals)

	plan = CleanupPlan(func.name)
	census: Dict[str, int] = {
		"site3_returns": 0,
		"site3_locals": 0,
		"site4_must_drop": 0,
		"site4_must_not_drop": 0,
		"nullsafe": 0,
		"nullsafe_synthetic": 0,
	}

	for bname, blk in func.blocks.items():
		for idx, instr in enumerate(blk.instructions):
			if not isinstance(instr, M.StoreLocal):
				continue
			local = instr.local
			# Null-safe check first (mirrors string_arc's branch order: the
			# nullsafe arm precedes the destructible/site-4 arm).
			if local in nullsafe_destructible_locals:
				if getattr(instr, "synthetic_zero_back", False):
					census["nullsafe_synthetic"] += 1
					raise PlannerStop(
						f"destructible_planner: unexpected synthetic_zero_back "
						f"null-safe StoreLocal at {func.name} {bname}:{idx} "
						f"(local {local!r}); the measured baseline synthetic "
						f"population is zero"
					)
				# Classification guarantees the type exists → index, not .get.
				ty = local_types[local]
				plan.add(
					obj=instr,
					coord=anchor_instr(bname, idx),
					site="nullsafe",
					fields={"local": local, "value": instr.value},
					type_bindings={local: ty},
					payload=NullsafePayload(local=local, ty=ty),
				)
				census["nullsafe"] += 1
			elif local in destructible_locals:
				ty = local_types[local]
				# Closed authority: verdict AND canonical needs_drop axis.
				verdict, needs_drop = site4_verdict(
					ledger,
					fn_name=func.name,
					block_name=bname,
					instr_idx=idx,
					local=local,
					local_ty=ty,
					type_table=type_table,
				)
				plan.add(
					obj=instr,
					coord=anchor_instr(bname, idx),
					site="site4",
					fields={"local": local, "value": instr.value},
					type_bindings={local: ty},
					payload=Site4Payload(
						local=local, ty=ty, needs_drop=needs_drop, verdict=verdict
					),
				)
				if verdict is DropVerdict.MUST_DROP:
					census["site4_must_drop"] += 1
				else:
					census["site4_must_not_drop"] += 1

		if isinstance(blk.terminator, M.Return):
			ordered = site3_return_drops(
				func, blk,
				ledger=ledger,
				type_table=type_table,
				destructible_locals=destructible_locals,
				local_types=local_types,
				move_state=move_state[bname],
				assigned_in=assigned_in,
				store_defs=store_defs,
				flag_managed=flag_managed,
			)
			drops = tuple(
				Site3Drop(local=_l, ty=local_types[_l]) for _l in ordered
			)
			plan.add(
				obj=blk.terminator,
				coord=anchor_term(bname, len(blk.instructions)),
				site="site3",
				fields={"value": blk.terminator.value},
				type_bindings={_d.local: _d.ty for _d in drops},
				payload=Site3ReturnPayload(drops=drops),
			)
			census["site3_returns"] += 1
			census["site3_locals"] += len(drops)

	plan.validate_and_freeze(func)
	return plan, census


__all__ = ("PlannerStop", "build_destructible_plan")
