# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 4 site-1 patch (Bug 2) — `cleanup_authoring.author_cleanup`
flag-guarded carriers and uniform flag-clearing invariant.

These tests pin the **post-fix** expected behaviour of the cleanup
re-authoring pass after the Bug 2 architecture lands:

  - **New Carrier 6**: `PATH_DEPENDENT` + non-variant +
    flag-managed local → emit a **flag-guarded** drop sequence at
    the `CleanupHook` position (NOT at the next overwrite, NOT at
    function exit).
  - **New Carrier 7**: any flag-managed local whose cleanup emits
    a `MoveOut + DropValue` (guarded OR unguarded) must **clear
    the flag** (`StoreLocal(flag, false)`) after the drop.  Keeps
    the flag bit invariant: ``true`` ≡ "this local currently owns
    destructible storage."  Necessary for fail-safe behaviour
    against double-drop at adjacent / duplicated cleanup sites.

Live since the Bug 2 architecture flip (2026-05-15):
`cleanup_authoring.author_cleanup` now consults
`func._drop_flag_managed_locals` / `func._drop_flag_for_local` and
emits via one of two paths:

  - **Per-arm edge elaboration (primary)**: for hooks where every
    candidate is non-variant PD + flag-managed AND every predecessor
    edge has a determinable LIVE/MOVED state, emit
    `MoveOut + DropValue + flag-clear` on each LIVE predecessor edge
    (in-place for single-successor preds, edge-split for multi-
    successor preds).  The hook position itself emits nothing.
    Lattice merge at the hook becomes uniformly MOVED_OUT.

  - **Flag-guarded fallback**: when per-arm cannot resolve all
    candidates (e.g. a predecessor's state is MAYBE_UNINIT from
    upstream branching), fall back to the original
    `LoadLocal(flag) → IfTerminator(flag, drop_blk, post_blk)`
    sequence at the hook position.

