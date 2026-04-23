# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Unit tests for the observational ownership ledger (Phase 3A, builder module).

Pins the lattice rules from `work/ownership-ledger/design.md`:

- raw state vs drop verdict are separate (classifier short-circuits on
  `needs_drop=False` before examining raw state, so MaybeUninit on a POD
  local produces MustNotDrop, not PathDependent — this is the K-finding
  from the first code review)
- join preserves non-owning equivalence: `MovedOut ∪ Tombstoned`,
  `MovedOut ∪ Uninit`, `Tombstoned ∪ Uninit` do NOT become `MaybeUninit`;
  only `Live` meeting a non-`Live` state produces path-dependence
- MaybeUninit is absorbing at joins only, not across the local's lifetime:
  a later definite StoreLocal refines it back to Live

These rules are the 3A contract.  Regressions here mean downstream
disagreement reports become noisy (false 3C queue) or miss leaks (false
MustNotDrop on a Live local).
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import (
	DropVerdict,
	LiveState,
	LiveStateMap,
	build_ledger,
	classify,
	join,
)


# TypeIds are plain ints; the ledger does not consult the table in 3A, so
# arbitrary sentinel ids are sufficient for unit scope.
_TY_DROPPABLE = 101
_TY_POD = 202


def _drop_policy_stub(_ty: int) -> None:
	"""Phase 3A builder does not consume drop_policy; a no-op is fine here."""
	return None


