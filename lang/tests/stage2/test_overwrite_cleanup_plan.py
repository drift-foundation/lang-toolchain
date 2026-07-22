# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Emission teeth for the B2+C S4 destructible plan CONSUMER in
`overwrite_cleanup.insert_overwrite_cleanup(..., plan=...)`.

The null-safe + site-4 drop-before-overwrite destructible cleanups
formerly authored inline by `string_arc` now emit here, driven by the
FROZEN `CleanupPlan` from `destructible_planner`.  These tests build a
tiny function carrying (a) a null-safe destructible store, (b) a site-4
MUST_DROP store (overwrite of a live destructible local), and (c) a
site-4 MUST_NOT_DROP store (first store into a destructible local), plan
it against a fresh ledger, then run the consumer and assert the exact
drop-before-store emission, object preservation, and consumption bijection.
"""
from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import DropVerdict, build_ledger
from lang.driftc.stage2.destructible_planner import build_destructible_plan
from lang.driftc.stage2.overwrite_cleanup import insert_overwrite_cleanup


# ── helpers copied from test_destructible_planner.py ──
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


def _canonical_drop_before(instrs, store_idx, local, ty) -> bool:
	"""True iff the four instrs immediately before `instrs[store_idx]` are
	the canonical destructible drop sequence for (local, ty):
	LoadLocal(tmp, local) / ZeroValue(zero, ty) / StoreLocal(local, zero) /
	DropValue(tmp, ty).  The `synthetic_zero_back` provenance tag is NOT
	checked here: the pass strips it (with `ow_authored_for`) after its
	validators consume it (S8 debt 2 — no transient attrs in output MIR)."""
	if store_idx < 4:
		return False
	load, zv, zb, drop = instrs[store_idx - 4:store_idx]
	return (
		isinstance(load, M.LoadLocal) and load.local == local
		and isinstance(zv, M.ZeroValue) and zv.ty == ty
		and isinstance(zb, M.StoreLocal) and zb.local == local
		and zb.value == zv.dest
		and isinstance(drop, M.DropValue) and drop.value == load.dest
		and drop.ty == ty
	)


def _idx_of(instrs, obj) -> int:
	for i, ins in enumerate(instrs):
		if ins is obj:
			return i
	raise AssertionError("store object not found after rewrite")


def test_plan_consumer_emits_nullsafe_and_site4_drops():
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	ns_ty = _nullsafe_struct(tt)
	func = _make_func("mix", locals_=["p", "x"], types={"p": ns_ty, "x": drop_ty})
	entry = M.BasicBlock(name="entry")
	ns_store = M.StoreLocal(local="p", value="a")        # null-safe -> drop before
	s4_first = M.StoreLocal(local="x", value="t_init")   # site-4 MUST_NOT_DROP -> nothing
	s4_over = M.StoreLocal(local="x", value="t_new")     # site-4 MUST_DROP -> drop before
	entry.instructions.extend([ns_store, s4_first, s4_over])
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)

	plan, census, _ = build_destructible_plan(func, type_table=tt)
	assert census["nullsafe"] == 1
	assert census["site4_must_drop"] == 1
	assert census["site4_must_not_drop"] == 1

	# Verdicts as expected before we consume.
	s4 = {id(d.obj): d.payload.verdict for d in plan.decisions_for_site("site4")}
	assert s4[id(s4_first)] is DropVerdict.MUST_NOT_DROP
	assert s4[id(s4_over)] is DropVerdict.MUST_DROP

	insert_overwrite_cleanup(func, type_table=tt, plan=plan)

	instrs = func.blocks["entry"].instructions
	# Original StoreLocal objects preserved (identity + value).
	i_ns = _idx_of(instrs, ns_store)
	i_first = _idx_of(instrs, s4_first)
	i_over = _idx_of(instrs, s4_over)
	assert ns_store.value == "a" and s4_first.value == "t_init" and s4_over.value == "t_new"

	# Null-safe store: canonical drop-before-store for p.
	assert _canonical_drop_before(instrs, i_ns, "p", ns_ty)
	# Site-4 MUST_DROP store: canonical drop-before-store for x.
	assert _canonical_drop_before(instrs, i_over, "x", drop_ty)
	# Site-4 MUST_NOT_DROP store: NOTHING emitted before it.
	assert not _canonical_drop_before(instrs, i_first, "x", drop_ty)

	# Exactly two DropValue drops emitted overall (nullsafe p + site-4 x).
	drops = [i for i in instrs if isinstance(i, M.DropValue)]
	assert len(drops) == 2
	assert {d.ty for d in drops} == {ns_ty, drop_ty}

	# Both consumed sites fully consumed (site-3 Return survives for a later
	# phase, so a scoped assertion — not assert_all_consumed).
	plan.assert_sites_consumed({"nullsafe", "site4"})


def test_plan_consumer_none_plan_is_refused():
	"""The plan is MANDATORY (item 2): passing `plan=None` raises a clean
	internal error rather than silently authoring nothing — a missing plan
	must never mean skipped destructible cleanup."""
	from lang.driftc.stage2.cleanup_plan import PlanContractError

	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("noplan", locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	s0 = M.StoreLocal(local="x", value="t0")
	s1 = M.StoreLocal(local="x", value="t1")
	entry.instructions.extend([s0, s1])
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry

	with pytest.raises(PlanContractError, match="CleanupPlan is REQUIRED"):
		insert_overwrite_cleanup(func, type_table=tt, plan=None)


# ── item 3: emission bijection teeth (fail BEFORE commit) ──


def _canonical_plan_seq(local, ty, *, tmp, zero):
	"""Build the canonical drop sequence for (local, ty). Returns
	(instrs, drop) — the drop is recorded in the emitter-local side table,
	NOT tagged onto the MIR node."""
	load = M.LoadLocal(dest=tmp, local=local)
	zv = M.ZeroValue(dest=zero, ty=ty)
	zb = M.StoreLocal(local=local, value=zero)
	setattr(zb, "synthetic_zero_back", True)
	drop = M.DropValue(value=tmp, ty=ty)
	return [load, zv, zb, drop], drop


def test_plan_bijection_suppressed_authoring_fails():
	"""An emitting decision with no authored drop in the side table fails the
	pre-commit bijection (suppressed authoring)."""
	from lang.driftc.stage2 import overwrite_cleanup as OC
	from lang.driftc.stage2.cleanup_plan import PlanContractError

	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("suppress", locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	store = M.StoreLocal(local="x", value="v")
	entry.instructions.append(store)
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	emit_anchors = {id(store): (store, "x", drop_ty, True)}
	with pytest.raises(PlanContractError, match="suppressed authoring"):
		OC._validate_plan_emission(func, emit_anchors, set(), [])   # empty side table


def test_plan_bijection_duplicate_authoring_fails():
	"""A store authored TWICE in the side table fails (duplicate authoring)."""
	from lang.driftc.stage2 import overwrite_cleanup as OC
	from lang.driftc.stage2.cleanup_plan import PlanContractError

	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("dup", locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	store = M.StoreLocal(local="x", value="v")
	seq1, d1 = _canonical_plan_seq("x", drop_ty, tmp="%t1", zero="%z1")
	seq2, d2 = _canonical_plan_seq("x", drop_ty, tmp="%t2", zero="%z2")
	entry.instructions.extend(seq1 + seq2 + [store])
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	emit_anchors = {id(store): (store, "x", drop_ty, True)}
	with pytest.raises(PlanContractError, match="duplicate authoring"):
		OC._validate_plan_emission(func, emit_anchors, set(), [(id(store), d1), (id(store), d2)])


def test_plan_bijection_orphan_authoring_fails():
	"""A side-table drop for a non-emitting store fails (orphan authoring)."""
	from lang.driftc.stage2 import overwrite_cleanup as OC
	from lang.driftc.stage2.cleanup_plan import PlanContractError

	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("orphan", locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	store = M.StoreLocal(local="x", value="v")
	seq, d = _canonical_plan_seq("x", drop_ty, tmp="%t1", zero="%z1")
	entry.instructions.extend(seq + [store])
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	with pytest.raises(PlanContractError, match="orphan authoring"):
		OC._validate_plan_emission(func, {}, set(), [(id(store), d)])


def test_plan_bijection_must_not_drop_authored_fails():
	"""A MUST_NOT_DROP site-4 store that authored a drop fails (must emit
	nothing)."""
	from lang.driftc.stage2 import overwrite_cleanup as OC
	from lang.driftc.stage2.cleanup_plan import PlanContractError

	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("mnd", locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	store = M.StoreLocal(local="x", value="v")
	seq, d = _canonical_plan_seq("x", drop_ty, tmp="%t1", zero="%z1")
	entry.instructions.extend(seq + [store])
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	# store is in emit_anchors AND must_not_drop → authored a drop illegally.
	emit_anchors = {id(store): (store, "x", drop_ty, True)}
	with pytest.raises(PlanContractError, match="MUST_NOT_DROP"):
		OC._validate_plan_emission(func, emit_anchors, {id(store)}, [(id(store), d)])


def test_plan_bijection_removed_anchor_fails_closed():
	"""item 4: an emitting store absent from the function after authoring
	fails as PlanContractError, not raw KeyError."""
	from lang.driftc.stage2 import overwrite_cleanup as OC
	from lang.driftc.stage2.cleanup_plan import PlanContractError

	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("removed", locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	seq, d = _canonical_plan_seq("x", drop_ty, tmp="%t1", zero="%z1")
	entry.instructions.extend(seq)          # the store itself is NOT in the block
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	store = M.StoreLocal(local="x", value="v")   # emit anchor, but absent from func
	emit_anchors = {id(store): (store, "x", drop_ty, True)}
	with pytest.raises(PlanContractError, match="absent from the function"):
		OC._validate_plan_emission(func, emit_anchors, set(), [(id(store), d)])
