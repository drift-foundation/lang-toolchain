# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Unit tests for the ownership-ledger disagreement reporter (Phase 3A).

Pins the classification table in
`lang/driftc/stage2/ownership_ledger_reporter.py`:

- verdicts match   → agree
- ledger PathDependent (raw MaybeUninit) → path_dependent, regardless of
  site verdict
- site drops a Tombstoned local → semantic_equivalent (safe no-op), NOT
  ledger_stricter
- ledger MustDrop + site MustNotDrop → ledger_stricter (leak case)
- ledger MustNotDrop (non-tombstoned) + site MustDrop → site_stricter
  (ledger bug)

The reporter does no I/O by itself — every test uses `collecting_emit()`
to capture records into a list.
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import (
	DropVerdict,
	LiveState,
	LiveStateMap,
	build_ledger,
)
from lang.driftc.stage2.ownership_ledger_events import (
	DropDecisionLog,
	REASON_MOVED,
	REASON_NEEDS_DROP,
	SITE_DROP_BEFORE_OVERWRITE,
	SITE_MATCH_CLEANUP,
	SITE_SCOPE_DROP,
	SITE_STRING_ARC_RETURN,
	VERDICT_MUST_DROP,
	VERDICT_MUST_NOT_DROP,
)
from lang.driftc.stage2.ownership_ledger_reporter import (
	CLASS_AGREE,
	CLASS_LEDGER_STRICTER,
	CLASS_PATH_DEPENDENT,
	CLASS_SEMANTIC_EQUIVALENT,
	CLASS_SITE_STRICTER,
	DisagreementRecord,
	check,
	classify_verdicts,
	collecting_emit,
	compare_events,
)


_TY_DROPPABLE = 101
_TY_POD = 202


def _drop_policy_stub(_ty: int) -> None:
	return None


def _needs_drop_always_true(_local: str) -> bool:
	return True


def _make_moved_out_fn() -> tuple[M.MirFunc, LiveStateMap]:
	"""Param `p` is moved out at instruction 0 of the entry block.  Pre-
	state at (entry, 0) is Live; post-state is MovedOut."""
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = M.MirFunc(
		name="f",
		params=["p"],
		locals=["p"],
		fn_id=fn_id,
		local_types={"p": _TY_DROPPABLE},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.MoveOut(dest="t0", local="p", ty=_TY_DROPPABLE))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	return func, build_ledger(func, drop_policy=_drop_policy_stub)


