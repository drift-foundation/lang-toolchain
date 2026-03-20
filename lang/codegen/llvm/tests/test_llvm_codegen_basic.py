"""
SSA-first LLVM codegen smoke tests.
"""

from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.codegen.llvm import LlvmModuleBuilder, lower_ssa_func_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.codegen.llvm import lower_module_to_llvm


import shutil
import subprocess
import tempfile


def _verify_ir_with_llvm_as(ir: str) -> None:
	"""Verify IR with llvm-as-20 (or llvm-as). Skips if not available."""
	llvm_as = shutil.which("llvm-as-20") or shutil.which("llvm-as")
	if llvm_as is None:
		import pytest
		pytest.skip("llvm-as not available — cannot verify IR")
	with tempfile.NamedTemporaryFile(suffix=".ll", mode="w", delete=False) as f:
		f.write(ir)
		f.flush()
		result = subprocess.run(
			[llvm_as, f.name, "-o", "/dev/null"],
			capture_output=True, text=True,
		)
	assert result.returncode == 0, f"llvm-as rejected IR:\n{result.stderr}"
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.stage2 import (
	BasicBlock,
	ConstBool,
	ConstInt,
	ConstUint,
	ConstUint64,
	ConstructError,
	ConstructResultErr,
	ConstructResultOk,
	ConstructDV,
	BinaryOpInstr,
	IntFromUint,
	MirFunc,
	Return,
	ConstString,
	ArrayAlloc,
	DropValue,
	UintFromInt,
)
from lang.driftc.stage4 import MirToSSA
from lang.driftc.stage1 import BinaryOp
from lang.driftc.core.types_core import TypeTable


def _fn_info(name: str, can_throw: bool, return_ty: int) -> FnInfo:
	"""Build a minimal FnInfo with the provided return TypeId."""
	return FnInfo(
		fn_id=FunctionId(module="main", name=name, ordinal=0),
		name=name,
		declared_can_throw=can_throw,
		return_type_id=return_ty,
	)


def test_codegen_plain_int_return():
	"""
	Non-can-throw function returning a constant Int should lower to isize return.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="t0", value=42)],
		terminator=Return(value="t0"),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="main", ordinal=0), name="main", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	fn_info = _fn_info("main", False, int_ty)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	ir = lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=word_bits)

	assert f"define {word_ty} @main()" in ir
	assert f"ret {word_ty} %t0" in ir


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
	mir = MirFunc(fn_id=FunctionId(module="main", name="f", ordinal=0), name="f", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	error_ty = table.ensure_error()
	fnresult_ty = table.new_fnresult(int_ty, error_ty)
	fn_info = _fn_info("f", True, fnresult_ty)

	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits()))
	ir = mod.render()

	assert "%FnResult_Int_Error" in ir
	assert "define %FnResult_Int_Error @f()" in ir
	assert "ret %FnResult_Int_Error %res" in ir


def test_codegen_fnresult_ref_ok_return():
	"""FnResult<Ref<Int>, Error> should use a named FnResult type with opaque pointer payload."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstructResultOk(dest="res", value="p0"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="f_ref", ordinal=0), name="f_ref", params=["p0"], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int = table.ensure_ref(int_ty)
	error_ty = table.ensure_error()
	fnresult_ty = table.new_fnresult(ref_int, error_ty)
	sig = FnSignature(name="f_ref", param_type_ids=[ref_int], return_type_id=fnresult_ty, declared_can_throw=True)
	fn_info = FnInfo(fn_id=FunctionId(module="main", name="f_ref", ordinal=0), name="f_ref", declared_can_throw=True, return_type_id=fnresult_ty, signature=sig)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	mod = LlvmModuleBuilder(word_bits=word_bits)
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=word_bits))
	ir = mod.render()

	assert f"%FnResult_Ref_Int_Error = type {{ i1, ptr, ptr }}" in ir
	assert f"define %FnResult_Ref_Int_Error @f_ref(ptr %p0)" in ir


