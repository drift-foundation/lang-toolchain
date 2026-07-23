# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
#
# Central MIR CFG-successor contract (work/scalar-match-jump-table Part A).
#
# Pins the single authoritative answer to "which blocks may a terminator branch
# to" — `MTerminator.successors()` / `.successor_edges()` and the `cfg` helpers
# that delegate to them.  Every CFG-walking pass (ownership_ledger,
# ownership_normalization,
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


def test_switch_terminator_successors_order():
	# Case targets in source order, then the default last.
	t = M.SwitchTerminator(scrutinee="%n", cases=[(0, "b0"), (5, "b5"), (-3, "bneg")], default_target="bd")
	assert t.successors() == ["b0", "b5", "bneg", "bd"]
	# Edge labels are INDEX-based, not value-based (unambiguous even if two cases
	# shared a value).
	assert t.successor_edges() == [
		("b0", "switch_case:0"),
		("b5", "switch_case:1"),
		("bneg", "switch_case:2"),
		("bd", "switch_default"),
	]


def test_switch_terminator_value_use_is_scrutinee_not_targets():
	t = M.SwitchTerminator(scrutinee="%n", cases=[(0, "b0"), (1, "b1")], default_target="bd")
	# The scrutinee is a value use; case/default block names are NOT values.
	assert t.value_uses() == ["%n"]
	assert "b0" not in t.value_uses() and "bd" not in t.value_uses()


def test_value_uses_per_terminator():
	assert M.Goto(target="x").value_uses() == []
	assert M.IfTerminator(cond="%c", then_target="t", else_target="e").value_uses() == ["%c"]
	assert M.Return(value="%r").value_uses() == ["%r"]
	assert M.Return(value=None).value_uses() == []
	assert M.Unreachable().value_uses() == []


def test_switch_remap_targets():
	t = M.SwitchTerminator(scrutinee="%n", cases=[(0, "b0"), (1, "b1")], default_target="bd")
	t.remap_targets({"b0": "B0", "bd": "BD"})
	assert t.cases == [(0, "B0"), (1, "b1")]
	assert t.default_target == "BD"
	assert t.scrutinee == "%n"  # value untouched


def test_switch_redirect_edge():
	t = M.SwitchTerminator(scrutinee="%n", cases=[(0, "b0"), (5, "b5")], default_target="bd")
	t.redirect_edge("switch_case:1", "edge_blk")  # index-based: case at index 1
	assert t.cases == [(0, "b0"), (5, "edge_blk")]
	t.redirect_edge("switch_default", "edge_d")
	assert t.default_target == "edge_d"
	import pytest as _pytest
	with _pytest.raises((AssertionError, IndexError)):
		t.redirect_edge("switch_case:9", "x")  # no such case index


def test_if_redirect_edge_and_remap():
	t = M.IfTerminator(cond="%c", then_target="t", else_target="e")
	t.redirect_edge("if_then", "T2")
	assert t.then_target == "T2"
	t.remap_targets({"e": "E2"})
	assert t.else_target == "E2"


def test_cfg_terminator_value_uses_helper():
	assert cfg.terminator_value_uses(None) == []
	assert cfg.terminator_value_uses(M.SwitchTerminator(scrutinee="%s", cases=[(1, "a")], default_target="d")) == ["%s"]


def test_switch_in_cfg_successor_helpers():
	sw = M.BasicBlock(name="entry", instructions=[], terminator=M.SwitchTerminator(scrutinee="%n", cases=[(0, "a"), (1, "b")], default_target="d"))
	a = M.BasicBlock(name="a", instructions=[], terminator=M.Goto(target="join"))
	b = M.BasicBlock(name="b", instructions=[], terminator=M.Goto(target="join"))
	d = M.BasicBlock(name="d", instructions=[], terminator=M.Goto(target="join"))
	join = M.BasicBlock(name="join", instructions=[], terminator=M.Return(value=None))

	class _F:
		pass

	f = _F()
	f.blocks = {"entry": sw, "a": a, "b": b, "d": d, "join": join}
	assert cfg.compute_successors(f)["entry"] == ["a", "b", "d"]
	preds = cfg.compute_predecessors(f)
	assert sorted(preds["a"]) == ["entry"] and sorted(preds["d"]) == ["entry"]
	assert sorted(preds["join"]) == ["a", "b", "d"]


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
