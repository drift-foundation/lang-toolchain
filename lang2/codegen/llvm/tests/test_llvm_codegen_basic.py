"""
SSA-first LLVM codegen smoke tests.
"""

from __future__ import annotations

from lang2.codegen.llvm import LlvmModuleBuilder, lower_ssa_func_to_llvm
from lang2.checker import FnInfo
from lang2.driftc.stage2 import BasicBlock, MirFunc, ConstInt, Return, ConstructResultOk
from lang2.driftc.stage4 import MirToSSA
from lang2.driftc.core.types_core import TypeTable


def _fn_info(name: str, can_throw: bool, return_ty: int) -> FnInfo:
	"""Build a minimal FnInfo with the provided return TypeId."""
	return FnInfo(
		name=name,
		declared_can_throw=can_throw,
		return_type_id=return_ty,
	)


def test_codegen_plain_int_return():
	"""
	Non-can-throw function returning a constant Int should lower to i64 return.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="t0", value=42)],
		terminator=Return(value="t0"),
	)
	mir = MirFunc(name="main", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.new_scalar("Int")
	fn_info = _fn_info("main", False, int_ty)

	ir = lower_ssa_func_to_llvm(mir, ssa, fn_info)

	assert "define i64 @main()" in ir
	assert "ret i64 %t0" in ir


def test_codegen_fnresult_ok_return():
	"""
	Can-throw function returning FnResult.Ok should lower to FnResult struct return.
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
	error_ty = table.new_error("Error")
	fnresult_ty = table.new_fnresult(int_ty, error_ty)
	fn_info = _fn_info("f", True, fnresult_ty)

	mod = LlvmModuleBuilder()
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info))
	ir = mod.render()

	assert "%FnResult_Int_Error" in ir
	assert "define %FnResult_Int_Error @f()" in ir
	assert "ret %FnResult_Int_Error %res" in ir
