# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
MIR / string_arc contract pins for `M.MoveFromRef` ownership transfer.

`MoveFromRef` is the explicit ownership-transfer primitive used by
`match_cleanup_authoring` for per-field cleanup chains.  The contract
this file pins:

  1. **No retain on transfer.**  `string_arc.insert_string_arc` must
     NOT insert a `StringRetain` at a `MoveFromRef` whose `inner_ty`
     is `String`.  The slot's stake is being transferred — retaining
     would create a parallel stake that the matched-up arm-end
     `MoveOut + DropValue` then unwinds, leaving the slot's original
     stake un-released.  This was the LANGUAGE_BUG carrier shape
     before `MoveFromRef` was introduced (see
     `lang/tests/memcheck/test_partial_move_copy_binder_string_slot_leak.py`).

  2. **Exactly one DropValue per transfer.**  After authoring +
     string_arc, the per-field cleanup must produce exactly one
     `M.DropValue` for each emitted `MoveFromRef` of the same field
     type — no more, no less.  Two would double-release; zero would
     leak.

  3. **Early-return / throw cleans the transferred field.**  Site-1
     `M.CleanupHook`s placed within the arm body must include the
     `__match_partial_drop_*` local as a candidate so any non-fall-
     through exit (return, throw) still releases the transferred
     stake.  This is the early-exit safety the drop_tmp pattern was
     designed for, preserved by the `MoveFromRef` migration.

