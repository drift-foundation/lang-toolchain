# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""S1 teeth for the B2+C frozen decision-plan container
(`lang/driftc/stage2/cleanup_plan.py`).

Contract (checkpoint §4.1): the plan-time proof coordinate
`(block, original_index)` is validated at BUILD/FINALIZE against the
original MIR, and is NOT re-required to equal the consumption-time
numerical index. Consumption validates object identity + exactly-once +
same-block + kind + semantic fields + relative order. A changed current
index is fine; disappearance / duplication / cross-block movement /
replacement / reordered anchors / wrong field / unconsumed(orphan) fail
closed. Decisions are immutable and consumption state is plan-private;
consumption is batch (one function scan serves many decisions).
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.cleanup_plan import (
	ANCHOR_INSTR,
	AnchorCoord,
	CleanupPlan,
	PlanContractError,
	anchor_instr,
	anchor_term,
)


def _mk_func(name="f"):
	return M.MirFunc(
		name=f"test::{name}",
		params=[],
		locals=[],
		fn_id=FunctionId(module="test", name=name, ordinal=0),
		local_types={},
	)


def _store(local, value="%v"):
	return M.StoreLocal(local=local, value=value)


def _pristine():
	"""entry: [store_x] ; Return(None)."""
	func = _mk_func()
	blk = M.BasicBlock(name="entry")
	store_x = _store("x")
	blk.instructions = [store_x]
	ret = M.Return(value=None)
	blk.terminator = ret
	func.blocks["entry"] = blk
	func.entry = "entry"
	return func, blk, store_x, ret


def _built(func, blk, store_x, ret):
	"""Plan with the store (site4) + the Return (site3), validated+frozen
	against the PRISTINE func."""
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	         fields={"local": "x", "value": "%v"}, payload=("drop", "x"))
	plan.add(obj=ret, coord=anchor_term("entry", 1), site="site3",
	         fields={"value": None}, payload=("drops", ("e",)))
	plan.validate_and_freeze(func)
	return plan


# --- happy path: insertion before anchor does NOT invalidate ----------

def test_insertion_before_anchor_keeps_anchor_valid():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)

	# Emitter inserts cleanup instrs BEFORE the store (index 0 -> 2), then
	# opens a session against the CURRENT MIR.
	blk.instructions = [_store("t1"), _store("t2"), store_x]

	with plan.open_session(func) as sess:
		s4 = plan.decisions_for_site("site4")[0]
		s3 = plan.decisions_for_site("site3")[0]
		assert s4.coord.orig_index == 0          # proof coordinate unchanged
		assert sess.consume(s4) == 2             # current index shifted — allowed
		assert sess.consume(s3) == 3             # terminator end-of-block moved
	plan.assert_all_consumed()


def test_stale_session_cannot_consume():
	"""BYPASS 2 fix: a session opened before a mutation must not validate or
	consume an anchor the mutation moved."""
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	sess = plan.open_session(func)               # scan: store_x at index 0
	blk.instructions = [_store("t1"), store_x]   # mutate AFTER open → store_x now at 1
	with pytest.raises(PlanContractError, match="stale session"):
		sess.consume(plan.decisions_for_site("site4")[0])


def test_closed_session_cannot_consume():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	with plan.open_session(func) as sess:
		pass                                     # context exit closes it
	with pytest.raises(PlanContractError, match="closed"):
		sess.consume(plan.decisions_for_site("site4")[0])


def test_no_public_mark_consumed_bypass():
	"""BYPASS 1 fix: there is no way to mark a decision consumed without a
	session validating its anchor."""
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	assert not hasattr(plan, "mark_consumed")


def test_one_scan_serves_many_decisions():
	"""Batch pin: a single session scans the function ONCE regardless of
	how many decisions it locates (no O(decisions × MIR))."""
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	with plan.open_session(func) as sess:
		for _ in range(50):
			sess.locate(plan.decisions_for_site("site4")[0])
			sess.locate(plan.decisions_for_site("site3")[0])
		assert sess.scan_count == 1


# --- validate_unconsumed: non-consuming survival postflight (item 1) --

