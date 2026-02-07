from lang.driftc.core.function_id import FunctionId
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-10
"""
IR lowering for string ops: len, eq, concat.
"""

from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import ArrayLen, BasicBlock, BinaryOpInstr, Call, ConstString, MirFunc, Return
from lang.driftc.stage1 import BinaryOp
from lang.driftc.stage4.ssa import MirToSSA
from lang.codegen.llvm import lower_module_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits


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
	 fn_id = FunctionId(module="main", name="main", ordinal=0)
	 func = MirFunc(fn_id=fn_id, name="main", params=[], locals=["t0", "t1"], blocks={"entry": block}, entry="entry")
	 ssa = MirToSSA().run(func)
	 sig = FnSignature(name="main", param_type_ids=[], return_type_id=int_ty)
	 info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, signature=sig, return_type_id=int_ty)

	 word_bits = host_word_bits()
	 word_ty = f"i{word_bits}"
	 mod = lower_module_to_llvm({fn_id: func}, {fn_id: ssa}, {fn_id: info}, type_table=table, word_bits=word_bits)
	 ir = mod.render()

	 assert "extractvalue %DriftString %t0, 0" in ir
	 assert f"define {word_ty} @main()" in ir


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
	 fn_id = FunctionId(module="main", name="main", ordinal=0)
	 func = MirFunc(fn_id=fn_id, name="main", params=[], locals=["t0", "t1", "t2", "t3"], blocks={"entry": block}, entry="entry")
	 ssa = MirToSSA().run(func)
	 sig = FnSignature(name="main", param_type_ids=[], return_type_id=str_ty)
	 info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	 mod = lower_module_to_llvm({fn_id: func}, {fn_id: ssa}, {fn_id: info}, type_table=table, word_bits=host_word_bits())
	 ir = mod.render()

	 assert "declare i1 @drift_string_eq(%DriftString, %DriftString)" in ir
	 assert "declare %DriftString @drift_string_concat(%DriftString, %DriftString)" in ir
	 assert "call i1 @drift_string_eq(%DriftString %t0, %DriftString %t1)" in ir
	 assert "call %DriftString @drift_string_concat(%DriftString %t0, %DriftString %t1)" in ir
	 assert "ret %DriftString" in ir
