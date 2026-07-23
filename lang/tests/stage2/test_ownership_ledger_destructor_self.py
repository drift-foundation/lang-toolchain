# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 site-3 sub-step 2 — destructor-method `self` consumption in
the lattice.

Inside a `std.core.Destructible::destroy` method the runtime owns
`self`: it is the runtime's call to `destroy(self)` that is consuming
it.  The destructor body may freely use `self` (read fields, call
methods), but at function exit the body is NOT responsible for
dropping `self` — that would recurse into the destructor again.

Pre-fix: the legacy string_arc site 3 had a hardcoded guard
`if is_destructor_method and "self" in func.params:
skip_cleanup_locals.add("self")` — site-local authority.

Post-fix: the ledger transitions `self` to `MOVED_OUT` at the END of
every Return-terminator block in a destructor method (after the last
instruction's per-instruction snapshot, so mid-body queries still
see `self` as `LIVE`).  Site 3's existing per-local
`verdict_at(...)` consultation (introduced in sub-step 1) then
returns `MUST_NOT_DROP` for `self` and adds it to
`skip_cleanup_locals` automatically.

Same authority shape as Return-as-move and composite-return-consume:
a function-exit consumption is materialised in the lattice, and the
site asks the lattice — it does not author its own skip.
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


def _make_destructor_func(*, locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	"""Build a MirFunc whose `fn_id.name` matches the destructor
	predicate (`"std.core.Destructible::destroy" in func.fn_id.name`)."""
	fn_id = FunctionId(module="test", name="MyType::std.core.Destructible::destroy", ordinal=0)
	return M.MirFunc(
		name="test::MyType::std.core.Destructible::destroy",
		params=["self"],
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


def _make_regular_func(*, params: list[str], locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="test", name="some_method", ordinal=0)
	return M.MirFunc(
		name="test::some_method",
		params=list(params),
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


# -- Carrier 1: `self` MOVED_OUT at end of destructor Return block --------


def test_destructor_self_moved_out_at_return_block_out() -> None:
	"""In a destructor method, `block_out` for the Return-terminator
	block reports `self` as `MOVED_OUT` — modelling the runtime's
	implicit consumption."""
	func = _make_destructor_func(locals_=["self"], types={"self": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	# Body uses self (read a field).
	entry.instructions.append(M.LoadLocal(dest="t_self", local="self"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["entry"]["self"] is LiveState.MOVED_OUT, (
		"destructor methods consume self implicitly at function exit; "
		"block_out for the Return block must reflect MOVED_OUT so "
		"site 3's verdict_at(return_cursor, 'self', needs_drop=True) "
		"returns MUST_NOT_DROP."
	)


# -- Mid-body snapshots see `self` as LIVE -------------------------------


def test_destructor_self_live_in_body_before_return_cursor() -> None:
	"""Per-instruction snapshots BEFORE the last instruction must see
	`self` as `LIVE` — the destructor body is allowed to use self
	freely.  Only the very-end snapshot (and block_out) reflects the
	implicit consumption."""
	func = _make_destructor_func(locals_=["self"], types={"self": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	# Two body instructions; the snapshot at index 0 (post-instr 0)
	# should still see self as LIVE.
	entry.instructions.append(M.LoadLocal(dest="t1", local="self"))
	entry.instructions.append(M.LoadLocal(dest="t2", local="self"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# Mid-body cursor (state_pre at index 1 = post-instr 0) — LIVE.
	assert ledger.state_pre(("entry", 1), "self") is LiveState.LIVE
	# Right-after-body cursor (state_pre at index 2 = post-instr 1) —
	# now MOVED_OUT (the post-loop transition lands on the last
	# per-instruction snapshot).
	assert ledger.state_pre(("entry", 2), "self") is LiveState.MOVED_OUT


# -- verdict_at returns MUST_NOT_DROP at site 3's cursor ------------------


def test_destructor_self_verdict_must_not_drop_at_return_cursor() -> None:
	"""Site 3's per-local query is at `(block, len(instructions))`.
	Verdict for `self` in a destructor must be MUST_NOT_DROP so it
	is added to `skip_cleanup_locals` by the ledger consultation."""
	func = _make_destructor_func(locals_=["self"], types={"self": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.LoadLocal(dest="t_self", local="self"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.verdict_at(("entry", 1), "self", needs_drop=True) is DropVerdict.MUST_NOT_DROP


# -- Non-destructor function: `self` stays LIVE --------------------------


def test_non_destructor_self_param_stays_live() -> None:
	"""A regular method with a `self` parameter is NOT a destructor;
	the runtime is not implicitly consuming self.  `self` remains
	`LIVE` at block_out (and the site 3 verdict would be MUST_DROP
	if needs_drop=True, leaving it for legacy cleanup paths)."""
	func = _make_regular_func(params=["self"], locals_=["self"], types={"self": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.LoadLocal(dest="t_self", local="self"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["entry"]["self"] is LiveState.LIVE


# -- Destructor returning a non-self value: self still consumed -----------


def test_destructor_self_consumed_even_when_return_value_is_other() -> None:
	"""Destructor signature returns something (e.g. internal Ok wrapper);
	self is still implicitly consumed at function exit regardless of
	the returned value."""
	func = _make_destructor_func(
		locals_=["self", "x"],
		types={"self": _TY_OWNED, "x": _TY_OWNED},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.LoadLocal(dest="t_ret", local="x"))
	entry.terminator = M.Return(value="t_ret")
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["entry"]["self"] is LiveState.MOVED_OUT
	# Return-as-move on `x` from sub-step 1 still applies.
	assert ledger.block_out["entry"]["x"] is LiveState.MOVED_OUT


# -- Destructor with multiple Return blocks ------------------------------


def test_destructor_self_consumed_at_every_return_block() -> None:
	"""Each Return-terminator block applies the consumption
	independently; `self` is MOVED_OUT in every Return block's
	block_out."""
	func = _make_destructor_func(
		locals_=["self", "cond"],
		types={"self": _TY_OWNED, "cond": _TY_OTHER},
	)
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="cond", then_target="a", else_target="b")
	a = M.BasicBlock(name="a")
	a.instructions.append(M.LoadLocal(dest="t_a", local="self"))
	a.terminator = M.Return(value=None)
	b = M.BasicBlock(name="b")
	b.instructions.append(M.LoadLocal(dest="t_b", local="self"))
	b.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	func.blocks["a"] = a
	func.blocks["b"] = b
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["a"]["self"] is LiveState.MOVED_OUT
	assert ledger.block_out["b"]["self"] is LiveState.MOVED_OUT


# -- Non-Return-terminator blocks are unaffected -------------------------


def test_destructor_non_return_block_does_not_consume_self() -> None:
	"""Only Return-terminator blocks apply the implicit consumption.
	A Goto-terminator block in a destructor leaves `self` LIVE so
	successor blocks can still observe self before their own Return
	consumes it."""
	func = _make_destructor_func(locals_=["self"], types={"self": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.LoadLocal(dest="t1", local="self"))
	entry.terminator = M.Goto(target="ret")
	ret = M.BasicBlock(name="ret")
	ret.instructions.append(M.LoadLocal(dest="t2", local="self"))
	ret.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	func.blocks["ret"] = ret
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["entry"]["self"] is LiveState.LIVE, (
		"non-Return-terminator blocks must NOT consume self — only "
		"the actual exit point applies the implicit consumption."
	)
	assert ledger.block_out["ret"]["self"] is LiveState.MOVED_OUT


# -- Defensive: destructor without `self` param -------------------------


def test_destructor_method_without_self_is_noop() -> None:
	"""Defensive: if a function's name matches the destructor pattern
	but the function has no `self` parameter (shouldn't happen in
	practice but guards against pattern false-positives), the
	transition is a no-op."""
	fn_id = FunctionId(module="test", name="oddly_named::std.core.Destructible::destroy", ordinal=0)
	func = M.MirFunc(
		name="test::oddly_named::std.core.Destructible::destroy",
		params=[],
		locals=["other"],
		fn_id=fn_id,
		local_types={"other": _TY_OWNED},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="other", value="t_init"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	# Should not raise.
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["entry"]["other"] is LiveState.LIVE
	# `self` is not tracked at all.
	assert "self" not in ledger.tracked_locals


# -- Empty Return block: `self` still transitions -----------------------


def test_destructor_self_consumed_in_empty_return_block() -> None:
	"""A Return block with zero instructions still applies the
	consumption — the transition lands in block_out (per_instr is
	empty, but block_out is what site 3's `verdict_at(("ret", 0))`
	reads via `block_in[succ]` — for an empty block where
	`state_pre` at index 0 falls back to `block_in`)."""
	func = _make_destructor_func(locals_=["self"], types={"self": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Goto(target="ret")
	ret = M.BasicBlock(name="ret")
	# No instructions; just the Return terminator.
	ret.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	func.blocks["ret"] = ret
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	assert ledger.block_out["ret"]["self"] is LiveState.MOVED_OUT
