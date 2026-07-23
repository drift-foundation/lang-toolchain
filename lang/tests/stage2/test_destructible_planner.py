# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Unit teeth for the standalone non-emitting destructible planner
(`lang/driftc/stage2/destructible_planner.py`), Milestone B.

The planner reuses the shared `destructible_authority` (covered by
`test_destructible_authority.py`); these tests lock the planner's
plumbing on hand-built MIR with a FRESH attached ledger (the real
pre-normalization slot): it builds a finalized `CleanupPlan` of immutable
payloads, counts the census, MUTATES NOTHING, and fails closed on an
unexpected marked-synthetic null-safe store.
"""
from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import DropVerdict, build_ledger
from lang.driftc.stage2.destructible_planner import build_destructible_plan, PlannerStop
from lang.driftc.stage2.cleanup_payloads import (
	Site3ReturnPayload,
	Site4Payload,
	NullsafePayload,
)


def _droppable_struct(tt: TypeTable, name="DropMe") -> int:
	string_ty = tt.ensure_string()
	tid = tt.declare_struct(module_id="test", name=name, field_names=["inner"])
	tt.define_struct_fields(tid, field_types=[string_ty])
	dfns = dict(getattr(tt, "destructor_fns", None) or {})
	dfns[tid] = FunctionId(module="test", name=f"{name}::destroy", ordinal=0)
	tt.destructor_fns = dfns
	nc = set(getattr(tt, "_nc", set())); nc.add(tid)
	tt._nc = nc
	tt._copy_query = lambda t: False if t in nc else None
	return tid


def _nullsafe_struct(tt: TypeTable, name="PlainStr") -> int:
	string_ty = tt.ensure_string()
	tid = tt.declare_struct(module_id="test", name=name, field_names=["inner"])
	tt.define_struct_fields(tid, field_types=[string_ty])
	return tid


def _make_func(name, *, locals_, types):
	return M.MirFunc(
		name=f"test::{name}", params=[], locals=list(locals_),
		fn_id=FunctionId(module="test", name=name, ordinal=0),
		local_types=dict(types),
	)


def _attach_ledger(func):
	ledger = build_ledger(func, drop_policy=lambda _t: None)
	setattr(func, "_ownership_ledger", ledger)
	setattr(func, "_ledger_dirty_reason", None)
	return ledger


def _mir_snapshot(func):
	return [
		(bn, [type(i).__name__ for i in blk.instructions], type(blk.terminator).__name__,
		 [id(i) for i in blk.instructions], id(blk.terminator))
		for bn, blk in func.blocks.items()
	]


def test_planner_site4_split_and_site3_and_non_mutation():
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("ov", locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))  # idx0 first store
	entry.instructions.append(M.StoreLocal(local="x", value="t_new"))   # idx1 overwrite
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)

	before = _mir_snapshot(func)
	plan, census, _ = build_destructible_plan(func, type_table=tt)

	# Two site-4 decisions: idx0 MUST_NOT_DROP, idx1 MUST_DROP.
	s4 = plan.decisions_for_site("site4")
	assert len(s4) == 2
	assert {d.payload.verdict for d in s4} == {DropVerdict.MUST_NOT_DROP, DropVerdict.MUST_DROP}
	assert census["site4_must_drop"] == 1
	assert census["site4_must_not_drop"] == 1
	for d in s4:
		assert isinstance(d.payload, Site4Payload)
		assert d.payload.ty == drop_ty                 # frozen expected type carried
		assert d.payload.emit == (d.payload.verdict is DropVerdict.MUST_DROP)

	# One site-3 Return decision; x is initialized + destructible → dropped.
	s3 = plan.decisions_for_site("site3")
	assert len(s3) == 1 == census["site3_returns"]
	assert isinstance(s3[0].payload, Site3ReturnPayload)
	assert [d.local for d in s3[0].payload.drops] == ["x"]
	assert census["site3_locals"] == 1

	assert census["nullsafe"] == 0 and census["nullsafe_synthetic"] == 0

	# NON-MUTATION: MIR objects/kinds/identities unchanged; ledger not dirtied.
	assert _mir_snapshot(func) == before
	assert getattr(func, "_ledger_dirty_reason", None) is None


def test_planner_nullsafe_overwrite_recorded():
	tt = TypeTable()
	ns_ty = _nullsafe_struct(tt)
	func = _make_func("ns", locals_=["p"], types={"p": ns_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="p", value="a"))
	entry.instructions.append(M.StoreLocal(local="p", value="b"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)

	plan, census, _ = build_destructible_plan(func, type_table=tt)
	ns = plan.decisions_for_site("nullsafe")
	assert census["nullsafe"] == 2 == len(ns)
	assert census["site4_must_drop"] == 0 and census["site4_must_not_drop"] == 0
	for d in ns:
		assert isinstance(d.payload, NullsafePayload)
		assert d.payload.ty == ns_ty


def test_planner_stops_on_marked_synthetic_nullsafe():
	tt = TypeTable()
	ns_ty = _nullsafe_struct(tt)
	func = _make_func("synth", locals_=["p"], types={"p": ns_ty})
	entry = M.BasicBlock(name="entry")
	st = M.StoreLocal(local="p", value="a")
	setattr(st, "synthetic_zero_back", True)   # marked at the pre-normalization surface
	entry.instructions.append(st)
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)

	with pytest.raises(PlannerStop, match="synthetic_zero_back"):
		build_destructible_plan(func, type_table=tt)


def test_planner_finalizes_against_original_snapshot():
	"""validate_and_freeze runs against the original MIR; the plan's anchors
	are the exact original objects."""
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("fin", locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	st = M.StoreLocal(local="x", value="t")
	entry.instructions.append(st)
	ret = M.Return(value=None)
	entry.terminator = ret
	func.blocks["entry"] = entry
	_attach_ledger(func)

	plan, _, _ = build_destructible_plan(func, type_table=tt)
	# The site-4 anchor is the exact original StoreLocal object; site-3 the ret.
	assert plan.decisions_for_site("site4")[0].obj is st
	assert plan.decisions_for_site("site3")[0].obj is ret
	# A fresh session over the (unmutated) func locates every anchor.
	with plan.open_session(func) as sess:
		for d in plan.all_decisions():
			sess.locate(d)


def test_planner_requires_ledger_even_with_no_site4_candidate():
	"""Ledger A is required at the planner slot regardless of population: a
	function with NO non-null-safe destructible store (no site-4 candidate)
	must still fail closed when no ledger is attached."""
	tt = TypeTable()
	int_ty = tt.ensure_int() if hasattr(tt, "ensure_int") else tt.ensure_string()
	func = _make_func("noledger", locals_=["n"], types={"n": int_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="n", value="c"))  # non-destructible
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	# No ledger attached.
	with pytest.raises(AssertionError, match="requires an attached ledger"):
		build_destructible_plan(func, type_table=tt)


def test_planner_dirty_ledger_fails():
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("dirty", locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	setattr(func, "_ledger_dirty_reason", "test.mutation")  # dirty
	with pytest.raises(AssertionError, match="STALE ledger"):
		build_destructible_plan(func, type_table=tt)


def test_site4_payload_rejects_path_dependent():
	"""PATH_DEPENDENT must be UNCONSTRUCTIBLE as an emission payload — an
	emitter must never silently treat it as 'do not emit'."""
	with pytest.raises(ValueError, match="PATH_DEPENDENT is never a payload"):
		Site4Payload(local="x", ty=1, needs_drop=True, verdict=DropVerdict.PATH_DEPENDENT)
	# The two legitimate verdicts construct fine.
	assert Site4Payload(local="x", ty=1, needs_drop=True, verdict=DropVerdict.MUST_DROP).emit is True
	assert Site4Payload(local="x", ty=1, needs_drop=False, verdict=DropVerdict.MUST_NOT_DROP).emit is False
