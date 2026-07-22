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
	string_return_releases,
)
from .cleanup_plan import CleanupPlan, anchor_instr, anchor_term
from .cleanup_payloads import (
	NullsafePayload,
	Site3Drop,
	Site3ReturnPayload,
	Site4Payload,
	StringReleasePayload,
)
from . import ownership_ledger_events as _ledger_events
from . import ownership_ledger_reporter as _reporter
from lang.driftc import debug as drift_debug


class PlannerStop(RuntimeError):
	"""An unexpected-population STOP surfaced during planning (e.g. a
	`synthetic_zero_back` null-safe store at the pre-string_arc surface,
	where the measured baseline is zero)."""


def build_destructible_plan(
	func: "M.MirFunc", *, type_table
) -> "Tuple[CleanupPlan, Dict[str, int], object]":
	"""Build + finalize the destructible + String-release `CleanupPlan` for `func`.

	Returns `(plan, census, c1_contribution)`. Non-emitting; reads
	`func._ownership_ledger` (ledger A) via `require_fresh_ledger`
	(hard-required at this slot) and mutates neither MIR nor ledger.

	The plan carries, per Return anchor, BOTH the site-3 destructible drop
	decision (`site="site3"`) AND the R3/R4 String scope-exit release
	decision (`site="string_release"`) — anchored to the SAME preserved
	`M.Return` — plus the per-`StoreLocal` null-safe / site-4 decisions.
	The unified Return authority (`return_cleanup_emitter`) consumes site3
	+ string_release atomically; `overwrite_cleanup` consumes nullsafe +
	site4.

	`c1_contribution` (B2+C S5) is the FROZEN ledger-A half of the C1
	string-scope-exit reconciliation — a `C1Contribution` when the audit
	is enabled, else `None` (zero-allocation, no telemetry dependency in
	codegen).  It reuses the SAME `move_state` / `string_return_releases`
	the string-release decisions use, and freezes `verdict_at`/`state_pre`
	at the original return coordinate using the `has_drop` axis the
	deferred single finalize consults.

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

	# The has_drop-based needs_drop axis the deferred single finalize's C1
	# loop uses (`string_arc._ledger_needs_drop`).  DISTINCT from the
	# DropPolicy axis the R3 release ELISION uses inside
	# `string_return_releases`; the C1 verdict/state freeze must match
	# finalize byte-for-byte, so it is reproduced here verbatim.
	def _ledger_needs_drop(local: str) -> bool:
		ty = local_types.get(local)
		if ty is None:
			return False
		try:
			return bool(type_table.has_drop(ty))
		except Exception:
			return False

	audit_on = _reporter.string_arc_audit_enabled()
	_sorted_string_locals = tuple(sorted(string_locals))
	_c1_boundaries: list = []

	plan = CleanupPlan(func.name)
	census: Dict[str, int] = {
		"site3_returns": 0,
		"site3_locals": 0,
		"site4_must_drop": 0,
		"site4_must_not_drop": 0,
		"nullsafe": 0,
		"nullsafe_synthetic": 0,
		"string_release_returns": 0,
		"string_release_locals": 0,
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
				# Site-4 observe-mode telemetry (S8 re-home): string_arc's
				# per-StoreLocal `[drift:ownership_ledger]` record — lost when
				# the S4 migration neutered its site-4 arm — now emits HERE,
				# where the verdict is decided.  Same site tag / verdict /
				# reason / point / has_drop needs_drop axis as the original
				# emission, so observe re-runs keep catching any new
				# bucket-5/6 class.  Debug-gated; zero cost otherwise.
				if drift_debug.enabled("ownership_ledger"):
					_reporter.check(
						ledger,
						fn_name=func.name,
						site=_ledger_events.SITE_DROP_BEFORE_OVERWRITE,
						point=(bname, idx),
						local=local,
						site_verdict=(
							_ledger_events.VERDICT_MUST_DROP
							if verdict is DropVerdict.MUST_DROP
							else _ledger_events.VERDICT_MUST_NOT_DROP
						),
						site_reason=(
							_ledger_events.REASON_NEEDS_DROP
							if verdict is DropVerdict.MUST_DROP
							else _ledger_events.REASON_NOT_DROP_NEEDING
						),
						needs_drop=_ledger_needs_drop,
						emit=_reporter.stderr_emit,
					)

		if isinstance(blk.terminator, M.Return):
			_coord = anchor_term(bname, len(blk.instructions))
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
				coord=_coord,
				site="site3",
				fields={"value": blk.terminator.value},
				type_bindings={_d.local: _d.ty for _d in drops},
				payload=Site3ReturnPayload(drops=drops),
			)
			census["site3_returns"] += 1
			census["site3_locals"] += len(drops)

			# R3/R4 String scope-exit releases — the ORDERED decision the
			# unified Return authority emits as the string-release band
			# BEFORE the site-3 tail (same object, second site).  Reuses
			# THIS block's `move_state` (the shared frozen bookkeeping).
			released = string_return_releases(
				func, blk,
				ledger=ledger,
				type_table=type_table,
				string_locals=string_locals,
				string_ty=string_ty,
				move_state=move_state[bname],
			)
			plan.add(
				obj=blk.terminator,
				coord=_coord,
				site="string_release",
				fields={"value": blk.terminator.value},
				type_bindings={_sl: string_ty for _sl in released},
				payload=StringReleasePayload(locals=tuple(released)),
			)
			census["string_release_returns"] += 1
			census["string_release_locals"] += len(released)

			# FROZEN C1 ledger-A half (audit only): the boundary universe +
			# the same `released`/`skipped` string_arc recorded, plus the
			# per-(point, local) ledger-A verdict/raw-state at the ORIGINAL
			# return coordinate.  Bijection: the C1 `released` IS the emitted
			# R3 release set (same `string_return_releases` result).
			if audit_on:
				_point = (bname, len(blk.instructions))
				_released_set = set(released)
				_verdicts = tuple(
					(_sl, ledger.verdict_at(_point, _sl, needs_drop=_ledger_needs_drop(_sl)))
					for _sl in _sorted_string_locals
				)
				_raws = tuple(
					(_sl, ledger.state_pre(_point, _sl))
					for _sl in _sorted_string_locals
				)
				_c1_boundaries.append(_reporter.C1BoundaryFrozen(
					point=_point,
					string_locals=_sorted_string_locals,
					skipped=tuple(_sl for _sl in _sorted_string_locals if _sl not in _released_set),
					released=tuple(released),
					verdicts=_verdicts,
					raw_states=_raws,
				))

	plan.validate_and_freeze(func)
	c1_contribution = (
		_reporter.C1Contribution(fn_name=func.name, boundaries=tuple(_c1_boundaries))
		if audit_on
		else None
	)
	if c1_contribution is not None:
		# S5 closure — validate the frozen contribution structurally AND
		# prove the C1↔plan bijection HERE, while the original Return
		# coordinates are still valid ("built in the same loop" is not a
		# guard; this is the structural cross-check).
		_reporter.validate_c1_contribution(c1_contribution)
		crosscheck_c1_against_plan(plan, c1_contribution)
	return plan, census, c1_contribution


def crosscheck_c1_against_plan(plan: "CleanupPlan", contribution) -> None:
	"""S5 closure — prove a BIJECTION between the frozen C1 boundaries and
	the plan's `string_release` decisions by original Return coordinate,
	including EXACT ORDERED equality of each boundary's `released` tuple and
	the corresponding `StringReleasePayload.locals`.  The C1 `released` set
	IS the emitter's R3 release set; any drift between the audit's frozen
	half and the plan the emitter consumes fails closed at the plan slot."""
	dec_by_point: dict = {}
	for dec in plan.decisions_for_site("string_release"):
		pt = (dec.coord.block, dec.coord.orig_index)
		if pt in dec_by_point:
			raise AssertionError(
				f"c1/plan crosscheck[{contribution.fn_name}]: duplicate "
				f"string_release decision at {pt}"
			)
		dec_by_point[pt] = dec
	points = [b.point for b in contribution.boundaries]
	if len(set(points)) != len(points):
		raise AssertionError(
			f"c1/plan crosscheck[{contribution.fn_name}]: duplicate C1 "
			f"boundary points {points}"
		)
	if set(points) != set(dec_by_point):
		raise AssertionError(
			f"c1/plan crosscheck[{contribution.fn_name}]: C1 boundaries and "
			f"plan string_release decisions are not in bijection "
			f"(c1-only={sorted(set(points) - set(dec_by_point))}, "
			f"plan-only={sorted(set(dec_by_point) - set(points))})"
		)
	for b in contribution.boundaries:
		payload = dec_by_point[b.point].payload
		if tuple(payload.locals) != tuple(b.released):
			raise AssertionError(
				f"c1/plan crosscheck[{contribution.fn_name}]: release drift at "
				f"{b.point} — plan payload {tuple(payload.locals)} != frozen C1 "
				f"released {tuple(b.released)} (ordered equality required)"
			)


__all__ = ("PlannerStop", "build_destructible_plan", "crosscheck_c1_against_plan")