def test_validate_unconsumed_passes_after_legitimate_insertion():
	"""After a legitimate insertion BEFORE the store (site-3 Return object
	preserved, only end-index shifted), the unconsumed site-3 anchor still
	validates — WITHOUT consuming it."""
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	blk.instructions = [_store("t1"), _store("t2"), store_x]  # insert before store
	plan.validate_unconsumed(func, sites={"site3"})           # no raise
	# Non-consuming: nothing marked consumed.
	assert not plan.is_consumed(plan.decisions_for_site("site3")[0])
	assert not plan.is_consumed(plan.decisions_for_site("site4")[0])


def test_validate_unconsumed_fails_on_replaced_return():
	"""A REPLACED Return (new identity) fails the non-consuming postflight."""
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	blk.terminator = M.Return(value=None)                     # same fields, new identity
	with pytest.raises(PlanContractError, match="exactly once"):
		plan.validate_unconsumed(func, sites={"site3"})


def test_validate_unconsumed_fails_on_field_drift():
	"""A field-drifted Return (value changed) fails the postflight."""
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	ret.value = "%z"                                          # was None
	with pytest.raises(PlanContractError, match="field 'value' changed"):
		plan.validate_unconsumed(func, sites={"site3"})


def test_validate_unconsumed_fails_on_disappeared_return():
	"""A disappeared Return (terminator dropped) fails the postflight."""
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	blk.terminator = None
	with pytest.raises(PlanContractError):
		plan.validate_unconsumed(func, sites={"site3"})


def test_validate_unconsumed_skips_already_consumed():
	"""A decision already consumed is NOT re-validated — so a post-consume
	drift on a consumed anchor does not fail the postflight."""
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	with plan.open_session(func) as sess:
		sess.consume(plan.decisions_for_site("site3")[0])     # site3 now consumed
	ret.value = "%drifted"                                    # drift the CONSUMED anchor
	plan.validate_unconsumed(func, sites={"site3"})           # skipped → no raise


def test_validate_unconsumed_before_freeze_refused():
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	plan.add(obj=ret, coord=anchor_term("entry", 1), site="site3",
	         fields={"value": None}, payload=("drops", ()))
	with pytest.raises(PlanContractError, match="before freeze"):
		plan.validate_unconsumed(func)


# --- fail-closed: the anchor object itself is disturbed ---------------

def test_replacement_fails_closed():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	blk.instructions = [_store("x")]  # same fields, different identity
	# The vanished INSTR anchor is caught at session open (relative-order),
	# an even earlier fail-closed than locate.
	with pytest.raises(PlanContractError, match="missing from block"):
		plan.open_session(func)


def test_replacement_of_terminator_only_fails_at_locate():
	"""When only a TERM anchor is disturbed (INSTR anchors intact), session
	open succeeds and locate is the fail-closed point."""
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	blk.terminator = M.Return(value=None)  # replace TERM, keep store_x
	sess = plan.open_session(func)
	with pytest.raises(PlanContractError, match="exactly once"):
		sess.locate(plan.decisions_for_site("site3")[0])


def test_duplication_fails_closed():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	blk.instructions = [store_x, store_x]
	sess = plan.open_session(func)
	with pytest.raises(PlanContractError, match="exactly once"):
		sess.locate(plan.decisions_for_site("site4")[0])


def test_cross_block_movement_fails_closed():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	other = M.BasicBlock(name="other")
	other.instructions = [store_x]
	other.terminator = M.Return(value=None)
	func.blocks["other"] = other
	blk.instructions = []
	with pytest.raises(PlanContractError, match="missing from block"):
		plan.open_session(func)


def test_disappearance_fails_closed():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	blk.instructions = []
	with pytest.raises(PlanContractError, match="missing from block"):
		plan.open_session(func)


def test_field_drift_fails_closed():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	store_x.local = "y"  # in-place semantic mutation of the SAME object
	sess = plan.open_session(func)
	with pytest.raises(PlanContractError, match="field"):
		sess.locate(plan.decisions_for_site("site4")[0])


def test_terminator_replaced_fails_closed():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	blk.terminator = M.Return(value=None)  # different Return object
	sess = plan.open_session(func)
	with pytest.raises(PlanContractError, match="exactly once"):
		sess.locate(plan.decisions_for_site("site3")[0])


# --- reordered anchors (caught at session open) -----------------------

