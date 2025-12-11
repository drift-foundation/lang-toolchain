# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-10
"""
LLVM lowering for String literals and returns.
"""

from lang2.driftc.checker import FnInfo, FnSignature
from lang2.driftc.core.types_core import TypeTable
from lang2.driftc.stage2 import BasicBlock, Call, ConstString, MirFunc, Return
from lang2.driftc.stage4.ssa import MirToSSA
from lang2.codegen.llvm import lower_ssa_func_to_llvm, lower_module_to_llvm


def _string_type(table: TypeTable):
	str_ty = table.new_scalar("String")
	table._string_type = str_ty  # type: ignore[attr-defined]
	return str_ty


def test_string_literal_return_ir():
	table = TypeTable()
	str_ty = _string_type(table)

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="abc"),
		],
		terminator=Return(value="t0"),
	)
	func = MirFunc(name="f", params=[], locals=[], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="f", return_type_id=str_ty, param_type_ids=[])
	fn_info = FnInfo(name="f", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	ir = lower_ssa_func_to_llvm(func, ssa, fn_info, {"f": fn_info}, type_table=table)

	assert "%drift.size = type i64" in ir
	assert "%DriftString = type { %drift.size, i8*" in ir
	assert '@.str0 = private unnamed_addr constant [4 x i8] c"abc\\00"' in ir
	assert "define %DriftString @f()" in ir
	assert "ret %DriftString" in ir


def test_string_utf8_literal_ir():
	"""UTF-8 literals should be escaped byte-wise in the LLVM global."""
	table = TypeTable()
	str_ty = _string_type(table)

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="Solidarność"),
		],
		terminator=Return(value="t0"),
	)
	func = MirFunc(name="f", params=[], locals=[], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="f", return_type_id=str_ty, param_type_ids=[])
	fn_info = FnInfo(name="f", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	ir = lower_ssa_func_to_llvm(func, ssa, fn_info, {"f": fn_info}, type_table=table)

	assert "%drift.size = type i64" in ir
	assert "%DriftString = type { %drift.size, i8*" in ir
	assert '@.str0 = private unnamed_addr constant [14 x i8] c"Solidarno\\C5\\9B\\C4\\87\\00"' in ir
	assert "define %DriftString @f()" in ir
	assert "ret %DriftString" in ir


def test_string_pass_through_call_ir():
	table = TypeTable()
	str_ty = _string_type(table)

	# callee: return its string argument
	callee_block = BasicBlock(
		name="entry",
		instructions=[
		],
		terminator=Return(value="s"),
	)
	callee = MirFunc(name="id", params=["s"], locals=[], blocks={"entry": callee_block}, entry="entry")
	callee_ssa = MirToSSA().run(callee)
	callee_sig = FnSignature(name="id", param_type_ids=[str_ty], return_type_id=str_ty)
	callee_info = FnInfo(name="id", declared_can_throw=False, signature=callee_sig, return_type_id=str_ty)

	# caller: pass literal through id
	caller_block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="abc"),
			Call(dest="t1", fn="id", args=["t0"]),
		],
		terminator=Return(value="t1"),
	)
	caller = MirFunc(name="main", params=[], locals=["t0", "t1"], blocks={"entry": caller_block}, entry="entry")
	caller_ssa = MirToSSA().run(caller)
	caller_sig = FnSignature(name="main", param_type_ids=[], return_type_id=str_ty)
	caller_info = FnInfo(name="main", declared_can_throw=False, signature=caller_sig, return_type_id=str_ty)

	mod = lower_module_to_llvm(
		{"id": callee, "main": caller},
		{"id": callee_ssa, "main": caller_ssa},
		{"id": callee_info, "main": caller_info},
		type_table=table,
	)
	ir = mod.render()

	assert "%drift.size = type i64" in ir
	assert "%DriftString = type { %drift.size, i8*" in ir
	assert '@.str0 = private unnamed_addr constant [4 x i8] c"abc\\00"' in ir
	assert "define %DriftString @id(%DriftString %s)" in ir
	assert "define %DriftString @main()" in ir
	assert "call %DriftString @id(%DriftString %t0)" in ir
	assert "ret %DriftString" in ir
