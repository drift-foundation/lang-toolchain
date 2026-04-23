# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 step 3c — site-2 per-field emission authority via the ledger.

Pins `trim_match_cleanup_by_ledger(func, needs_drop_fn=...)`:

  1. Ledger says `MUST_NOT_DROP` on the recorded field → the drop
     chain for that `drop_local` is excised (StoreLocal, every
     MoveOut(local=drop_local), and the DropValue paired with it).
  2. Ledger says `MUST_DROP` (field Live + needs_drop=True) → no
     trim; MIR is untouched.
  3. Non-drop-needing field (`needs_drop_fn` returns False) → even on
     a Live field, classify returns `MUST_NOT_DROP` and the drop
     chain is excised.  The test pins that site 2's legacy "emit
     anyway" branch (today shielded by `_needs_runtime_drop`) is a
     would-be-trim if it ever emitted for a POD field — defence-in-
     depth: the ledger is the final authority.
  4. Empty side table → no-op.
  5. `_match_cleanup_per_field_drops` not populated → no-op.
  6. Ledger unset → no-op (defensive: builds that skipped
     `build_ledger` must not crash).
  7. Multiple entries are handled independently.
  8. After a trim, `drop_local` is removed from `func.locals` /
     `func.local_types`.

Tests hand-build minimal `MirFunc`s so the trim pass is exercised in
isolation from HIR→MIR.  Real e2e coverage comes from the observe
re-run — step 3b already showed zero disagreement; this file pins the
behaviour of the trim pass itself when the ledger DOES disagree.
"""

from __future__ import annotations

from typing import Callable, Tuple

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import build_ledger
from lang.driftc.stage2.ownership_ledger_trim import trim_match_cleanup_by_ledger


_TY_VARIANT = 101
_TY_PAYLOAD = 202
_TY_REF = 303


def _drop_policy_stub(_ty: int) -> None:
	return None


def _make_func(
	name: str,
	*,
	locals_: list[str],
	types: dict[str, int],
) -> M.MirFunc:
	fn_id = FunctionId(module="main", name=name, ordinal=0)
	return M.MirFunc(
		name=name,
		params=[],
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


def _build_partial_move_func(
	fn_name: str,
	*,
	drop_local: str,
	emit_drop_chain: bool = True,
	include_binder_move: bool = True,
) -> tuple[M.MirFunc, tuple[str, int]]:
	"""
	Hand-builds the MIR shape that triggers trim.  When
	`include_binder_move=True`, a *binder* `VariantGetFieldAddr(s,
	Some, 0)` is emitted BEFORE the cleanup_point — modelling a prior
	consumer that moved the field — so the ledger's `field_state_pre`
	at the cleanup point reports `MOVED_OUT`.  Site 2 then
	(hypothetically) emitted a drop chain anyway, which the trim pass
	should remove.

	When `include_binder_move=False`, no binder move is emitted; the
	field stays `LIVE` at the cleanup point, and the trim pass keeps
	the drop chain.

	Returns `(func, cleanup_point)`.  `cleanup_point` is the program
	point captured AFTER the binder's VariantGetFieldAddr (so
	`state_pre` at this point reflects the binder's move), matching
	the telemetry convention of capturing program_point = current
	cursor index BEFORE site 2's own cleanup-chain emits.
	"""
	locals_ = ["s", drop_local]
	types = {"s": _TY_VARIANT, drop_local: _TY_PAYLOAD}
	func = _make_func(fn_name, locals_=locals_, types=types)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	entry.instructions.append(M.AddrOfLocal(dest="t_ref", local="s", is_mut=True))
	if include_binder_move:
		# Binder's field move — 3a marks `(s, ((Some, 0),))` MovedOut.
		entry.instructions.append(
			M.VariantGetFieldAddr(
				dest="t_binder_addr",
				variant_ref="t_ref",
				variant_ty=_TY_VARIANT,
				ctor="Some",
				field_index=0,
				field_ty=_TY_PAYLOAD,
			)
		)
	cleanup_point = ("entry", len(entry.instructions))
	# Site-2 cleanup setup (would be skipped in real code because
	# the binder already moved the field, but the test models the
	# hypothetical disagreement scenario).
	entry.instructions.append(
		M.VariantGetFieldAddr(
			dest="t_slot_addr",
			variant_ref="t_ref",
			variant_ty=_TY_VARIANT,
			ctor="Some",
			field_index=0,
			field_ty=_TY_PAYLOAD,
		)
	)
	entry.instructions.append(
		M.LoadRef(dest="t_slot_val", ptr="t_slot_addr", inner_ty=_TY_PAYLOAD)
	)
	entry.instructions.append(M.StoreLocal(local=drop_local, value="t_slot_val"))
	if emit_drop_chain:
		entry.instructions.append(
			M.MoveOut(dest="t_drop", local=drop_local, ty=_TY_PAYLOAD)
		)
		entry.instructions.append(M.DropValue(value="t_drop", ty=_TY_PAYLOAD))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	return func, cleanup_point


def _attach_ledger(func: M.MirFunc) -> None:
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	setattr(func, "_ownership_ledger", ledger)


def _count(block: M.BasicBlock, cls: type) -> int:
	return sum(1 for i in block.instructions if isinstance(i, cls))


# -- Case 1: ledger says MUST_NOT_DROP → drop chain excised ---------------


def test_trim_removes_drop_chain_when_ledger_says_must_not_drop() -> None:
	func, cleanup_point = _build_partial_move_func("f1", drop_local="drop_tmp_1")
	func._match_cleanup_per_field_drops = [
		("s", (("Some", 0),), cleanup_point, "drop_tmp_1", _TY_PAYLOAD),
	]
	_attach_ledger(func)
	# `needs_drop=True` yet field marked MovedOut by VariantGetFieldAddr
	# → ledger classifies MUST_NOT_DROP → trim removes chain.
	trimmed = trim_match_cleanup_by_ledger(func, needs_drop_fn=lambda _ty: True)
	assert trimmed == 1
	block = func.blocks["entry"]
	# StoreLocal(local=drop_tmp_1) gone.
	for i in block.instructions:
		if isinstance(i, M.StoreLocal):
			assert i.local != "drop_tmp_1", "StoreLocal for trimmed drop_local survived"
	# MoveOut(drop_tmp_1) gone, along with its paired DropValue.
	assert _count(block, M.MoveOut) == 0
	assert _count(block, M.DropValue) == 0
	# drop_local removed from locals table.
	assert "drop_tmp_1" not in func.locals
	assert "drop_tmp_1" not in func.local_types


# -- Case 2: ledger says MUST_DROP → trim is a no-op ---------------------


def test_trim_is_noop_when_ledger_says_must_drop() -> None:
	"""No binder move on field 0 → ledger defaults Live → with
	`needs_drop=True`, classify returns `MUST_DROP` → trim keeps
	the chain."""
	func, cleanup_point = _build_partial_move_func(
		"f2",
		drop_local="drop_tmp_2",
		include_binder_move=False,
	)
	func._match_cleanup_per_field_drops = [
		("s", (("Some", 0),), cleanup_point, "drop_tmp_2", _TY_PAYLOAD),
	]
	_attach_ledger(func)
	trimmed = trim_match_cleanup_by_ledger(func, needs_drop_fn=lambda _ty: True)
	assert trimmed == 0
	block = func.blocks["entry"]
	# StoreLocals: s, drop_tmp_2 → 2.
	assert _count(block, M.StoreLocal) == 2
	assert _count(block, M.MoveOut) == 1
	assert _count(block, M.DropValue) == 1
	assert "drop_tmp_2" in func.locals


# -- Case 3: needs_drop=False → classify yields MUST_NOT_DROP → trim -----


def test_trim_removes_drop_chain_when_needs_drop_false() -> None:
	"""Defence-in-depth: even on a Live field, a POD `needs_drop=False`
	produces `classify` → MUST_NOT_DROP, and the trim pass removes the
	chain.  Site 2's legacy `_needs_runtime_drop` short-circuit
	already shields this branch today, so a side-table entry
	landing here would mean legacy emitted a drop for a POD — which
	the ledger vetoes."""
	func, cleanup_point = _build_partial_move_func(
		"f3",
		drop_local="drop_tmp_3",
		include_binder_move=False,
	)
	func._match_cleanup_per_field_drops = [
		("s", (("Some", 0),), cleanup_point, "drop_tmp_3", _TY_PAYLOAD),
	]
	_attach_ledger(func)
	trimmed = trim_match_cleanup_by_ledger(func, needs_drop_fn=lambda _ty: False)
	assert trimmed == 1
	assert "drop_tmp_3" not in func.locals


# -- Case 4: empty side table → no-op ------------------------------------


def test_trim_noop_empty_side_table() -> None:
	func, _ = _build_partial_move_func("f4", drop_local="drop_tmp_4")
	func._match_cleanup_per_field_drops = []
	_attach_ledger(func)
	trimmed = trim_match_cleanup_by_ledger(func, needs_drop_fn=lambda _ty: True)
	assert trimmed == 0
	# Drop chain still present.
	block = func.blocks["entry"]
	assert _count(block, M.MoveOut) == 1
	assert _count(block, M.DropValue) == 1


# -- Case 5: side table attribute absent → no-op -------------------------


def test_trim_noop_side_table_missing() -> None:
	func, _ = _build_partial_move_func("f5", drop_local="drop_tmp_5")
	# Simulate a lowering path that didn't populate the side table
	# (attribute absent).
	if hasattr(func, "_match_cleanup_per_field_drops"):
		delattr(func, "_match_cleanup_per_field_drops")
	_attach_ledger(func)
	trimmed = trim_match_cleanup_by_ledger(func, needs_drop_fn=lambda _ty: True)
	assert trimmed == 0


# -- Case 6: ledger unset → no-op ----------------------------------------


def test_trim_noop_ledger_unset() -> None:
	func, cleanup_point = _build_partial_move_func("f6", drop_local="drop_tmp_6")
	func._match_cleanup_per_field_drops = [
		("s", (("Some", 0),), cleanup_point, "drop_tmp_6", _TY_PAYLOAD),
	]
	# No _ownership_ledger attached.
	trimmed = trim_match_cleanup_by_ledger(func, needs_drop_fn=lambda _ty: True)
	assert trimmed == 0
	# Chain still present.
	block = func.blocks["entry"]
	assert _count(block, M.MoveOut) == 1
	assert _count(block, M.DropValue) == 1


# -- Case 7: multiple entries handled independently ----------------------


def test_trim_handles_multiple_entries_independently() -> None:
	"""Entry-a: binder moved field 0 → ledger MovedOut → trim.
	Entry-b: no binder on field 1 → ledger Live → keep."""
	func = _make_func(
		"f7",
		locals_=["s", "drop_tmp_a", "drop_tmp_b"],
		types={"s": _TY_VARIANT, "drop_tmp_a": _TY_PAYLOAD, "drop_tmp_b": _TY_PAYLOAD},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	entry.instructions.append(M.AddrOfLocal(dest="t_ref", local="s", is_mut=True))
	# Binder move on field 0 — marks `(s, ((Some, 0),))` MovedOut.
	entry.instructions.append(
		M.VariantGetFieldAddr(
			dest="t_binder_addr",
			variant_ref="t_ref",
			variant_ty=_TY_VARIANT,
			ctor="Some",
			field_index=0,
			field_ty=_TY_PAYLOAD,
		)
	)
	# cleanup_point_a captured AFTER the binder move → state_pre
	# sees MovedOut.
	cleanup_point_a = ("entry", len(entry.instructions))
	# Site-2 cleanup setup for field 0 (hypothetically emitted
	# despite the binder move — the case trim is designed to
	# catch).
	entry.instructions.append(
		M.VariantGetFieldAddr(
			dest="t_slot_addr_a",
			variant_ref="t_ref",
			variant_ty=_TY_VARIANT,
			ctor="Some",
			field_index=0,
			field_ty=_TY_PAYLOAD,
		)
	)
	entry.instructions.append(M.LoadRef(dest="t_slot_val_a", ptr="t_slot_addr_a", inner_ty=_TY_PAYLOAD))
	entry.instructions.append(M.StoreLocal(local="drop_tmp_a", value="t_slot_val_a"))
	# cleanup_point_b for field 1 — no binder move on field 1 → Live.
	cleanup_point_b = ("entry", len(entry.instructions))
	entry.instructions.append(M.StoreLocal(local="drop_tmp_b", value="t_other"))
	entry.instructions.append(M.MoveOut(dest="t_drop_a", local="drop_tmp_a", ty=_TY_PAYLOAD))
	entry.instructions.append(M.DropValue(value="t_drop_a", ty=_TY_PAYLOAD))
	entry.instructions.append(M.MoveOut(dest="t_drop_b", local="drop_tmp_b", ty=_TY_PAYLOAD))
	entry.instructions.append(M.DropValue(value="t_drop_b", ty=_TY_PAYLOAD))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	func._match_cleanup_per_field_drops = [
		("s", (("Some", 0),), cleanup_point_a, "drop_tmp_a", _TY_PAYLOAD),
		("s", (("Some", 1),), cleanup_point_b, "drop_tmp_b", _TY_PAYLOAD),
	]
	_attach_ledger(func)
	trimmed = trim_match_cleanup_by_ledger(func, needs_drop_fn=lambda _ty: True)
	assert trimmed == 1
	block = func.blocks["entry"]
	mos = [i for i in block.instructions if isinstance(i, M.MoveOut)]
	assert len(mos) == 1 and mos[0].local == "drop_tmp_b"
	dvs = [i for i in block.instructions if isinstance(i, M.DropValue)]
	assert len(dvs) == 1 and dvs[0].value == "t_drop_b"
	assert "drop_tmp_a" not in func.locals
	assert "drop_tmp_b" in func.locals


# -- Case 8: empty scrut_local entry is skipped --------------------------


def test_trim_skips_entries_with_empty_scrut_local() -> None:
	"""Defensive: when site 2 recorded a drop without a scrutinee
	tmp local (shouldn't happen in practice but the telemetry path
	already handles empty-string locals), the trim pass skips
	rather than crashing on an empty-string lookup."""
	func, cleanup_point = _build_partial_move_func("f8", drop_local="drop_tmp_8")
	func._match_cleanup_per_field_drops = [
		("", (("Some", 0),), cleanup_point, "drop_tmp_8", _TY_PAYLOAD),
	]
	_attach_ledger(func)
	trimmed = trim_match_cleanup_by_ledger(func, needs_drop_fn=lambda _ty: True)
	assert trimmed == 0
	block = func.blocks["entry"]
	assert _count(block, M.MoveOut) == 1
	assert _count(block, M.DropValue) == 1
