# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Decision-event log for the observational ownership ledger (Phase 3A, sites 1
and 2).

Sites 1 (`_emit_scope_drops`) and 2 (match-arm per-field cleanup) run inside
HIR→MIR, which means the ledger cannot yet exist when they make their verdict.
Retrospective inference from finished MIR is insufficient: a site that decides
"skip drop" emits nothing, so the finished MIR carries no evidence that a
decision point existed at all — and the leak case is exactly what observation
is meant to catch.

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


@dataclass(frozen=True, slots=True)
class DropDecisionEvent:
	"""
	One decision made by an in-flight HIR→MIR site about whether to emit
	a drop for a given local at a given program point.

	`program_point` is `(block_name, instr_idx)` where the hypothetical
	drop would land if emitted — the reporter queries ledger state
	immediately *before* that point (see `LiveStateMap.verdict_at`).

	`local` is the named local under consideration.  Field-move decisions
	use the synthesized binder local name (e.g. the `__match_binder_*`
	form) rather than an abstract "scrutinee.field" path, because the
	ledger tracks named locals only.
	"""
	site: str
	fn_name: str
	program_point: Tuple[str, int]
	local: str
	verdict: str
	reason: str


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
	) -> None:
		self.events.append(
			DropDecisionEvent(
				site=site,
				fn_name=self.fn_name,
				program_point=program_point,
				local=local,
				verdict=verdict,
				reason=reason,
			)
		)

	def drain(self) -> List[DropDecisionEvent]:
		drained = self.events
		self.events = []
		return drained
