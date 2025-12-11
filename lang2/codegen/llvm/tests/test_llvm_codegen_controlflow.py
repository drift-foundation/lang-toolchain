"""
Control-flow lowering smoke tests (diamonds).
"""

from __future__ import annotations

from lang2.codegen.llvm import lower_ssa_func_to_llvm
from lang2.driftc.checker import FnInfo
from lang2.driftc.stage2 import (
	BasicBlock,
	ConstBool,
	ConstInt,
	Goto,
	IfTerminator,
	LoadLocal,
	MirFunc,
	Return,
	StoreLocal,
)
from lang2.driftc.stage4 import MirToSSA
from lang2.driftc.core.types_core import TypeTable


def _int_fn_info(name: str, can_throw: bool, table: TypeTable) -> FnInfo:
	return FnInfo(
		name=name,
		declared_can_throw=can_throw,
		return_type_id=table.new_scalar("Int"),
	)


def test_if_else_phi_lowering():
	"""
	If/else diamond should produce branch + phi and lower to correct LLVM types.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[ConstBool(dest="cond", value=True)],
		terminator=IfTerminator(cond="cond", then_target="then", else_target="else"),
	)
	then_block = BasicBlock(
		name="then",
		instructions=[ConstInt(dest="t_then", value=10), StoreLocal(local="x", value="t_then")],
		terminator=Goto(target="join"),
	)
	else_block = BasicBlock(
		name="else",
		instructions=[ConstInt(dest="t_else", value=20), StoreLocal(local="x", value="t_else")],
		terminator=Goto(target="join"),
	)
	join_block = BasicBlock(
		name="join",
		instructions=[LoadLocal(dest="ret", local="x")],
		terminator=Return(value="ret"),
	)

	mir = MirFunc(
		name="if_phi",
		params=[],
		locals=["x"],
		blocks={
			"entry": entry,
			"then": then_block,
			"else": else_block,
			"join": join_block,
		},
		entry="entry",
	)
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	fn_info = _int_fn_info("if_phi", False, table)
	ir = lower_ssa_func_to_llvm(mir, ssa, fn_info, {"if_phi": fn_info})

	assert "br i1 %cond, label %then, label %else" in ir
	assert "phi i64" in ir
	assert "ret i64" in ir
