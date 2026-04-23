# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 — Return-as-move in the `LiveStateMap` transfer function.

Pins the ledger's new understanding that a `Return` terminator
consumes its operand's source local.  Carrier shapes K specified:

  1. Direct return move (already correct via `MoveOut`+`Return`) — the
     ledger has always handled this; pinned here so the new logic
     doesn't regress it.
  2. `LoadLocal(t, X); Return(t)` — the gap.  HIR knew the source
     code wrote `return X;` for an owned local but MIR's
     `_lower_return_value` falls through to plain `lower_expr` for
     some shapes (notably HVar projections / non-direct bases),
     yielding `LoadLocal` + `Return` without an explicit `MoveOut`.
     Pre-fix the ledger leaves X as `LIVE` at end-of-function;
     post-fix the ledger transitions X → `MOVED_OUT` at the
     `LoadLocal` index, so `state_pre` at any subsequent
     program point (e.g. site 1's scope-drop cursor that runs
     AFTER `_lower_return_value`) reflects the consumption.
  3. Non-return uses MUST NOT count as transfer.  A `LoadLocal(t, X)`
     whose `dest` is consumed by anything other than the `Return`
     operand chain (`use(t)`, `StoreLocal(Y, t)`, etc.) leaves X
     `LIVE`.

Tests build minimal MIR `MirFunc`s and exercise `build_ledger`
directly — no HIR lowering.  Site changes are deliberately deferred
per K's directive (this patch is purely a lattice enhancement).
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import (
	DropVerdict,
	LiveState,
	build_ledger,
)


_TY_OWNED = 101
_TY_OTHER = 202


def _drop_policy_stub(_ty: int) -> None:
	return None


