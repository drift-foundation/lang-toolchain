# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3B step 3 — `scope_drop` consumer-swap pin.

Pins the step-3 refactor of `_emit_scope_drops` (site 1) in
`lang/driftc/stage2/hir_to_mir.py`:

- The deterministic scope-drop decision is now mediated by the
  `HIRToMIR._scope_drop_verdict(local) -> (DropVerdict, reason_tag)`
  helper — the shared three-state drop-decision model used across
  the ledger/3B machinery (MustDrop / MustNotDrop / PathDependent).
- `PathDependent` is deliberately a DEFER verdict at site 1: the
  site skips emission and Phase 3C's flag-guarded drop block (if the
  local was flagged) is the authoritative source of the actual drop.
  Per the step-3 directive, site 1 must not invent a new
  path-dependent policy here; PathDependent === "same behaviour as
  the legacy `_moved_locals`-based skip, now named."
- `_moved_locals` is NOT deleted.  It is still populated by `HMove`
  lowering and consulted by the helper.  Wholesale removal is
  Phase 4 cleanup work.

Three positive shapes pinned:

  1. definite-live local at scope exit → helper says MustDrop;
     site emits `MoveOut + DropValue`.
  2. definite-moved local (unconditional move) at scope exit →
     helper says PathDependent; site skips.  Wire-compatible with
     the legacy skip-on-`_moved_locals`.
  3. non-drop-needing type → helper says MustNotDrop; site skips.
     Separate from (2) by reason tag.

Plus a negative shape:

  4. conditional move with a use after the move (path-dependent
     mid-function) → helper says PathDependent; site skips emission.
     The bucket-6 carrier regressions in
     `test_hir_to_mir_path_insensitive_moved_locals.py` continue to
     pin the end-to-end correctness (site 1 skips + 3C's flag-
     guarded drop at the real Return point).

These tests construct MirBuilder+HIRToMIR directly and inspect the
helper's verdict + the emitted MIR, not the full HIR→MIR pipeline.
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1 import (
	HBlock,
	HIf,
	HLet,
	HLiteralInt,
	HLiteralString,
	HMove,
	HPlaceExpr,
	HReturn,
	HVar,
	assign_callsite_ids,
	assign_node_ids,
)
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.stage2 import DropValue, HIRToMIR, MoveOut, Return, make_builder
from lang.driftc.stage2.ownership_ledger import DropVerdict
from lang.driftc.stage2.ownership_ledger_events import (
	REASON_DESTRUCTIBLE,
	REASON_MOVED,
	REASON_NEEDS_DROP,
	REASON_NOT_DROP_NEEDING,
)


def _build_lowerer(type_table: TypeTable, *, param_types=None, return_type=None) -> HIRToMIR:
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	return HIRToMIR(
		builder,
		type_table=type_table,
		param_types=param_types or {},
		return_type=return_type,
	)


# -- helper semantics (unit-scope, no HIR lowering) -----------------------


def test_verdict_int_local_is_must_not_drop() -> None:
	"""POD `Int` local → MustNotDrop + not_drop_needing reason."""
	type_table = TypeTable()
	int_ty = type_table.ensure_int()
	lower = _build_lowerer(type_table, param_types={"x": int_ty})
	verdict, reason = lower._scope_drop_verdict("x")
	assert verdict is DropVerdict.MUST_NOT_DROP
	assert reason == REASON_NOT_DROP_NEEDING


def test_verdict_string_local_not_moved_is_must_drop() -> None:
	"""`String` local that is NOT in `_moved_locals` → MustDrop +
	needs_drop reason."""
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	lower = _build_lowerer(type_table, param_types={"s": string_ty})
	verdict, reason = lower._scope_drop_verdict("s")
	assert verdict is DropVerdict.MUST_DROP
	assert reason == REASON_NEEDS_DROP


def test_verdict_string_local_in_moved_set_is_path_dependent() -> None:
	"""`String` local in `_moved_locals` → PathDependent + moved
	reason.  The helper cannot distinguish unconditional vs
	conditional moves from HIRToMIR state alone; both yield
	PathDependent.  Site 1 defers either way (legacy skip for
	unconditional; 3C flag-guarded drop for conditional)."""
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	lower = _build_lowerer(type_table, param_types={"s": string_ty})
	lower._moved_locals.add("s")
	verdict, reason = lower._scope_drop_verdict("s")
	assert verdict is DropVerdict.PATH_DEPENDENT
	assert reason == REASON_MOVED


