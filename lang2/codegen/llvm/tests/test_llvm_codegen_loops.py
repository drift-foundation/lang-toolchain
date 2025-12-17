# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Loop/backedge codegen smoke test (v1).

Historically, the SSA and LLVM backends rejected loops/backedges while the
compiler was being brought up. The `for`/`while` surface constructs desugar to
`loop` in HIR, so we must be able to lower cyclic CFGs end-to-end.

This test builds a tiny MIR loop by hand, runs SSA conversion, and lowers to
LLVM IR, asserting the emitted IR contains the expected backedge branch.
"""

from __future__ import annotations

from lang2.codegen.llvm import lower_ssa_func_to_llvm
from lang2.driftc.checker import FnInfo
from lang2.driftc.core.types_core import TypeTable
from lang2.driftc.stage2 import (
	BasicBlock,
	MirFunc,
	Goto,
	Return,
	ConstInt,
	LoadLocal,
	StoreLocal,
	BinaryOpInstr,
	IfTerminator,
)
from lang2.driftc.stage2.mir_nodes import BinaryOp
from lang2.driftc.stage4 import MirToSSA


def test_loop_backedge_supported() -> None:
	# A simple countdown loop:
	#
	#   var x = 3
	#   loop:
	#     if x > 0 { x = x - 1; goto loop } else { return 0 }
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="t_init", value=3),
			StoreLocal(local="x", value="t_init"),
		],
		terminator=Goto(target="loop"),
	)
	loop_hdr = BasicBlock(
		name="loop",
		instructions=[
			LoadLocal(dest="t_x", local="x"),
			ConstInt(dest="t0", value=0),
			BinaryOpInstr(dest="t_cmp", op=BinaryOp.GT, left="t_x", right="t0"),
		],
		terminator=IfTerminator(cond="t_cmp", then_target="body", else_target="exit"),
	)
	body = BasicBlock(
		name="body",
		instructions=[
			LoadLocal(dest="t_x2", local="x"),
			ConstInt(dest="t1", value=1),
			BinaryOpInstr(dest="t_dec", op=BinaryOp.SUB, left="t_x2", right="t1"),
			StoreLocal(local="x", value="t_dec"),
		],
		terminator=Goto(target="loop"),
	)
	exit_block = BasicBlock(
		name="exit",
		instructions=[ConstInt(dest="t_ret", value=0)],
		terminator=Return(value="t_ret"),
	)
	mir = MirFunc(
		name="loopy",
		params=[],
		locals=["x"],
		blocks={"entry": entry, "loop": loop_hdr, "body": body, "exit": exit_block},
		entry="entry",
	)

	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	fn_info = FnInfo(name="loopy", declared_can_throw=False, return_type_id=int_ty)

	ir = lower_ssa_func_to_llvm(mir, ssa, fn_info, {"loopy": fn_info}, type_table=table)
	assert "loop:" in ir
	assert "body:" in ir
	# Backedge must exist in the textual IR.
	assert "br label %loop" in ir

