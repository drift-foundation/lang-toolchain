"""
SSA-first LLVM codegen smoke tests.
"""

from __future__ import annotations

from lang2.codegen.llvm import LlvmModuleBuilder, lower_ssa_func_to_llvm
from lang2.driftc.checker import FnInfo, FnSignature
from lang2.driftc.stage2 import (
	BasicBlock,
	ConstInt,
	ConstructError,
	ConstructResultErr,
	ConstructResultOk,
	ConstructDV,
	MirFunc,
	Return,
	ConstString,
)
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
	int_ty = table.ensure_int()
	fn_info = _fn_info("main", False, int_ty)

	ir = lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table)

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
	int_ty = table.ensure_int()
	error_ty = table.ensure_error()
	fnresult_ty = table.new_fnresult(int_ty, error_ty)
	fn_info = _fn_info("f", True, fnresult_ty)

	mod = LlvmModuleBuilder()
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table))
	ir = mod.render()

	assert "%FnResult_Int_Error" in ir
	assert "define %FnResult_Int_Error @f()" in ir
	assert "ret %FnResult_Int_Error %res" in ir


def test_codegen_fnresult_ref_ok_return():
	"""FnResult<Ref<Int>, Error> should use a named FnResult type with ptr payload."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstructResultOk(dest="res", value="p0"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(name="f_ref", params=["p0"], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int = table.ensure_ref(int_ty)
	error_ty = table.ensure_error()
	fnresult_ty = table.new_fnresult(ref_int, error_ty)
	sig = FnSignature(name="f_ref", param_type_ids=[ref_int], return_type_id=fnresult_ty, declared_can_throw=True)
	fn_info = FnInfo(name="f_ref", declared_can_throw=True, return_type_id=fnresult_ty, signature=sig)

	mod = LlvmModuleBuilder()
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table))
	ir = mod.render()

	assert "%FnResult_Ref_Int_Error = type { i1, ptr, %DriftError* }" in ir
	assert "define %FnResult_Ref_Int_Error @f_ref(ptr %p0)" in ir


def test_codegen_fnresult_ref_err_zero_ok_slot():
	"""
	FnResult.Err for Ref<Int> ok payload should zero-initialize the ptr ok slot cleanly.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="code", value=1),
			ConstructDV(dest="dv", dv_type_name="Missing", args=[]),
			ConstString(dest="key", value="payload"),
			ConstructError(dest="err", code="code", payload="dv", attr_key="key"),
			ConstructResultErr(dest="res", error="err"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(name="f_ref_err", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int = table.ensure_ref(int_ty)
	error_ty = table.ensure_error()
	fnresult_ty = table.new_fnresult(ref_int, error_ty)
	fn_info = FnInfo(name="f_ref_err", declared_can_throw=True, return_type_id=fnresult_ty)

	mod = LlvmModuleBuilder()
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table))
	ir = mod.render()

	assert "%FnResult_Ref_Int_Error = type { i1, ptr, %DriftError* }" in ir
	assert "define %FnResult_Ref_Int_Error @f_ref_err()" in ir
	assert any(
		"%FnResult_Ref_Int_Error" in line and ", ptr null, 1" in line for line in ir.splitlines()
	)
