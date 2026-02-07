# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2026-01-30
r"""
SSA should supply a value for φ incomings even when a local is not defined
along a predecessor path. This guards against invalid LLVM φ nodes (missing
incoming edges), which can crash clang.
"""

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import BasicBlock, MirFunc, StoreLocal, LoadLocal, Return, Goto, IfTerminator, Phi, ZeroValue, AssignSSA
from lang.driftc.stage4 import MirToSSA


def test_phi_missing_incoming_filled_with_zero():
	tt = TypeTable()
	int_ty = tt.ensure_int()
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
		instructions=[],
		terminator=IfTerminator(cond="c2", then_target="join", else_target="err"),
	)
	err = BasicBlock(
		name="err",
		instructions=[StoreLocal(local="x", value="v_err")],
		terminator=Goto(target="join"),
	)
	join = BasicBlock(
		name="join",
		instructions=[LoadLocal(dest="t0", local="x")],
		terminator=Return(value="t0"),
	)
	func = MirFunc(
		fn_id=FunctionId(module="main", name="f_phi_missing", ordinal=0),
		name="f_phi_missing",
		params=[],
		locals=["x"],
		blocks={
			"entry": entry,
			"then": then,
			"else": else_block,
			"err": err,
			"join": join,
		},
		entry="entry",
		local_types={"x": int_ty},
	)

	s = MirToSSA().run(func)
	join_block = s.func.blocks["join"]
	assert join_block.instructions
	phi = join_block.instructions[0]
	assert isinstance(phi, Phi)
	assert "then" in phi.incoming
	assert "else" in phi.incoming
	assert "err" in phi.incoming
	else_block = s.func.blocks["else"]
	assert any(isinstance(instr, ZeroValue) for instr in else_block.instructions)
	assign = join_block.instructions[1]
	assert isinstance(assign, AssignSSA)
	assert assign.src == phi.dest
