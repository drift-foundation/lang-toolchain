from lang.driftc.core.function_id import FunctionId
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
IR lowering for string ordering operators via drift_string_cmp runtime helper.
"""

from lang.codegen.llvm import lower_module_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1 import BinaryOp
from lang.driftc.stage2 import BasicBlock, BinaryOpInstr, ConstInt, ConstString, IfTerminator, MirFunc, Return, StringCmp
from lang.driftc.stage4.ssa import MirToSSA


def _types():
	table = TypeTable()
	int_ty = table.new_scalar("Int")
	str_ty = table.new_scalar("String")
	table._int_type = int_ty  # type: ignore[attr-defined]
	table._string_type = str_ty  # type: ignore[attr-defined]
	return table, int_ty, str_ty


def test_string_cmp_emits_runtime_call_and_decl() -> None:
	table, int_ty, str_ty = _types()

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="s0", value="a"),
			ConstString(dest="s1", value="b"),
			StringCmp(dest="cmp", left="s0", right="s1"),
		],
		terminator=Return(value="cmp"),
	)
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	func = MirFunc(fn_id=fn_id, name="main", params=[], locals=["s0", "s1", "cmp"], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="main", param_type_ids=[], return_type_id=int_ty)
	info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, signature=sig, return_type_id=int_ty)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	mod = lower_module_to_llvm({fn_id: func}, {fn_id: ssa}, {fn_id: info}, type_table=table, word_bits=word_bits)
	ir = mod.render()

	assert "declare i32 @drift_string_cmp(%DriftString, %DriftString)" in ir
	assert "call i32 @drift_string_cmp(%DriftString %s0, %DriftString %s1)" in ir


def test_string_lt_lowered_via_string_cmp_and_zero_compare() -> None:
	table, int_ty, str_ty = _types()

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="s0", value="a"),
			ConstString(dest="s1", value="b"),
			StringCmp(dest="cmp", left="s0", right="s1"),
			ConstInt(dest="z", value=0),
			BinaryOpInstr(dest="lt", op=BinaryOp.LT, left="cmp", right="z"),
		],
		terminator=IfTerminator(cond="lt", then_target="then", else_target="else"),
	)
	then_block = BasicBlock(
		name="then",
		instructions=[ConstInt(dest="t0", value=0)],
		terminator=Return(value="t0"),
	)
	else_block = BasicBlock(
		name="else",
		instructions=[ConstInt(dest="t1", value=1)],
		terminator=Return(value="t1"),
	)
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	func = MirFunc(
		fn_id=fn_id,
		name="main",
		params=[],
		locals=["s0", "s1", "cmp", "z", "lt", "t0", "t1"],
		blocks={"entry": entry, "then": then_block, "else": else_block},
		entry="entry",
	)
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="main", param_type_ids=[], return_type_id=int_ty)
	info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, signature=sig, return_type_id=int_ty)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	mod = lower_module_to_llvm({fn_id: func}, {fn_id: ssa}, {fn_id: info}, type_table=table, word_bits=word_bits)
	ir = mod.render()

	assert "call i32 @drift_string_cmp(%DriftString %s0, %DriftString %s1)" in ir
	if word_bits == 32:
		assert f"add {word_ty}" in ir
	else:
		assert "sext i32" in ir
	assert f"icmp slt {word_ty}" in ir