def _empty_fn(name: str, *, params: list[str], locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="main", name=name, ordinal=0)
	return M.MirFunc(
		name=name,
		params=list(params),
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


# -- join rules --------------------------------------------------------------


def test_join_live_with_itself_is_live() -> None:
	assert join(LiveState.LIVE, LiveState.LIVE) is LiveState.LIVE


def test_join_live_with_non_live_is_maybe_uninit() -> None:
	assert join(LiveState.LIVE, LiveState.MOVED_OUT) is LiveState.MAYBE_UNINIT
	assert join(LiveState.LIVE, LiveState.UNINIT) is LiveState.MAYBE_UNINIT
	assert join(LiveState.LIVE, LiveState.TOMBSTONED) is LiveState.MAYBE_UNINIT


def test_join_moved_with_tombstoned_is_moved_not_path_dependent() -> None:
	"""K-classifier point: both states are non-owning; join must not produce
	a false 3C signal."""
	assert join(LiveState.MOVED_OUT, LiveState.TOMBSTONED) is LiveState.MOVED_OUT
	assert join(LiveState.TOMBSTONED, LiveState.MOVED_OUT) is LiveState.MOVED_OUT


def test_join_moved_with_uninit_is_moved() -> None:
	assert join(LiveState.MOVED_OUT, LiveState.UNINIT) is LiveState.MOVED_OUT
	assert join(LiveState.UNINIT, LiveState.MOVED_OUT) is LiveState.MOVED_OUT


def test_join_tombstoned_with_uninit_is_tombstoned() -> None:
	assert join(LiveState.TOMBSTONED, LiveState.UNINIT) is LiveState.TOMBSTONED


def test_join_maybe_uninit_is_absorbing_except_with_self() -> None:
	assert join(LiveState.MAYBE_UNINIT, LiveState.LIVE) is LiveState.MAYBE_UNINIT
	assert join(LiveState.MAYBE_UNINIT, LiveState.MOVED_OUT) is LiveState.MAYBE_UNINIT
	assert join(LiveState.MAYBE_UNINIT, LiveState.UNINIT) is LiveState.MAYBE_UNINIT
	assert join(LiveState.MAYBE_UNINIT, LiveState.MAYBE_UNINIT) is LiveState.MAYBE_UNINIT


# -- classifier rules --------------------------------------------------------


def test_classify_live_drop_needing_is_must_drop() -> None:
	assert classify(LiveState.LIVE, needs_drop=True) is DropVerdict.MUST_DROP


def test_classify_live_pod_is_must_not_drop() -> None:
	assert classify(LiveState.LIVE, needs_drop=False) is DropVerdict.MUST_NOT_DROP


def test_classify_moved_is_must_not_drop_regardless_of_drop_policy() -> None:
	assert classify(LiveState.MOVED_OUT, needs_drop=True) is DropVerdict.MUST_NOT_DROP
	assert classify(LiveState.MOVED_OUT, needs_drop=False) is DropVerdict.MUST_NOT_DROP


def test_classify_tombstoned_is_must_not_drop() -> None:
	assert classify(LiveState.TOMBSTONED, needs_drop=True) is DropVerdict.MUST_NOT_DROP


def test_classify_uninit_is_must_not_drop() -> None:
	assert classify(LiveState.UNINIT, needs_drop=True) is DropVerdict.MUST_NOT_DROP


def test_classify_maybe_uninit_drop_needing_is_path_dependent() -> None:
	"""The 3C signal."""
	assert classify(LiveState.MAYBE_UNINIT, needs_drop=True) is DropVerdict.PATH_DEPENDENT


def test_classify_maybe_uninit_pod_is_must_not_drop() -> None:
	"""K-finding: PODs short-circuit before raw-state dispatch.  A
	path-dependent POD emits no drop on any path, so it is not 3C work."""
	assert classify(LiveState.MAYBE_UNINIT, needs_drop=False) is DropVerdict.MUST_NOT_DROP


# -- builder: entry seeding --------------------------------------------------


def test_entry_seeds_params_live_and_locals_uninit() -> None:
	func = _empty_fn(
		"f",
		params=["p"],
		locals_=["p", "x"],
		types={"p": _TY_DROPPABLE, "x": _TY_DROPPABLE},
	)
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_in["entry"]["p"] is LiveState.LIVE
	assert ledger.block_in["entry"]["x"] is LiveState.UNINIT


# -- builder: straight-line transfer functions -------------------------------


def test_straight_line_store_then_move_transitions_local() -> None:
	func = _empty_fn("f", params=[], locals_=["x"], types={"x": _TY_DROPPABLE})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t0"))
	entry.instructions.append(M.MoveOut(dest="t1", local="x", ty=_TY_DROPPABLE))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.post_instr[("entry", 0)]["x"] is LiveState.LIVE
	assert ledger.post_instr[("entry", 1)]["x"] is LiveState.MOVED_OUT


def test_zero_value_then_store_transitions_to_tombstoned() -> None:
	func = _empty_fn("f", params=[], locals_=["x"], types={"x": _TY_DROPPABLE})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.ZeroValue(dest="zt", ty=_TY_DROPPABLE))
	entry.instructions.append(M.StoreLocal(local="x", value="zt"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.post_instr[("entry", 1)]["x"] is LiveState.TOMBSTONED


def test_plain_store_does_not_transition_to_tombstoned() -> None:
	func = _empty_fn("f", params=[], locals_=["x"], types={"x": _TY_DROPPABLE})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t0"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.post_instr[("entry", 0)]["x"] is LiveState.LIVE


# -- builder: join + refinement ---------------------------------------------


def _diamond_assigning_on_both_arms(final_store_local: bool) -> M.MirFunc:
	"""Builds:
	    entry: if cond -> then / else
	    then:  x = t_then; goto join
	    else:  x = t_else; goto join
	    join:  (optional) x = t_join; return
	`final_store_local=False` returns with no extra store — useful for
	checking post-join raw state.  `final_store_local=True` adds the
	refining write.
	"""
	func = _empty_fn("f", params=["cond"], locals_=["cond", "x"], types={"cond": _TY_POD, "x": _TY_DROPPABLE})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="cond", then_target="then", else_target="else")
	then_b = M.BasicBlock(name="then")
	then_b.instructions.append(M.StoreLocal(local="x", value="t_then"))
	then_b.terminator = M.Goto(target="join")
	else_b = M.BasicBlock(name="else")
	else_b.instructions.append(M.StoreLocal(local="x", value="t_else"))
	else_b.terminator = M.Goto(target="join")
	join_b = M.BasicBlock(name="join")
	if final_store_local:
		join_b.instructions.append(M.StoreLocal(local="x", value="t_join"))
	join_b.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "then": then_b, "else": else_b, "join": join_b}
	return func


def test_join_of_two_live_arms_stays_live() -> None:
	"""Both arms definitely assign; the join's in-state is Live, not MaybeUninit."""
	func = _diamond_assigning_on_both_arms(final_store_local=False)
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_in["join"]["x"] is LiveState.LIVE


def test_join_of_uninit_entry_and_live_arm_is_maybe_uninit() -> None:
	"""Only one arm writes; join's in-state is MaybeUninit."""
	func = _empty_fn("f", params=["cond"], locals_=["cond", "x"], types={"cond": _TY_POD, "x": _TY_DROPPABLE})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="cond", then_target="then", else_target="join")
	then_b = M.BasicBlock(name="then")
	then_b.instructions.append(M.StoreLocal(local="x", value="t_then"))
	then_b.terminator = M.Goto(target="join")
	join_b = M.BasicBlock(name="join")
	join_b.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "then": then_b, "join": join_b}
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_in["join"]["x"] is LiveState.MAYBE_UNINIT


