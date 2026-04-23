# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Disagreement reporter for the observational ownership ledger (Phase 3A).

Two APIs, both pure:

- `compare_events(events, ledger, *, needs_drop, emit)` — retrospective
  comparator for sites 1 (scope-drop) and 2 (match-arm cleanup).  The
  recording sites inside HIR→MIR deposit `DropDecisionEvent` records as
  they run; this function walks the drained log, queries the ledger at
  each event's program point, and classifies the result.

- `check(ledger, *, site, point, local, site_verdict, site_reason,
  needs_drop, emit)` — prospective hook for sites 3 (string-arc return
  cleanup) and 4 (drop-before-overwrite), which run in a pass that has
  the full ledger already built and can query at decision time.

Both APIs invoke `emit(record)` for every observation — agreement *and*
disagreement — so an aggregator can see coverage, not just failures.  The
module itself does no I/O and reads no environment variables; gating is
the caller's responsibility at the site.  A convenience `stderr_emit`
factory is provided for the production path and produces one JSON line
per record.

Disagreement classification (stable strings, mirrored in triage
tooling):

- ``agree`` — site and ledger produced the same verdict.
- ``path_dependent`` — ledger returned `PathDependent` (raw state
  `MaybeUninit`).  Site chose a verdict; this is 3C queue material, NOT
  a site-vs-ledger bug.
- ``semantic_equivalent`` — site emits a drop on a `Tombstoned` local.
  Safe no-op; counted separately so the ledger-stricter bucket stays
  clean of non-bugs.
- ``ledger_stricter`` — ledger says `MustDrop`, site says `MustNotDrop`.
  Potential leak; the case that motivates observation.
- ``site_stricter`` — ledger says `MustNotDrop` (not tombstoned), site
  says `MustDrop`.  Suggests the ledger is missing an axis the site
  already accounts for; 3A ledger bug to fix before 3B.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Optional, Tuple

from .ownership_ledger import DropVerdict, LiveState, LiveStateMap
from .ownership_ledger_events import (
	DropDecisionEvent,
	VERDICT_MUST_DROP,
	VERDICT_MUST_NOT_DROP,
)


# Stable classification strings.  Consumed by triage tooling; keep in
# sync with `work/ownership-ledger/design.md` gate criteria.
CLASS_AGREE = "agree"
CLASS_PATH_DEPENDENT = "path_dependent"
CLASS_SEMANTIC_EQUIVALENT = "semantic_equivalent"
CLASS_LEDGER_STRICTER = "ledger_stricter"
CLASS_SITE_STRICTER = "site_stricter"


Emit = Callable[["DisagreementRecord"], None]
NeedsDrop = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class DisagreementRecord:
	"""
	One observation — agreement or disagreement — for a single site
	decision at a single program point.

	`raw_state` is the pre-state the ledger saw (before the site's
	hypothetical emission).  Exposing raw state alongside the verdict
	lets triage separate semantic-equivalence cases (Tombstoned drop) from
	genuine leak-side disagreements without having to re-query the ledger.
	"""
	fn_name: str
	site: str
	program_point: Tuple[str, int]
	local: str
	site_verdict: str
	site_reason: str
	ledger_verdict: str
	raw_state: str
	classification: str


def classify_verdicts(
	*,
	site_verdict: str,
	ledger_verdict: DropVerdict,
	raw_state: LiveState,
) -> str:
	"""
	Map (site verdict string, ledger verdict enum, raw state) → class tag.

	Semantic-equivalence short-circuits `ledger_stricter`: if the site
	emits a drop but the local is `Tombstoned`, the emission is a safe
	runtime no-op and we do not count it as a leak-side false alarm.
	"""
	if ledger_verdict is DropVerdict.PATH_DEPENDENT:
		return CLASS_PATH_DEPENDENT
	ledger_str = _verdict_to_str(ledger_verdict)
	if site_verdict == ledger_str:
		return CLASS_AGREE
	if site_verdict == VERDICT_MUST_DROP and raw_state is LiveState.TOMBSTONED:
		return CLASS_SEMANTIC_EQUIVALENT
	if ledger_str == VERDICT_MUST_DROP and site_verdict == VERDICT_MUST_NOT_DROP:
		return CLASS_LEDGER_STRICTER
	return CLASS_SITE_STRICTER


