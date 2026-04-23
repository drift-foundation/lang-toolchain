# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 step 3a — per-field state hooks in `LiveStateMap`.

Pins the data-structure-only landing for per-field tracking.  This
file does NOT exercise any site or aggregator change (those are 3b /
3c).  It pins:

- The two MIR shapes that populate field state:
  - `VariantGetField(dest, variant=v_local, ctor, field_index, ...)` —
    by-value extraction; treated as immediate per-field MovedOut.
  - `VariantGetFieldAddr(dest, variant_ref=ref, ctor, field_index, ...)`
    where `ref` was produced by `AddrOfLocal(_, v_local, _)`;
    conservatively treated as MovedOut (see
    `ownership_ledger._apply_field_state` docstring for the exact
    detection + the over-report limitation that step 3b/3c will
    tighten).
- The five carrier shapes K listed: whole-untouched / single-moved /
  sibling-still-live / scrutinee-then-field-read / unknown-path
  defensive case.
- The block-join intersection rule for per-field MovedOut.

Tests use hand-built `MirFunc`s — no HIR lowering.
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import (
	DropVerdict,
	LiveState,
	build_ledger,
)


_TY_VARIANT = 101
_TY_PAYLOAD = 202


def _make_func(name: str, *, locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="main", name=name, ordinal=0)
	return M.MirFunc(
		name=name,
		params=[],
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


def _drop_policy_stub(_ty: int) -> None:
	return None


# -- Shape #1: whole scrutinee untouched ----------------------------------


def test_field_state_whole_scrutinee_untouched_defaults_to_live() -> None:
	"""No `VariantGetField`/`VariantGetFieldAddr` for the local.
	Field state defaults to `Live` for any (ctor, idx) query."""
	func = _make_func("untouched", locals_=["s"], types={"s": _TY_VARIANT})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# At post-StoreLocal point, no field event yet.  Query any field.
	assert ledger.field_state_post(("entry", 0), "s", (("Some", 0),)) is LiveState.LIVE
	assert ledger.field_state_post(("entry", 0), "s", (("None", 0),)) is LiveState.LIVE
	# verdict should match: drop-needing field on a Live local → MustDrop.
	assert ledger.field_verdict_at(
		("entry", 1), "s", (("Some", 0),), needs_drop=True
	) is DropVerdict.MUST_DROP


# -- Shape #2: single field moved (sibling still live) -------------------


def test_field_state_single_field_moved_via_variant_get_field() -> None:
	"""`VariantGetField(_, variant=s, ctor=Some, field_index=0)` →
	post-state has (s, [Some.0]) == MovedOut.  Sibling field
	(Some.1) is unaffected → Live."""
	func = _make_func("single_moved", locals_=["s"], types={"s": _TY_VARIANT})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	entry.instructions.append(
		M.VariantGetField(dest="t_field", variant="s", variant_ty=_TY_VARIANT, ctor="Some", field_index=0, field_ty=_TY_PAYLOAD)
	)
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# Field 0 of Some: MovedOut after instr 1.
	assert ledger.field_state_post(("entry", 1), "s", (("Some", 0),)) is LiveState.MOVED_OUT
	# Field 1 of Some: untouched → Live.
	assert ledger.field_state_post(("entry", 1), "s", (("Some", 1),)) is LiveState.LIVE
	# verdict_at: moved field with needs_drop=True → MustNotDrop.
	# (verdict_at queries pre-state; query AFTER instr 1 → use idx 2)
	# But there's no instr 2 in this block; field_state_pre at (entry, 2)
	# returns post-state of (entry, 1) = MovedOut.
	assert ledger.field_verdict_at(
		("entry", 2), "s", (("Some", 0),), needs_drop=True
	) is DropVerdict.MUST_NOT_DROP
	assert ledger.field_verdict_at(
		("entry", 2), "s", (("Some", 1),), needs_drop=True
	) is DropVerdict.MUST_DROP


def test_field_state_via_variant_get_field_addr_chain() -> None:
	"""The by-reference shape: `AddrOfLocal(ref, s, _) +
	VariantGetFieldAddr(_, variant_ref=ref, ctor, idx)` → field
	state[(s, ctor, idx)] = MovedOut.

	Conservative detection per step-3a (over-reports for read-only
	borrows); pin documents the current contract."""
	func = _make_func("by_ref", locals_=["s"], types={"s": _TY_VARIANT})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	entry.instructions.append(M.AddrOfLocal(dest="t_ref", local="s", is_mut=True))
	entry.instructions.append(
		M.VariantGetFieldAddr(
			dest="t_field_addr",
			variant_ref="t_ref",
			variant_ty=_TY_VARIANT,
			ctor="Some",
			field_index=0,
			field_ty=_TY_PAYLOAD,
		)
	)
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.field_state_post(("entry", 2), "s", (("Some", 0),)) is LiveState.MOVED_OUT


# -- Shape #4: whole scrutinee moved after field read --------------------


def test_field_state_whole_local_moveout_clears_field_state() -> None:
	"""After a whole-local `MoveOut`, all per-field entries for that
	local are cleared.  This matches the semantics: the local's
	storage is gone; per-field queries on it are meaningless and
	default back to Live (the lattice's defensive default).

	The whole-local state itself transitions to MovedOut (existing
	behaviour) — the per-local and per-field tracks are independent
	but the per-field clear avoids stale "this field is MovedOut"
	signals lingering after the local itself was moved as a whole."""
	func = _make_func("scrut_then_whole_move", locals_=["s"], types={"s": _TY_VARIANT})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	# First read field 0 by-value.
	entry.instructions.append(
		M.VariantGetField(dest="t_field", variant="s", variant_ty=_TY_VARIANT, ctor="Some", field_index=0, field_ty=_TY_PAYLOAD)
	)
	# Then move the whole scrutinee.
	entry.instructions.append(M.MoveOut(dest="t_whole", local="s", ty=_TY_VARIANT))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# After the field read (instr 1), field 0 is MovedOut.
	assert ledger.field_state_post(("entry", 1), "s", (("Some", 0),)) is LiveState.MOVED_OUT
	# After the whole-local MoveOut (instr 2), per-field state for s
	# is cleared.  Default = Live.
	assert ledger.field_state_post(("entry", 2), "s", (("Some", 0),)) is LiveState.LIVE
	# Whole-local state is MovedOut.
	assert ledger.state_post(("entry", 2), "s") is LiveState.MOVED_OUT


# -- Shape #5: ctor mismatch / unknown field path defensive case ---------


def test_field_state_unknown_field_path_defaults_to_live() -> None:
	"""Querying an (ctor, field_index) the builder never saw returns
	`Live` — defensive default.  Sites must still range-check
	ctor/idx against the type table; this default is a guard, not a
	contract."""
	func = _make_func("unknown_query", locals_=["s"], types={"s": _TY_VARIANT})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	entry.instructions.append(
		M.VariantGetField(dest="t_field", variant="s", variant_ty=_TY_VARIANT, ctor="Some", field_index=0, field_ty=_TY_PAYLOAD)
	)
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# Some.0 is MovedOut.  Some.1, Some.999, Other.0 default to Live.
	assert ledger.field_state_post(("entry", 1), "s", (("Some", 0),)) is LiveState.MOVED_OUT
	assert ledger.field_state_post(("entry", 1), "s", (("Some", 1),)) is LiveState.LIVE
	assert ledger.field_state_post(("entry", 1), "s", (("Some", 999),)) is LiveState.LIVE
	assert ledger.field_state_post(("entry", 1), "s", (("Other", 0),)) is LiveState.LIVE
	# Untracked local also defaults to Live.
	assert ledger.field_state_post(("entry", 1), "ghost", (("Some", 0),)) is LiveState.LIVE


# -- Block-join intersection rule -----------------------------------------


def test_field_state_join_intersection_partial_move_arms() -> None:
	"""When the if-then arm moves field 0 and the if-else arm does
	not, the post-join state is `Live` (intersection-style: only
	MovedOut if EVERY arm moved it).  This is the conservative rule
	step 3a uses to avoid silently suppressing drops on partial-
	move arms."""
	func = _make_func("join_partial_move", locals_=["s", "b"], types={"s": _TY_VARIANT, "b": 1})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_join")
	then_block = M.BasicBlock(name="if_then")
	then_block.instructions.append(
		M.VariantGetField(dest="t_field", variant="s", variant_ty=_TY_VARIANT, ctor="Some", field_index=0, field_ty=_TY_PAYLOAD)
	)
	then_block.terminator = M.Goto(target="if_join")
	join_block = M.BasicBlock(name="if_join")
	join_block.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_block, "if_join": join_block}
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# At join: then-arm moved Some.0, else-arm did not → Live.
	assert ledger.field_block_in["if_join"].get(("s", (("Some", 0),)), LiveState.LIVE) is LiveState.LIVE


def test_field_state_join_intersection_both_arms_move() -> None:
	"""When BOTH arms move field 0, the post-join state is MovedOut.
	Pins the other half of the intersection rule."""
	func = _make_func("join_both_move", locals_=["s", "b"], types={"s": _TY_VARIANT, "b": 1})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="s", value="t_init"))
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_else")
	then_block = M.BasicBlock(name="if_then")
	then_block.instructions.append(
		M.VariantGetField(dest="t_then", variant="s", variant_ty=_TY_VARIANT, ctor="Some", field_index=0, field_ty=_TY_PAYLOAD)
	)
	then_block.terminator = M.Goto(target="if_join")
	else_block = M.BasicBlock(name="if_else")
	else_block.instructions.append(
		M.VariantGetField(dest="t_else", variant="s", variant_ty=_TY_VARIANT, ctor="Some", field_index=0, field_ty=_TY_PAYLOAD)
	)
	else_block.terminator = M.Goto(target="if_join")
	join_block = M.BasicBlock(name="if_join")
	join_block.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_block, "if_else": else_block, "if_join": join_block}
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# Both arms moved Some.0 → join is MovedOut.
	assert ledger.field_block_in["if_join"].get(("s", (("Some", 0),))) is LiveState.MOVED_OUT
