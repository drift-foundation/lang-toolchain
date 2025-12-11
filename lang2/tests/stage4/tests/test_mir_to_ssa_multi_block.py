# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
r"""
Experimental SSA: ensure Φ placement on a simple diamond CFG.

This exercises `MirToSSA.run_experimental_multi_block` using a classic diamond:

    entry -> then -> join
          \\-> else -> join

Each branch defines the same local `x` with different values. The join block
should receive a Φ for `x` with incoming values from both predecessors.
"""

from lang2.driftc.stage2 import BasicBlock, MirFunc, StoreLocal, LoadLocal, Return, Goto, IfTerminator
from lang2.driftc.stage4 import MirToSSA


def test_phi_placed_in_join_for_local_defined_in_branches():
	# Build MIR CFG manually.
	entry = BasicBlock(
		name="entry",
		instructions=[],
		terminator=IfTerminator(cond="c", then_target="then", else_target="else"),
	)
	then = BasicBlock(
		name="then",
		instructions=[StoreLocal(local="x", value="v_then")],
		terminator=Goto(target="join"),
	)
	else_block = BasicBlock(
		name="else",
		instructions=[StoreLocal(local="x", value="v_else")],
		terminator=Goto(target="join"),
	)
	join = BasicBlock(
		name="join",
		instructions=[LoadLocal(dest="t0", local="x")],
		terminator=Return(value="t0"),
	)
	func = MirFunc(
		name="f_phi",
		params=[],
		locals=["x"],
		blocks={
			"entry": entry,
			"then": then,
			"else": else_block,
			"join": join,
		},
		entry="entry",
	)

	s = MirToSSA().run(func)

	join_block = s.func.blocks["join"]
	assert join_block.instructions
	phi = join_block.instructions[0]
	# Should have inserted a Phi for x at the top of join.
	from lang2.driftc.stage2 import Phi  # local import to keep test narrow
	assert isinstance(phi, Phi)
	assert phi.dest.startswith("x_")
	# Incoming should reference SSA names from each branch.
	assert phi.incoming.get("then", "").startswith("x_")
	assert phi.incoming.get("else", "").startswith("x_")
	# LoadLocal rewritten to AssignSSA consuming the phi value.
	from lang2.driftc.stage2 import AssignSSA
	assign = join_block.instructions[1]
	assert isinstance(assign, AssignSSA)
	assert assign.src == phi.dest
