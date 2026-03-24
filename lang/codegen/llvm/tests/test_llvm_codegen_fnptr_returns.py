"""
LLVM lowering for functions that return function-pointer values.
"""

from __future__ import annotations

from lang.codegen.llvm import lower_module_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.function_id import FunctionId, FunctionRefId, FunctionRefKind
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1.call_info import CallSig
from lang.driftc.stage2 import BasicBlock, MirFunc, Return, FnPtrConst, ConstructResultOk
from lang.driftc.stage4 import MirToSSA


def _build_add1(table: TypeTable) -> tuple[MirFunc, object, FnInfo, FunctionRefId, CallSig]:
	int_ty = table.ensure_int()
	fn_id = FunctionId(module="main", name="add1", ordinal=0)
	call_sig = CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=False)
	fn_ref = FunctionRefId(fn_id=fn_id, kind=FunctionRefKind.IMPL, has_wrapper=False)

	add1_block = BasicBlock(
		name="entry",
		instructions=[],
		terminator=Return(value="x"),
	)
	add1_mir = MirFunc(fn_id=fn_id, name="add1", params=["x"], locals=[], blocks={"entry": add1_block}, entry="entry")
	add1_ssa = MirToSSA().run(add1_mir)
	add1_sig = FnSignature(name="add1", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False)
	add1_info = FnInfo(fn_id=fn_id, name="add1", declared_can_throw=False, return_type_id=int_ty, signature=add1_sig)
	return add1_mir, add1_ssa, add1_info, fn_ref, call_sig


def test_fnptr_return_type_lowering() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	fn_ty = table.ensure_function([int_ty], int_ty, can_throw=False)

	add1_mir, add1_ssa, add1_info, fn_ref, call_sig = _build_add1(table)

	make_block = BasicBlock(
		name="entry",
		instructions=[FnPtrConst(dest="fp", fn_ref=fn_ref, call_sig=call_sig)],
		terminator=Return(value="fp"),
	)
	make_id = FunctionId(module="main", name="make", ordinal=0)
	make_mir = MirFunc(fn_id=make_id, name="make", params=[], locals=[], blocks={"entry": make_block}, entry="entry")
	make_ssa = MirToSSA().run(make_mir)
	make_sig = FnSignature(name="make", return_type_id=fn_ty, declared_can_throw=False)
	make_info = FnInfo(fn_id=make_id, name="make", declared_can_throw=False, return_type_id=fn_ty, signature=make_sig)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	mod = lower_module_to_llvm(
		funcs={add1_info.fn_id: add1_mir, make_id: make_mir},
		ssa_funcs={add1_info.fn_id: add1_ssa, make_id: make_ssa},
		fn_infos={add1_info.fn_id: add1_info, make_id: make_info},
		type_table=table, word_bits=word_bits)
	ir = mod.render()

	assert f"define ptr @make()" in ir
	assert "@add1" in ir


def test_fnptr_return_fnresult_ok_payload() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	fn_ty = table.ensure_function([int_ty], int_ty, can_throw=False)

	add1_mir, add1_ssa, add1_info, fn_ref, call_sig = _build_add1(table)

	make_block = BasicBlock(
		name="entry",
		instructions=[
			FnPtrConst(dest="fp", fn_ref=fn_ref, call_sig=call_sig),
			ConstructResultOk(dest="res", value="fp"),
		],
		terminator=Return(value="res"),
	)
	make_id = FunctionId(module="main", name="make", ordinal=0)
	make_mir = MirFunc(fn_id=make_id, name="make", params=[], locals=[], blocks={"entry": make_block}, entry="entry")
	make_ssa = MirToSSA().run(make_mir)
	make_sig = FnSignature(name="make", return_type_id=fn_ty, declared_can_throw=True)
	make_info = FnInfo(fn_id=make_id, name="make", declared_can_throw=True, return_type_id=fn_ty, signature=make_sig)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	mod = lower_module_to_llvm(
		funcs={add1_info.fn_id: add1_mir, make_id: make_mir},
		ssa_funcs={add1_info.fn_id: add1_ssa, make_id: make_ssa},
		fn_infos={add1_info.fn_id: add1_info, make_id: make_info},
		type_table=table, word_bits=word_bits)
	ir = mod.render()

	assert f"%FnResult_FnPtr_Int_to_Int_NoThrow_Error = type {{ i8, ptr, ptr }}" in ir
	assert "define %FnResult_FnPtr_Int_to_Int_NoThrow_Error @make()" in ir
