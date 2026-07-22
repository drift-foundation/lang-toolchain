# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""S3 teeth for the isolated site-3 Return-boundary emitter
(`lang/driftc/stage2/site3_return_emitter.py`).

The emitter consumes frozen `Site3ReturnPayload` decisions through the S1
`EmitterPhase` postflight lifecycle and appends the canonical drop
sequence before the PRESERVED `M.Return`. These tests lock: empty +
multi-drop payloads, exact ordering/sequence, Return-object + value/span
preservation, original-instruction preservation, and fail-closed on a
disturbed Return anchor. Focused tests only — no corpus for isolated S3.
"""
from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import build_ledger
from lang.driftc.stage2.destructible_planner import build_destructible_plan
from lang.driftc.stage2 import site3_return_emitter as SM
from lang.driftc.stage2.site3_return_emitter import emit_site3_returns
from lang.driftc.stage2.cleanup_plan import CleanupPlan, PlanContractError, anchor_term
from lang.driftc.stage2.cleanup_payloads import Site3ReturnPayload, Site3Drop


def _droppable_struct(tt, name="DropMe"):
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


def _make_func(name, *, locals_, types):
	return M.MirFunc(
		name=f"test::{name}", params=[], locals=list(locals_),
		fn_id=FunctionId(module="test", name=name, ordinal=0),
		local_types=dict(types),
	)


def _attach_ledger(func):
	setattr(func, "_ownership_ledger", build_ledger(func, drop_policy=lambda _t: None))
	setattr(func, "_ledger_dirty_reason", None)


def _kinds(block):
	return [type(i).__name__ for i in block.instructions]


def test_emit_multi_drop_sorted_sequence_and_return_preserved():
	tt = TypeTable()
	dty = _droppable_struct(tt)
	func = _make_func("multi", locals_=["x", "y"], types={"x": dty, "y": dty})
	entry = M.BasicBlock(name="entry")
	sx = M.StoreLocal(local="x", value="vx")
	sy = M.StoreLocal(local="y", value="vy")
	entry.instructions = [sx, sy]
	ret = M.Return(value=None)
	setattr(ret, "span", ("f", 7))
	entry.terminator = ret
	func.blocks["entry"] = entry
	_attach_ledger(func)

	plan, census = build_destructible_plan(func, type_table=tt)
	assert census["site3_locals"] == 2   # x and y dropped
	orig_ids = [id(i) for i in entry.instructions]

	n = emit_site3_returns(func, plan)
	assert n == 2

	# Original two StoreLocals are still first, same identities/order.
	assert [id(i) for i in entry.instructions[:2]] == orig_ids
	# Then the canonical drop sequence for x, then y (sorted order), 4 each.
	tail = _kinds(entry)[2:]
	assert tail == [
		"LoadLocal", "ZeroValue", "StoreLocal", "DropValue",   # x
		"LoadLocal", "ZeroValue", "StoreLocal", "DropValue",   # y
	]
	# The dropped locals are x then y (sorted); the zero-back store is marked.
	loads = [i for i in entry.instructions if isinstance(i, M.LoadLocal)]
	assert [ld.local for ld in loads] == ["x", "y"]
	zbs = [i for i in entry.instructions if isinstance(i, M.StoreLocal) and getattr(i, "synthetic_zero_back", False)]
	assert [z.local for z in zbs] == ["x", "y"]
	# Return object + value + span PRESERVED (same identity).
	assert entry.terminator is ret
	assert entry.terminator.value is None
	assert getattr(entry.terminator, "span") == ("f", 7)
	# Site-3 fully consumed.
	plan.assert_sites_consumed({"site3"})


def test_emit_empty_payload_appends_nothing():
	tt = TypeTable()
	func = _make_func("empty", locals_=[], types={})
	entry = M.BasicBlock(name="entry")
	entry.instructions = []
	ret = M.Return(value=None)
	entry.terminator = ret
	func.blocks["entry"] = entry
	_attach_ledger(func)

	plan, census = build_destructible_plan(func, type_table=tt)
	assert census["site3_returns"] == 1 and census["site3_locals"] == 0
	n = emit_site3_returns(func, plan)
	assert n == 0
	assert entry.instructions == []          # nothing appended
	assert entry.terminator is ret           # Return preserved
	plan.assert_sites_consumed({"site3"})


def test_emit_fails_closed_on_replaced_return():
	tt = TypeTable()
	dty = _droppable_struct(tt)
	func = _make_func("repl", locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.StoreLocal(local="x", value="v")]
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)

	plan, _ = build_destructible_plan(func, type_table=tt)
	# Disturb the Return anchor AFTER planning, BEFORE emit → preflight fails.
	entry.terminator = M.Return(value=None)
	with pytest.raises(PlanContractError):
		emit_site3_returns(func, plan)


def test_emit_fails_closed_on_type_drift():
	tt = TypeTable()
	dty = _droppable_struct(tt)
	func = _make_func("drift", locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.StoreLocal(local="x", value="v")]
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)

	plan, _ = build_destructible_plan(func, type_table=tt)
	func.local_types["x"] = 999999      # type binding drift on a dropped local
	with pytest.raises(PlanContractError, match="type binding"):
		emit_site3_returns(func, plan)


# ── item 4 hardening teeth ────────────────────────────────────────────


def test_temps_collision_proof_against_existing_s3d_name():
	"""A func already carrying a `.s3d*`-shaped name must NOT collide: fresh
	drop temps skip every pre-existing name."""
	tt = TypeTable()
	dty = _droppable_struct(tt)
	func = _make_func("coll", locals_=["x", ".s3d1"], types={"x": dty, ".s3d1": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.StoreLocal(local="x", value="v")]
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)

	plan, _ = build_destructible_plan(func, type_table=tt)
	emit_site3_returns(func, plan)
	# The pre-existing `.s3d1` must not be reused as an emitted dest.
	dests = [
		i.dest for i in entry.instructions
		if isinstance(i, (M.LoadLocal, M.ZeroValue))
	]
	assert ".s3d1" not in dests, dests
	assert len(dests) == 2 and all(d.startswith(".s3d") for d in dests), dests


def test_dirty_iff_emission():
	"""The ledger is marked dirty when ≥1 drop is emitted, and NOT marked
	when the emission is empty."""
	# emission → dirty
	tt = TypeTable()
	dty = _droppable_struct(tt)
	func = _make_func("d1", locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.StoreLocal(local="x", value="v")]
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	plan, _ = build_destructible_plan(func, type_table=tt)
	assert emit_site3_returns(func, plan) == 1
	assert getattr(func, "_ledger_dirty_reason", None) is not None

	# no emission → NOT dirty
	tt2 = TypeTable()
	func2 = _make_func("d0", locals_=[], types={})
	e2 = M.BasicBlock(name="entry")
	e2.instructions = []
	e2.terminator = M.Return(value=None)
	func2.blocks["entry"] = e2
	_attach_ledger(func2)
	plan2, _ = build_destructible_plan(func2, type_table=tt2)
	assert emit_site3_returns(func2, plan2) == 0
	assert getattr(func2, "_ledger_dirty_reason", None) is None


def test_malformed_payload_wrong_type_raises_plancontract():
	"""A site-3 decision whose payload is not a Site3ReturnPayload raises
	PlanContractError (never a raw AttributeError)."""
	tt = TypeTable()
	func = _make_func("bad", locals_=[], types={})
	entry = M.BasicBlock(name="entry")
	entry.instructions = []
	ret = M.Return(value=None)
	entry.terminator = ret
	func.blocks["entry"] = entry

	plan = CleanupPlan(func.name)
	plan.add(obj=ret, coord=anchor_term("entry", 0), site="site3",
	         fields={"value": None}, payload=("not", "a", "payload"))
	plan.validate_and_freeze(func)
	with pytest.raises(PlanContractError, match="expected Site3ReturnPayload"):
		emit_site3_returns(func, plan)


def test_malformed_drop_entry_raises_plancontract():
	"""A site-3 payload whose drop entry is not a Site3Drop (arbitrary
	lookalike/string) raises PlanContractError, never a raw AttributeError."""
	tt = TypeTable()
	func = _make_func("baddrop", locals_=[], types={})
	entry = M.BasicBlock(name="entry")
	entry.instructions = []
	ret = M.Return(value=None)
	entry.terminator = ret
	func.blocks["entry"] = entry

	plan = CleanupPlan(func.name)
	plan.add(obj=ret, coord=anchor_term("entry", 0), site="site3",
	         fields={"value": None}, payload=Site3ReturnPayload(drops=("bogus",)))
	plan.validate_and_freeze(func)
	with pytest.raises(PlanContractError, match="expected Site3Drop"):
		emit_site3_returns(func, plan)


def _s3_seq(tmp, zero, local, ty):
	"""Canonical site-3 drop sequence; returns (instrs, DropValue)."""
	load = M.LoadLocal(dest=tmp, local=local)
	zv = M.ZeroValue(dest=zero, ty=ty)
	zb = M.StoreLocal(local=local, value=zero)
	setattr(zb, "synthetic_zero_back", True)
	dv = M.DropValue(value=tmp, ty=ty)
	return [load, zv, zb, dv], dv


def test_site3_bijection_suppressed_authoring_fails():
	"""A planned site-3 drop with an EMPTY side table fails (missing)."""
	tt = TypeTable()
	dty = _droppable_struct(tt)
	func = _make_func("supp3", locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.StoreLocal(local="x", value="v")]
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	plan, _ = build_destructible_plan(func, type_table=tt)
	site3 = plan.decisions_for_site("site3")
	assert sum(len(d.payload.drops) for d in site3) == 1
	with pytest.raises(PlanContractError, match="missing/duplicate"):
		SM._validate_site3_emission(func, site3, {})   # empty side table


def test_site3_bijection_duplicate_authoring_fails():
	"""A planned site-3 drop authored TWICE (side table) fails."""
	tt = TypeTable()
	dty = _droppable_struct(tt)
	func = _make_func("dup3", locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.StoreLocal(local="x", value="v")]
	ret = M.Return(value=None)
	entry.terminator = ret
	func.blocks["entry"] = entry
	_attach_ledger(func)
	plan, _ = build_destructible_plan(func, type_table=tt)
	site3 = plan.decisions_for_site("site3")
	s1, d1 = _s3_seq(".a", ".b", "x", dty)
	s2, d2 = _s3_seq(".c", ".d", "x", dty)
	entry.instructions.extend(s1 + s2)
	# Side table records TWO drops for the single planned drop of x.
	with pytest.raises(PlanContractError, match="missing/duplicate"):
		SM._validate_site3_emission(func, site3, {id(ret): [d1, d2]})


def test_site3_bijection_reordered_sequence_fails():
	"""item 3: the emitted (local, ty) sequence must equal the payload's
	`sorted(destructible_locals)` order EXACTLY — a swapped order FAILS."""
	tt = TypeTable()
	dty = _droppable_struct(tt)
	func = _make_func("ord3", locals_=["x", "y"], types={"x": dty, "y": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.StoreLocal(local="x", value="vx"),
	                      M.StoreLocal(local="y", value="vy")]
	ret = M.Return(value=None)
	entry.terminator = ret
	func.blocks["entry"] = entry
	_attach_ledger(func)
	plan, _ = build_destructible_plan(func, type_table=tt)
	site3 = plan.decisions_for_site("site3")
	assert [d.local for d in site3[0].payload.drops] == ["x", "y"]   # sorted order
	# Author y's sequence BEFORE x's in the block (block order = [y, x]),
	# side table in payload order [dx, dy] → validator sorts by block position
	# → [dy, dx] != planned [x, y].
	sy, dy = _s3_seq(".ya", ".yb", "y", dty)
	sx, dx = _s3_seq(".xa", ".xb", "x", dty)
	entry.instructions.extend(sy + sx)
	with pytest.raises(PlanContractError, match="destruction order"):
		SM._validate_site3_emission(func, site3, {id(ret): [dx, dy]})


def test_site3_placement_not_contiguous_tail_fails():
	"""item 2 (S5-wiring placement rule): every authored sequence must be the
	CONTIGUOUS tail immediately before the preserved Return. A spurious
	instruction between the sequence and the Return fails placement."""
	tt = TypeTable()
	dty = _droppable_struct(tt)
	func = _make_func("place3", locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.StoreLocal(local="x", value="v")]
	ret = M.Return(value=None)
	entry.terminator = ret
	func.blocks["entry"] = entry
	_attach_ledger(func)
	plan, _ = build_destructible_plan(func, type_table=tt)
	site3 = plan.decisions_for_site("site3")
	assert [d.local for d in site3[0].payload.drops] == ["x"]
	# Author the canonical sequence, then a SPURIOUS store after it (so the
	# sequence is NOT the contiguous tail before the Return).
	seq, dv = _s3_seq(".a", ".b", "x", dty)
	entry.instructions.extend(seq + [M.StoreLocal(local="x", value="spurious")])
	with pytest.raises(PlanContractError, match="placement mismatch|contiguous"):
		SM._validate_site3_emission(func, site3, {id(ret): [dv]})


def test_site3_placement_wrong_block_return_fails():
	"""An authored decision whose Return is no longer its block's terminator
	(wrong block / replaced Return) fails placement."""
	tt = TypeTable()
	dty = _droppable_struct(tt)
	func = _make_func("wblk3", locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.StoreLocal(local="x", value="v")]
	ret = M.Return(value=None)
	entry.terminator = ret
	func.blocks["entry"] = entry
	_attach_ledger(func)
	plan, _ = build_destructible_plan(func, type_table=tt)
	site3 = plan.decisions_for_site("site3")
	seq, dv = _s3_seq(".a", ".b", "x", dty)
	entry.instructions.extend(seq)
	entry.terminator = M.Return(value=None)   # REPLACED the decision's Return
	with pytest.raises(PlanContractError, match="wrong block / replaced Return"):
		SM._validate_site3_emission(func, site3, {id(ret): [dv]})