def test_reordered_anchors_fail_closed():
	func = _mk_func()
	blk = M.BasicBlock(name="entry")
	a, b = _store("a"), _store("b")
	blk.instructions = [a, b]
	blk.terminator = M.Return(value=None)
	func.blocks["entry"] = blk
	plan = CleanupPlan(func.name)
	plan.add(obj=a, coord=anchor_instr("entry", 0), site="site4",
	         fields={"local": "a"}, payload=None)
	plan.add(obj=b, coord=anchor_instr("entry", 1), site="nullsafe",
	         fields={"local": "b"}, payload=None)
	plan.validate_and_freeze(func)

	plan.open_session(func)          # preserved order is fine
	blk.instructions = [b, a]        # swap anchors
	with pytest.raises(PlanContractError, match="relative order"):
		plan.open_session(func)


# --- build-time occupancy validation (validate_and_freeze) ------------

def test_wrong_original_index_fails_at_finalize():
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 1), site="site4",
	         fields={"local": "x"}, payload=None)  # store_x is at index 0, not 1
	with pytest.raises(PlanContractError, match="not at entry:1"):
		plan.validate_and_freeze(func)


def test_wrong_block_fails_at_finalize():
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("nope", 0), site="site4",
	         fields={"local": "x"}, payload=None)
	with pytest.raises(PlanContractError, match="does not exist"):
		plan.validate_and_freeze(func)


def test_terminator_declared_instr_fails_at_finalize():
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	# ret (a MTerminator) declared as an INSTR anchor.
	plan.add(obj=ret, coord=anchor_instr("entry", 1), site="site3",
	         fields={}, payload=None)
	with pytest.raises(PlanContractError, match="not a MInstr"):
		plan.validate_and_freeze(func)


def test_term_orig_index_must_equal_len_instructions():
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	plan.add(obj=ret, coord=anchor_term("entry", 0), site="site3",
	         fields={"value": None}, payload=None)  # len(instructions)==1, not 0
	with pytest.raises(PlanContractError, match="len\\(instructions\\)"):
		plan.validate_and_freeze(func)


def test_belongs_to_func():
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	         fields={"local": "x"}, payload=None)
	other = _mk_func("g")
	with pytest.raises(PlanContractError, match="does not belong"):
		plan.validate_and_freeze(other)


# --- build-time collision / cross-site consistency --------------------

def test_invalid_anchor_kind_fails_at_add():
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	bad = AnchorCoord(block="entry", orig_index=0, anchor_kind="bogus")
	with pytest.raises(PlanContractError, match="invalid anchor_kind"):
		plan.add(obj=store_x, coord=bad, site="site4", fields={}, payload=None)


def test_coordinate_collision_fails_at_add():
	func, blk, store_x, ret = _pristine()
	other_store = _store("x")
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	         fields={"local": "x"}, payload=None)
	with pytest.raises(PlanContractError, match="coordinate collision"):
		plan.add(obj=other_store, coord=anchor_instr("entry", 0), site="nullsafe",
		         fields={"local": "x"}, payload=None)


def test_cross_site_inconsistent_coordinate_fails_at_add():
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	plan.add(obj=ret, coord=anchor_term("entry", 1), site="site3",
	         fields={"value": None}, payload=None)
	with pytest.raises(PlanContractError, match="inconsistent coordinate"):
		plan.add(obj=ret, coord=anchor_term("entry", 5), site="r3",
		         fields={"value": None}, payload=None)


def test_cross_site_conflicting_field_fails_at_add():
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	plan.add(obj=ret, coord=anchor_term("entry", 1), site="site3",
	         fields={"value": None}, payload=None)
	with pytest.raises(PlanContractError, match="conflicting field"):
		plan.add(obj=ret, coord=anchor_term("entry", 1), site="r3",
		         fields={"value": "%x"}, payload=None)


def test_two_sites_may_share_the_return_anchor():
	"""The unified Return authority (S5) consumes site-3 AND R3/R4 for the
	same Return object; identical coord + compatible fields is allowed."""
	func = _mk_func()
	blk = M.BasicBlock(name="entry")
	ret = M.Return(value=None)
	blk.instructions = []
	blk.terminator = ret
	func.blocks["entry"] = blk
	plan = CleanupPlan(func.name)
	plan.add(obj=ret, coord=anchor_term("entry", 0), site="site3",
	         fields={"value": None}, payload=("drops", ("e",)))
	plan.add(obj=ret, coord=anchor_term("entry", 0), site="r3",
	         fields={"value": None}, payload=("releases", ("s",)))
	plan.validate_and_freeze(func)
	assert len(plan) == 2
	with plan.open_session(func) as sess:
		for d in plan.all_decisions():
			assert sess.consume(d) == 0
	plan.assert_all_consumed()


