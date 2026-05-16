# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Runtime regression test for the ledger-cache-safety dirty bit
contract.

Pins:
  * `mark_ledger_dirty(func, reason)` flips a bit that
    `require_fresh_ledger(func, consumer)` reads as an assertion.
  * The assertion message includes BOTH the consumer name and
    the dirty reason recorded by the mutation site.
  * `maybe_fresh_ledger` is soft-on-missing but hard-on-stale —
    a *stale* ledger is always an assertion regardless of helper
    used.
  * `build_and_attach_ledger` clears the dirty bit as a side
    effect of rebuilding.

These pin the runtime-assertion side of the contract.  The
discipline side (every direct MIR mutation in the four scoped
files calls `mark_ledger_dirty`) is pinned separately by
`test_ledger_cache_safety_mutation_audit.py`.
"""
from __future__ import annotations

import pytest

from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ledger_cache import (
	attach_ledger,
	build_and_attach_ledger,
	mark_ledger_dirty,
	maybe_fresh_ledger,
	require_fresh_ledger,
)
from lang.driftc.stage2.ownership_ledger import LiveStateMap
from lang.driftc.core.function_id import FunctionId


def _make_minimal_func() -> M.MirFunc:
	"""Construct a trivial single-block MirFunc — empty entry block,
	one local.  Enough to exercise the dirty-bit helpers; no real
	ownership semantics required."""
	fn_id = FunctionId(module="test", name="ledger_cache_pin", ordinal=0)
	func = M.MirFunc(
		name="test::ledger_cache_pin",
		params=[],
		locals=["x"],
		fn_id=fn_id,
		local_types={},
	)
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	return func


# -- positive paths ---------------------------------------------------------


def test_build_and_attach_returns_fresh_ledger_and_require_succeeds() -> None:
	"""Happy path: build_and_attach_ledger → require_fresh_ledger
	returns the same ledger without raising."""
	func = _make_minimal_func()
	ledger = build_and_attach_ledger(
		func,
		drop_policy=lambda _t: None,
		reason="test.initial_build",
	)
	got = require_fresh_ledger(func, "test_consumer")
	assert got is ledger


def test_maybe_fresh_returns_none_when_no_ledger_attached() -> None:
	"""maybe_fresh_ledger soft-fails on missing ledger (returns None)."""
	func = _make_minimal_func()
	got = maybe_fresh_ledger(func, "test_optional_consumer")
	assert got is None


def test_maybe_fresh_returns_ledger_when_fresh() -> None:
	"""maybe_fresh_ledger returns the ledger when attached AND fresh."""
	func = _make_minimal_func()
	ledger = build_and_attach_ledger(
		func,
		drop_policy=lambda _t: None,
		reason="test.build",
	)
	got = maybe_fresh_ledger(func, "test_optional_consumer")
	assert got is ledger


# -- negative paths ---------------------------------------------------------


def test_require_fresh_ledger_raises_when_no_ledger_attached() -> None:
	"""require_fresh_ledger (hard form) asserts when no ledger
	attached.  Error message names the consumer."""
	func = _make_minimal_func()
	with pytest.raises(AssertionError) as exc_info:
		require_fresh_ledger(func, "test_strict_consumer")
	msg = str(exc_info.value)
	assert "test_strict_consumer" in msg, f"consumer name missing from message: {msg!r}"
	assert "ledger-cache-safety" in msg, f"contract identifier missing from message: {msg!r}"


def test_require_fresh_ledger_raises_on_stale_ledger() -> None:
	"""Negative path: build → attach → mutate + mark_dirty →
	require_fresh_ledger raises with consumer name + dirty reason."""
	func = _make_minimal_func()
	build_and_attach_ledger(
		func,
		drop_policy=lambda _t: None,
		reason="test.build",
	)
	# Simulate a direct MIR mutation followed by the discipline call.
	func.blocks["entry"].instructions.append(M.ConstBool(dest="t0", value=True))
	mark_ledger_dirty(func, "test.mutation_during_pin")
	with pytest.raises(AssertionError) as exc_info:
		require_fresh_ledger(func, "test_strict_consumer")
	msg = str(exc_info.value)
	assert "test_strict_consumer" in msg, f"consumer name missing: {msg!r}"
	assert "test.mutation_during_pin" in msg, f"dirty reason missing: {msg!r}"
	assert "test.build" in msg, f"last-build reason missing: {msg!r}"


def test_maybe_fresh_still_raises_on_stale_ledger() -> None:
	"""Soft form is soft ONLY on missing-ledger; a stale ledger
	still asserts.  This is the bug class we are catching:
	`maybe_fresh_ledger` must not silently return a stale read."""
	func = _make_minimal_func()
	build_and_attach_ledger(
		func,
		drop_policy=lambda _t: None,
		reason="test.build",
	)
	func.blocks["entry"].instructions.append(M.ConstBool(dest="t0", value=True))
	mark_ledger_dirty(func, "test.stale_mutation")
	with pytest.raises(AssertionError) as exc_info:
		maybe_fresh_ledger(func, "test_soft_consumer")
	msg = str(exc_info.value)
	assert "test_soft_consumer" in msg, f"consumer name missing: {msg!r}"
	assert "test.stale_mutation" in msg, f"dirty reason missing: {msg!r}"


# -- rebuild clears dirty bit -----------------------------------------------


def test_build_and_attach_after_mutation_clears_dirty_bit() -> None:
	"""Rebuilding via build_and_attach_ledger after a mutation
	clears the dirty bit; the next require_fresh_ledger succeeds."""
	func = _make_minimal_func()
	build_and_attach_ledger(
		func,
		drop_policy=lambda _t: None,
		reason="test.initial_build",
	)
	func.blocks["entry"].instructions.append(M.ConstBool(dest="t0", value=True))
	mark_ledger_dirty(func, "test.mutation_before_rebuild")
	# Rebuild — should clear the bit.
	build_and_attach_ledger(
		func,
		drop_policy=lambda _t: None,
		reason="test.rebuild_after_mutation",
	)
	got = require_fresh_ledger(func, "test_consumer")
	assert got is not None


def test_mark_ledger_dirty_is_noop_when_no_ledger_attached() -> None:
	"""When no ledger is attached, mark_ledger_dirty is a no-op —
	this is the normal state during initial HIR→MIR construction,
	when MirBuilder.emit fires but no ledger exists yet."""
	func = _make_minimal_func()
	# No build_and_attach_ledger call; no ledger present.
	mark_ledger_dirty(func, "test.early_mutation")
	# Subsequent attach should give a clean state.
	build_and_attach_ledger(
		func,
		drop_policy=lambda _t: None,
		reason="test.late_build",
	)
	got = require_fresh_ledger(func, "test_consumer")
	assert got is not None


def test_first_dirty_reason_wins() -> None:
	"""When multiple mutations stack up between rebuilds, the FIRST
	dirty reason is preserved (closest to source-order "what
	mutated me first").  Diagnostic stability invariant."""
	func = _make_minimal_func()
	build_and_attach_ledger(
		func,
		drop_policy=lambda _t: None,
		reason="test.build",
	)
	mark_ledger_dirty(func, "test.first_mutation")
	mark_ledger_dirty(func, "test.second_mutation")
	mark_ledger_dirty(func, "test.third_mutation")
	with pytest.raises(AssertionError) as exc_info:
		require_fresh_ledger(func, "test_consumer")
	msg = str(exc_info.value)
	assert "test.first_mutation" in msg, (
		f"first dirty reason should be preserved; got: {msg!r}"
	)
	# Second/third should not appear (only one reason is tracked).
	assert "test.second_mutation" not in msg
	assert "test.third_mutation" not in msg


# -- attach_ledger (low-level) round-trips -----------------------------------


def test_attach_ledger_directly_round_trips() -> None:
	"""Low-level attach_ledger: caller builds the ledger
	separately and attaches.  Same invariants as
	build_and_attach_ledger."""
	func = _make_minimal_func()
	# Build a placeholder LiveStateMap directly.
	ledger = LiveStateMap(tracked_locals=set(), local_types={})
	attach_ledger(func, ledger, reason="test.external_attach")
	got = require_fresh_ledger(func, "test_consumer")
	assert got is ledger
	mark_ledger_dirty(func, "test.after_external_attach")
	with pytest.raises(AssertionError):
		require_fresh_ledger(func, "test_consumer")
