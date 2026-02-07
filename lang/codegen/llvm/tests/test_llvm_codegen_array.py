from lang.driftc.core.function_id import FunctionId
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-07
"""
LLVM lowering for array MIR ops: ArrayAlloc/ElemInit + ArrayIndexLoad/ElemAssign.
"""

from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.types_core import TypeTable
import re

from lang.driftc.stage2 import (
	ArrayIndexLoad,
	ArrayElemAssign,
	ArrayElemInitUnchecked,
	ArrayAlloc,
	ArrayElemTake,
	ArrayLen,
	ArraySetLen,
	BasicBlock,
	ConstInt,
	ConstUint,
	ConstString,
	MirFunc,
	Return,
)
from lang.driftc.stage4.ssa import MirToSSA
from lang.codegen.llvm import lower_ssa_func_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits


def _int_types():
	table = TypeTable()
	int_ty = table.new_scalar("Int")
	table._int_type = int_ty  # type: ignore[attr-defined]
	return table, int_ty


def _int_string_types():
	table = TypeTable()
	int_ty = table.new_scalar("Int")
	uint_ty = table.new_scalar("Uint")
	str_ty = table.new_scalar("String")
	table._int_type = int_ty  # type: ignore[attr-defined]
	table._uint_type = uint_ty  # type: ignore[attr-defined]
	table._string_type = str_ty  # type: ignore[attr-defined]
	return table, int_ty, str_ty


def test_array_literal_and_index_ir_contains_alloc_and_load():
	table, int_ty = _int_types()
	# MIR: t1=1; t2=2; t3=[t1,t2]; t4=t3[1]; return t4
	block = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="t1", value=1),
			ConstInt(dest="t2", value=2),
			ConstInt(dest="i0", value=0),
			ConstInt(dest="i1", value=1),
			ConstInt(dest="tlen0", value=0),
			ConstInt(dest="tlen", value=2),
			ConstInt(dest="tcap", value=2),
			ArrayAlloc(dest="t3", elem_ty=int_ty, length="tlen0", cap="tcap"),
			ArrayElemInitUnchecked(elem_ty=int_ty, array="t3", index="i0", value="t1"),
			ArrayElemInitUnchecked(elem_ty=int_ty, array="t3", index="i1", value="t2"),
			ArraySetLen(dest="t3_len", array="t3", length="tlen"),
			ArrayIndexLoad(dest="t4", elem_ty=int_ty, array="t3_len", index="t1"),
			ArrayLen(dest="t5", array="t3_len"),
		],
		terminator=Return(value="t4"),
	)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = MirFunc(fn_id=fn_id, name="f", params=[], locals=[], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="f", return_type_id=int_ty, param_type_ids=[])
	fn_info = FnInfo(fn_id=fn_id, name="f", declared_can_throw=False, signature=sig, return_type_id=int_ty)
	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	ir = lower_ssa_func_to_llvm(func, ssa, fn_info, {fn_id: fn_info}, type_table=table, word_bits=word_bits)
	assert "declare i8* @drift_alloc_array" in ir
	assert "call i8* @drift_alloc_array" in ir
	assert "drift_bounds_check" in ir
	assert f"getelementptr {word_ty}" in ir
	assert ("extractvalue %DriftArrayHeader" in ir) or (f"extractvalue {{ {word_ty}, {word_ty}, {word_ty}, i8* }}" in ir)


def test_array_index_store_ir_contains_store():
	table, int_ty = _int_types()
	block = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="t1", value=1),
			ConstInt(dest="t2", value=2),
			ConstInt(dest="i0", value=0),
			ConstInt(dest="i1", value=1),
			ConstInt(dest="tlen0", value=0),
			ConstInt(dest="tlen", value=2),
			ConstInt(dest="tcap", value=2),
			ArrayAlloc(dest="t3", elem_ty=int_ty, length="tlen0", cap="tcap"),
			ArrayElemInitUnchecked(elem_ty=int_ty, array="t3", index="i0", value="t1"),
			ArrayElemInitUnchecked(elem_ty=int_ty, array="t3", index="i1", value="t2"),
			ArraySetLen(dest="t3_len", array="t3", length="tlen"),
			ArrayElemAssign(elem_ty=int_ty, array="t3_len", index="t1", value="t2"),
		],
		terminator=Return(value="t1"),
	)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = MirFunc(fn_id=fn_id, name="f", params=[], locals=[], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="f", return_type_id=int_ty, param_type_ids=[])
	fn_info = FnInfo(fn_id=fn_id, name="f", declared_can_throw=False, signature=sig, return_type_id=int_ty)
	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	ir = lower_ssa_func_to_llvm(func, ssa, fn_info, {fn_id: fn_info}, type_table=table, word_bits=word_bits)
	assert "drift_bounds_check" in ir
	assert f"store {word_ty} %t2" in ir


def test_array_take_tombstones_nested_array():
	table, int_ty, str_ty = _int_string_types()
	inner_ty = table.new_array(str_ty)
	outer_ty = table.new_array(inner_ty)

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="s0", value="x"),
			ConstInt(dest="i0", value=0),
			ConstInt(dest="tlen0", value=0),
			ConstInt(dest="tlen", value=1),
			ConstInt(dest="tcap0", value=0),
			ConstInt(dest="tcap1", value=1),
			ArrayAlloc(dest="inner", elem_ty=str_ty, length="tlen0", cap="tcap0"),
			ArrayElemInitUnchecked(elem_ty=str_ty, array="inner", index="i0", value="s0"),
			ArraySetLen(dest="inner_len", array="inner", length="tlen"),
			ArrayAlloc(dest="outer", elem_ty=inner_ty, length="tlen0", cap="tcap1"),
			ArrayElemInitUnchecked(elem_ty=inner_ty, array="outer", index="i0", value="inner_len"),
			ArraySetLen(dest="outer_len", array="outer", length="tlen"),
			ArrayElemTake(dest="taken", elem_ty=inner_ty, array="outer_len", index="i0"),
		],
		terminator=Return(value="tlen"),
	)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = MirFunc(fn_id=fn_id, name="f", params=[], locals=[], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="f", return_type_id=int_ty, param_type_ids=[])
	fn_info = FnInfo(fn_id=fn_id, name="f", declared_can_throw=False, signature=sig, return_type_id=int_ty)
	ir = lower_ssa_func_to_llvm(func, ssa, fn_info, {fn_id: fn_info}, type_table=table, word_bits=host_word_bits())

	match = re.search(
		r"call void @drift_bounds_check[^\n]*\n\s*(%[\w\.]+) = getelementptr[^\n]*\n(?s:.*?store [^\n]*, [^\n]*\* \1)",
		ir,
	)
	assert match is not None