These pins exercise `match_cleanup_authoring → string_arc` directly
on hand-built MIR; they do NOT require running a full compile.  The
end-to-end runtime regression carrier is
`lang/tests/memcheck/test_partial_move_copy_binder_string_slot_leak.py`.
"""
from __future__ import annotations

from typing import List

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import (
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.checker import FnInfo  # noqa: F401  (signature requires the type even when {})
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ledger_cache import build_and_attach_ledger
from lang.driftc.stage2.match_cleanup_authoring import author_match_cleanup
# RELEASE-ARM TRIPWIRE EXEMPTION: the single family temp here (t_str,
# ConstString) is CONSUMED by a ConstructVariant String field, so it
# never reaches the fail-closed last-use release arm.  Adding family
# temps with non-consuming last uses requires the _run_pipeline
# pattern (see test_string_arc_audit_reporter.py).
from lang.driftc.stage2.string_arc import insert_string_arc


def _build_v_string_table() -> tuple[TypeTable, int, int]:
	"""Build a TypeTable with `V<String>` variant; return
	`(type_table, v_string_ty, string_ty)`."""
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	var_base = type_table.declare_variant(
		module_id="main",
		name="V",
		type_params=["T"],
		arms=[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	v_string_ty = type_table.ensure_instantiated(var_base, [string_ty])
	return type_table, v_string_ty, string_ty


def _build_authoring_carrier(
	*,
	include_early_exit: bool = False,
) -> tuple[M.MirFunc, TypeTable, int]:
	"""Hand-build a MIR function carrying a `MatchCleanupHook` for the
	String field of `V<String>` plus a real-shape arm body.

	Setup mirrors `_visit_expr_HMatchExpr`'s partial-move emission
	for `match v { Some(s_bound) => ... }` where `s_bound` is a
	Copy-binder of String:

	  - `arm_scrut_local = __match_scrut`, holding the moved-in variant
	  - `__match_scrut_ptr = AddrOfLocal(arm_scrut_local)`
	  - Token-style binder MOVE that sets `arm_scrut_payload_moved=True`
	    is simulated by NOT moving the String field (keeping it Live)
	  - `MatchCleanupHook` carries the String slot as a candidate;
	    `drop_tmp = __match_partial_drop_t1`

	If `include_early_exit=True`, the arm body emits an early-return
	site-1 `CleanupHook` between the per-field hook and the arm-end
	position — exercising the early-exit drop_tmp cleanup path.
	"""
	type_table, v_string_ty, string_ty = _build_v_string_table()
	int_ty = type_table.ensure_int()

	fn_id = FunctionId(module="main", name="run", ordinal=0)
	func = M.MirFunc(
		name="run",
		params=[],
		locals=["__match_scrut", "__match_partial_drop_t1"],
		fn_id=fn_id,
		local_types={
			"__match_scrut": v_string_ty,
			"__match_partial_drop_t1": string_ty,
		},
	)

	entry = M.BasicBlock(name="entry")
	# `__match_partial_drop_t1` is declared but NOT explicitly
	# initialised — matches HIR→MIR's `ensure_local + register_drop_local`
	# pattern, where the alloca starts uninit (zero bytes).  Adding an
	# explicit `ZeroValue + StoreLocal` here would trigger a spurious
	# `StringRetain` on the zero from string_arc's own
	# StoreLocal-of-String handler — that's a test-setup artefact, not
	# the contract we want to pin.
	# Construct an Optional<String>::Some("x") and store into __match_scrut.
	entry.instructions.append(M.ConstString(dest="t_str", value="x"))
	func.local_types["t_str"] = string_ty
	entry.instructions.append(
		M.ConstructVariant(
			dest="t_var",
			variant_ty=v_string_ty,
			ctor="Some",
			args=["t_str"],
		)
	)
	func.local_types["t_var"] = v_string_ty
	entry.instructions.append(M.StoreLocal(local="__match_scrut", value="t_var"))
	entry.instructions.append(
		M.AddrOfLocal(dest="t_ref", local="__match_scrut", is_mut=True)
	)
	func.local_types["t_ref"] = type_table.ensure_ref_mut(v_string_ty)
	# MatchCleanupHook with a single String-field candidate.  ae_block /
	# ae_index are set so authoring's tail_chain lands at the next
	# instruction — emulating the arm-end position captured by
	# HIR→MIR.
	if include_early_exit:
		# Emit a site-1 CleanupHook BETWEEN the MatchCleanupHook and
		# the arm-end position, simulating an early-return inside the
		# arm body.  Its candidate list includes drop_tmp so site-1
		# authoring releases the transferred stake on the early exit.
		hook_idx_for_match = len(entry.instructions)
		entry.instructions.append(
			M.MatchCleanupHook(
				scope_id=1,
				arm_scrut_local="__match_scrut",
				arm_scrut_ptr_local="t_ref",
				variant_ty=v_string_ty,
				ctor="Some",
				candidates=[("__match_partial_drop_t1", 0, string_ty)],
				arm_end_block="entry",
				arm_end_index=hook_idx_for_match + 2,  # filled below
			)
		)
		# Site-1 CleanupHook — the early-exit drainage point.
		entry.instructions.append(
			M.CleanupHook(
				scope_id=2,
				candidates=[("__match_partial_drop_t1", string_ty)],
			)
		)
	else:
		hook_idx_for_match = len(entry.instructions)
		entry.instructions.append(
			M.MatchCleanupHook(
				scope_id=1,
				arm_scrut_local="__match_scrut",
				arm_scrut_ptr_local="t_ref",
				variant_ty=v_string_ty,
				ctor="Some",
				candidates=[("__match_partial_drop_t1", 0, string_ty)],
				arm_end_block="entry",
				arm_end_index=hook_idx_for_match + 1,
			)
		)
	entry.instructions.append(M.ConstInt(dest="t_ret", value=0))
	func.local_types["t_ret"] = int_ty
	entry.terminator = M.Return(value="t_ret")
	func.blocks["entry"] = entry
	return func, type_table, string_ty


def _run_authoring_then_string_arc(
	func: M.MirFunc,
	type_table: TypeTable,
) -> M.MirFunc:
	"""Run the production pipeline order: ledger build → authoring →
	ledger rebuild → string_arc."""
	build_and_attach_ledger(
		func, drop_policy=lambda _t: None, reason="test.initial_build"
	)
	author_match_cleanup(func, type_table=type_table)
	build_and_attach_ledger(
		func,
		drop_policy=lambda _t: None,
		reason="test.rebuild_after_match_cleanup_authoring",
	)
	# Note: site-1 cleanup_authoring would normally run here too; for
	# pin #3 we exercise it via the test that includes the early-exit
	# CleanupHook.  string_arc is the consumer that decides whether
	# to insert StringRetain.
	insert_string_arc(func, type_table=type_table, fn_infos={})
	return func


def _collect_block_instructions(func: M.MirFunc) -> List[M.MInstr]:
	"""Flatten every block's instruction list (block-order is
	deterministic for these single-block carriers)."""
	out: List[M.MInstr] = []
	for blk_name in func.blocks:
		out.extend(func.blocks[blk_name].instructions)
	return out


def test_move_from_ref_does_not_get_string_retain() -> None:
	"""Pin #1: `string_arc` must NOT insert `StringRetain` at the
	site of a `MoveFromRef` of a String.

	A `StringRetain` adjacent to (or surrounding) the `MoveFromRef`
	would mean the transferred stake is being copied (retained) —
	the exact wash that caused the slot's original stake to leak
	pre-`MoveFromRef`.

	We assert two things:
	  - There IS a `MoveFromRef` instruction for the String field.
	  - There is no `StringRetain` instruction in the block whose
	    `value` is reachable from any operand involved in the
	    transfer chain.  (We use a strict shape check: no
	    StringRetain anywhere in the block, since the carrier has
	    no other String-Copy site that would legitimately need a
	    retain.)
	"""
	func, type_table, _string_ty = _build_authoring_carrier()
	func = _run_authoring_then_string_arc(func, type_table)

	all_instrs = _collect_block_instructions(func)

	move_from_refs = [
		ins for ins in all_instrs
		if isinstance(ins, M.MoveFromRef) and ins.local == "__match_partial_drop_t1"
	]
	assert len(move_from_refs) == 1, (
		f"Expected exactly one MoveFromRef for __match_partial_drop_t1 "
		f"after authoring; got {len(move_from_refs)}.  Authoring may "
		f"have regressed off the MoveFromRef shape."
	)

	retains = [ins for ins in all_instrs if isinstance(ins, M.StringRetain)]
	assert not retains, (
		f"string_arc inserted {len(retains)} `StringRetain`(s) into the "
		f"per-field cleanup chain.  `MoveFromRef` is an ownership "
		f"transfer — retaining the transferred value re-introduces the "
		f"slot-stake leak this primitive was designed to fix.  "
		f"Retain instructions: {retains!r}"
	)


def test_per_field_cleanup_emits_exactly_one_drop_value() -> None:
	"""Pin #2: exactly one `DropValue(ty=String)` is emitted for the
	per-field cleanup chain.

	The chain is:
	  hook position: VariantGetFieldAddr + MoveFromRef
	  arm-end position: MoveOut(_, drop_tmp) + DropValue

	After string_arc rewrites `MoveOut` into LoadLocal + ZeroValue +
	StoreLocal, the surviving DropValue count must be exactly 1.
	More than one would double-release.  Zero would leak (the
	regression LANGUAGE_BUG shape this whole stack was built to
	close).
	"""
	func, type_table, string_ty = _build_authoring_carrier()
	func = _run_authoring_then_string_arc(func, type_table)

	all_instrs = _collect_block_instructions(func)
	drop_values = [
		ins for ins in all_instrs
		if isinstance(ins, M.DropValue) and getattr(ins, "ty", None) == string_ty
	]
	assert len(drop_values) == 1, (
		f"Per-field cleanup must emit exactly one `DropValue(ty=String)`; "
		f"got {len(drop_values)}.  More than one double-releases the "
		f"transferred stake; zero leaks it.  All DropValues: {drop_values!r}"
	)


def test_early_exit_cleanuphook_includes_transferred_drop_tmp() -> None:
	"""Pin #3: site-1 `CleanupHook`s placed within the arm body
	(simulating an early return / throw) include `__match_partial_drop_*`
	as a candidate.  This is the early-exit safety the drop_tmp
	pattern was designed for, preserved by the `MoveFromRef`
	migration.

	The actual emission of the early-exit drop is site-1's
	`cleanup_authoring` job; this pin verifies the CONTRACT input
	(the candidate is visible to site-1) is preserved.  Without
	this, a return/throw inside the arm body would skip the
	transferred field's release.
	"""
	func, type_table, _string_ty = _build_authoring_carrier(include_early_exit=True)
	func = _run_authoring_then_string_arc(func, type_table)

	all_instrs = _collect_block_instructions(func)
	site1_hooks = [ins for ins in all_instrs if isinstance(ins, M.CleanupHook)]
	assert site1_hooks, (
		"Expected at least one site-1 `CleanupHook` in the carrier; "
		"the early-exit simulation didn't emit it."
	)

	# At least one CleanupHook in the arm body must list drop_tmp as a
	# candidate so site-1 cleanup_authoring sees it on the early-exit
	# path.
	def _has_candidate(hook: M.CleanupHook, name: str) -> bool:
		for cand in hook.candidates:
			if isinstance(cand, tuple) and len(cand) >= 1 and cand[0] == name:
				return True
		return False

	hooks_with_drop_tmp = [h for h in site1_hooks if _has_candidate(h, "__match_partial_drop_t1")]
	assert hooks_with_drop_tmp, (
		f"No site-1 CleanupHook in the arm body lists "
		f"`__match_partial_drop_t1` as a candidate.  Without this, an "
		f"early-return / throw inside the arm body would skip the "
		f"transferred field's release.  All hooks: {site1_hooks!r}"
	)