# --- immutability + plan-private consumption state --------------------

def test_decision_is_immutable():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	dec = plan.decisions_for_site("site4")[0]
	with pytest.raises(FrozenInstanceError):
		dec.site = "hacked"
	with pytest.raises(FrozenInstanceError):
		dec.payload = None
	# No public `consumed` attribute to forge.
	assert not hasattr(dec, "consumed")


def test_foreign_decision_locate_fails():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	other_plan = CleanupPlan(func.name)
	other_plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	               fields={"local": "x"}, payload=None)
	other_plan.validate_and_freeze(func)
	foreign = other_plan.decisions_for_site("site4")[0]
	with plan.open_session(func) as sess:
		with pytest.raises(PlanContractError, match="foreign decision"):
			sess.locate(foreign)
		with pytest.raises(PlanContractError, match="foreign decision"):
			sess.consume(foreign)


def test_consume_before_freeze_fails():
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	         fields={"local": "x"}, payload=None)
	with pytest.raises(PlanContractError, match="before validate_and_freeze"):
		plan.open_session(func)


def test_duplicate_registration_same_site_object_fails():
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	         fields={"local": "x"}, payload=None)
	with pytest.raises(PlanContractError, match="duplicate registration"):
		plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
		         fields={"local": "x"}, payload=None)


def test_unconsumed_decision_fails_closed():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	with plan.open_session(func) as sess:
		sess.consume(plan.decisions_for_site("site4")[0])  # leave site3 orphaned
	with pytest.raises(PlanContractError, match="unconsumed"):
		plan.assert_all_consumed()


def test_double_consume_fails_closed():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	with plan.open_session(func) as sess:
		s4 = plan.decisions_for_site("site4")[0]
		sess.consume(s4)
		with pytest.raises(PlanContractError, match="consumed twice"):
			sess.consume(s4)


def test_declared_field_mismatch_fails_at_finalize():
	"""Declared semantic fields are validated at validate_and_freeze, not
	only at consumption."""
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	         fields={"local": "WRONG"}, payload=None)  # store_x.local == "x"
	with pytest.raises(PlanContractError, match="does not match the anchor object"):
		plan.validate_and_freeze(func)


def test_declared_missing_field_fails_at_finalize():
	"""A declared field the anchor object does not have fails at finalize."""
	func, blk, store_x, ret = _pristine()
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	         fields={"nonexistent_attr": 1}, payload=None)
	with pytest.raises(PlanContractError, match="does not match the anchor object"):
		plan.validate_and_freeze(func)


# --- EmitterPhase: preflight -> rewrite -> postflight -> consume -------

def test_phase_preflight_rewrite_postflight_consume():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	phase = plan.begin_phase(func)
	s4 = plan.decisions_for_site("site4")[0]
	s3 = plan.decisions_for_site("site3")[0]
	assert phase.stage(s4) == 0                  # preflight index
	assert phase.stage(s3) == 1
	# Emitter rewrites: insert two drops before the store.
	blk.instructions = [_store("d1"), _store("d2"), store_x]
	phase.mark_rewritten()
	phase.commit()                               # postflight fresh-validate + consume
	plan.assert_all_consumed()


def test_phase_stage_after_rewrite_fails():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	phase = plan.begin_phase(func)
	phase.mark_rewritten()
	with pytest.raises(PlanContractError, match="stage.*after"):
		phase.stage(plan.decisions_for_site("site4")[0])


def test_phase_commit_before_rewrite_fails():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	phase = plan.begin_phase(func)
	phase.stage(plan.decisions_for_site("site4")[0])
	with pytest.raises(PlanContractError, match="commit.*before"):
		phase.commit()


def test_phase_commit_fails_closed_if_rewrite_broke_anchor():
	"""If the rewrite disturbed a staged anchor, postflight validation in
	commit fails closed and marks nothing consumed."""
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	phase = plan.begin_phase(func)
	s4 = plan.decisions_for_site("site4")[0]
	phase.stage(s4)
	blk.instructions = []                        # rewrite REMOVED the anchor
	phase.mark_rewritten()
	with pytest.raises(PlanContractError):
		phase.commit()
	# Nothing was consumed.
	assert not plan.is_consumed(s4)


