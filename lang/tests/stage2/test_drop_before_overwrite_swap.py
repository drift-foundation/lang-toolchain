# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
`drop_before_overwrite` (site 4) — consumer-swap + Tier-1 promotion pin.

This file pins BOTH historical milestones on site 4 in `string_arc.py`'s
StoreLocal-rewrite loop.  They landed in sequence and the tests here
still cover each contract:

Phase 3B step 1 — consumer-swap:
- The drop verdict at every `StoreLocal(L, _)` for a destructible
  non-array, non-nullsafe local is read from the 3A ledger's
  `verdict_at`, with `needs_drop` from `compute_drop_policy` (the
  canonical `DropPolicy.needs_drop` axis, NOT raw `TypeTable.has_drop`).
- For `MustDrop` / `MustNotDrop` the ledger is authoritative.
- The site continues to emit observe-mode telemetry records so
  observe runs can catch any new bucket-5/6 class a swap introduces.

Phase 4 Tier-1 promotion (2026-04-23):
- The site no longer authors or consults the legacy
  `initialized_destructibles` dataflow fallback — the set is deleted
  from `string_arc.py`, along with its post-StoreLocal `.add` and
  post-MoveOut `.discard` maintenance.  Site 4 is pure ledger
  authority.
- Cases that previously downgraded to the fallback now raise
  `RuntimeError` as proof-obligation tripwires:
  - `_ledger is None` — caller invoked `insert_string_arc` without
    attaching the driver-built ledger.
  - `verdict is PathDependent` — the lattice produced `MaybeUninit`
    at a StoreLocal point.  Unreached across 1031 e2e cases at
    promotion time; raise fires if a future change breaks that.

Tests in this file build minimal MIR fixtures and exercise both
contracts through `insert_string_arc` directly, asserting MIR-shape
outcomes.
"""

from __future__ import annotations

import os

from lang.driftc.checker import FnInfo
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
# BARE-USE SAFETY: this file's funcs construct NO family producers,
# so bare insert_string_arc leaves nothing under-released (string_arc
# authors no last-use releases of its own — tripwire-deletion slice,
# 2026-07-18).  Adding family temps with non-consuming last uses
# requires the _run_pipeline pattern
# (see test_string_arc_audit_reporter.py).
from lang.driftc.stage2.string_arc import insert_string_arc


def _make_droppable_struct(type_table: TypeTable) -> int:
	"""A struct with a String field — string_arc's `_type_needs_drop`
	returns True iff any field transitively needs drop, so the String
	field is what gets the struct into `destructible_locals`.  A
	destructor_fns entry makes it non-nullsafe (so it goes through
	the conditional `initialized_destructibles` flow, which is the
	site-4 swap path)."""
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
	`func._ownership_ledger`.  Required for site 4 to consult it."""
	from lang.driftc.stage2.ownership_ledger import build_ledger
	ledger = build_ledger(func, drop_policy=lambda _t: None)
	setattr(func, "_ownership_ledger", ledger)


