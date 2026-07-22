# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
`drop_before_overwrite` (site 4) — consumer-swap + Tier-1 promotion pin.

This file pins BOTH historical milestones on site 4, exercised through
the PRODUCTION-FAITHFUL pipeline (B2+C S8 item 6 repair): the site-4
verdict is decided at the pre-string_arc PLAN slot
(`build_destructible_plan` → `site4_verdict`, the closed authority) and
the drop-before-overwrite sequence is EMITTED by `overwrite_cleanup`'s
plan phase — string_arc no longer owns any part of site 4.

Phase 3B step 1 — consumer-swap:
- The drop verdict at every `StoreLocal(L, _)` for a destructible
  non-array, non-nullsafe local is read from the 3A ledger's
  `verdict_at`, with `needs_drop` from `compute_drop_policy` (the
  canonical `DropPolicy.needs_drop` axis, NOT raw `TypeTable.has_drop`).
- For `MustDrop` / `MustNotDrop` the ledger is authoritative.
- The site continues to emit observe-mode telemetry records (now from
  the planner's site-4 arm) so observe runs can catch any new
  bucket-5/6 class a swap introduces.

Phase 4 Tier-1 promotion (2026-04-23):
- No `initialized_destructibles` dataflow fallback anywhere.  Site 4 is
  pure ledger authority.
- Cases that previously downgraded to the fallback now fail loudly as
  proof-obligation tripwires, at the PLAN slot in production:
  - missing ledger — `require_fresh_ledger` refuses the plan build
    (`AssertionError`); the authority-level `site4_verdict` keeps its
    own missing-ledger `RuntimeError` for direct callers.
  - `verdict is PathDependent` — the lattice produced `MaybeUninit`
    at a StoreLocal point.  Unreached across 1031 e2e cases at
    promotion time; the planner-hosted raise fires if a future change
    breaks that.

Tests build minimal MIR fixtures and run the driver's per-fn ownership
sequence: plan (ledger A) → string_arc → unified Return cleanup →
overwrite cleanup, asserting MIR-shape outcomes.
"""

from __future__ import annotations

import os

from lang.driftc.checker import FnInfo
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.string_arc import insert_string_arc


def _make_droppable_struct(type_table: TypeTable) -> int:
	"""A struct with a String field — `_type_needs_drop` returns True iff
	any field transitively needs drop, so the String field is what gets
	the struct into `destructible_locals`.  A destructor_fns entry makes
	it non-nullsafe (so it takes the site-4 verdict path, not the
	null-safe arm)."""
	string_ty = type_table.ensure_string()
	arc_tid = type_table.declare_struct(module_id="test", name="DropMe", field_names=["inner"])
	type_table.define_struct_fields(arc_tid, field_types=[string_ty])
	destroy_fn = FunctionId(module="test", name="DropMe::destroy", ordinal=0)
	type_table.destructor_fns = {arc_tid: destroy_fn}
	non_copy: set[int] = {arc_tid}
	type_table._copy_query = lambda tid: False if tid in non_copy else None  # type: ignore[attr-defined]
	return arc_tid


def _make_func(name: str, *, params: list[str], locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="test", name=name, ordinal=0)
	return M.MirFunc(
		name=f"test::{name}",
		params=list(params),
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


def _attach_ledger(func: M.MirFunc) -> None:
	"""Mirror the driver wiring: build the 3A ledger and attach as
	`func._ownership_ledger` (fresh — no dirty reason).  Required for
	the plan build to consult it."""
	from lang.driftc.stage2.ownership_ledger import build_ledger
	ledger = build_ledger(func, drop_policy=lambda _t: None)
	setattr(func, "_ownership_ledger", ledger)
	setattr(func, "_ledger_dirty_reason", None)


def _run_production_pipeline(func: M.MirFunc, type_table: TypeTable, fn_infos=None):
	"""The driver's per-fn ownership sequence post-B2+C (production-
	faithful): frozen plan at the ledger-A slot → string_arc → unified
	Return cleanup → overwrite cleanup (the null-safe + site-4 consumer).
	Returns the consumed plan."""
	from lang.driftc.stage2.destructible_planner import build_destructible_plan
	from lang.driftc.stage2.overwrite_cleanup import insert_overwrite_cleanup
	from lang.driftc.stage2.return_cleanup_emitter import emit_return_cleanups
	fi = fn_infos if fn_infos is not None else {}
	plan, _census, _c1 = build_destructible_plan(func, type_table=type_table)
	insert_string_arc(func, type_table=type_table, fn_infos=fi)
	emit_return_cleanups(func, plan)
	insert_overwrite_cleanup(func, type_table=type_table, plan=plan)
	return plan


def _drop_just_before_storelocal(func: M.MirFunc, local_name: str, value_name: str) -> bool:
	"""Returns True iff there's a canonical destructible-drop 4-instruction
	pattern immediately before a `StoreLocal(local_name, value_name)`.

	The pattern is exactly what the overwrite plan phase emits:
	  LoadLocal(tmp, L); ZeroValue(z); StoreLocal(L, z); DropValue(tmp).

	Distinguishing from site 3 emissions: site-3's drop appears at
	function-exit (before a Return terminator), not immediately before
	a user-level StoreLocal in the middle of the function.  This helper
	checks only the four-instruction window preceding a specific
	StoreLocal target, ignoring drops elsewhere."""
	for blk in func.blocks.values():
		instrs = blk.instructions
		for i, ins in enumerate(instrs):
			if not (isinstance(ins, M.StoreLocal) and ins.local == local_name and ins.value == value_name):
				continue
			if i < 4:
				continue
			a, b, c, d = instrs[i - 4], instrs[i - 3], instrs[i - 2], instrs[i - 1]
			if not (isinstance(a, M.LoadLocal) and a.local == local_name):
				continue
			if not isinstance(b, M.ZeroValue):
				continue
			if not (isinstance(c, M.StoreLocal) and c.local == local_name and c.value == b.dest):
				continue
			if not (isinstance(d, M.DropValue) and d.value == a.dest):
				continue
			return True
	return False


# -- Swap retains existing behaviour for the common case --------------------


def test_swap_emits_drop_before_overwrite_when_local_is_live() -> None:
	"""Ledger says `Live` at the second StoreLocal → MustDrop → the
	overwrite plan phase emits the canonical drop sequence (legacy and
	ledger agree).  This is the common case the smoke run reported at
	100 % agreement."""
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	func = _make_func("overwrite_live", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.StoreLocal(local="x", value="t_new"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	_run_production_pipeline(func, type_table)
	assert _drop_just_before_storelocal(func, "x", "t_new"), (
		"swap broke the common case: ledger reports Live at the "
		"second StoreLocal but no drop-before-overwrite sequence was "
		"emitted immediately before `StoreLocal(x, t_new)`"
	)
	assert not _drop_just_before_storelocal(func, "x", "t_init"), (
		"swap regressed: emitted a drop-before-overwrite at the FIRST "
		"StoreLocal (initial assignment of x) — would drop garbage"
	)


def test_swap_skips_drop_at_first_store_when_local_is_uninit() -> None:
	"""At the first StoreLocal of an uninitialized local, the ledger
	reports `Uninit` (pre-state) → MustNotDrop → no emission.
	Legacy and ledger agree."""
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	func = _make_func("first_store", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	_run_production_pipeline(func, type_table)
	assert not _drop_just_before_storelocal(func, "x", "t_init"), (
		"swap regressed: emitted a drop-before-overwrite at the first "
		"StoreLocal of an uninitialized local — would drop garbage"
	)


def test_swap_skips_drop_after_moveout_zero_store_pattern() -> None:
	"""After a MoveOut, the local's ledger state at the plan window is
	`MovedOut` — a subsequent StoreLocal has verdict MustNotDrop → no
	drop emission.  Confirms the swap respects the same "skip drop on
	consumed storage" semantic the legacy `moved_out_locals.discard`
	flow encoded."""
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	func = _make_func("moveout_then_store", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.MoveOut(dest="t_consumed", local="x", ty=drop_ty))
	# After the MoveOut(+expansion), x storage is zero.  A subsequent
	# StoreLocal should NOT drop the zero.
	entry.instructions.append(M.StoreLocal(local="x", value="t_replacement"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	_run_production_pipeline(func, type_table)
	# At the post-move replacement store `StoreLocal(x, t_replacement)`,
	# the plan-window ledger pre-state is consumed storage, so
	# MustNotDrop → the plan must NOT emit a drop-before-overwrite
	# immediately before that StoreLocal.  (Site 3 may still emit a
	# function-exit cleanup for `x` later, which is fine and out of
	# scope for this test.)
	assert not _drop_just_before_storelocal(func, "x", "t_replacement"), (
		"swap regressed: emitted a drop-before-overwrite on consumed "
		"(zeroed) storage → would call destroy on null/zero bytes"
	)


# -- Swap uses canonical DropPolicy, not raw has_drop ----------------------


def test_swap_consults_compute_drop_policy_not_raw_has_drop() -> None:
	"""Site 4 (drop_before_overwrite) MUST consult `compute_drop_policy`
	rather than calling raw `TypeTable.has_drop` directly.  The
	original 2026-04 framing of this test pinned a Copy && has_drop
	type expecting `DropPolicy.needs_drop=False` (via the
	`copy_status is True → needs_drop=False` short-circuit) — which
	was the LANGUAGE_BUG the policy fix removed (2026-04-24, see
	`lang/tests/driver/test_drop_policy_copy_short_circuit_bug.py`).
	After the fix, `compute_drop_policy` correctly returns
	`needs_drop=True` for has_drop types regardless of Copy, so a
	drop-before-overwrite IS the correct emission for this
	scenario.

	Re-purposed: pin that the swap respects DropPolicy by using a
	pure POD type — `has_drop=False, copy_status=True (default for
	POD scalars), needs_drop=False`.  Site 4 must not emit
	drop-before-overwrite for a value the policy says doesn't need
	dropping."""
	type_table = TypeTable()
	int_ty = type_table.ensure_int()
	# Pure POD: Int.  has_drop=False, copy_status=True (POD scalar),
	# DropPolicy.needs_drop=False.  Site 4 must skip drop emission.
	from lang.driftc.stage2.drop_policy_compute import compute_drop_policy
	assert not type_table.has_drop(int_ty), "test setup: Int should not have drop"
	policy = compute_drop_policy(type_table, int_ty)
	assert policy.needs_drop is False, (
		"test setup: DropPolicy.needs_drop for POD Int should be False"
	)
	func = _make_func("pod_overwrite", params=[], locals_=["x"], types={"x": int_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.StoreLocal(local="x", value="t_new"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	_run_production_pipeline(func, type_table)
	assert not _drop_just_before_storelocal(func, "x", "t_new"), (
		"swap emitted drop-before-overwrite for a POD Int — DropPolicy"
		".needs_drop is False so site 4 must skip emission.  If this "
		"fails, site 4 is no longer reading DropPolicy or DropPolicy "
		"is over-classifying POD types as needing drop."
	)


# -- Observe mode still emits records after the swap -----------------------


def test_swap_emits_observe_records_when_flag_on(capfd) -> None:
	"""Confirms the migrated authority retains the observe-mode telemetry
	path: when `DRIFT_COMPILER_DEBUG='{"ownership_ledger":true}'` is set,
	the site-4 verdict computation (now at the PLAN slot in
	`destructible_planner`) still emits a `[drift:ownership_ledger]`
	record per StoreLocal it processes for a destructible local.  This is
	the signal that lets future observe re-runs catch a new bucket-5/6
	class introduced by a later swap."""
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	func = _make_func("observe_records", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.StoreLocal(local="x", value="t_new"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	old_env = os.environ.get("DRIFT_COMPILER_DEBUG")
	os.environ["DRIFT_COMPILER_DEBUG"] = '{"ownership_ledger":true}'
	# Bust the debug flag cache.
	from lang.driftc import debug as drift_debug
	drift_debug._cached_flags = None
	try:
		_run_production_pipeline(func, type_table)
	finally:
		drift_debug._cached_flags = None
		if old_env is None:
			os.environ.pop("DRIFT_COMPILER_DEBUG", None)
		else:
			os.environ["DRIFT_COMPILER_DEBUG"] = old_env
	captured = capfd.readouterr()
	assert "[drift:ownership_ledger]" in captured.err, (
		"observe-mode telemetry from site 4 was lost in the migration: no "
		"`[drift:ownership_ledger]` records reached stderr.  Future "
		"observe re-runs would silently miss any new bucket-5/6 class "
		"introduced by subsequent swaps"
	)
	assert "drop_before_overwrite" in captured.err, (
		"expected `drop_before_overwrite` site tag in the observe "
		"records emitted at the plan slot"
	)


# -- Tier-1 proof-obligation tripwires (Phase 4 post-3c) ------------------


def test_tier1_raises_when_ledger_unattached() -> None:
	"""Post-promotion: the fallback `initialized_destructibles` state is
	gone.  PRODUCTION path: the plan build refuses to run without a fresh
	attached ledger (`require_fresh_ledger`).  AUTHORITY path: a direct
	`site4_verdict` call with no ledger keeps the original Tier-1
	missing-ledger RuntimeError.  Silent wrong behaviour on either path
	would reintroduce the split authority the promotion retired."""
	import pytest
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	func = _make_func("ledger_missing", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.StoreLocal(local="x", value="t_new"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	# Deliberately do NOT call `_attach_ledger(func)`.
	from lang.driftc.stage2.destructible_planner import build_destructible_plan
	with pytest.raises(AssertionError, match="requires an attached ledger"):
		build_destructible_plan(func, type_table=type_table)
	# Authority-level tripwire preserved for direct callers.
	from lang.driftc.stage2.destructible_authority import site4_verdict
	with pytest.raises(RuntimeError, match="without an attached ownership ledger"):
		site4_verdict(
			None,
			fn_name=func.name,
			block_name="entry",
			instr_idx=1,
			local="x",
			local_ty=drop_ty,
			type_table=type_table,
		)


def test_tier1_raises_on_path_dependent_verdict() -> None:
	"""Post-promotion: PathDependent at a drop_before_overwrite point
	is the proof-obligation tripwire, raised at the PLAN slot in
	production.  Today's lattice never produces MaybeUninit at any real
	StoreLocal in observe (1031/1031 cases clean); if a future change
	starts producing it, this raise fires so we investigate before
	silently falling back to legacy.

	CFG to force MaybeUninit at `join:0`:

	    entry → (A stores x) → join
	    entry → (B skips)    → join
	    join:  StoreLocal(x, t_new)   ← site 4 queries here

	`block_in[join][x]` joins `LIVE` (A) with `UNINIT` (B) →
	`MAYBE_UNINIT` → `verdict_at(...)` returns `PATH_DEPENDENT`."""
	import pytest
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	func = _make_func(
		"path_dependent_raise",
		params=["cond"],
		locals_=["cond", "x"],
		types={"cond": type_table.ensure_bool(), "x": drop_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="cond", then_target="a", else_target="b")
	a = M.BasicBlock(name="a")
	a.instructions.append(M.StoreLocal(local="x", value="t_a"))
	a.terminator = M.Goto(target="join")
	b = M.BasicBlock(name="b")
	b.terminator = M.Goto(target="join")
	join = M.BasicBlock(name="join")
	# The plan build queries this StoreLocal.  state_pre at `join:0` =
	# MAYBE_UNINIT.
	join.instructions.append(M.StoreLocal(local="x", value="t_new"))
	join.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	func.blocks["a"] = a
	func.blocks["b"] = b
	func.blocks["join"] = join
	_attach_ledger(func)
	from lang.driftc.stage2.destructible_planner import build_destructible_plan
	with pytest.raises(RuntimeError, match="returned PathDependent"):
		build_destructible_plan(func, type_table=type_table)


# -- S8 debt (2): no transient attributes in output MIR --------------------


def test_pipeline_output_carries_no_transient_attrs() -> None:
	"""After the full production sequence, NO instruction carries
	`ow_authored_for` (host-process object ids) or `synthetic_zero_back`
	(migration provenance) — overwrite_cleanup strips both once every
	consumer has run (SLICE-B §10 debt 2)."""
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	string_ty = type_table.ensure_string()
	func = _make_func(
		"strip_attrs", params=[], locals_=["x", "s"],
		types={"x": drop_ty, "s": string_ty},
	)
	entry = M.BasicBlock(name="entry")
	# A destructible overwrite (site-4 MUST_DROP → plan-phase zero-back)
	# AND a String overwrite (R2 → ow_authored_for-tagged release).
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.StoreLocal(local="x", value="t_new"))
	entry.instructions.append(M.ConstString(dest="%c1", value="a"))
	entry.instructions.append(M.StoreLocal(local="s", value="%c1"))
	entry.instructions.append(M.ConstString(dest="%c2", value="b"))
	entry.instructions.append(M.StoreLocal(local="s", value="%c2"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	func.local_types["%c1"] = string_ty
	func.local_types["%c2"] = string_ty
	_attach_ledger(func)
	_run_production_pipeline(func, type_table)
	# The cleanups themselves must exist…
	assert _drop_just_before_storelocal(func, "x", "t_new")
	# …but carry no transient metadata.
	for blk in func.blocks.values():
		for ins in blk.instructions:
			assert not hasattr(ins, "ow_authored_for"), (
				f"{type(ins).__name__} still carries ow_authored_for after "
				f"the pipeline (object ids must not survive into output MIR)"
			)
			assert not hasattr(ins, "synthetic_zero_back"), (
				f"{type(ins).__name__} still carries synthetic_zero_back "
				f"after the pipeline (migration provenance must not survive)"
			)