# --- stale-matrix: open-session then disturb -> rejected --------------
# (open BEFORE the disturbance; the O(1) is-recheck must reject, without a
#  per-decision whole-function rescan.)

def _open_then(disturb):
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	sess = plan.open_session(func)               # snapshot at index 0
	disturb(blk, store_x)
	return plan, sess


def test_stale_matrix_insert_before():
	plan, sess = _open_then(lambda blk, s: blk.instructions.insert(0, _store("t")))
	with pytest.raises(PlanContractError, match="stale session"):
		sess.consume(plan.decisions_for_site("site4")[0])


def test_stale_matrix_remove():
	plan, sess = _open_then(lambda blk, s: blk.instructions.clear())
	with pytest.raises(PlanContractError, match="stale session"):
		sess.consume(plan.decisions_for_site("site4")[0])


def test_stale_matrix_move_within_block():
	def disturb(blk, s):
		blk.instructions[:] = [_store("t"), s]   # s moves 0 -> 1
	plan, sess = _open_then(disturb)
	with pytest.raises(PlanContractError, match="stale session"):
		sess.consume(plan.decisions_for_site("site4")[0])


def test_stale_matrix_duplicate():
	"""A duplicated anchor is caught by a fresh session (the postflight path);
	the stale preflight session's O(1) is-recheck cannot see a copy added
	elsewhere, which is exactly why commit re-validates on a fresh scan."""
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	blk.instructions = [store_x, store_x]        # duplicate
	sess = plan.open_session(func)               # fresh scan sees the duplicate
	with pytest.raises(PlanContractError, match="exactly once"):
		sess.consume(plan.decisions_for_site("site4")[0])


def test_stale_matrix_field_drift():
	plan, sess = _open_then(lambda blk, s: setattr(s, "local", "y"))
	with pytest.raises(PlanContractError, match="field"):
		sess.consume(plan.decisions_for_site("site4")[0])


def test_add_after_freeze_fails_closed():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	with pytest.raises(PlanContractError, match="after freeze"):
		plan.add(obj=_store("z"), coord=anchor_instr("entry", 0),
		         site="site4", fields={}, payload=None)


def test_double_finalize_fails():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)
	with pytest.raises(PlanContractError, match="twice"):
		plan.validate_and_freeze(func)


# --- Amendment 4: production must consume via EmitterPhase, not the raw
#     session.consume bypass (which can mark consumed before a later rewrite
#     invalidates the anchor). Fail-closed source/AST pin over production
#     modules that use cleanup_plan. ---

import ast as _ast


def _find_consume_bypass(source: str, name: str = "<probe>") -> list:
	"""Return `[(lineno, attr)]` for every `X.consume(...)` or
	`X._mark_consumed(...)` Call in `source`. These are the forbidden
	production consumption bypasses — production must go through
	`plan.begin_phase(...)` → stage → mark_rewritten → commit."""
	found = []
	tree = _ast.parse(source, filename=name)
	for node in _ast.walk(tree):
		if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute):
			if node.func.attr in ("consume", "_mark_consumed"):
				found.append((node.lineno, node.func.attr))
	return found


def test_consume_bypass_detector_catches_synthetic_calls():
	"""The detector must actually FIRE on a production-shaped bypass — a
	vacuous scan is worthless."""
	probe = (
		"def emit(plan, func):\n"
		"    sess = plan.open_session(func)\n"
		"    sess.consume(dec)\n"          # forbidden
		"    plan._mark_consumed(dec)\n"    # forbidden
	)
	hits = _find_consume_bypass(probe)
	attrs = sorted(a for _ln, a in hits)
	assert attrs == ["_mark_consumed", "consume"], hits
	# And it does NOT fire on the sanctioned phase lifecycle.
	ok = ("def emit(plan, func):\n"
	      "    ph = plan.begin_phase(func)\n"
	      "    ph.stage(dec); ph.mark_rewritten(); ph.commit()\n")
	assert _find_consume_bypass(ok) == []


