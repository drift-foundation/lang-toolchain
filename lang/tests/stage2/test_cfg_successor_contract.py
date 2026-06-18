# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
#
# Central MIR CFG-successor contract (work/scalar-match-jump-table Part A).
#
# Pins the single authoritative answer to "which blocks may a terminator branch
# to" — `MTerminator.successors()` / `.successor_edges()` and the `cfg` helpers
# that delegate to them.  Every CFG-walking pass (ownership_ledger, string_arc,
# cleanup_authoring, ssa, dom) is being migrated to consult these; this test is the
# one place that guarantees each terminator reports its successors correctly, so a
# new terminator (or a changed one) is caught here rather than silently mis-walked
# by a downstream pass.

from __future__ import annotations

import pytest

from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2 import cfg


def test_goto_successors():
	t = M.Goto(target="b1")
	assert t.successors() == ["b1"]
	assert t.successor_edges() == [("b1", "goto")]


def test_if_terminator_successors_order_and_edges():
	t = M.IfTerminator(cond="%c", then_target="bt", else_target="be")
	# Order is stable: then before else.
	assert t.successors() == ["bt", "be"]
	assert t.successor_edges() == [("bt", "if_then"), ("be", "if_else")]


def test_return_has_no_successors():
	t = M.Return(value=None)
	assert t.successors() == []
	assert t.successor_edges() == []


def test_unreachable_has_no_successors():
	t = M.Unreachable()
	assert t.successors() == []
	assert t.successor_edges() == []


def test_base_terminator_raises_loudly():
	# The base intentionally raises so a future terminator that forgets to
	# implement the contract fails loudly instead of silently reporting no
	# successors (which would make dataflow treat reachable code as dead).
	class _NewTerminator(M.MTerminator):
		pass

	with pytest.raises(NotImplementedError):
		_NewTerminator().successors()


def test_cfg_helpers_handle_none():
	assert cfg.terminator_successors(None) == []
	assert cfg.terminator_successor_edges(None) == []


def test_cfg_helpers_delegate():
	assert cfg.terminator_successors(M.Goto(target="x")) == ["x"]
	assert cfg.terminator_successors(M.IfTerminator(cond="%c", then_target="t", else_target="e")) == ["t", "e"]
	assert cfg.terminator_successor_edges(M.IfTerminator(cond="%c", then_target="t", else_target="e")) == [
		("t", "if_then"),
		("e", "if_else"),
	]


def _func_with_blocks():
	# Minimal CFG:  entry -> {a, b};  a -> join;  b -> join;  join: return
	entry = M.BasicBlock(name="entry", instructions=[], terminator=M.IfTerminator(cond="%c", then_target="a", else_target="b"))
	a = M.BasicBlock(name="a", instructions=[], terminator=M.Goto(target="join"))
	b = M.BasicBlock(name="b", instructions=[], terminator=M.Goto(target="join"))
	join = M.BasicBlock(name="join", instructions=[], terminator=M.Return(value=None))

	class _F:
		pass

	f = _F()
	f.blocks = {"entry": entry, "a": a, "b": b, "join": join}
	return f


def test_compute_successors():
	f = _func_with_blocks()
	succ = cfg.compute_successors(f)
	assert succ["entry"] == ["a", "b"]
	assert succ["a"] == ["join"]
	assert succ["b"] == ["join"]
	assert succ["join"] == []


def test_compute_predecessors_counts_each_edge():
	f = _func_with_blocks()
	preds = cfg.compute_predecessors(f)
	assert preds["entry"] == []
	assert sorted(preds["a"]) == ["entry"]
	assert sorted(preds["b"]) == ["entry"]
	# join is reached from both a and b (two distinct edges).
	assert sorted(preds["join"]) == ["a", "b"]


def test_predecessors_edge_multiplicity():
	# An if whose both arms target the same block records two predecessor edges.
	entry = M.BasicBlock(name="entry", instructions=[], terminator=M.IfTerminator(cond="%c", then_target="j", else_target="j"))
	j = M.BasicBlock(name="j", instructions=[], terminator=M.Return(value=None))

	class _F:
		pass

	f = _F()
	f.blocks = {"entry": entry, "j": j}
	preds = cfg.compute_predecessors(f)
	assert preds["j"] == ["entry", "entry"]
