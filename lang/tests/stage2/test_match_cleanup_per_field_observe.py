# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 step 3b — site-2 per-field telemetry pins.

Pins K's 5 carrier shapes for the per-field telemetry that landed
in step 3b:

  1. Direct VariantGetField partial-move case where telemetry now
     agrees (per-field MovedOut matches site's skip-because-moved).
  2. Sibling-field-live case where telemetry now agrees (per-field
     Live matches site's emit-slot-drop).
  3. VariantGetFieldAddr conservative case pinned as current
     behavior — 3a's immediate-mark rule may flag a field MovedOut
     even when no downstream consumer moves it.  Documented by the
     test as expected residual until 3c chain-aware tightening.
  4. A residual case proving `per_field_still_disagrees` is distinct
     from `per_field_gap`.
  5. Existing match-scrut MIR pins
     (`test_match_scrut_copy_store_emits_copyvalue.py`,
     `test_ownership_ledger_three_quadrant_pin.py`) remain untouched
     and green — verified separately by full-stage2 run, not
     re-pinned here.

Tests use the reporter directly (`compare_events`) against
hand-built MirFuncs and DropDecisionEvents to keep the per-field
telemetry semantics independent of full HIR→MIR lowering.  The
e2e telemetry comes from real driver runs and is exercised by
the observe-rerun aggregator, not by these unit tests.
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import build_ledger
from lang.driftc.stage2.ownership_ledger_events import (
	DropDecisionEvent,
	REASON_FIELD_MOVED,
	REASON_FIELD_NEEDS_DROP,
	SITE_MATCH_CLEANUP,
	VERDICT_MUST_DROP,
	VERDICT_MUST_NOT_DROP,
)
from lang.driftc.stage2.ownership_ledger_reporter import (
	CLASS_AGREE,
	CLASS_LEDGER_STRICTER,
	CLASS_SITE_STRICTER,
	collecting_emit,
	compare_events,
)


_TY_VARIANT = 101
_TY_PAYLOAD = 202


def _drop_policy_stub(_ty: int) -> None:
	return None


def _make_func(name: str, *, locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="main", name=name, ordinal=0)
	return M.MirFunc(
		name=name,
		params=[],
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


# Shape 1: VariantGetField partial-move → telemetry agrees ----------------


def test_per_field_telemetry_agrees_when_field_moved_via_variant_get_field() -> None:
	"""VariantGetField marks `(s, ((Some, 0),))` MovedOut.  Site
	emits a per-field record with verdict MustNotDrop + reason
	field_moved (skip slot drop because field already consumed).
	Per-field comparison: ledger says MustNotDrop, site says
	MustNotDrop → AGREE."""
	func = _make_func("partial_move", locals_=["s"], types={"s": _TY_VARIANT})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	entry.instructions.append(
		M.VariantGetField(dest="t_field", variant="s", variant_ty=_TY_VARIANT, ctor="Some", field_index=0, field_ty=_TY_PAYLOAD)
	)
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# Site's per-field event AFTER the move (program point: index 2,
	# i.e. after the VariantGetField at index 1).
	event = DropDecisionEvent(
		site=SITE_MATCH_CLEANUP,
		fn_name=func.name,
		program_point=("entry", 2),
		local="s",
		verdict=VERDICT_MUST_NOT_DROP,
		reason=REASON_FIELD_MOVED,
		field_path=(("Some", 0),),
	)
	emit, captured = collecting_emit()
	compare_events([event], ledger, needs_drop=lambda _l: True, emit=emit)
	assert len(captured) == 1
	rec = captured[0]
	assert rec.classification == CLASS_AGREE, (
		f"per-field telemetry disagreement: site says MustNotDrop "
		f"(field moved), ledger gave {rec.ledger_verdict}.  Both "
		f"should agree on MustNotDrop after VariantGetField marks "
		f"the field MovedOut."
	)
	assert rec.field_path == (("Some", 0),)


# Shape 2: sibling field still live → telemetry agrees --------------------


def test_per_field_telemetry_agrees_for_sibling_live_field() -> None:
	"""VariantGetField on field 0 leaves field 1 untouched.  Site
	emits a per-field record for field 1 with MustDrop +
	field_needs_drop (slot still owns its +1).  Ledger: per-field
	state Live, needs_drop=True → MustDrop.  AGREE."""
	func = _make_func("sibling_live", locals_=["s"], types={"s": _TY_VARIANT})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	entry.instructions.append(
		M.VariantGetField(dest="t_field", variant="s", variant_ty=_TY_VARIANT, ctor="Some", field_index=0, field_ty=_TY_PAYLOAD)
	)
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# Per-field event for field 1 (untouched).
	event = DropDecisionEvent(
		site=SITE_MATCH_CLEANUP,
		fn_name=func.name,
		program_point=("entry", 2),
		local="s",
		verdict=VERDICT_MUST_DROP,
		reason=REASON_FIELD_NEEDS_DROP,
		field_path=(("Some", 1),),
	)
	emit, captured = collecting_emit()
	compare_events([event], ledger, needs_drop=lambda _l: True, emit=emit)
	assert captured[0].classification == CLASS_AGREE, (
		f"sibling-field telemetry disagreed unexpectedly: site says "
		f"MustDrop (field still live), ledger gave "
		f"{captured[0].ledger_verdict}.  Defensive default in "
		f"`field_state_pre` for never-seen fields is Live → MustDrop "
		f"under needs_drop=True."
	)


# Shape 3: VariantGetFieldAddr conservative over-report --------------------


def test_per_field_telemetry_pins_variant_get_field_addr_conservative_overreport() -> None:
	"""3a's IMMEDIATE-MARK-MOVED rule for VariantGetFieldAddr fires
	regardless of downstream consumer.  If a Drift code path takes
	an addr-of-field but doesn't actually move (e.g., read-only
	borrow, or Copy-classified field whose downstream is `CopyValue`
	not `MoveOut`), the ledger reports per-field MovedOut while the
	site might still treat the field as live.  Pinned as current
	behavior — these records flow into `per_field_still_disagrees`,
	the bucket K wants visible to gate 3c.

	If an observe re-run shows this bucket dominated by
	VariantGetFieldAddr-without-MoveOut shapes, 3c MUST tighten the
	chain-aware detection before site-2 emission authority changes.
	"""
	func = _make_func("addr_overreport", locals_=["s"], types={"s": _TY_VARIANT})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	entry.instructions.append(M.AddrOfLocal(dest="t_ref", local="s", is_mut=True))
	# VariantGetFieldAddr without a downstream MoveOut chain — purely
	# conservative.  3a marks the field MovedOut anyway.
	entry.instructions.append(
		M.VariantGetFieldAddr(
			dest="t_field_addr",
			variant_ref="t_ref",
			variant_ty=_TY_VARIANT,
			ctor="Some",
			field_index=0,
			field_ty=_TY_PAYLOAD,
		)
	)
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# Site says MustDrop (it didn't see a downstream move; field
	# still owns).  Ledger says MustNotDrop (over-reported as
	# MovedOut).  → CLASS_SITE_STRICTER → bucket
	# per_field_still_disagrees.
	event = DropDecisionEvent(
		site=SITE_MATCH_CLEANUP,
		fn_name=func.name,
		program_point=("entry", 3),
		local="s",
		verdict=VERDICT_MUST_DROP,
		reason=REASON_FIELD_NEEDS_DROP,
		field_path=(("Some", 0),),
	)
	emit, captured = collecting_emit()
	compare_events([event], ledger, needs_drop=lambda _l: True, emit=emit)
	assert captured[0].classification == CLASS_SITE_STRICTER, (
		f"expected SITE_STRICTER (ledger over-reports MovedOut due "
		f"to VariantGetFieldAddr conservative rule); got "
		f"{captured[0].classification}.  If this assertion flipped, "
		f"3a's detection rule changed — re-derive whether the change "
		f"is a tightening or a regression."
	)


# Shape 4: per_field_still_disagrees distinct from per_field_gap ---------


def test_per_field_still_disagrees_is_distinct_from_per_field_gap() -> None:
	"""Aggregator-level pin: a per-field record (non-empty
	field_path) with a disagreement classification routes to
	`per_field_still_disagrees`, NOT `per_field_gap`.  A whole-local
	record (empty field_path) with a field-related reason still
	routes to `per_field_gap` for back-compat with pre-3b
	telemetry."""
	import sys
	sys.path.insert(0, "work/ownership-ledger")
	try:
		from aggregate_triage import bucket_for
	finally:
		sys.path.pop(0)
	# Per-field disagreement → per_field_still_disagrees.
	per_field_disagree = {
		"site": "match_cleanup",
		"site_reason": "field_needs_drop",
		"classification": "site_stricter",
		"field_path": [["Some", 0]],
	}
	assert bucket_for(per_field_disagree) == "per_field_still_disagrees", (
		"per-field record with disagreement should route to "
		"per_field_still_disagrees, NOT per_field_gap"
	)
	# Per-field agreement → agree (no bucket return except via
	# `bucket_for`'s final `agree` path).
	per_field_agree = {
		"site": "match_cleanup",
		"site_reason": "field_needs_drop",
		"classification": "agree",
		"field_path": [["Some", 0]],
	}
	assert bucket_for(per_field_agree) == "agree"
	# Whole-local record with field-related reason (no field_path)
	# stays in per_field_gap for back-compat — though step 3b's
	# site-2 changes mean such records should no longer be emitted
	# by match_cleanup in the partial-move branch.  Other sites or
	# legacy emissions still flow here if they exist.
	whole_local_field_reason = {
		"site": "match_cleanup",
		"site_reason": "field_moved",
		"classification": "ledger_stricter",
		"field_path": [],
	}
	assert bucket_for(whole_local_field_reason) == "per_field_gap"


# Shape 5: existing match-scrut MIR pins remain green --------------------
# (Verified by the full stage2 run; not re-pinned here.)
