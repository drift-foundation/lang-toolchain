"""
Module-level lowering and inter-function call ABI tests.
"""

from __future__ import annotations

from lang2.codegen.llvm import lower_module_to_llvm
from lang2.checker import FnInfo
from lang2.driftc.stage2 import BasicBlock, MirFunc, ConstInt, Return, ConstructResultOk, Call
from lang2.stage4 import MirToSSA
from lang2.driftc.core.types_core import TypeTable


def test_module_lowering_non_throwing_call():
	"""
	Lower two non-throwing functions and ensure the call uses i64 ABI.
	"""
	# callee: returns 7
	callee_entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="c0", value=7)],
		terminator=Return(value="c0"),
	)
	callee_mir = MirFunc(name="callee", params=[], locals=[], blocks={"entry": callee_entry}, entry="entry")
	callee_ssa = MirToSSA().run(callee_mir)

	# drift_main: calls callee, returns its result
	main_entry = BasicBlock(
		name="entry",
		instructions=[Call(dest="m0", fn="callee", args=[])],
		terminator=Return(value="m0"),
	)
	main_mir = MirFunc(name="drift_main", params=[], locals=[], blocks={"entry": main_entry}, entry="entry")
	main_ssa = MirToSSA().run(main_mir)

	table = TypeTable()
	int_ty = table.new_scalar("Int")
	fn_infos = {
		"callee": FnInfo(name="callee", declared_can_throw=False, return_type_id=int_ty),
		"drift_main": FnInfo(name="drift_main", declared_can_throw=False, return_type_id=int_ty),
	}

	mod = lower_module_to_llvm(
		funcs={"callee": callee_mir, "drift_main": main_mir},
		ssa_funcs={"callee": callee_ssa, "drift_main": main_ssa},
		fn_infos=fn_infos,
	)
	ir = mod.render()

	assert "define i64 @callee()" in ir
	assert "define i64 @drift_main()" in ir
	assert "call i64 @callee()" in ir


def test_module_lowering_can_throw_callee_call():
	"""
	Lower a can-throw callee and ensure caller uses FnResult ABI.
	"""
	# callee: returns FnResult.Ok(1)
	callee_entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="c0", value=1), ConstructResultOk(dest="cres", value="c0")],
		terminator=Return(value="cres"),
	)
	callee_mir = MirFunc(name="callee", params=[], locals=[], blocks={"entry": callee_entry}, entry="entry")
	callee_ssa = MirToSSA().run(callee_mir)

	# drift_main: call callee, return ok part
	main_entry = BasicBlock(
		name="entry",
		instructions=[Call(dest="m0", fn="callee", args=[])],
		terminator=Return(value="m0"),
	)
	main_mir = MirFunc(name="drift_main", params=[], locals=[], blocks={"entry": main_entry}, entry="entry")
	main_ssa = MirToSSA().run(main_mir)

	table = TypeTable()
	int_ty = table.new_scalar("Int")
	err_ty = table.new_error("Error")
	fnresult_ty = table.new_fnresult(int_ty, err_ty)
	fn_infos = {
		"callee": FnInfo(name="callee", declared_can_throw=True, return_type_id=fnresult_ty, error_type_id=err_ty),
		"drift_main": FnInfo(name="drift_main", declared_can_throw=False, return_type_id=int_ty),
	}

	mod = lower_module_to_llvm(
		funcs={"callee": callee_mir, "drift_main": main_mir},
		ssa_funcs={"callee": callee_ssa, "drift_main": main_ssa},
		fn_infos=fn_infos,
	)
	ir = mod.render()

	assert "define %FnResult_Int_Error @callee()" in ir
	assert "define i64 @drift_main()" in ir
	assert "call %FnResult_Int_Error @callee()" in ir
	assert "extractvalue %FnResult_Int_Error" in ir