This file pins both shapes plus the uniform flag-clear invariant.
"""

from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.cleanup_authoring import author_cleanup
from lang.driftc.stage2.ownership_ledger import build_ledger


def _make_func(name: str, *, params: list[str], locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="test", name=name, ordinal=0)
	return M.MirFunc(
		name=f"test::{name}",
		params=list(params),
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


def _make_droppable_struct(type_table: TypeTable, name: str = "DropMe") -> int:
	string_ty = type_table.ensure_string()
	tid = type_table.declare_struct(module_id="test", name=name, field_names=["inner"])
	type_table.define_struct_fields(tid, field_types=[string_ty])
	if not hasattr(type_table, "destructor_fns"):
		type_table.destructor_fns = {}
	type_table.destructor_fns[tid] = FunctionId(module="test", name=f"{name}::destroy", ordinal=0)
	non_copy = {tid}
	prev_query = getattr(type_table, "_copy_query", None)
	def _query(t):
		if t in non_copy:
			return False
		return prev_query(t) if prev_query else None
	type_table._copy_query = _query  # type: ignore[attr-defined]
	return tid


def _attach_ledger(func: M.MirFunc) -> None:
	ledger = build_ledger(func, drop_policy=lambda _t: None)
	setattr(func, "_ownership_ledger", ledger)


def _find_drop_chain_for(func: M.MirFunc, local: str) -> tuple[M.BasicBlock, int] | None:
	"""Locate `(block, idx)` of the `MoveOut(local) → DropValue` pair,
	or None if no such pair exists."""
	for blk in func.blocks.values():
		instrs = blk.instructions
		for i, ins in enumerate(instrs):
			if isinstance(ins, M.MoveOut) and ins.local == local:
				if i + 1 < len(instrs):
					nxt = instrs[i + 1]
					if isinstance(nxt, M.DropValue) and nxt.value == ins.dest:
						return blk, i
	return None


def _flag_clear_after(blk: M.BasicBlock, idx: int, flag_local: str) -> bool:
	"""True iff some StoreLocal(flag_local, <ConstBool false>) appears
	after `idx` in this block."""
	instrs = blk.instructions
	const_false_dests: set[str] = set()
	for j in range(idx, len(instrs)):
		ins = instrs[j]
		if isinstance(ins, M.ConstBool) and ins.value is False:
			const_false_dests.add(ins.dest)
		if isinstance(ins, M.StoreLocal) and ins.local == flag_local and ins.value in const_false_dests:
			return True
	return False


def _block_terminator_loads_flag(blk: M.BasicBlock, flag_local: str) -> bool:
	"""True iff this block ends in an IfTerminator whose condition is
	driven by a LoadLocal of `flag_local`."""
	if not isinstance(blk.terminator, M.IfTerminator):
		return False
	cond = blk.terminator.cond
	for ins in blk.instructions:
		if isinstance(ins, M.LoadLocal) and ins.local == flag_local and ins.dest == cond:
			return True
	return False


def test_authoring_edge_elaborates_path_dependent_non_variant_managed_on_live_predecessor() -> None:
	"""Loop-iteration shape (Bug 2): a destructible local is
	conditionally moved on one branch and not on another, joined at
	a `CleanupHook`.  All hook candidates are non-variant PD +
	flag-managed.

	Per-arm elaboration (primary path) emits `MoveOut + DropValue +
	flag-clear` at the END of the predecessor block where `w` is
	LIVE (the no-move branch), in-place before the terminator since
	that predecessor is single-successor.  The hook block itself
	emits nothing.

	No `LoadLocal(flag) → IfTerminator` chain — that's the fallback
	path; per-arm runs first when it can resolve cleanly.
	"""
	type_table = TypeTable()
	bool_ty = type_table.ensure_bool()
	sty = _make_droppable_struct(type_table, name="StructLoopShape")
	flag_name = "__drop_flag_w"
	func = _make_func(
		"cond_move_loop_shape",
		params=["b"],
		locals_=["b", "w", flag_name, "sink"],
		types={"b": bool_ty, "w": sty, flag_name: bool_ty, "sink": sty},
	)
	# Mimic the planning pass: flag init at entry, set on user
	# StoreLocal w, clear on user MoveOut w.
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.ConstBool(dest="t_flag_init", value=False))
	entry.instructions.append(M.StoreLocal(local=flag_name, value="t_flag_init"))
	entry.terminator = M.IfTerminator(cond="b", then_target="then_blk", else_target="else_blk")
	# Then: init w, move it out.
	then_blk = M.BasicBlock(name="then_blk")
	then_blk.instructions.append(M.StoreLocal(local="w", value="t_init_then"))
	then_blk.instructions.append(M.ConstBool(dest="t_flag_set_then", value=True))
	then_blk.instructions.append(M.StoreLocal(local=flag_name, value="t_flag_set_then"))
	then_blk.instructions.append(M.MoveOut(dest="t_user_move", local="w", ty=sty))
	then_blk.instructions.append(M.ConstBool(dest="t_flag_clear_then", value=False))
	then_blk.instructions.append(M.StoreLocal(local=flag_name, value="t_flag_clear_then"))
	then_blk.instructions.append(M.StoreLocal(local="sink", value="t_user_move"))
	then_blk.terminator = M.Goto(target="join_blk")
	# Else: init w, no move (LIVE at end of else_blk).
	else_blk = M.BasicBlock(name="else_blk")
	else_blk.instructions.append(M.StoreLocal(local="w", value="t_init_else"))
	else_blk.instructions.append(M.ConstBool(dest="t_flag_set_else", value=True))
	else_blk.instructions.append(M.StoreLocal(local=flag_name, value="t_flag_set_else"))
	else_blk.terminator = M.Goto(target="join_blk")
	# Join: CleanupHook for w.
	join_blk = M.BasicBlock(name="join_blk")
	join_blk.instructions.append(M.CleanupHook(scope_id=0, candidates=[("w", sty)]))
	join_blk.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "then_blk": then_blk, "else_blk": else_blk, "join_blk": join_blk}
	setattr(func, "_drop_flag_managed_locals", {"w"})
	setattr(func, "_drop_flag_for_local", {"w": flag_name})
	_attach_ledger(func)

	emitted = author_cleanup(func, type_table=type_table)
	assert emitted >= 1, "author_cleanup must report at least one emitted drop chain."

	# Per-arm: the drop chain lives in else_blk (where w was LIVE),
	# not at the join_blk hook position.
	chain = _find_drop_chain_for(func, "w")
	assert chain is not None, "per-arm elaboration must emit a MoveOut+DropValue chain for w"
	drop_blk, drop_idx = chain
	assert drop_blk.name == "else_blk", (
		f"per-arm: drop chain for w must land in else_blk (the LIVE-w "
		f"predecessor of join_blk), not {drop_blk.name!r}.  The then-blk "
		"path already moved w; placing the drop there would double-drop."
	)

	# Flag-clear invariant: drop is followed by flag clear in the
	# same predecessor block.
	assert _flag_clear_after(drop_blk, drop_idx, flag_name), (
		"per-arm: flag-managed edge cleanup must clear the flag after "
		"the DropValue (uniform flag-clear invariant)."
	)

	# Join block has NO drop chain (per-arm absorbed it).
	join_blk_post = func.blocks["join_blk"]
	for ins in join_blk_post.instructions:
		assert not isinstance(ins, M.MoveOut) or ins.local != "w", (
			"per-arm: hook position must emit nothing for the elaborated "
			"local; the edge cleanup at the predecessor handles it."
		)

	# Terminator of else_blk unchanged (single-successor path —
	# in-place insert before the terminator).
	assert isinstance(func.blocks["else_blk"].terminator, M.Goto), (
		"single-successor predecessor: in-place insert preserves the "
		"original Goto terminator; no edge split needed."
	)
	assert func.blocks["else_blk"].terminator.target == "join_blk"


def test_authoring_falls_back_to_flag_guarded_when_predecessor_state_is_unresolvable() -> None:
	"""Fallback path: when at least one predecessor edge has a
	candidate in MAYBE_UNINIT state (cannot decide LIVE vs MOVED
	without recursing further — Phase 1 declines), per-arm aborts
	atomically and the flag-guarded sequence is emitted at the hook
	position instead.

	Construct a shape where the predecessor of the hook block has an
	UPSTREAM split (i.e., the predecessor block itself is reached
	via a merge that yields MAYBE_UNINIT for w).  Then per-arm
	cannot decide on that edge; fallback fires.
	"""
	type_table = TypeTable()
	bool_ty = type_table.ensure_bool()
	sty = _make_droppable_struct(type_table, name="StructFallback")
	flag_name = "__drop_flag_w"
	func = _make_func(
		"upstream_merge",
		params=["b1", "b2"],
		locals_=["b1", "b2", "w", flag_name, "sink"],
		types={"b1": bool_ty, "b2": bool_ty, "w": sty, flag_name: bool_ty, "sink": sty},
	)
	# Shape:
	#   entry: if b1 → inner_then ; else → inner_else
	#   inner_then: StoreLocal w ; MoveOut w → state MOVED_OUT
	#   inner_else: StoreLocal w → state LIVE
	#   pre_hook: (merge — w state is MAYBE_UNINIT here)
	#   pre_hook -> hook_blk (CleanupHook for w)
	#
	# At pre_hook's end (the predecessor of hook_blk), w is
	# MAYBE_UNINIT.  Per-arm declines; flag-guarded fallback fires
	# at hook_blk.
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.ConstBool(dest="t_flag_init", value=False))
	entry.instructions.append(M.StoreLocal(local=flag_name, value="t_flag_init"))
	entry.terminator = M.IfTerminator(cond="b1", then_target="inner_then", else_target="inner_else")
	inner_then = M.BasicBlock(name="inner_then")
	inner_then.instructions.append(M.StoreLocal(local="w", value="t_init_inner_then"))
	inner_then.instructions.append(M.ConstBool(dest="t_set_then", value=True))
	inner_then.instructions.append(M.StoreLocal(local=flag_name, value="t_set_then"))
	inner_then.instructions.append(M.MoveOut(dest="t_user_move", local="w", ty=sty))
	inner_then.instructions.append(M.ConstBool(dest="t_clear_then", value=False))
	inner_then.instructions.append(M.StoreLocal(local=flag_name, value="t_clear_then"))
	inner_then.instructions.append(M.StoreLocal(local="sink", value="t_user_move"))
	inner_then.terminator = M.Goto(target="pre_hook")
	inner_else = M.BasicBlock(name="inner_else")
	inner_else.instructions.append(M.StoreLocal(local="w", value="t_init_inner_else"))
	inner_else.instructions.append(M.ConstBool(dest="t_set_else", value=True))
	inner_else.instructions.append(M.StoreLocal(local=flag_name, value="t_set_else"))
	inner_else.terminator = M.Goto(target="pre_hook")
	# pre_hook is the predecessor of hook_blk.  Its block_in state
	# for w merges MOVED_OUT (from inner_then) and LIVE (from
	# inner_else) → MAYBE_UNINIT.  Its state_post at end is also
	# MAYBE_UNINIT (no further w mutations).
	pre_hook = M.BasicBlock(name="pre_hook")
	pre_hook.terminator = M.Goto(target="hook_blk")
	hook_blk = M.BasicBlock(name="hook_blk")
	hook_blk.instructions.append(M.CleanupHook(scope_id=0, candidates=[("w", sty)]))
	hook_blk.terminator = M.Return(value=None)
	func.blocks = {
		"entry": entry,
		"inner_then": inner_then,
		"inner_else": inner_else,
		"pre_hook": pre_hook,
		"hook_blk": hook_blk,
	}
	setattr(func, "_drop_flag_managed_locals", {"w"})
	setattr(func, "_drop_flag_for_local", {"w": flag_name})
	_attach_ledger(func)

	emitted = author_cleanup(func, type_table=type_table)
	assert emitted >= 1, "fallback must emit a drop chain via flag-guarded sequence"

	# The drop chain must be in a NEW block reached via IfTerminator
	# on a LoadLocal(flag) — the flag-guarded fallback shape.
	chain = _find_drop_chain_for(func, "w")
	assert chain is not None
	drop_blk, drop_idx = chain
	# It should NOT be in pre_hook or in inner_else — per-arm declined.
	assert drop_blk.name not in ("pre_hook", "inner_else", "inner_then"), (
		f"fallback: drop chain should be in a flag-guarded sub-block of "
		f"hook_blk, not at the per-arm in-place position {drop_blk.name!r}.  "
		"If per-arm activated here, it would have split the pre_hook "
		"merge incorrectly (pre_hook state is MAYBE_UNINIT)."
	)
	# Guarded by a LoadLocal(flag) → IfTerminator predecessor.
	guarded = False
	for pred in func.blocks.values():
		term = pred.terminator
		if isinstance(term, M.IfTerminator) and term.then_target == drop_blk.name:
			if _block_terminator_loads_flag(pred, flag_name):
				guarded = True
				break
	assert guarded, "fallback drop must be guarded by IfTerminator on a LoadLocal of the flag local"
	assert _flag_clear_after(drop_blk, drop_idx, flag_name), (
		"fallback: flag-managed drop must clear the flag after DropValue."
	)


def test_authoring_rebuilds_ledger_between_mutating_hooks_in_same_block() -> None:
	"""K-review regression (2026-05-15): a block containing two
	`CleanupHook` instances, where processing the FIRST hook splits
	the block (guarded fallback) leaving the SECOND hook in the
	post-split block.  The ledger MUST be rebuilt between hooks so
	the second hook's classification sees the post-mutation MIR,
	not the original ledger's stale (block, idx) entries.

	Pre-fix bug: the second hook would be classified against a
	ledger that has no entries for the newly-split block (state_pre
	falls back to UNINIT → MUST_NOT_DROP, skipping a required drop)
	or against indices that no longer correspond to the original
	instructions (misclassification).

	Shape: two flag-managed locals (`w1`, `w2`), each with its own
	CleanupHook in the same block.  `w1` is non-variant PD (gets
	flag-guarded fallback because it has no resolvable per-arm path
	in this fixture — we deliberately construct an unresolvable
	shape).  After w1's guarded split, w2's hook is in the new
	post-split block; w2 must STILL be classified correctly (it's
	LIVE/MUST_DROP at its hook position) and a drop must fire."""
	type_table = TypeTable()
	bool_ty = type_table.ensure_bool()
	sty = _make_droppable_struct(type_table, name="StructMultiHook")
	flag_w1 = "__drop_flag_w1"
	flag_w2 = "__drop_flag_w2"
	func = _make_func(
		"two_hooks",
		params=["b1", "b2"],
		locals_=["b1", "b2", "w1", "w2", flag_w1, flag_w2, "sink"],
		types={
			"b1": bool_ty, "b2": bool_ty,
			"w1": sty, "w2": sty,
			flag_w1: bool_ty, flag_w2: bool_ty,
			"sink": sty,
		},
	)
	# Construct: w1 reaches an unresolvable PD (upstream merge
	# yields MAYBE_UNINIT at every predecessor of hook1's block);
	# w2 reaches MUST_DROP at hook2's position (after hook1).
	#
	# Layout:
	#   entry: init flags=false; if b1 → inner_then ; else → inner_else
	#   inner_then: init w1; MoveOut(w1); clear flag_w1; → pre_merge
	#   inner_else: init w1; set flag_w1; → pre_merge
	#   pre_merge: (no w1 mutation; w1 state = MAYBE_UNINIT here)
	#                init w2; set flag_w2; → multi_hook_blk
	#   multi_hook_blk:
	#     CleanupHook(candidates=[(w1, sty)])   # hook 1 — PD non-variant flag-managed
	#     CleanupHook(candidates=[(w2, sty)])   # hook 2 — MUST_DROP (w2 LIVE here)
	#     Return
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.ConstBool(dest="t_flag_w1_init", value=False))
	entry.instructions.append(M.StoreLocal(local=flag_w1, value="t_flag_w1_init"))
	entry.instructions.append(M.ConstBool(dest="t_flag_w2_init", value=False))
	entry.instructions.append(M.StoreLocal(local=flag_w2, value="t_flag_w2_init"))
	entry.terminator = M.IfTerminator(cond="b1", then_target="inner_then", else_target="inner_else")
	inner_then = M.BasicBlock(name="inner_then")
	inner_then.instructions.append(M.StoreLocal(local="w1", value="t_init_w1_then"))
	inner_then.instructions.append(M.ConstBool(dest="t_set_w1_then", value=True))
	inner_then.instructions.append(M.StoreLocal(local=flag_w1, value="t_set_w1_then"))
	inner_then.instructions.append(M.MoveOut(dest="t_move_w1", local="w1", ty=sty))
	inner_then.instructions.append(M.ConstBool(dest="t_clear_w1_then", value=False))
	inner_then.instructions.append(M.StoreLocal(local=flag_w1, value="t_clear_w1_then"))
	inner_then.instructions.append(M.StoreLocal(local="sink", value="t_move_w1"))
	inner_then.terminator = M.Goto(target="pre_merge")
	inner_else = M.BasicBlock(name="inner_else")
	inner_else.instructions.append(M.StoreLocal(local="w1", value="t_init_w1_else"))
	inner_else.instructions.append(M.ConstBool(dest="t_set_w1_else", value=True))
	inner_else.instructions.append(M.StoreLocal(local=flag_w1, value="t_set_w1_else"))
	inner_else.terminator = M.Goto(target="pre_merge")
	pre_merge = M.BasicBlock(name="pre_merge")
	# Initialise w2 here so it's LIVE at multi_hook_blk's entry.
	pre_merge.instructions.append(M.StoreLocal(local="w2", value="t_init_w2"))
	pre_merge.instructions.append(M.ConstBool(dest="t_set_w2", value=True))
	pre_merge.instructions.append(M.StoreLocal(local=flag_w2, value="t_set_w2"))
	pre_merge.terminator = M.Goto(target="multi_hook_blk")
	multi_hook = M.BasicBlock(name="multi_hook_blk")
	multi_hook.instructions.append(M.CleanupHook(scope_id=0, candidates=[("w1", sty)]))
	multi_hook.instructions.append(M.CleanupHook(scope_id=1, candidates=[("w2", sty)]))
	multi_hook.terminator = M.Return(value=None)
	func.blocks = {
		"entry": entry,
		"inner_then": inner_then,
		"inner_else": inner_else,
		"pre_merge": pre_merge,
		"multi_hook_blk": multi_hook,
	}
	setattr(func, "_drop_flag_managed_locals", {"w1", "w2"})
	setattr(func, "_drop_flag_for_local", {"w1": flag_w1, "w2": flag_w2})
	_attach_ledger(func)

	emitted = author_cleanup(func, type_table=type_table)

	# Both w1 and w2 must have a drop chain emitted SOMEWHERE.
	# w1: flag-guarded fallback at multi_hook_blk (per-arm declined
	#     because pre_merge's state for w1 is MAYBE_UNINIT).
	# w2: MUST_DROP at hook2's position — emitted after w1's guarded
	#     sequence, possibly in a new post-split block.
	w1_chain = _find_drop_chain_for(func, "w1")
	w2_chain = _find_drop_chain_for(func, "w2")
	assert w1_chain is not None, (
		"w1 drop chain missing — fallback flag-guarded emission "
		"should have fired even though per-arm declined."
	)
	assert w2_chain is not None, (
		"w2 drop chain missing — the SECOND hook (w2 MUST_DROP) was "
		"not classified correctly after the first hook's block split.  "
		"This is the K-review bug: stale ledger across the mutating "
		"hook boundary.  After w1's guarded split moved w2's hook into "
		"a new post-split block, w2's classification queried a ledger "
		"with no entries for that block (state_pre returns UNINIT → "
		"MUST_NOT_DROP) → w2 leaks."
	)
	# w2's drop must be in some block reachable from the function
	# entry — not just the original multi_hook_blk (which after
	# splitting may not contain the post-hook2 instructions).
	w2_blk, _ = w2_chain
	# Walk forward from entry: w2_blk must be reachable.
	reachable: set = set()
	stack = ["entry"]
	while stack:
		bn = stack.pop()
		if bn in reachable:
			continue
		reachable.add(bn)
		blk = func.blocks.get(bn)
		if blk is None:
			continue
		t = blk.terminator
		if isinstance(t, M.Goto):
			stack.append(t.target)
		elif isinstance(t, M.IfTerminator):
			stack.append(t.then_target)
			stack.append(t.else_target)
	assert w2_blk.name in reachable, (
		f"w2 drop block {w2_blk.name!r} is unreachable from function "
		f"entry — the block-split rewiring broke control flow."
	)
	assert emitted >= 2, "expected at least 2 drop chains (w1 + w2)"


def test_authoring_clears_flag_on_unguarded_must_drop_of_managed_local() -> None:
	"""Carrier 7 invariant: if a flag-managed local hits a `MUST_DROP`
	(unconditionally live) `CleanupHook` — e.g. an outer scope whose
	body unconditionally initialised the local without any move — the
	unguarded MoveOut + DropValue path must STILL clear the flag.

	The invariant is "flag bit ≡ currently owns destructible storage."
	Without uniform clearing, a later cleanup site for the same local
	(e.g. an outer-scope CleanupHook reached via a goto-back, or a
	duplicated hook inserted by a future authoring pass) would see
	`flag == true` and double-drop.

	Pre-fix: drop is emitted (current MUST_DROP path), but no flag
	clear follows — invariant is silently violated.
	"""
	type_table = TypeTable()
	bool_ty = type_table.ensure_bool()
	sty = _make_droppable_struct(type_table, name="StructMustDropManaged")
	flag_name = "__drop_flag_x"
	func = _make_func(
		"must_drop_managed",
		params=[],
		locals_=["x", flag_name],
		types={"x": sty, flag_name: bool_ty},
	)
	entry = M.BasicBlock(name="entry")
	# Planning pass: flag init = false at entry.
	entry.instructions.append(M.ConstBool(dest="t_flag_init", value=False))
	entry.instructions.append(M.StoreLocal(local=flag_name, value="t_flag_init"))
	# User StoreLocal: x = ..., flag = true.
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.ConstBool(dest="t_flag_set", value=True))
	entry.instructions.append(M.StoreLocal(local=flag_name, value="t_flag_set"))
	# CleanupHook for x — ledger verdict is MUST_DROP (unconditionally LIVE).
	entry.instructions.append(M.CleanupHook(scope_id=0, candidates=[("x", sty)]))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	setattr(func, "_drop_flag_managed_locals", {"x"})
	setattr(func, "_drop_flag_for_local", {"x": flag_name})
	_attach_ledger(func)

	emitted = author_cleanup(func, type_table=type_table)
	assert emitted == 1, "MUST_DROP must emit one drop chain (pre- and post-fix)."

	chain = _find_drop_chain_for(func, "x")
	assert chain is not None, "MUST_DROP path must produce a MoveOut + DropValue pair."
	drop_blk, drop_idx = chain
	assert _flag_clear_after(drop_blk, drop_idx, flag_name), (
		"post-fix uniform invariant: a flag-managed local's cleanup-authored "
		"drop must clear the flag, even on the unguarded MUST_DROP path. "
		"Otherwise the bit no longer means `currently owns storage` and "
		"future adjacent / duplicated cleanup sites can double-drop."
	)
