# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-10
"""
IR lowering for string ops: len, eq, concat.
"""

from lang2.checker import FnInfo, FnSignature
from lang2.core.types_core import TypeTable
from lang2.stage2 import ArrayLen, BasicBlock, BinaryOpInstr, Call, ConstString, MirFunc, Return
from lang2.stage1 import BinaryOp
from lang2.stage4.ssa import MirToSSA
from lang2.codegen.llvm import lower_module_to_llvm


def _types():
	 table = TypeTable()
	 int_ty = table.new_scalar("Int")
	 str_ty = table.new_scalar("String")
	 table._int_type = int_ty  # type: ignore[attr-defined]
	 table._string_type = str_ty  # type: ignore[attr-defined]
	 return table, int_ty, str_ty


def test_string_len_ir():
	 table, int_ty, str_ty = _types()

	 block = BasicBlock(
	 	 name="entry",
	 	 instructions=[ConstString(dest="t0", value="abc"), ArrayLen(dest="t1", array="t0")],
	 	 terminator=Return(value="t1"),
	 )
	 func = MirFunc(name="main", params=[], locals=["t0", "t1"], blocks={"entry": block}, entry="entry")
	 ssa = MirToSSA().run(func)
	 sig = FnSignature(name="main", param_type_ids=[], return_type_id=int_ty)
	 info = FnInfo(name="main", declared_can_throw=False, signature=sig, return_type_id=int_ty)

	 mod = lower_module_to_llvm({"main": func}, {"main": ssa}, {"main": info}, type_table=table)
	 ir = mod.render()

	 assert "extractvalue %DriftString %t0, 0" in ir
	 assert "define i64 @main()" in ir


def test_string_eq_and_concat_ir():
	 table, int_ty, str_ty = _types()

	 block = BasicBlock(
	 	 name="entry",
	 	 instructions=[
	 	 	 ConstString(dest="t0", value="a"),
	 	 	 ConstString(dest="t1", value="b"),
	 	 	 BinaryOpInstr(dest="t2", op=BinaryOp.EQ, left="t0", right="t1"),
	 	 	 BinaryOpInstr(dest="t3", op=BinaryOp.ADD, left="t0", right="t1"),
	 	 ],
	 	 terminator=Return(value="t3"),
	 )
	 func = MirFunc(name="main", params=[], locals=["t0", "t1", "t2", "t3"], blocks={"entry": block}, entry="entry")
	 ssa = MirToSSA().run(func)
	 sig = FnSignature(name="main", param_type_ids=[], return_type_id=str_ty)
	 info = FnInfo(name="main", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	 mod = lower_module_to_llvm({"main": func}, {"main": ssa}, {"main": info}, type_table=table)
	 ir = mod.render()

	 assert "declare i1 @drift_string_eq(%DriftString, %DriftString)" in ir
	 assert "declare %DriftString @drift_string_concat(%DriftString, %DriftString)" in ir
	 assert "call i1 @drift_string_eq(%DriftString %t0, %DriftString %t1)" in ir
	 assert "call %DriftString @drift_string_concat(%DriftString %t0, %DriftString %t1)" in ir
	 assert "ret %DriftString" in ir
