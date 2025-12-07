"""
Basic smoke tests for the textual LLVM codegen skeleton.
"""

from __future__ import annotations

from lang2.codegen.llvm import LlvmModuleBuilder, lower_mir_func_to_llvm
from lang2.stage2 import BasicBlock, MirFunc, ConstInt, Return, ConstructResultOk


def test_codegen_plain_int_return():
	"""Non-can-throw function returning a constant Int should lower to i64 return."""
	entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="t0", value=42)],
		terminator=Return(value="t0"),
	)
	mir = MirFunc(name="main", params=[], locals=[], blocks={"entry": entry}, entry="entry")

	ir = lower_mir_func_to_llvm(mir, can_throw=False)

	assert "define i64 @main()" in ir
	assert "ret i64 %t0" in ir


def test_codegen_fnresult_ok_return():
	"""Can-throw function returning FnResult.Ok should lower to FnResult struct return."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="v", value=1),
			ConstructResultOk(dest="res", value="v"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(name="f", params=[], locals=[], blocks={"entry": entry}, entry="entry")

	mod = LlvmModuleBuilder()
	mod.emit_func(lower_mir_func_to_llvm(mir, can_throw=True))
	ir = mod.render()

	assert "%FnResult_Int_Error" in ir
	assert "define %FnResult_Int_Error @f()" in ir
	assert "ret %FnResult_Int_Error %res" in ir
