"""
Negative/backend guardrail tests.
"""

from __future__ import annotations

import pytest

from lang2.codegen.llvm import lower_ssa_func_to_llvm
from lang2.checker import FnInfo
from lang2.driftc.stage2 import (
	BasicBlock,
	ConstructResultOk,
	ConstInt,
	ConstString,
	BinaryOpInstr,
	IfTerminator,
	MirFunc,
	Return,
)
from lang2.driftc.stage4 import MirToSSA
from lang2.driftc.core.types_core import TypeTable
from lang2.driftc.stage1 import BinaryOp


def test_branch_condition_must_be_bool():
	"""
	IfTerminator with non-i1 condition should be rejected.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="cond", value=1)],
		terminator=IfTerminator(cond="cond", then_target="then", else_target="else"),
	)
	then_block = BasicBlock(name="then", instructions=[ConstInt(dest="t0", value=1)], terminator=Return(value="t0"))
	else_block = BasicBlock(name="else", instructions=[ConstInt(dest="t1", value=2)], terminator=Return(value="t1"))
	mir = MirFunc(
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry, "then": then_block, "else": else_block},
		entry="entry",
	)
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.new_scalar("Int")
	fn_info = FnInfo(name="f", declared_can_throw=False, return_type_id=int_ty)

	with pytest.raises(NotImplementedError, match="branch condition must be bool"):
		lower_ssa_func_to_llvm(mir, ssa, fn_info, {"f": fn_info})


def test_non_can_throw_returning_fnresult_rejected():
	"""
	Returning FnResult from a non-can-throw function should fail in codegen.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="v", value=1),
			ConstructResultOk(dest="res", value="v"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(name="f", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.new_scalar("Int")
	fn_info = FnInfo(name="f", declared_can_throw=False, return_type_id=int_ty)

	with pytest.raises(NotImplementedError, match="non-can-throw return must be Int"):
		lower_ssa_func_to_llvm(mir, ssa, fn_info, {"f": fn_info})


def test_string_binaryop_unsupported():
	"""String binary ops other than ==/+ should raise, not emit garbage IR."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="a"),
			ConstString(dest="t1", value="b"),
			BinaryOpInstr(dest="t2", op=BinaryOp.NE, left="t0", right="t1"),
		],
		terminator=Return(value="t2"),
	)
	mir = MirFunc(name="f", params=[], locals=["t0", "t1", "t2"], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.new_scalar("Int")
	fn_info = FnInfo(name="f", declared_can_throw=False, return_type_id=int_ty)

	with pytest.raises(NotImplementedError, match="string binary op"):
		lower_ssa_func_to_llvm(mir, ssa, fn_info, {"f": fn_info}, type_table=table)
