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
