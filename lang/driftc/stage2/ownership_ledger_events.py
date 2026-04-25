# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Decision-event log for the observational ownership ledger (Phase 3A, historical
context — sites 1 and 2 emission decisions).

Originally, site 1 (legacy `_emit_scope_drops`, retired in patch 6c) and site 2
(match-arm per-field cleanup) ran inside HIR→MIR, where the ledger could not
yet exist when they made their verdict.  Telemetry was needed because a
"skip drop" decision emits nothing, leaving no evidence in finished MIR that a
decision point existed.

Post Phase 4 patches 1–6: site 1 emission is decided by `cleanup_authoring`
querying `verdict_at` against the post-build ledger (no inline HIR-side
decisions remain), and site 2 carried-candidate emission is decided by
`match_cleanup_authoring` (patch 5).  Both passes emit their decision records
directly with `classification=agree` pre-baked.  This event log remains in
place for the legacy site-2 whole-scrutinee branch (still inline in HIR→MIR)
and for any future sites that need pre-ledger telemetry.

This module defines the tiny event record emitted by those sites at each
verdict, plus an append-only log that hangs off HIR→MIR for the reporter to
drain once the ledger is built.  No environment reads here — gating is done
at call sites.  This module is pure data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple


# Stable site identifiers.  Emission sites reference these names, not line
# numbers, so moving the code does not invalidate reporter records.
SITE_SCOPE_DROP = "scope_drop"
SITE_MATCH_CLEANUP = "match_cleanup"
SITE_STRING_ARC_RETURN = "string_arc_return"
SITE_DROP_BEFORE_OVERWRITE = "drop_before_overwrite"


# Stable verdict strings.  Mirrored by `DropVerdict` in `ownership_ledger` so
# the retrospective comparator can diff site-recorded verdicts against
# ledger-computed verdicts without importing the enum here (keeps this module
# import-cheap for HIR→MIR).
VERDICT_MUST_DROP = "must_drop"
VERDICT_MUST_NOT_DROP = "must_not_drop"


# Stable reason tags.  Callers pass one of these — new tags are added here,
# not invented ad hoc at the call site, so the triage aggregator can bucket
# without string-matching surprises.
REASON_MOVED = "moved"
REASON_NOT_DROP_NEEDING = "not_drop_needing"
REASON_DESTRUCTIBLE = "destructible"
REASON_NEEDS_DROP = "needs_drop"
REASON_FIELD_MOVED = "field_moved"
REASON_FIELD_NOT_DROP_NEEDING = "field_not_drop_needing"
REASON_FIELD_NEEDS_DROP = "field_needs_drop"
# Site 3 (`string_arc_return`) uses this reason when it skips emission
# for a local managed by Phase 3C drop-flag plumbing.  3C is the sole
# authority on those scope-exit drops; site 3 records the skip so the
# observe stream documents the responsibility split (without it, the
# missing site-3 emission would look like a regression in observe
# triage).
REASON_DROP_FLAG_OWNED = "drop_flag_owned"
# Phase 4 step 2 — `_scope_drop_verdict` distinguishes unconditional
# moves (the move was in the same scope as the local's declaration —
# definitely on this path; existing scope-drop SHOULD skip) from
# conditional moves (the move was in a nested scope — may not have
# executed; site 1 defers, 3C's flag-guarded drop covers).  Old code
# conflated both as `REASON_MOVED`.  The new tag lets observe triage
# distinguish "definite move, legacy-correct skip" from "potentially
# conditional move, 3C-handled skip."
REASON_MOVED_UNCONDITIONAL = "moved_unconditional"
# Phase 4 step 2 — replaces the silent fall-through that previously
# returned `MustNotDrop + REASON_NOT_DROP_NEEDING` for locals whose
# type is unknown to HIRToMIR (`local_types.get(L) is None`).  K
# flagged this as a blind spot: such locals were skipped without
# leaving a distinct trace.  The dedicated tag preserves the skip
# (no behaviour change) but lets the observe stream surface the
# case for follow-up diagnosis.
REASON_UNKNOWN_TYPE = "unknown_type"


@dataclass(frozen=True, slots=True)
class DropDecisionEvent:
	"""
	One decision made by an in-flight HIR→MIR site about whether to emit
	a drop for a given local at a given program point.

	`program_point` is `(block_name, instr_idx)` where the hypothetical
	drop would land if emitted — the reporter queries ledger state
	immediately *before* that point (see `LiveStateMap.verdict_at`).

	`local` is the named local under consideration.

	`field_path` (Phase 4 step 3b): optional sequence of
	`(ctor_name, field_index)` projections from `local` for per-field
	records.  Empty tuple `()` = whole-local record (existing
	behaviour, preserved for back-compat).  Non-empty = per-field
	record; the reporter compares against
	`LiveStateMap.field_verdict_at`.
	"""
	site: str
	fn_name: str
	program_point: Tuple[str, int]
	local: str
	verdict: str
	reason: str
	field_path: Tuple[Tuple[str, int], ...] = ()


@dataclass
class DropDecisionLog:
	"""
	Append-only log of decision events collected during one function's
	HIR→MIR lowering.

	One log per function.  The reporter drains the log after the function
	finishes lowering and the ledger has been built.  Draining is
	destructive — callers that want the events kept should copy first.
	"""
	fn_name: str
	events: List[DropDecisionEvent] = field(default_factory=list)

	def record(
		self,
		*,
		site: str,
		program_point: Tuple[str, int],
		local: str,
		verdict: str,
		reason: str,
		field_path: Tuple[Tuple[str, int], ...] = (),
	) -> None:
		self.events.append(
			DropDecisionEvent(
				site=site,
				fn_name=self.fn_name,
				program_point=program_point,
				local=local,
				verdict=verdict,
				reason=reason,
				field_path=field_path,
			)
		)

	def drain(self) -> List[DropDecisionEvent]:
		drained = self.events
		self.events = []
		return drained
