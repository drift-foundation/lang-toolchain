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
drop-verdict APIs above do no I/O and read no environment variables;
gating is the caller's responsibility at the site.  A convenience
`stderr_emit` factory is provided for the production path and produces
one JSON line per record.  (The B-arch-0 string-stake section appended
below IS env-gated and does its own JSONL I/O — see its banner comment.)

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

	`field_path` (Phase 4 step 3b): empty tuple `()` for whole-local
	records (back-compat); non-empty tuple of `(ctor_name,
	field_index)` projections for per-field records.  When non-empty,
	the ledger comparison used `field_verdict_at` instead of
	`verdict_at`.  Aggregator buckets per-field records into
	`per_field_still_disagrees` (when classification != agree)
	separately from whole-local `per_field_gap` records.
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
	field_path: Tuple[Tuple[str, int], ...] = ()


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
			field_path=event.field_path,
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
	field_path: Tuple[Tuple[str, int], ...] = (),
) -> DisagreementRecord:
	"""
	Prospective hook for sites 3 and 4.

	Sites 3/4 run in the `string_arc` pass, which operates on a finished
	MIR; the ledger is already built and the site can query at decision
	time.  The returned record is classified identically to retrospective
	events — so downstream triage aggregates both APIs through one
	schema.

	`field_path` (Phase 4 step 3b): empty for whole-local checks
	(existing behaviour); non-empty tuple of `(ctor_name,
	field_index)` projections to query `field_verdict_at` instead of
	`verdict_at`.
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
		field_path=field_path,
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
	field_path: Tuple[Tuple[str, int], ...] = (),
) -> DisagreementRecord:
	# Phase 4 step 3b: when `field_path` is non-empty, query
	# `field_state_pre` / `field_verdict_at` on the per-field tracker
	# instead of the whole-local one.  Records are otherwise shaped
	# identically; the aggregator distinguishes per-field records by
	# the non-empty `field_path` field.
	if field_path:
		raw = ledger.field_state_pre(point, local, field_path)
		ledger_verdict = ledger.field_verdict_at(
			point, local, field_path, needs_drop=needs_drop(local)
		)
	else:
		raw = ledger.state_pre(point, local)
		ledger_verdict = ledger.verdict_at(
			point, local, needs_drop=needs_drop(local)
		)
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
		field_path=field_path,
	)


def stderr_emit(record: DisagreementRecord) -> None:
	"""
	Default production emit: one JSON line per record to stderr, prefixed
	with a recognisable tag so log scraping is trivial.
	"""
	payload = dict(asdict(record))
	payload["program_point"] = list(payload["program_point"])
	# field_path is a tuple-of-tuples — `asdict` converts the outer
	# tuple to list of tuples; serialize each inner pair as a 2-list
	# so the JSON form is `[["Some", 0]]` (back-compat: empty for
	# whole-local records).
	payload["field_path"] = [list(p) for p in payload.get("field_path", ())]
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


# ═══════════════════════════════════════════════════════════════════
# B-arch-0: string_arc differential stake reporter (Scope B §11.2).
#
# OBSERVATIONAL ONLY.  Off by default; enabled by DRIFT_STRING_ARC_AUDIT=1.
# When disabled, string_arc constructs no audit object and emits nothing —
# behavior-identical compilation.  When enabled, the audit records one
# StringStakeEvent per string_arc-emitted refcount instruction (tagged at
# the emission point from the CLOSED site_class enumeration below), then
# diffs the pass's input ledger (L_pre) against a ledger rebuilt on its
# output MIR (L_post) and classifies every divergence into the closed
# C1-C4/UNCLASSIFIED set.  It makes NO fixes and changes NO verdicts.
# ═══════════════════════════════════════════════════════════════════

import atexit as _atexit
import os as _os

# Event kinds.
STAKE_RETAIN = "RETAIN"
STAKE_RELEASE = "RELEASE"
STAKE_MOVEOUT_EXPANSION = "MOVEOUT_EXPANSION"

# CLOSED site_class enumeration — every string_arc emission point tags
# itself with one of these AT the emission site (never inferred).  The
# enumeration itself is a B-arch-0 deliverable: relative to the plan's
# draft list, `temp_lastuse_release` (SSA-temp last-use releases inside
# `_ensure_owned`/`_note_use`), `store_value_retain` (stake for the copy
# written by StoreLocal/StoreRef/ArrayIndexStore), and
# `value_position_retain` (ctor/exc-ABI/array-elem value positions) were
# added because real emission sites did not fit the draft tags;
# `destructor_self` is retained for completeness but is structurally
# unused since Phase 4 site-3 sub-step 2 retired the site-local
# destructor-self path.  A note() with a tag outside this set is counted
# UNTAGGED — itself a finding.
SITE_CLASS_CALL_ARG_RETAIN = "call_arg_retain"
SITE_CLASS_STORE_VALUE_RETAIN = "store_value_retain"
SITE_CLASS_VALUE_POSITION_RETAIN = "value_position_retain"
SITE_CLASS_RETURN_RETAIN_SITE3 = "return_retain_site3"
SITE_CLASS_OVERWRITE_RELEASE = "overwrite_release"
SITE_CLASS_SCOPE_EXIT_RELEASE = "scope_exit_release"
SITE_CLASS_TEMP_LASTUSE_RELEASE = "temp_lastuse_release"
SITE_CLASS_DROP_BEFORE_OVERWRITE_SITE4 = "drop_before_overwrite_site4"
SITE_CLASS_MOVEOUT_EXPANSION = "moveout_expansion"
SITE_CLASS_DESTRUCTOR_SELF = "destructor_self"

STRING_ARC_SITE_CLASSES = frozenset({
	SITE_CLASS_CALL_ARG_RETAIN,
	SITE_CLASS_STORE_VALUE_RETAIN,
	SITE_CLASS_VALUE_POSITION_RETAIN,
	SITE_CLASS_RETURN_RETAIN_SITE3,
	SITE_CLASS_OVERWRITE_RELEASE,
	SITE_CLASS_SCOPE_EXIT_RELEASE,
	SITE_CLASS_TEMP_LASTUSE_RELEASE,
	SITE_CLASS_DROP_BEFORE_OVERWRITE_SITE4,
	SITE_CLASS_MOVEOUT_EXPANSION,
	SITE_CLASS_DESTRUCTOR_SELF,
})

# Divergence classes (closed set, plan §11.2).
DIV_C1_RELEASE_WITHOUT_MUST_DROP = "c1_release_without_must_drop"
DIV_C1_MUST_DROP_WITHOUT_RELEASE = "c1_must_drop_without_release"
DIV_C1_PATH_DEPENDENT = "c1_path_dependent"
DIV_C2_INVISIBLE_STAKE = "c2_invisible_stake"
DIV_C3_MOVEOUT_NOT_OWNED = "c3_moveout_not_owned"
DIV_C4_ALLOWLISTED = "c4_allowlisted"
DIV_PRE_POST_VERDICT_DRIFT = "pre_post_verdict_drift"
# Hard failure: the L_post snapshot could not be built for this fn.  The
# "exactly two snapshots" contract (plan §11.2) is broken for that fn —
# the corpus gate MUST treat any nonzero count as a failed run (the
# UNCLASSIFIED=0 result is not trustworthy without the L_post half).
DIV_POST_LEDGER_BUILD_FAILED = "post_ledger_build_failed"
DIV_UNTAGGED = "untagged"
DIV_UNCLASSIFIED = "unclassified"

# Anti-telemetry-creep bound: counts are always exact; detailed records
# are truncated at this many PER CLASS PER FN-AUDIT (a per-corpus-run
# cap would need cross-process state; the per-fn cap is stricter than
# the plan's per-run wording and serves the same anti-creep purpose).
_DETAIL_CAP_PER_CLASS = 50

_AUDIT_ENV = "DRIFT_STRING_ARC_AUDIT"
_AUDIT_FILE_ENV = "DRIFT_STRING_ARC_AUDIT_FILE"


def string_arc_audit_enabled() -> bool:
	return _os.environ.get(_AUDIT_ENV) in ("1", "true", "True")


@dataclass(frozen=True, slots=True)
class StringStakeEvent:
	"""One string_arc-emitted refcount instruction (or MoveOut expansion).

	`pre_point` is the (block, index) of the SOURCE instruction being
	rewritten when the emission happened — the L_pre-queryable anchor.
	`post_point` is the (block, index) the emitted instruction landed at
	in the OUTPUT MIR.  Return-boundary emissions use the established
	site-3 convention `(block, len(original_instructions))` as pre_point.
	"""
	fn_name: str
	kind: str
	subject: str
	site_class: str
	pre_point: Tuple[str, int]
	post_point: Tuple[str, int]


@dataclass(frozen=True, slots=True)
class _ReturnBoundary:
	"""Marker for one Return-terminator cleanup boundary: the universe
	C1 quantifies over (string locals × released-vs-skipped)."""
	point: Tuple[str, int]
	string_locals: Tuple[str, ...]
	skipped: Tuple[str, ...]


# Process-wide aggregate across all audited functions; flushed once at
# exit as a single JSON line so corpus tooling can sum per-compile
# aggregates without parsing per-event records.
_GLOBAL_AGGREGATE: dict = {}
_GLOBAL_FLUSH_REGISTERED = False


def _audit_stream():
	path = _os.environ.get(_AUDIT_FILE_ENV)
	if path:
		return open(path, "a", encoding="utf-8")
	return None  # caller falls back to sys.stderr and must not close it


def _emit_line(payload: dict) -> None:
	line = "[drift:string_arc_audit] " + json.dumps(payload, sort_keys=True) + "\n"
	f = _audit_stream()
	if f is not None:
		with f:
			f.write(line)
	else:
		sys.stderr.write(line)


def _flush_global_aggregate() -> None:
	if _GLOBAL_AGGREGATE:
		_emit_line({"record": "aggregate", **_GLOBAL_AGGREGATE})


def _bump(agg: dict, key: str, n: int = 1) -> None:
	agg[key] = agg.get(key, 0) + n


class StringArcAudit:
	"""Per-function collector + differential classifier.

	Created by `insert_string_arc` ONLY when the audit env is set; every
	recording call in the pass is guarded on the audit object being
	non-None, so the disabled path allocates nothing and emits nothing.
	"""

	def __init__(self, fn_name: str) -> None:
		self.fn_name = fn_name
		self.events: list[StringStakeEvent] = []
		self.return_boundaries: list[_ReturnBoundary] = []
		self.untagged = 0

	def note(
		self,
		kind: str,
		subject: str,
		site_class: str,
		*,
		pre_point: Tuple[str, int],
		post_point: Tuple[str, int],
	) -> None:
		if site_class not in STRING_ARC_SITE_CLASSES:
			self.untagged += 1
			site_class = "UNTAGGED:" + site_class
		self.events.append(StringStakeEvent(
			fn_name=self.fn_name,
			kind=kind,
			subject=subject,
			site_class=site_class,
			pre_point=pre_point,
			post_point=post_point,
		))

	def note_return_boundary(
		self,
		point: Tuple[str, int],
		*,
		string_locals: Iterable[str],
		skipped: Iterable[str],
	) -> None:
		self.return_boundaries.append(_ReturnBoundary(
			point=point,
			string_locals=tuple(sorted(string_locals)),
			skipped=tuple(sorted(skipped)),
		))

	# ── classification ────────────────────────────────────────────

	def finalize(
		self,
		*,
		l_pre,
		l_post,
		needs_drop: NeedsDrop,
	) -> dict:
		"""Run the C1-C4 comparisons and emit per-fn JSONL + fold into
		the process aggregate.  Returns the per-fn aggregate (tests use
		the return value; production consumers read the JSONL)."""
		agg: dict = {"fn": self.fn_name, "events": len(self.events)}
		details: list[dict] = []

		def _detail(cls: str, **kw) -> None:
			_bump(agg, cls)
			if sum(1 for d in details if d["class"] == cls) < _DETAIL_CAP_PER_CLASS:
				details.append({"class": cls, **kw})

		for ev in self.events:
			_bump(agg, "site_class:" + ev.site_class)
		if self.untagged:
			agg[DIV_UNTAGGED] = self.untagged

		if l_pre is not None and l_post is None:
			# Review finding (B-arch-0 acceptance): a swallowed
			# build_ledger failure previously skipped the post-snapshot
			# comparisons SILENTLY.  Hard-count it; the per-fn record is
			# force-emitted below regardless of the volume guard.
			_bump(agg, DIV_POST_LEDGER_BUILD_FAILED)

		if l_pre is None:
			# Pass-local invocation without an attached ledger (legal
			# per maybe_fresh_ledger's soft contract): events are
			# counted, differential classification is skipped, and the
			# aggregate says so rather than silently reporting zero
			# divergences.
			agg["skipped_no_ledger"] = 1
			self._emit(agg, details)
			return agg

		# C1: scope-exit releases vs ledger verdict, per Return boundary.
		released_at: dict = {}
		for ev in self.events:
			if ev.site_class == SITE_CLASS_SCOPE_EXIT_RELEASE:
				released_at.setdefault(ev.pre_point, set()).add(ev.subject)
		for rb in self.return_boundaries:
			released = released_at.get(rb.point, set())
			for local in rb.string_locals:
				verdict = l_pre.verdict_at(rb.point, local, needs_drop=needs_drop(local))
				raw = l_pre.state_pre(rb.point, local)
				was_released = local in released
				if verdict is DropVerdict.PATH_DEPENDENT:
					_bump(agg, DIV_C1_PATH_DEPENDENT)
					continue
				if was_released and verdict is DropVerdict.MUST_NOT_DROP:
					# C4 allowlist — REVISED interpretation (B-arch-1
					# stop finding, 2026-07-09): the late site-3
					# retain-wrap this allowlist was originally written
					# for is structurally extinct (zero
					# return_retain_site3 events corpus-wide; plain
					# returns MOVE via the Phase-4 alias walk).  What
					# this bucket actually catches is the RELEASE face:
					# a String local consumed into a RETURN-REACHING
					# COMPOSITE (ctor/call arg) that the lattice models
					# as Return-as-move (MOVED_OUT) while string_arc
					# copies at the value position and correctly
					# releases the still-owned local — i.e. the
					# downstream shadow of a C2 invisible stake.
					# B-arch-1a materializes the call-arg stakes
					# (CopyValue pre-ledger), shrinking this bucket
					# where call args were the cause; the
					# value_position residue goes with that later
					# slice.  Counted, never failed.
					if raw is LiveState.MOVED_OUT:
						_detail(DIV_C4_ALLOWLISTED, kind="return_move_blind_release",
							local=local, point=list(rb.point))
					else:
						_detail(DIV_C1_RELEASE_WITHOUT_MUST_DROP,
							local=local, point=list(rb.point), raw_state=raw.value)
				elif (not was_released) and verdict is DropVerdict.MUST_DROP:
					if local in rb.skipped:
						# Skip decided by another authority (drop
						# flags, moved-into-return, destructible
						# handling) — record separately from a true
						# missing release.
						_detail(DIV_C1_MUST_DROP_WITHOUT_RELEASE,
							local=local, point=list(rb.point), skipped=True)
					else:
						_detail(DIV_C1_MUST_DROP_WITHOUT_RELEASE,
							local=local, point=list(rb.point), skipped=False)
				else:
					_bump(agg, "c1_agree")
				# Both-snapshots check: does the rebuilt L_post still
				# reach the same verdict at the same boundary?  The
				# rewrite expands MoveOut into Load+ZeroValue+Store,
				# which the lattice models as re-initialization — this
				# is where the two snapshots genuinely diverge.
				if l_post is not None:
					post_point = (rb.point[0], _post_block_len(l_post, rb.point[0]))
					v_post = l_post.verdict_at(post_point, local, needs_drop=needs_drop(local))
					if v_post is not verdict:
						_detail(DIV_PRE_POST_VERDICT_DRIFT, local=local,
							point=list(rb.point), pre=verdict.value, post=v_post.value)

		# C2: every retain is a stake the ledger has no event model for
		# (StringRetain/StringRelease are string_arc's private
		# vocabulary).  Operationalization: a RETAIN is a visible stake
		# only if its subject is a ledger-tracked storage local whose
		# pre-state at the emission anchor the lattice already models as
		# consumed/handoff (MOVED_OUT/TOMBSTONED); every other retain is
		# the invisible-stake inventory B-arch-1 migrates shape by
		# shape.  return_retain_site3 retains are C4 (documented
		# divergence), never C2.
		for ev in self.events:
			if ev.kind != STAKE_RETAIN:
				continue
			if ev.site_class == SITE_CLASS_RETURN_RETAIN_SITE3:
				_detail(DIV_C4_ALLOWLISTED, kind="return_retain_site3",
					subject=ev.subject, point=list(ev.pre_point))
				continue
			tracked = getattr(l_pre, "tracked_locals", None) or set()
			if ev.subject in tracked:
				raw = l_pre.state_pre(ev.pre_point, ev.subject)
				if raw in (LiveState.MOVED_OUT, LiveState.TOMBSTONED):
					_bump(agg, "c2_visible_stake")
					continue
			_detail(DIV_C2_INVISIBLE_STAKE, subject=ev.subject,
				site_class=ev.site_class, point=list(ev.pre_point))

		# C3: MoveOut expansions where the local is not Owned (LIVE) in
		# L_pre at that point.
		for ev in self.events:
			if ev.kind != STAKE_MOVEOUT_EXPANSION:
				continue
			raw = l_pre.state_pre(ev.pre_point, ev.subject)
			if raw is LiveState.LIVE:
				_bump(agg, "c3_moveout_owned")
			else:
				_detail(DIV_C3_MOVEOUT_NOT_OWNED, subject=ev.subject,
					point=list(ev.pre_point), raw_state=raw.value)

		# UNCLASSIFIED: any event whose (kind, site_class) pair no
		# comparison above consumed and that is not a counted-only
		# class.  Counted-only (observational, no divergence defined in
		# the plan): temp_lastuse_release, overwrite_release,
		# drop_before_overwrite_site4 (site 4 has its own Tier-1
		# reporter), scope_exit_release (consumed by C1 via
		# boundaries).
		_counted_only = {
			SITE_CLASS_TEMP_LASTUSE_RELEASE,
			SITE_CLASS_OVERWRITE_RELEASE,
			SITE_CLASS_DROP_BEFORE_OVERWRITE_SITE4,
			SITE_CLASS_SCOPE_EXIT_RELEASE,
		}
		for ev in self.events:
			if ev.kind == STAKE_RETAIN or ev.kind == STAKE_MOVEOUT_EXPANSION:
				continue  # consumed by C2/C3/C4 above
			if ev.site_class in _counted_only:
				continue
			_detail(DIV_UNCLASSIFIED, kind=ev.kind, subject=ev.subject,
				site_class=ev.site_class, point=list(ev.pre_point))

		self._emit(agg, details)
		return agg

	def _emit(self, agg: dict, details: list[dict]) -> None:
		global _GLOBAL_FLUSH_REGISTERED
		payload = {"record": "fn", **agg}
		if details:
			payload["details"] = details
		# Volume guard (anti-telemetry-creep, plan §11.2): per-fn lines
		# are emitted only for fns with at least one divergence detail —
		# clean fns are fully represented in the aggregate counts.  Set
		# DRIFT_STRING_ARC_AUDIT_VERBOSE=1 to emit every fn (single-file
		# triage).  A corpus run over ~1k cases × ~1.2k fns/compile would
		# otherwise produce hundreds of MB of agreeing records.
		if (
			details
			or agg.get(DIV_POST_LEDGER_BUILD_FAILED)
			or _os.environ.get("DRIFT_STRING_ARC_AUDIT_VERBOSE") in ("1", "true", "True")
		):
			_emit_line(payload)
		for k, v in agg.items():
			if isinstance(v, int):
				_bump(_GLOBAL_AGGREGATE, k, v)
		_bump(_GLOBAL_AGGREGATE, "fns")
		if not _GLOBAL_FLUSH_REGISTERED:
			_GLOBAL_FLUSH_REGISTERED = True
			_atexit.register(_flush_global_aggregate)


def _post_block_len(l_post, block_name: str) -> int:
	"""Length-of-block anchor in the POST ledger's view: the largest
	instruction index the post ledger has state for, +1 — mirroring the
	`(block, len(instructions))` return-boundary convention without
	needing the MIR object here."""
	# LiveStateMap does not retain the MIR; `post_instr` is keyed by
	# (block, idx), so probe upward from 0.  Bounded by the real block
	# length; the loop is cheap (audit-only path).
	n = 0
	while (block_name, n) in l_post.post_instr:
		n += 1
	return n
