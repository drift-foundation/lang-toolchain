"""
Negative/backend guardrail tests.
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
import pytest

from lang.codegen.llvm import lower_ssa_func_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo
from lang.driftc.stage2 import (
	BasicBlock,
	ConstructResultOk,
	ConstInt,
	ConstString,
	BinaryOpInstr,
	IfTerminator,
	MirFunc,
	Return,
)
from lang.driftc.stage4 import MirToSSA
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1 import BinaryOp


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
		fn_id=FunctionId(module="main", name="f", ordinal=0),
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry, "then": then_block, "else": else_block},
		entry="entry",
	)
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.new_scalar("Int")
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	fn_info = FnInfo(fn_id=fn_id, name="f", declared_can_throw=False, return_type_id=int_ty)

	with pytest.raises(NotImplementedError, match="branch condition must be bool"):
		lower_ssa_func_to_llvm(mir, ssa, fn_info, {fn_id: fn_info}, type_table=table, word_bits=host_word_bits())


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
	mir = MirFunc(fn_id=FunctionId(module="main", name="f", ordinal=0), name="f", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.new_scalar("Int")
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	fn_info = FnInfo(fn_id=fn_id, name="f", declared_can_throw=False, return_type_id=int_ty)

	with pytest.raises(NotImplementedError, match="FnResult construction in non-can-throw function"):
		lower_ssa_func_to_llvm(mir, ssa, fn_info, {fn_id: fn_info}, type_table=table, word_bits=host_word_bits())


def test_can_throw_fnresult_with_unsupported_interface_ok_type_is_rejected():
	"""
	Can-throw FnResult with an unsupported ok payload should fail fast with a clear diagnostic.
	"""
	table = TypeTable()
	opt_ty = table.declare_interface("m", "I", type_params=[])
	err_ty = table.ensure_error()
	fnresult_ty = table.new_fnresult(opt_ty, err_ty)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstructResultOk(dest="res", value="v0"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="f", ordinal=0), name="f", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	fn_info = FnInfo(fn_id=fn_id, name="f", declared_can_throw=True, return_type_id=fnresult_ty, error_type_id=err_ty)

	with pytest.raises(NotImplementedError, match="FnResult ok type INTERFACE is not supported"):
		lower_ssa_func_to_llvm(mir, ssa, fn_info, {fn_id: fn_info}, type_table=table, word_bits=host_word_bits())


def test_string_binaryop_unsupported():
	"""String binary ops other than ==/+ should raise, not emit garbage IR."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="a"),
			ConstString(dest="t1", value="b"),
			BinaryOpInstr(dest="t2", op=BinaryOp.SUB, left="t0", right="t1"),
		],
		terminator=Return(value="t2"),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="f", ordinal=0), name="f", params=[], locals=["t0", "t1", "t2"], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.new_scalar("Int")
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	fn_info = FnInfo(fn_id=fn_id, name="f", declared_can_throw=False, return_type_id=int_ty)

	with pytest.raises(NotImplementedError, match="string binary op"):
		lower_ssa_func_to_llvm(mir, ssa, fn_info, {fn_id: fn_info}, type_table=table, word_bits=host_word_bits())