def _verdict_to_str(v: DropVerdict) -> str:
	if v is DropVerdict.MUST_DROP:
		return VERDICT_MUST_DROP
	if v is DropVerdict.MUST_NOT_DROP:
		return VERDICT_MUST_NOT_DROP
	return "path_dependent"


def compare_events(
	events: Iterable[DropDecisionEvent],
	ledger: LiveStateMap,
	*,
	needs_drop: NeedsDrop,
	emit: Optional[Emit] = None,
) -> list[DisagreementRecord]:
	"""
	Retrospective comparison for sites 1 and 2.

	`needs_drop(local)` supplies the DropPolicy.needs_drop axis for the
	local's type.  Callers already have a policy handle; passing it
	through by local name keeps the ledger module pure.  Returns the
	full list of records so the caller can summarise or persist.
	"""
	records: list[DisagreementRecord] = []
	for event in events:
		record = _compare_one(
			fn_name=event.fn_name,
			site=event.site,
			point=event.program_point,
			local=event.local,
			site_verdict=event.verdict,
			site_reason=event.reason,
			ledger=ledger,
			needs_drop=needs_drop,
		)
		records.append(record)
		if emit is not None:
			emit(record)
	return records


def check(
	ledger: LiveStateMap,
	*,
	fn_name: str,
	site: str,
	point: Tuple[str, int],
	local: str,
	site_verdict: str,
	site_reason: str,
	needs_drop: NeedsDrop,
	emit: Optional[Emit] = None,
) -> DisagreementRecord:
	"""
	Prospective hook for sites 3 and 4.

	Sites 3/4 run in the `string_arc` pass, which operates on a finished
	MIR; the ledger is already built and the site can query at decision
	time.  The returned record is classified identically to retrospective
	events — so downstream triage aggregates both APIs through one
	schema.
	"""
	record = _compare_one(
		fn_name=fn_name,
		site=site,
		point=point,
		local=local,
		site_verdict=site_verdict,
		site_reason=site_reason,
		ledger=ledger,
		needs_drop=needs_drop,
	)
	if emit is not None:
		emit(record)
	return record


def _compare_one(
	*,
	fn_name: str,
	site: str,
	point: Tuple[str, int],
	local: str,
	site_verdict: str,
	site_reason: str,
	ledger: LiveStateMap,
	needs_drop: NeedsDrop,
) -> DisagreementRecord:
	raw = ledger.state_pre(point, local)
	ledger_verdict = ledger.verdict_at(point, local, needs_drop=needs_drop(local))
	classification = classify_verdicts(
		site_verdict=site_verdict,
		ledger_verdict=ledger_verdict,
		raw_state=raw,
	)
	return DisagreementRecord(
		fn_name=fn_name,
		site=site,
		program_point=point,
		local=local,
		site_verdict=site_verdict,
		site_reason=site_reason,
		ledger_verdict=_verdict_to_str(ledger_verdict),
		raw_state=raw.value,
		classification=classification,
	)


def stderr_emit(record: DisagreementRecord) -> None:
	"""
	Default production emit: one JSON line per record to stderr, prefixed
	with a recognisable tag so log scraping is trivial.
	"""
	payload = dict(asdict(record))
	payload["program_point"] = list(payload["program_point"])
	sys.stderr.write("[drift:ownership_ledger] " + json.dumps(payload, sort_keys=True) + "\n")


def collecting_emit() -> Tuple[Emit, list[DisagreementRecord]]:
	"""
	Test convenience: returns `(emit, records)` where `emit` appends to
	the returned list.  Useful for wiring a reporter into a test without
	capturing stderr.
	"""
	bucket: list[DisagreementRecord] = []
	def _emit(r: DisagreementRecord) -> None:
		bucket.append(r)
	return _emit, bucket
