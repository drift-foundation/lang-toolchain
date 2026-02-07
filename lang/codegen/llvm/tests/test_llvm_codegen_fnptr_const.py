"""
LLVM lowering for function pointer constants.
"""

from __future__ import annotations

from lang.codegen.llvm import lower_module_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.function_id import FunctionId, FunctionRefId, FunctionRefKind
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1.call_info import CallSig
from lang.driftc.stage2 import BasicBlock, MirFunc, ConstInt, Return, FnPtrConst, CallIndirect
from lang.driftc.stage4 import MirToSSA


def test_fnptr_const_emits_bitcast() -> None:
	table = TypeTable()
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

	main_block = BasicBlock(
		name="entry",
		instructions=[
			FnPtrConst(dest="fp", fn_ref=fn_ref, call_sig=call_sig),
			ConstInt(dest="arg0", value=3),
			CallIndirect(
				dest="res",
				callee="fp",
				args=["arg0"],
				param_types=[int_ty],
				user_ret_type=int_ty,
				can_throw=False,
			),
		],
		terminator=Return(value="res"),
	)
	main_id = FunctionId(module="main", name="drift_main", ordinal=0)
	main_mir = MirFunc(fn_id=main_id, name="drift_main", params=[], locals=[], blocks={"entry": main_block}, entry="entry")
	main_ssa = MirToSSA().run(main_mir)

	add1_sig = FnSignature(name="add1", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False)
	main_sig = FnSignature(name="drift_main", return_type_id=int_ty, declared_can_throw=False)
	fn_infos = {
		fn_id: FnInfo(fn_id=fn_id, name="add1", declared_can_throw=False, return_type_id=int_ty, signature=add1_sig),
		main_id: FnInfo(fn_id=main_id, name="drift_main", declared_can_throw=False, return_type_id=int_ty, signature=main_sig),
	}

	mod = lower_module_to_llvm(
		funcs={fn_id: add1_mir, main_id: main_mir},
		ssa_funcs={fn_id: add1_ssa, main_id: main_ssa},
		fn_infos=fn_infos,
		type_table=table, word_bits=host_word_bits())
	ir = mod.render()

	assert "@add1" in ir