def test_later_definite_store_refines_maybe_uninit_back_to_live() -> None:
	"""K-refinement: MaybeUninit after a partial join, then a definite
	StoreLocal in the join block brings state back to Live.  Without this
	rule the ledger would report false PathDependent at any drop after
	the refining store."""
	func = _empty_fn("f", params=["cond"], locals_=["cond", "x"], types={"cond": _TY_POD, "x": _TY_DROPPABLE})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="cond", then_target="then", else_target="join")
	then_b = M.BasicBlock(name="then")
	then_b.instructions.append(M.StoreLocal(local="x", value="t_then"))
	then_b.terminator = M.Goto(target="join")
	join_b = M.BasicBlock(name="join")
	join_b.instructions.append(M.StoreLocal(local="x", value="t_join"))
	join_b.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "then": then_b, "join": join_b}
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_in["join"]["x"] is LiveState.MAYBE_UNINIT
	assert ledger.post_instr[("join", 0)]["x"] is LiveState.LIVE


def test_moved_on_one_arm_tombstoned_on_other_joins_to_moved_not_maybe_uninit() -> None:
	"""K-classifier end-to-end: one arm moves the local, the other
	tombstones it.  Both arms are non-owning; the join must NOT produce
	MaybeUninit — no drop is needed on either path."""
	func = _empty_fn("f", params=[], locals_=["x"], types={"x": _TY_DROPPABLE})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.terminator = M.IfTerminator(cond="c", then_target="move_arm", else_target="tomb_arm")
	move_arm = M.BasicBlock(name="move_arm")
	move_arm.instructions.append(M.MoveOut(dest="mv", local="x", ty=_TY_DROPPABLE))
	move_arm.terminator = M.Goto(target="join")
	tomb_arm = M.BasicBlock(name="tomb_arm")
	tomb_arm.instructions.append(M.ZeroValue(dest="z", ty=_TY_DROPPABLE))
	tomb_arm.instructions.append(M.StoreLocal(local="x", value="z"))
	tomb_arm.terminator = M.Goto(target="join")
	join_b = M.BasicBlock(name="join")
	join_b.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "move_arm": move_arm, "tomb_arm": tomb_arm, "join": join_b}
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_in["join"]["x"] is LiveState.MOVED_OUT


# -- verdict_at pre-state convention ----------------------------------------


def test_verdict_at_uses_pre_state_not_post_state() -> None:
	"""Sites ask the ledger "should a drop land at this point" — the
	question is about state just before the hypothetical emission.  A
	MoveOut at idx N turns the local MovedOut; `verdict_at((block, N),
	local)` must still see Live because the move has not yet happened
	from the reporter's perspective."""
	func = _empty_fn("f", params=["p"], locals_=["p"], types={"p": _TY_DROPPABLE})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.MoveOut(dest="t", local="p", ty=_TY_DROPPABLE))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.state_pre(("entry", 0), "p") is LiveState.LIVE
	assert ledger.state_post(("entry", 0), "p") is LiveState.MOVED_OUT
	assert ledger.verdict_at(("entry", 0), "p", needs_drop=True) is DropVerdict.MUST_DROP


def test_untracked_local_defaults_to_live_verdict() -> None:
	"""SSA temps / unknown names fall back to Live; the reporter should
	not query them but a defensive default keeps the API robust."""
	func = _empty_fn("f", params=[], locals_=[], types={})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.state_pre(("entry", 0), "__not_tracked") is LiveState.LIVE


# -- event log (sites 1/2 recording surface) --------------------------------


def test_event_log_records_and_drains() -> None:
	from lang.driftc.stage2.ownership_ledger_events import (
		DropDecisionLog,
		REASON_NEEDS_DROP,
		SITE_SCOPE_DROP,
		VERDICT_MUST_DROP,
	)
	log = DropDecisionLog(fn_name="f")
	log.record(
		site=SITE_SCOPE_DROP,
		program_point=("entry", 3),
		local="x",
		verdict=VERDICT_MUST_DROP,
		reason=REASON_NEEDS_DROP,
	)
	drained = log.drain()
	assert len(drained) == 1
	assert drained[0].site == SITE_SCOPE_DROP
	assert drained[0].program_point == ("entry", 3)
	assert drained[0].local == "x"
	assert drained[0].verdict == VERDICT_MUST_DROP
	assert drained[0].reason == REASON_NEEDS_DROP
	assert log.drain() == []