def test_verdict_unknown_type_local_is_must_not_drop() -> None:
	"""Defensive: a local with no recorded type → MustNotDrop (prior
	code silently `continue`d; the helper preserves that under a
	distinct verdict/reason so the observe path can see the case)."""
	type_table = TypeTable()
	lower = _build_lowerer(type_table)
	verdict, reason = lower._scope_drop_verdict("ghost")
	assert verdict is DropVerdict.MUST_NOT_DROP
	assert reason == REASON_NOT_DROP_NEEDING


# -- emission shape through HIR→MIR lowering ------------------------------


def _find_drop_for_local(func, local: str) -> bool:
	"""Return True iff any block contains a `MoveOut(t, local) +
	DropValue(t)` pair (the canonical scope-drop emission shape)."""
	moveout_dests: set[str] = set()
	saw = False
	for blk in func.blocks.values():
		for ins in blk.instructions:
			if isinstance(ins, MoveOut) and getattr(ins, "local", None) == local:
				moveout_dests.add(getattr(ins, "dest", ""))
			elif isinstance(ins, DropValue) and getattr(ins, "value", None) in moveout_dests:
				saw = True
	return saw


def test_emission_definite_live_string_emits_drop_at_scope_exit() -> None:
	"""HIR: `var s = "hi"; return 0;` — s is Live at return.  Helper
	says MustDrop; site 1 emits MoveOut+DropValue before the
	Return."""
	hir = HBlock(statements=[
		HLet(name="s", value=HLiteralString("hi"), declared_type_expr=None, is_mutable=True, binding_id=None),
		HReturn(value=HLiteralInt(0)),
	])
	hir = normalize_hir(hir)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	type_table = TypeTable()
	int_ty = type_table.ensure_int()
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	lower = HIRToMIR(builder, type_table=type_table, return_type=int_ty)
	lower.lower_block(hir)
	assert _find_drop_for_local(builder.func, "s"), (
		"step 3 regression: a definite-live destructible local at "
		"scope exit should get a MoveOut+DropValue emission — the "
		"helper says MustDrop and site 1 must emit"
	)


def test_emission_unconditional_move_skips_scope_drop() -> None:
	"""HIR: `var s = "hi"; return move s;` — the move terminates the
	function; s is in `_moved_locals` at scope-exit.  Helper says
	PathDependent; site 1 skips (wire-compatible with legacy skip)."""
	hir = HBlock(statements=[
		HLet(name="s", value=HLiteralString("hi"), declared_type_expr=None, is_mutable=True, binding_id=None),
		HReturn(value=HMove(subject=HPlaceExpr(base=HVar("s"), projections=[], loc=Span()))),
	])
	hir = normalize_hir(hir)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	lower = HIRToMIR(builder, type_table=type_table, return_type=string_ty)
	lower.lower_block(hir)
	# Count MoveOut+DropValue pairs for `s`.  The ONLY MoveOut for
	# `s` should be the user `move s` — which is consumed by the
	# return value, NOT by a DropValue.  If site 1 also emitted a
	# drop, the count would be ≥ 1.
	moveout_dests: set[str] = set()
	drops = 0
	for blk in builder.func.blocks.values():
		for ins in blk.instructions:
			if isinstance(ins, MoveOut) and getattr(ins, "local", None) == "s":
				moveout_dests.add(getattr(ins, "dest", ""))
			elif isinstance(ins, DropValue) and getattr(ins, "value", None) in moveout_dests:
				drops += 1
	assert drops == 0, (
		f"step 3 regression: an unconditionally-moved destructible "
		f"local at scope-exit should NOT get a site-1 drop — helper "
		f"says PathDependent and site 1 must defer.  Emitting here "
		f"double-drops the moved local.  Saw {drops} drop(s)."
	)