def _drop_just_before_storelocal(func: M.MirFunc, local_name: str, value_name: str) -> bool:
	"""Returns True iff there's a `_drop_destructible_local` 4-instruction
	pattern immediately before a `StoreLocal(local_name, value_name)`.

	The pattern is exactly what `_drop_destructible_local` emits:
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
	"""Ledger says `Live` at the second StoreLocal → MustDrop →
	site emits `_drop_destructible_local` sequence (legacy and ledger
	agree).  This is the common case the smoke run reported at 100 %
	agreement."""
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	func = _make_func("overwrite_live", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.StoreLocal(local="x", value="t_new"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	insert_string_arc(func, type_table=type_table, fn_infos={})
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
	reports `Uninit` (pre-state) → MustNotDrop → site skips drop.
	Legacy and ledger agree."""
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	func = _make_func("first_store", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	insert_string_arc(func, type_table=type_table, fn_infos={})
	assert not _drop_just_before_storelocal(func, "x", "t_init"), (
		"swap regressed: emitted a drop-before-overwrite at the first "
		"StoreLocal of an uninitialized local — would drop garbage"
	)


def test_swap_skips_drop_after_moveout_zero_store_pattern() -> None:
	"""After a MoveOut (which string_arc expands to LoadLocal +
	ZeroValue + StoreLocal), the local's ledger state is `Tombstoned`.
	A subsequent StoreLocal would have ledger pre-state =
	Tombstoned → MustNotDrop → skip drop.  Confirms the swap respects
	the same "skip drop on tombstoned" semantic the legacy
	`moved_out_locals.discard` flow encoded."""
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	func = _make_func("moveout_then_store", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.MoveOut(dest="t_consumed", local="x", ty=drop_ty))
	# After the MoveOut+expansion, x storage is zero (Tombstoned).
	# A subsequent StoreLocal should NOT drop the zero.
	entry.instructions.append(M.StoreLocal(local="x", value="t_replacement"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	insert_string_arc(func, type_table=type_table, fn_infos={})
	# At the post-move replacement store `StoreLocal(x, t_replacement)`,
	# the ledger pre-state is `Tombstoned`, so MustNotDrop → site 4
	# must NOT emit a drop-before-overwrite immediately before that
	# StoreLocal.  (Site 3 may still emit a function-exit cleanup for
	# `x` later, which is fine and out of scope for this test.)
	assert not _drop_just_before_storelocal(func, "x", "t_replacement"), (
		"swap regressed: emitted a drop-before-overwrite on a "
		"Tombstoned local → would call destroy on null/zero bytes"
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
	insert_string_arc(func, type_table=type_table, fn_infos={})
	assert not _drop_just_before_storelocal(func, "x", "t_new"), (
		"swap emitted drop-before-overwrite for a POD Int — DropPolicy"
		".needs_drop is False so site 4 must skip emission.  If this "
		"fails, site 4 is no longer reading DropPolicy or DropPolicy "
		"is over-classifying POD types as needing drop."
	)


# -- Observe mode still emits records after the swap -----------------------


def test_swap_emits_observe_records_when_flag_on(capfd) -> None:
	"""Confirms that the swap retains the observe-mode telemetry path:
	when `DRIFT_COMPILER_DEBUG='{"ownership_ledger":true}'` is set,
	site 4 still emits a `[drift:ownership_ledger]` record per
	StoreLocal it processes for a destructible local.  This is the
	signal that lets future observe re-runs catch a new bucket-5/6
	class introduced by a later 3B swap."""
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
		insert_string_arc(func, type_table=type_table, fn_infos={})
	finally:
		drift_debug._cached_flags = None
		if old_env is None:
			os.environ.pop("DRIFT_COMPILER_DEBUG", None)
		else:
			os.environ["DRIFT_COMPILER_DEBUG"] = old_env
	captured = capfd.readouterr()
	assert "[drift:ownership_ledger]" in captured.err, (
		"observe-mode telemetry from site 4 was lost in the swap: no "
		"`[drift:ownership_ledger]` records reached stderr.  Future "
		"observe re-runs would silently miss any new bucket-5/6 class "
		"introduced by subsequent swaps"
	)
	assert "drop_before_overwrite" in captured.err, (
		"expected `drop_before_overwrite` site tag in the observe "
		"records emitted by the swap"
	)


# -- Tier-1 proof-obligation tripwires (Phase 4 post-3c) ------------------


def test_tier1_raises_when_ledger_unattached() -> None:
	"""Post-promotion: the fallback `initialized_destructibles` state
	is gone.  A caller that runs `insert_string_arc` without attaching
	`func._ownership_ledger` first MUST fail loudly — silent wrong
	behaviour here would reintroduce the split authority that the
	Tier-1 promotion retired."""
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
	with pytest.raises(RuntimeError, match="without an attached ownership ledger"):
		insert_string_arc(func, type_table=type_table, fn_infos={})


def test_tier1_raises_on_path_dependent_verdict() -> None:
	"""Post-promotion: PathDependent at a drop_before_overwrite point
	is the proof-obligation tripwire.  Today's lattice never produces
	MaybeUninit at any real StoreLocal in observe (1031/1031 cases
	clean); if a future change starts producing it, this raise fires
	so we investigate before silently falling back to legacy.

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
	# Site 4 runs on this StoreLocal.  state_pre at `join:0` = MAYBE_UNINIT.
	join.instructions.append(M.StoreLocal(local="x", value="t_new"))
	join.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	func.blocks["a"] = a
	func.blocks["b"] = b
	func.blocks["join"] = join
	_attach_ledger(func)
	with pytest.raises(RuntimeError, match="returned PathDependent"):
		insert_string_arc(func, type_table=type_table, fn_infos={})