def test_codegen_fnresult_ref_bool_ok_return():
	"""FnResult<Ref<Bool>, Error> should use an opaque pointer payload (ptr)."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstructResultOk(dest="res", value="p0"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="f_ref_bool", ordinal=0), name="f_ref_bool", params=["p0"], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	bool_ty = table.ensure_bool()
	ref_bool = table.ensure_ref(bool_ty)
	error_ty = table.ensure_error()
	fnresult_ty = table.new_fnresult(ref_bool, error_ty)
	sig = FnSignature(name="f_ref_bool", param_type_ids=[ref_bool], return_type_id=fnresult_ty, declared_can_throw=True)
	fn_info = FnInfo(fn_id=FunctionId(module="main", name="f_ref_bool", ordinal=0), name="f_ref_bool", declared_can_throw=True, return_type_id=fnresult_ty, signature=sig)

	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits()))
	ir = mod.render()

	assert "%FnResult_Ref_Bool_Error = type { i1, ptr, ptr }" in ir
	assert "define %FnResult_Ref_Bool_Error @f_ref_bool(ptr %p0)" in ir


def test_export_wrapper_bool_return_uses_i8():
	"""Exported Bool return uses i8 in the ABI wrapper and zexts the impl result."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstBool(dest="b", value=True),
		],
		terminator=Return(value="b"),
	)
	fn_id = FunctionId(module="main", name="foo", ordinal=0)
	mir = MirFunc(fn_id=fn_id, name="foo", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	bool_ty = table.ensure_bool()
	sig = FnSignature(
		name="foo",
		param_type_ids=[],
		return_type_id=bool_ty,
		declared_can_throw=False,
		is_exported_entrypoint=True,
	)
	fn_info = FnInfo(
		fn_id=fn_id,
		name="foo",
		declared_can_throw=False,
		return_type_id=bool_ty,
		signature=sig,
	)

	mod = lower_module_to_llvm(
		funcs={fn_id: mir},
		ssa_funcs={fn_id: ssa},
		fn_infos={fn_id: fn_info},
		type_table=table,
		word_bits=host_word_bits(),
	)
	ir = mod.render()

	assert "define { i8, ptr } @foo()" in ir
	assert "define i1 @foo__impl()" in ir
	assert "zext i1 %ok to i8" in ir


def test_codegen_fnresult_ref_err_zero_ok_slot():
	"""
	FnResult.Err for Ref<Int> ok payload should zero-initialize the ptr ok slot cleanly.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstUint64(dest="code", value=1),
			ConstString(dest="ename", value="m:Evt"),
			ConstructDV(dest="dv", dv_type_name="Missing", args=[]),
			ConstString(dest="key", value="k"),
			ConstructError(dest="err", code="code", event_fqn="ename", payload="dv", attr_key="key"),
			ConstructResultErr(dest="res", error="err"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="f_ref_err", ordinal=0), name="f_ref_err", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int = table.ensure_ref(int_ty)
	error_ty = table.ensure_error()
	fnresult_ty = table.new_fnresult(ref_int, error_ty)
	fn_info = FnInfo(fn_id=FunctionId(module="main", name="f_ref_err", ordinal=0), name="f_ref_err", declared_can_throw=True, return_type_id=fnresult_ty)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	mod = LlvmModuleBuilder(word_bits=word_bits)
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=word_bits))
	ir = mod.render()

	assert f"%FnResult_Ref_Int_Error = type {{ i1, ptr, ptr }}" in ir
	assert "define %FnResult_Ref_Int_Error @f_ref_err()" in ir
	assert any(
		f"%FnResult_Ref_Int_Error" in line and f", ptr null, 1" in line for line in ir.splitlines()
	)


def test_codegen_nested_array_drop_helper_verifies():
	"""Nested array drop helpers should emit verifiable LLVM IR."""

	table = TypeTable()
	string_ty = table.ensure_string()
	inner_array_ty = table.new_array(string_ty)
	outer_array_ty = table.new_array(inner_array_ty)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="len0", value=0),
			ConstInt(dest="cap0", value=1),
			ArrayAlloc(dest="arr", elem_ty=inner_array_ty, length="len0", cap="cap0"),
			DropValue(value="arr", ty=outer_array_ty),
		],
		terminator=Return(value=None),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="drop_arrays", ordinal=0), name="drop_arrays", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	void_ty = table.ensure_void()
	sig = FnSignature(
		name="drop_arrays",
		param_type_ids=[],
		return_type_id=void_ty,
		declared_can_throw=False,
	)
	fn_info = FnInfo(
		fn_id=FunctionId(module="main", name="drop_arrays", ordinal=0),
		name="drop_arrays",
		declared_can_throw=False,
		return_type_id=void_ty,
		signature=sig,
	)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	ir = lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=word_bits)

	_verify_ir_with_llvm_as(ir)

	assert "define void @__drift_array_drop_" in ir
	assert "call void @__drift_array_drop_" in ir
	assert "call void @drift_free_array" in ir


def test_codegen_int_uint_conversions_verify():
	"""IntFromUint/UintFromInt should produce verifier-clean IR."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstUint(dest="u0", value=5),
			IntFromUint(dest="i0", value="u0"),
			UintFromInt(dest="u1", value="i0"),
		],
		terminator=Return(value="i0"),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="conv", ordinal=0), name="conv", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	fn_info = _fn_info("conv", False, int_ty)

	ir = lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits())
	_verify_ir_with_llvm_as(ir)


def test_codegen_signed_unsigned_ops_are_op_selected():
	"""Signed/unsigned behavior should be encoded by ops, not types."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="i0", value=1),
			ConstInt(dest="i1", value=2),
			BinaryOpInstr(dest="i_lt", op=BinaryOp.LT, left="i0", right="i1"),
			ConstUint(dest="u0", value=1),
			ConstUint(dest="u1", value=2),
			BinaryOpInstr(dest="u_lt", op=BinaryOp.LT, left="u0", right="u1"),
			BinaryOpInstr(dest="u_div", op=BinaryOp.DIV, left="u1", right="u0"),
			BinaryOpInstr(dest="u_mod", op=BinaryOp.MOD, left="u1", right="u0"),
		],
		terminator=Return(value="i0"),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="ops", ordinal=0), name="ops", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	fn_info = _fn_info("ops", False, int_ty)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	ir = lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=word_bits)

	_verify_ir_with_llvm_as(ir)

	assert f"icmp slt {word_ty}" in ir
	assert f"icmp ult {word_ty}" in ir
	assert f"udiv {word_ty}" in ir
	assert f"urem {word_ty}" in ir