def test_production_consumes_via_emitter_phase_not_session_bypass():
	import pathlib

	# `<repo>/lang/tests/stage2/test_cleanup_plan.py`.parents:
	#   [0]=stage2 [1]=tests [2]=lang → the production root is lang/driftc.
	root = pathlib.Path(__file__).resolve().parents[2] / "driftc"
	assert root.is_dir(), f"production scan root does not exist: {root}"

	# cleanup_plan.py is the plan's own implementation — EmitterPhase.commit
	# and ConsumptionSession.consume legitimately call `_mark_consumed`
	# there. Every OTHER production module must not.
	impl = (root / "stage2" / "cleanup_plan.py").resolve()

	visited = 0
	offenders = []
	for path in root.rglob("*.py"):
		if path.resolve() == impl:
			continue
		visited += 1
		for lineno, attr in _find_consume_bypass(path.read_text(), str(path)):
			offenders.append(f"{path.relative_to(root)}:{lineno} .{attr}()")

	assert visited > 50, f"scan visited too few production files ({visited}); path is likely wrong"
	assert not offenders, (
		"production modules must consume plan decisions through the S1 "
		"EmitterPhase postflight lifecycle (begin_phase -> stage -> "
		"mark_rewritten -> commit), never a raw session.consume / "
		"plan._mark_consumed bypass that marks consumed before a later "
		"rewrite can invalidate the anchor. Offenders:\n  "
		+ "\n  ".join(offenders)
	)


# --- Amendment 3: enforce type bindings + StoreLocal.value operand ------

def _typed_func():
	"""entry: [store_x (value=%v)] ; Return, with x typed 7 in local_types."""
	func = _mk_func()
	func.local_types = {"x": 7}
	blk = M.BasicBlock(name="entry")
	store_x = _store("x", "%v")
	blk.instructions = [store_x]
	blk.terminator = M.Return(value=None)
	func.blocks["entry"] = blk
	func.entry = "entry"
	return func, blk, store_x


def test_type_binding_wrong_at_freeze():
	func, blk, store_x = _typed_func()
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	         fields={"local": "x", "value": "%v"},
	         type_bindings={"x": 999},  # func.local_types["x"] == 7
	         payload=None)
	with pytest.raises(PlanContractError, match="type binding"):
		plan.validate_and_freeze(func)


def test_type_binding_absent_local_at_freeze():
	func, blk, store_x = _typed_func()
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	         fields={"local": "x", "value": "%v"},
	         type_bindings={"ghost": 7},  # not in func.local_types
	         payload=None)
	with pytest.raises(PlanContractError, match="absent from func.local_types"):
		plan.validate_and_freeze(func)


def test_type_binding_drift_at_consume():
	func, blk, store_x = _typed_func()
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	         fields={"local": "x", "value": "%v"},
	         type_bindings={"x": 7}, payload=None)
	plan.validate_and_freeze(func)
	func.local_types["x"] = 8            # post-freeze type drift
	with plan.open_session(func) as sess:
		with pytest.raises(PlanContractError, match="type binding"):
			sess.locate(plan.all_decisions()[0])


def test_storelocal_value_operand_drift_at_consume():
	func, blk, store_x = _typed_func()
	plan = CleanupPlan(func.name)
	plan.add(obj=store_x, coord=anchor_instr("entry", 0), site="site4",
	         fields={"local": "x", "value": "%v"},
	         type_bindings={"x": 7}, payload=None)
	plan.validate_and_freeze(func)
	store_x.value = "%other"             # operand drift on the same object
	with plan.open_session(func) as sess:
		with pytest.raises(PlanContractError, match="field 'value'"):
			sess.locate(plan.all_decisions()[0])


# --- site-scoped completeness (survives multiple emitter phases) --------

def test_assert_sites_consumed_complete_and_orphan():
	func, blk, store_x, ret = _pristine()
	plan = _built(func, blk, store_x, ret)   # site4 (store) + site3 (return)
	s4 = plan.decisions_for_site("site4")[0]
	s3 = plan.decisions_for_site("site3")[0]
	with plan.open_session(func) as sess:
		sess.consume(s4)
	# site4 fully consumed; site3 not yet → intermediate phase is OK for site4.
	plan.assert_sites_consumed({"site4"})
	# But the global check must still see the site3 orphan.
	with pytest.raises(PlanContractError, match="unconsumed"):
		plan.assert_all_consumed()
	# And a site-scoped check over an unconsumed site fails closed.
	with pytest.raises(PlanContractError, match="unconsumed"):
		plan.assert_sites_consumed({"site3"})
	# Consume site3 too → both pass.
	with plan.open_session(func) as sess:
		sess.consume(s3)
	plan.assert_sites_consumed({"site3"})
	plan.assert_all_consumed()
