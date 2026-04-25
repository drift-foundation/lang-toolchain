# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Site-1 scope-drop emission pin (post Phase 4 patches 1–6).

Originally pinned the step-3 refactor of the legacy `_emit_scope_drops`
helper + `_scope_drop_verdict` shared decision model.  Both helpers
were retired in patch 6c (2026-04-24); the per-verdict unit tests
they pinned were removed alongside.  What remains are the
end-to-end emission pins exercised through `lower_block` /
`lower_function_body` + the production cleanup-authoring path:

  - definite-live destructible local at scope exit → MoveOut+DropValue
    chain emitted (via `M.CleanupHook` + `cleanup_authoring`).
  - unconditional move at scope exit → no emission (lattice sees
    MOVED_OUT, `verdict_at` returns MUST_NOT_DROP).
  - non-drop-needing type → no emission (POD short-circuit).
  - path-dependent move (bucket-6 shape) → site 1 skips at scope
    exit; Phase 3C `drop_flags` inserts a flag-guarded drop at the
    actual Return point.  The path-insensitive carrier in
    `test_hir_to_mir_path_insensitive_moved_locals.py` pins the
    end-to-end correctness.
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


def _build_lowerer(type_table: TypeTable, *, param_types=None, return_type=None) -> HIRToMIR:
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	return HIRToMIR(
		builder,
		type_table=type_table,
		param_types=param_types or {},
		return_type=return_type,
	)


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
	"""HIR: `var s = "hi"; return 0;` — s is Live at return.

	Post-Phase-4-site-1-patch-1, function-exit scope drops are no
	longer emitted inline by `_emit_scope_drops(scope_index=0)`.
	HIR→MIR emits a `M.CleanupHook` marker; the actual
	`MoveOut + DropValue` pair is authored by
	`cleanup_authoring.author_cleanup` after `build_ledger` runs.
	The semantic property pinned here is unchanged: a definite-live
	destructible local at function-exit gets a drop chain in the
	final MIR.  The route through the new pass is what the test
	now exercises end-to-end.
	"""
	from lang.driftc.stage2.ownership_ledger import build_ledger
	from lang.driftc.stage2.cleanup_authoring import author_cleanup
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
	ledger = build_ledger(builder.func, drop_policy=lambda _t: None)
	setattr(builder.func, "_ownership_ledger", ledger)
	author_cleanup(builder.func, type_table=type_table)
	assert _find_drop_for_local(builder.func, "s"), (
		"patch 1 regression: a definite-live destructible local at "
		"function-exit must get a MoveOut+DropValue emission via "
		"`cleanup_authoring.author_cleanup` (the legacy inline "
		"`_emit_scope_drops` emission was migrated to a post-pass "
		"`M.CleanupHook` marker → authoring step)."
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


# -- Phase 4 site-1 patch 3 (LANDED 2026-04-24) -------------------------
# `lower_block` end-of-block fall-through cleanup is migrated to the
# `M.CleanupHook` + `cleanup_authoring` post-pass pattern.  The first
# attempt (2026-04-23) surfaced a runtime UAF traced to a
# `core.drop_value` HIR→MIR lowering gap (bare `LoadLocal + DropValue`,
# no `MoveOut`); fix landed at the lowering layer, pinned by
# `lang/tests/stage2/test_drop_value_intrinsic_ownership.py`.  The
# end-to-end carrier
# `lang/tests/memcheck/test_patch3_nested_scope_uaf_regression.py`
# proves nested-scope re-authoring no longer double-drops a destructible
# inside a fat `Arc<Interface>` view.  The function-exit shape pin
# (`test_emission_definite_live_string...`) remains in place.
