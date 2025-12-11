# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage4 dominator analysis tests.

Cases:
  - straight line: entry -> b1 -> b2
  - diamond: entry -> then/else -> join
  - simple loop shape: entry -> header -> body -> header/exit
"""

from __future__ import annotations

from lang2.stage2 import MirFunc, BasicBlock, Goto, IfTerminator
from lang2.stage4 import DominatorAnalysis


def test_dominators_straight_line():
	entry = BasicBlock(name="entry", instructions=[], terminator=Goto(target="b1"))
	b1 = BasicBlock(name="b1", instructions=[], terminator=Goto(target="b2"))
	b2 = BasicBlock(name="b2", instructions=[], terminator=None)
	func = MirFunc(name="f", params=[], locals=[], blocks={"entry": entry, "b1": b1, "b2": b2}, entry="entry")
	info = DominatorAnalysis().compute(func)
	assert info.idom["entry"] is None
	assert info.idom["b1"] == "entry"
	assert info.idom["b2"] == "b1"


def test_dominators_diamond():
	entry = BasicBlock(
		name="entry",
		instructions=[],
		terminator=IfTerminator(cond="c", then_target="then", else_target="else"),
	)
	then = BasicBlock(name="then", instructions=[], terminator=Goto(target="join"))
	else_b = BasicBlock(name="else", instructions=[], terminator=Goto(target="join"))
	join = BasicBlock(name="join", instructions=[], terminator=None)
	func = MirFunc(
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry, "then": then, "else": else_b, "join": join},
		entry="entry",
	)
	info = DominatorAnalysis().compute(func)
	assert info.idom["entry"] is None
	assert info.idom["then"] == "entry"
	assert info.idom["else"] == "entry"
	assert info.idom["join"] == "entry"


def test_dominators_loop_shape():
	entry = BasicBlock(name="entry", instructions=[], terminator=Goto(target="loop_header"))
	loop_header = BasicBlock(name="loop_header", instructions=[], terminator=Goto(target="loop_body"))
	loop_body = BasicBlock(
		name="loop_body",
		instructions=[],
		terminator=IfTerminator(cond="c", then_target="loop_header", else_target="loop_exit"),
	)
	loop_exit = BasicBlock(name="loop_exit", instructions=[], terminator=None)
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
	info = DominatorAnalysis().compute(func)
	assert info.idom["entry"] is None
	assert info.idom["loop_header"] == "entry"
	assert info.idom["loop_body"] == "loop_header"
	# loop_exit is dominated by loop_body in this simple shape
	assert info.idom["loop_exit"] == "loop_body"
