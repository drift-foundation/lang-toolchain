"""
Module-level lowering and inter-function call ABI tests.
"""

from __future__ import annotations

from lang2.driftc.core.function_id import FunctionId
from lang2.codegen.llvm import lower_module_to_llvm
from lang2.codegen.llvm.test_utils import host_word_bits
from lang2.driftc.checker import FnInfo, FnSignature, make_fn_info
from lang2.driftc.stage2 import BasicBlock, MirFunc, ConstInt, Return, ConstructResultOk, Call, ResultOk
from lang2.driftc.stage4 import MirToSSA
from lang2.driftc.core.types_core import TypeTable


def test_module_lowering_non_throwing_call():
	"""
	Lower two non-throwing functions and ensure the call uses isize ABI.
	"""
	# callee: -> 7
	callee_entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="c0", value=7)],
		terminator=Return(value="c0"),
	)
	callee_id = FunctionId(module="main", name="callee", ordinal=0)
	callee_mir = MirFunc(fn_id=callee_id, name="callee", params=[], locals=[], blocks={"entry": callee_entry}, entry="entry")
	callee_ssa = MirToSSA().run(callee_mir)

	# drift_main: calls callee, -> its result
	main_entry = BasicBlock(
		name="entry",
		instructions=[Call(dest="m0", fn_id=callee_id, args=[], can_throw=False)],
		terminator=Return(value="m0"),
	)
	main_id = FunctionId(module="main", name="drift_main", ordinal=0)
	main_mir = MirFunc(fn_id=main_id, name="drift_main", params=[], locals=[], blocks={"entry": main_entry}, entry="entry")
	main_ssa = MirToSSA().run(main_mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	fn_infos = {
		callee_id: FnInfo(fn_id=callee_id, name="callee", declared_can_throw=False, return_type_id=int_ty),
		main_id: FnInfo(fn_id=main_id, name="drift_main", declared_can_throw=False, return_type_id=int_ty),
	}

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	mod = lower_module_to_llvm(
		funcs={callee_id: callee_mir, main_id: main_mir},
		ssa_funcs={callee_id: callee_ssa, main_id: main_ssa},
		fn_infos=fn_infos, word_bits=word_bits)
	ir = mod.render()

	assert f"define {word_ty} @callee()" in ir
	assert f"define {word_ty} @drift_main()" in ir
	assert f"call {word_ty} @callee()" in ir


def test_module_lowering_can_throw_callee_call():
	"""
	Lower a can-throw callee and ensure caller uses FnResult ABI.
	"""
	# callee: -> FnResult.Ok(1)
	callee_entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="c0", value=1), ConstructResultOk(dest="cres", value="c0")],
		terminator=Return(value="cres"),
	)
	callee_id = FunctionId(module="main", name="callee", ordinal=0)
	callee_mir = MirFunc(fn_id=callee_id, name="callee", params=[], locals=[], blocks={"entry": callee_entry}, entry="entry")
	callee_ssa = MirToSSA().run(callee_mir)

	# drift_main: call callee, extract ok part, and return it
	main_entry = BasicBlock(
		name="entry",
		instructions=[Call(dest="mres", fn_id=callee_id, args=[], can_throw=True), ResultOk(dest="m0", result="mres")],
		terminator=Return(value="m0"),
	)
	main_id = FunctionId(module="main", name="drift_main", ordinal=0)
	main_mir = MirFunc(fn_id=main_id, name="drift_main", params=[], locals=[], blocks={"entry": main_entry}, entry="entry")
	main_ssa = MirToSSA().run(main_mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	fnresult_ty = table.new_fnresult(int_ty, err_ty)
	fn_infos = {
		callee_id: FnInfo(fn_id=callee_id, name="callee", declared_can_throw=True, return_type_id=fnresult_ty, error_type_id=err_ty),
		main_id: FnInfo(fn_id=main_id, name="drift_main", declared_can_throw=False, return_type_id=int_ty),
	}

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	mod = lower_module_to_llvm(
		funcs={callee_id: callee_mir, main_id: main_mir},
		ssa_funcs={callee_id: callee_ssa, main_id: main_ssa},
		fn_infos=fn_infos,
		type_table=table, word_bits=word_bits)
	ir = mod.render()

	assert "define %FnResult_Int_Error @callee()" in ir
	assert f"define {word_ty} @drift_main()" in ir
	assert "call %FnResult_Int_Error @callee()" in ir
	assert "extractvalue %FnResult_Int_Error" in ir


def test_lang_thread_exec_create_lowers_to_runtime_call():
	"""
	Calls to lang.thread::exec_create must lower to drift_exec_create, not a direct lang.thread symbol.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="c0", value=1),
			ConstInt(dest="c1", value=1),
			ConstInt(dest="c2", value=0),
			ConstInt(dest="c3", value=0),
			ConstInt(dest="c4", value=0),
			ConstInt(dest="c5", value=262144),
			Call(
				dest="h0",
				fn_id=FunctionId(module="lang.thread", name="exec_create", ordinal=0),
				args=["c0", "c1", "c2", "c3", "c4", "c5"],
				can_throw=False,
			),
		],
		terminator=Return(value="h0"),
	)
	main_id = FunctionId(module="main", name="drift_main", ordinal=0)
	main_mir = MirFunc(fn_id=main_id, name="drift_main", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	main_ssa = MirToSSA().run(main_mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	exec_sig = FnSignature(
		name="exec_create",
		param_type_ids=[int_ty, int_ty, int_ty, int_ty, int_ty, int_ty],
		return_type_id=int_ty,
		is_intrinsic=True,
	)
	fn_infos = {
		main_id: FnInfo(fn_id=main_id, name="drift_main", declared_can_throw=False, return_type_id=int_ty),
		FunctionId(module="lang.thread", name="exec_create", ordinal=0): make_fn_info(
			FunctionId(module="lang.thread", name="exec_create", ordinal=0),
			exec_sig,
			declared_can_throw=False,
		),
	}

	word_bits = host_word_bits()
	mod = lower_module_to_llvm(
		funcs={main_id: main_mir},
		ssa_funcs={main_id: main_ssa},
		fn_infos=fn_infos,
		type_table=table,
		word_bits=word_bits,
	)
	ir = mod.render()

	assert "@drift_exec_create" in ir
	assert "lang.thread::exec_create" not in ir
