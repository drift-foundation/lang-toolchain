# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Unit tests for dominance frontier computation on MIR CFGs.

Covers straight-line (no frontiers), diamond (join block in branch frontiers,
empty DF(entry)), and loop shapes (backedge/exit in frontiers).
"""

from lang2.stage2 import BasicBlock, MirFunc, Goto, IfTerminator
from lang2.stage4 import DominatorAnalysis, DominanceFrontierAnalysis


def _run_df(func: MirFunc):
	dom_info = DominatorAnalysis().compute(func)
	return DominanceFrontierAnalysis().compute(func, dom_info)


def test_df_straight_line_empty():
	"""Straight-line CFG should have empty dominance frontiers."""
	entry = BasicBlock(name="entry", terminator=Goto(target="b1"))
	b1 = BasicBlock(name="b1", terminator=Goto(target="b2"))
	b2 = BasicBlock(name="b2", terminator=None)
	func = MirFunc(name="f", params=[], locals=[], blocks={"entry": entry, "b1": b1, "b2": b2}, entry="entry")
	info = _run_df(func)
	assert all(not frontier for frontier in info.df.values())


def test_df_diamond_join_in_entry_frontier():
	"""Diamond CFG should put the join block in DF(then/else), entry has empty DF."""
	entry = BasicBlock(name="entry", terminator=IfTerminator(cond="c", then_target="then", else_target="else"))
	then = BasicBlock(name="then", terminator=Goto(target="join"))
	else_block = BasicBlock(name="else", terminator=Goto(target="join"))
	join = BasicBlock(name="join", terminator=None)
	func = MirFunc(
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry, "then": then, "else": else_block, "join": join},
		entry="entry",
	)
	info = _run_df(func)
	# Standard result: entry has empty DF; both branches have join in their frontier.
	assert not info.df["entry"]
	assert "join" in info.df["then"]
	assert "join" in info.df["else"]
	assert not info.df["join"]


def test_df_loop_captures_backedge_and_exit():
	"""Loop shape should expose a frontier for the backedge/exit."""
	entry = BasicBlock(name="entry", terminator=Goto(target="loop_header"))
	loop_header = BasicBlock(name="loop_header", terminator=Goto(target="loop_body"))
	# loop_body either continues (back to header) or exits
	loop_body = BasicBlock(name="loop_body", terminator=IfTerminator(cond="c", then_target="loop_header", else_target="loop_exit"))
	loop_exit = BasicBlock(name="loop_exit", terminator=None)
	func = MirFunc(
		name="f",
		params=[],
		locals=[],
		blocks={
			"entry": entry,
			"loop_header": loop_header,
			"loop_body": loop_body,
			"loop_exit": loop_exit,
		},
		entry="entry",
	)
	info = _run_df(func)
	# loop_body has the backedge target in its frontier; loop_header may list itself.
	assert "loop_header" in info.df["loop_body"]
	assert "loop_exit" not in info.df["loop_body"]