def test_emission_int_local_no_drop_at_scope_exit() -> None:
	"""HIR: `var i = 3; return 0;` — Int is POD.  Helper says
	MustNotDrop; site 1 skips.  No drop emission anywhere for `i`."""
	hir = HBlock(statements=[
		HLet(name="i", value=HLiteralInt(3), declared_type_expr=None, is_mutable=True, binding_id=None),
		HReturn(value=HLiteralInt(0)),
	])
	hir = normalize_hir(hir)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	type_table = TypeTable()
	int_ty = type_table.ensure_int()
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	lower = HIRToMIR(builder, type_table=type_table, return_type=int_ty)
	lower.lower_block(hir)
	assert not _find_drop_for_local(builder.func, "i"), (
		"step 3 regression: a POD local at scope-exit should NOT get "
		"a drop emission — helper says MustNotDrop"
	)


def test_emission_path_dependent_bucket6_shape_still_skips_at_site1() -> None:
	"""HIR: `var s = "hi"; if b { return move s; } return;` — the
	bucket-6 carrier shape.  The user move on the then-arm puts `s`
	in `_moved_locals` function-wide.  At the trailing Return's
	scope-drop, the helper sees `s in _moved_locals` → PathDependent.
	Site 1 skips — 3C's flag-guarded drop block is the authority on
	the drop for the b=false runtime path.

	This test does NOT run `insert_drop_flags`; it only asserts site
	1's emission in isolation.  The end-to-end bucket-6 fix is
	pinned in `test_hir_to_mir_path_insensitive_moved_locals.py`
	(which DOES run `insert_drop_flags` and asserts a flag-guarded
	drop appears on the no-move path)."""
	hir = HBlock(statements=[
		HLet(name="s", value=HLiteralString("hi"), declared_type_expr=None, is_mutable=True, binding_id=None),
		HIf(
			cond=HVar("b"),
			then_block=HBlock(statements=[
				HReturn(value=HMove(subject=HPlaceExpr(base=HVar("s"), projections=[], loc=Span()))),
			]),
			else_block=None,
		),
		HReturn(value=HLiteralString("fresh")),
	])
	hir = normalize_hir(hir)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	type_table = TypeTable()
	bool_ty = type_table.ensure_bool()
	string_ty = type_table.ensure_string()
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	builder.func.params = ["b"]
	lower = HIRToMIR(builder, type_table=type_table, param_types={"b": bool_ty}, return_type=string_ty)
	lower.lower_block(hir)
	# Site 1 should emit at most one drop for `s` — the one that
	# pairs with the user move in the then-arm (if_then return
	# extraction).  Post-3C, a flag-guarded drop appears on the
	# else-arm; that pass is NOT run here.
	# Concretely: site 1 must NOT emit an unconditional drop at the
	# trailing Return (what would be the if_join block or the
	# else-fallthrough return).
	# Detection: inspect the trailing Return block's instructions
	# for a MoveOut(s)+DropValue pair.
	# The trailing return is the Return block whose value comes from a
	# ConstString("fresh") (not a MoveOut of `s`, which is the
	# if-then arm's return).
	from lang.driftc.stage2 import ConstString
	fresh_dests: set[str] = set()
	for blk in builder.func.blocks.values():
		for ins in blk.instructions:
			if isinstance(ins, ConstString) and ins.value == "fresh":
				fresh_dests.add(getattr(ins, "dest", ""))
	trailing_return_block = None
	for blk in builder.func.blocks.values():
		if isinstance(blk.terminator, Return) and getattr(blk.terminator, "value", None) in fresh_dests:
			trailing_return_block = blk
			break
	assert trailing_return_block is not None, (
		"test setup: no trailing `return \"fresh\";` block found in lowered MIR"
	)
	moveout_dests: set[str] = set()
	unconditional_drop = False
	for ins in trailing_return_block.instructions:
		if isinstance(ins, MoveOut) and getattr(ins, "local", None) == "s":
			moveout_dests.add(getattr(ins, "dest", ""))
		elif isinstance(ins, DropValue) and getattr(ins, "value", None) in moveout_dests:
			unconditional_drop = True
	assert not unconditional_drop, (
		"step 3 regression (bucket-6 shape at site 1): an unconditional "
		"drop for `s` was emitted at the trailing `return;` block.  "
		"The helper must return PathDependent for this shape and site "
		"1 must skip emission; 3C's flag-guarded drop is the sole "
		"source of the drop on the no-move path.  Adding an "
		"unconditional drop here double-drops on the b=true path."
	)