def _make_func(
	name: str,
	*,
	params: list[str],
	locals_: list[str],
	types: dict[str, int],
) -> M.MirFunc:
	fn_id = FunctionId(module="main", name=name, ordinal=0)
	return M.MirFunc(
		name=name,
		params=list(params),
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


# -- Carrier 1: MoveOut + Return — already correct, pinned ----------------


def test_return_consume_direct_moveout_keeps_existing_behavior() -> None:
	"""`MoveOut(t, X); Return(t)` — `_apply` already transitions X →
	`MOVED_OUT` at the `MoveOut` index.  This test confirms the new
	Return-as-move logic doesn't double-apply or regress this shape."""
	func = _make_func("direct_moveout", params=[], locals_=["x"], types={"x": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.MoveOut(dest="t_ret", local="x", ty=_TY_OWNED))
	entry.terminator = M.Return(value="t_ret")
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["entry"]["x"] is LiveState.MOVED_OUT


# -- Carrier 2: LoadLocal + Return — newly modeled -----------------------


def test_return_consume_loadlocal_plus_return_marks_source_moved() -> None:
	"""`LoadLocal(t, X); Return(t)` — pre-fix X stays `LIVE` at
	end-of-block (and end-of-function), causing site 1 / site 3
	scope-drop disagreements (bucket 5: `implicit_return_move_gap`).
	Post-fix the ledger treats the LoadLocal as a `MoveOut`-equivalent
	when its dest feeds the Return terminator."""
	func = _make_func("loadlocal_return", params=[], locals_=["x"], types={"x": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.LoadLocal(dest="t_ret", local="x"))
	entry.terminator = M.Return(value="t_ret")
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["entry"]["x"] is LiveState.MOVED_OUT, (
		"LoadLocal feeding Return must be treated as a consumption — "
		"otherwise site 1's scope-drop helper, which records its "
		"verdict AFTER `_lower_return_value` emits the LoadLocal, "
		"would see Live at the cursor and disagree with the HIR's "
		"`_moved_locals` (bucket 5: implicit_return_move_gap)."
	)


# -- Per-instruction snapshot timing -------------------------------------


def test_return_consume_loadlocal_transitions_at_loadlocal_index() -> None:
	"""The transition must land AT THE LOADLOCAL INDEX (not deferred
	to block_out only) — site 1's scope-drop cursor is captured at a
	program point that comes AFTER `_lower_return_value` has emitted
	the LoadLocal but BEFORE the Return terminator is set.  If the
	transition only appears in block_out, mid-block `state_pre`
	queries continue to disagree with the site."""
	func = _make_func("snapshot_timing", params=[], locals_=["x"], types={"x": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	# Index 1: the LoadLocal that feeds the Return.
	entry.instructions.append(M.LoadLocal(dest="t_ret", local="x"))
	entry.terminator = M.Return(value="t_ret")
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# `state_pre(("entry", 2))` — the program point a hypothetical
	# scope-drop emitted AFTER the LoadLocal would record at — must
	# read the post-state of index 1 (the LoadLocal) and see
	# MOVED_OUT.
	assert ledger.state_pre(("entry", 2), "x") is LiveState.MOVED_OUT, (
		"per-instruction snapshot at the cursor AFTER the "
		"return-feeding LoadLocal must reflect the consumption — "
		"otherwise bucket 5 disagreement persists at site 1's "
		"scope-drop cursors emitted by `_emit_scope_drops` which "
		"runs after `_lower_return_value`."
	)


# -- Carrier 3: non-return uses MUST NOT count as transfer ---------------


def test_return_consume_loadlocal_with_non_return_use_keeps_local_live() -> None:
	"""`LoadLocal(t, X); StoreLocal(Y, t); Return(t)` — t is read by
	BOTH the `StoreLocal(Y, t)` and the `Return(t)`.  Conservative
	rule per K: any non-chain consumer of the LoadLocal's dest
	disqualifies the LoadLocal as a Return-consume candidate, so X
	stays `LIVE`.  This is what prevents the new logic from
	misclassifying Copy-trait or shared-load shapes as ownership
	transfers."""
	func = _make_func(
		"non_return_use",
		params=[],
		locals_=["x", "y"],
		types={"x": _TY_OWNED, "y": _TY_OWNED},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.LoadLocal(dest="t_shared", local="x"))
	# t_shared used non-as-Return-operand:
	entry.instructions.append(M.StoreLocal(local="y", value="t_shared"))
	entry.terminator = M.Return(value="t_shared")
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["entry"]["x"] is LiveState.LIVE, (
		"a LoadLocal whose dest has uses outside the Return chain "
		"must NOT be treated as a consumption — otherwise we'd "
		"silently move-out for shapes the source language considers "
		"shared (e.g., Copy types, or store-and-also-return for the "
		"same SSA value)."
	)


# -- LoadLocal where the loaded local is NOT returned --------------------


def test_return_consume_loadlocal_unrelated_to_return_keeps_local_live() -> None:
	"""`LoadLocal(t, X); use(t); Return(other)` — X is never reached
	by the Return chain, so X stays `LIVE`."""
	func = _make_func(
		"unrelated_load",
		params=[],
		locals_=["x", "other"],
		types={"x": _TY_OWNED, "other": _TY_OWNED},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_initx"))
	entry.instructions.append(M.StoreLocal(local="other", value="t_inito"))
	entry.instructions.append(M.LoadLocal(dest="t_x", local="x"))
	entry.instructions.append(M.LoadLocal(dest="t_other", local="other"))
	entry.terminator = M.Return(value="t_other")
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["entry"]["x"] is LiveState.LIVE
	assert ledger.block_out["entry"]["other"] is LiveState.MOVED_OUT


# -- Void return doesn't consume anything --------------------------------


def test_return_consume_void_return_consumes_nothing() -> None:
	"""`Return(value=None)` — no operand, no candidate."""
	func = _make_func("void_return", params=[], locals_=["x"], types={"x": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["entry"]["x"] is LiveState.LIVE


# -- AssignSSA chain --------------------------------------------------


def test_return_consume_through_assign_ssa_chain() -> None:
	"""`LoadLocal(t, X); AssignSSA(s, t); Return(s)` — the alias chain
	walks back through `AssignSSA` to find the original `LoadLocal`,
	and the consumption is recognised."""
	func = _make_func("assign_ssa_chain", params=[], locals_=["x"], types={"x": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.LoadLocal(dest="t_load", local="x"))
	entry.instructions.append(M.AssignSSA(dest="t_alias", src="t_load"))
	entry.terminator = M.Return(value="t_alias")
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["entry"]["x"] is LiveState.MOVED_OUT


# -- Cross-block isolation --------------------------------------------


def test_return_consume_does_not_poison_cross_block_state() -> None:
	"""A LoadLocal+Return in block B must NOT affect block A's view of
	the same local — the consumption is local to the Return block."""
	func = _make_func(
		"cross_block",
		params=[],
		locals_=["x"],
		types={"x": _TY_OWNED},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.terminator = M.Goto(target="b")
	b = M.BasicBlock(name="b")
	b.instructions.append(M.LoadLocal(dest="t_ret", local="x"))
	b.terminator = M.Return(value="t_ret")
	func.blocks["entry"] = entry
	func.blocks["b"] = b
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# entry's out_state — nothing consumed yet.
	assert ledger.block_out["entry"]["x"] is LiveState.LIVE
	# b's out_state — consumed by Return-feeding LoadLocal.
	assert ledger.block_out["b"]["x"] is LiveState.MOVED_OUT


# -- Tracked-local guard ---------------------------------------------


def test_return_consume_ignores_loadlocal_of_untracked_value() -> None:
	"""Defensive: a LoadLocal whose source isn't in `func.params` ∪
	`func.locals` is not tracked by the ledger — the new logic must
	not crash on it."""
	func = _make_func(
		"untracked_source",
		params=[],
		locals_=["x"],
		types={"x": _TY_OWNED},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	# LoadLocal of an unknown local — must not be tracked.
	entry.instructions.append(M.LoadLocal(dest="t_ret", local="not_a_real_local"))
	entry.terminator = M.Return(value="t_ret")
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# x should be unaffected.
	assert ledger.block_out["entry"]["x"] is LiveState.LIVE


# -- Verdict-level check -------------------------------------------


def test_return_consume_verdict_at_after_loadlocal_returns_must_not_drop() -> None:
	"""End-to-end: at the cursor immediately after the LoadLocal,
	`verdict_at(..., needs_drop=True)` returns `MUST_NOT_DROP` —
	the bucket-5 collapse condition.  Pre-fix it returned `MUST_DROP`."""
	func = _make_func("verdict_check", params=[], locals_=["x"], types={"x": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.LoadLocal(dest="t_ret", local="x"))
	entry.terminator = M.Return(value="t_ret")
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.verdict_at(("entry", 2), "x", needs_drop=True) is DropVerdict.MUST_NOT_DROP