def _make_tombstone_fn() -> tuple[M.MirFunc, LiveStateMap]:
	"""Local `x` is initialised, then zero-stored (tombstoned), then the
	site-4 decision point is a follow-up StoreLocal at index 3.  Pre-
	state at (entry, 3) is Tombstoned — that is what a real drop-before-
	overwrite site sees when it queries before re-assigning a previously
	zeroed local."""
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = M.MirFunc(
		name="f",
		params=[],
		locals=["x"],
		fn_id=fn_id,
		local_types={"x": _TY_DROPPABLE},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.ZeroValue(dest="z", ty=_TY_DROPPABLE))
	entry.instructions.append(M.StoreLocal(local="x", value="z"))
	entry.instructions.append(M.StoreLocal(local="x", value="t_new"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	return func, build_ledger(func, drop_policy=_drop_policy_stub)


# -- classify_verdicts ------------------------------------------------------


def test_classify_agree_when_verdicts_match() -> None:
	c = classify_verdicts(
		site_verdict=VERDICT_MUST_DROP,
		ledger_verdict=DropVerdict.MUST_DROP,
		raw_state=LiveState.LIVE,
	)
	assert c == CLASS_AGREE


def test_classify_path_dependent_takes_precedence() -> None:
	"""Ledger PathDependent masks verdict comparison entirely — site
	verdict is recorded for telemetry but does not change the class."""
	c = classify_verdicts(
		site_verdict=VERDICT_MUST_DROP,
		ledger_verdict=DropVerdict.PATH_DEPENDENT,
		raw_state=LiveState.MAYBE_UNINIT,
	)
	assert c == CLASS_PATH_DEPENDENT
	c2 = classify_verdicts(
		site_verdict=VERDICT_MUST_NOT_DROP,
		ledger_verdict=DropVerdict.PATH_DEPENDENT,
		raw_state=LiveState.MAYBE_UNINIT,
	)
	assert c2 == CLASS_PATH_DEPENDENT


def test_classify_semantic_equivalent_on_tombstoned_drop() -> None:
	"""Site emits drop on a Tombstoned local.  Runtime result is a no-op;
	must not pollute the ledger_stricter bucket."""
	c = classify_verdicts(
		site_verdict=VERDICT_MUST_DROP,
		ledger_verdict=DropVerdict.MUST_NOT_DROP,
		raw_state=LiveState.TOMBSTONED,
	)
	assert c == CLASS_SEMANTIC_EQUIVALENT


def test_classify_ledger_stricter_is_the_leak_case() -> None:
	"""Ledger says MustDrop, site says MustNotDrop — potential leak."""
	c = classify_verdicts(
		site_verdict=VERDICT_MUST_NOT_DROP,
		ledger_verdict=DropVerdict.MUST_DROP,
		raw_state=LiveState.LIVE,
	)
	assert c == CLASS_LEDGER_STRICTER


def test_classify_site_stricter_is_the_ledger_bug_case() -> None:
	"""Ledger says MustNotDrop on non-Tombstoned state, site says
	MustDrop — site has information the ledger is missing."""
	c = classify_verdicts(
		site_verdict=VERDICT_MUST_DROP,
		ledger_verdict=DropVerdict.MUST_NOT_DROP,
		raw_state=LiveState.MOVED_OUT,
	)
	assert c == CLASS_SITE_STRICTER


# -- compare_events (sites 1/2 retrospective) ------------------------------


def test_compare_events_agree_on_move_then_skip() -> None:
	"""Site 1 scope-drop records MustNotDrop for a moved local; ledger
	pre-state at the event point is MovedOut (post-move) so verdicts
	agree."""
	func, ledger = _make_moved_out_fn()
	log = DropDecisionLog(fn_name=func.name)
	log.record(
		site=SITE_SCOPE_DROP,
		program_point=("entry", 1),
		local="p",
		verdict=VERDICT_MUST_NOT_DROP,
		reason=REASON_MOVED,
	)
	records = compare_events(
		log.drain(),
		ledger,
		needs_drop=_needs_drop_always_true,
	)
	assert len(records) == 1
	assert records[0].classification == CLASS_AGREE
	assert records[0].raw_state == LiveState.MOVED_OUT.value


def test_compare_events_emit_invoked_per_record() -> None:
	func, ledger = _make_moved_out_fn()
	log = DropDecisionLog(fn_name=func.name)
	log.record(
		site=SITE_SCOPE_DROP,
		program_point=("entry", 1),
		local="p",
		verdict=VERDICT_MUST_NOT_DROP,
		reason=REASON_MOVED,
	)
	emit, captured = collecting_emit()
	compare_events(log.drain(), ledger, needs_drop=_needs_drop_always_true, emit=emit)
	assert len(captured) == 1
	assert isinstance(captured[0], DisagreementRecord)


def test_compare_events_detects_ledger_stricter_leak() -> None:
	"""Construct a pretend event where the site claims MustNotDrop on a
	Live local.  The ledger says MustDrop; class is `ledger_stricter`
	(the leak case we care about)."""
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = M.MirFunc(name="f", params=["p"], locals=["p"], fn_id=fn_id, local_types={"p": _TY_DROPPABLE})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	log = DropDecisionLog(fn_name="f")
	log.record(
		site=SITE_SCOPE_DROP,
		program_point=("entry", 0),
		local="p",
		verdict=VERDICT_MUST_NOT_DROP,
		reason=REASON_MOVED,
	)
	records = compare_events(log.drain(), ledger, needs_drop=_needs_drop_always_true)
	assert records[0].classification == CLASS_LEDGER_STRICTER
	assert records[0].raw_state == LiveState.LIVE.value


# -- check (sites 3/4 prospective) -----------------------------------------


def test_check_agrees_on_live_local_needing_drop() -> None:
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = M.MirFunc(name="f", params=["p"], locals=["p"], fn_id=fn_id, local_types={"p": _TY_DROPPABLE})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	record = check(
		ledger,
		fn_name="f",
		site=SITE_STRING_ARC_RETURN,
		point=("entry", 0),
		local="p",
		site_verdict=VERDICT_MUST_DROP,
		site_reason=REASON_NEEDS_DROP,
		needs_drop=_needs_drop_always_true,
	)
	assert record.classification == CLASS_AGREE


def test_check_semantic_equivalent_when_site_drops_tombstoned() -> None:
	"""Site 4 (drop-before-overwrite) emits drop unconditionally if its
	local's `initialized_destructibles` dataflow says so.  If the value
	has been zeroed in-place since, the ledger sees Tombstoned and the
	drop is a safe no-op."""
	func, ledger = _make_tombstone_fn()
	record = check(
		ledger,
		fn_name=func.name,
		site=SITE_DROP_BEFORE_OVERWRITE,
		point=("entry", 3),
		local="x",
		site_verdict=VERDICT_MUST_DROP,
		site_reason=REASON_NEEDS_DROP,
		needs_drop=_needs_drop_always_true,
	)
	assert record.classification == CLASS_SEMANTIC_EQUIVALENT
	assert record.raw_state == LiveState.TOMBSTONED.value


def test_check_emit_invoked_when_provided() -> None:
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = M.MirFunc(name="f", params=["p"], locals=["p"], fn_id=fn_id, local_types={"p": _TY_DROPPABLE})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	emit, captured = collecting_emit()
	check(
		ledger,
		fn_name="f",
		site=SITE_MATCH_CLEANUP,
		point=("entry", 0),
		local="p",
		site_verdict=VERDICT_MUST_DROP,
		site_reason=REASON_NEEDS_DROP,
		needs_drop=_needs_drop_always_true,
		emit=emit,
	)
	assert len(captured) == 1


# -- needs_drop POD short-circuit passthrough ------------------------------


def test_pod_local_collapses_to_agree_regardless_of_raw_state() -> None:
	"""K-finding propagates through the reporter: a POD local (needs_drop=
	False) has ledger verdict MustNotDrop for any raw state, so a site
	that correctly emits no drop agrees."""
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = M.MirFunc(name="f", params=["p"], locals=["p"], fn_id=fn_id, local_types={"p": _TY_POD})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	record = check(
		ledger,
		fn_name="f",
		site=SITE_SCOPE_DROP,
		point=("entry", 0),
		local="p",
		site_verdict=VERDICT_MUST_NOT_DROP,
		site_reason=REASON_MOVED,
		needs_drop=lambda _l: False,
	)
	assert record.classification == CLASS_AGREE
	assert record.ledger_verdict == VERDICT_MUST_NOT_DROP
