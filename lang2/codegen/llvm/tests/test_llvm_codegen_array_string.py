import re

from lang2.driftc.core.function_id import FunctionId
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-10
"""
LLVM lowering for Array<String> allocations and indexing.
"""

from lang2.driftc.checker import FnInfo, FnSignature
from lang2.driftc.core.types_core import TypeKind, TypeTable
from lang2.driftc.stage2 import (
	ArrayAlloc,
	ArrayLit,
	ArrayElemAssign,
	ArrayElemInitUnchecked,
	ArrayElemTake,
	ArrayIndexLoad,
	ArrayLen,
	ArraySetLen,
	BasicBlock,
	CopyValue,
	ConstInt,
	ConstUint,
	ConstString,
	ConstructVariant,
	MirFunc,
	Return,
)
from lang2.driftc.stage4.ssa import MirToSSA
from lang2.codegen.llvm import lower_module_to_llvm
from lang2.codegen.llvm.test_utils import host_word_bits


def _types():
    table = TypeTable()
    int_ty = table.new_scalar("Int")
    uint_ty = table.new_scalar("Uint")
    str_ty = table.new_scalar("String")
    table._int_type = int_ty  # type: ignore[attr-defined]
    table._uint_type = uint_ty  # type: ignore[attr-defined]
    table._string_type = str_ty  # type: ignore[attr-defined]
    def _copy_query(tid: int) -> bool | None:
        td = table.get(tid)
        if td.kind is TypeKind.SCALAR and td.name in {"Int", "Uint", "Bool", "Float", "String", "Void"}:
            return True
        if td.kind in {TypeKind.REF, TypeKind.VOID}:
            return bool(getattr(td, "ref_mut", False)) is False
        return None
    table.set_copy_query(_copy_query, allow_fallback=True)
    return table, int_ty, uint_ty, str_ty


def test_array_string_literal_and_index_ir():
	table, int_ty, uint_ty, str_ty = _types()

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="a"),
			ConstString(dest="t1", value="bb"),
			ConstInt(dest="i0", value=0),
			ConstInt(dest="i1", value=1),
			ConstInt(dest="tlen0", value=0),
			ConstInt(dest="tlen", value=2),
			ConstInt(dest="tcap", value=2),
			ArrayAlloc(dest="t2", elem_ty=str_ty, length="tlen0", cap="tcap"),
			ArrayElemInitUnchecked(elem_ty=str_ty, array="t2", index="i0", value="t0"),
			ArrayElemInitUnchecked(elem_ty=str_ty, array="t2", index="i1", value="t1"),
			ArraySetLen(dest="t2_len", array="t2", length="tlen"),
			ConstInt(dest="idx", value=1),
			ArrayIndexLoad(dest="t3", elem_ty=str_ty, array="t2_len", index="idx"),
			CopyValue(dest="t3_copy", ty=str_ty, value="t3"),
		],
		terminator=Return(value="t3_copy"),
	)
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	func = MirFunc(
		fn_id=fn_id,
		name="main",
		params=[],
		locals=["t0", "t1", "t2", "t2_len", "t3", "t3_copy", "idx", "i0", "i1", "tlen0", "tlen", "tcap"],
		blocks={"entry": block},
		entry="entry",
	)
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="main", param_type_ids=[], return_type_id=str_ty)
	info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	mod = lower_module_to_llvm({fn_id: func}, {fn_id: ssa}, {fn_id: info}, type_table=table, word_bits=word_bits)
	ir = mod.render()

	assert "declare i8* @drift_alloc_array" in ir
	assert f"%DriftString = type {{ {word_ty}, i8*" in ir
	assert "getelementptr %DriftString" in ir
	assert "call void @drift_bounds_check" in ir
	assert "call %DriftString @drift_string_retain" in ir


def test_array_string_lit_retains_elements():
	table, int_ty, uint_ty, str_ty = _types()
	arr_ty = table.new_array(str_ty)

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="a"),
			ConstString(dest="t1", value="bb"),
			ArrayLit(dest="arr", elem_ty=str_ty, elements=["t0", "t1"]),
			ArrayLen(dest="len", array="arr"),
		],
		terminator=Return(value="len"),
	)
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	func = MirFunc(
		fn_id=fn_id,
		name="main",
		params=[],
		locals=["t0", "t1", "arr", "len"],
		blocks={"entry": block},
		entry="entry",
	)
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="main", param_type_ids=[], return_type_id=int_ty)
	info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, signature=sig, return_type_id=int_ty)

	mod = lower_module_to_llvm({fn_id: func}, {fn_id: ssa}, {fn_id: info}, type_table=table, word_bits=host_word_bits())
	ir = mod.render()

	assert "call %DriftString @drift_string_retain" in ir


def test_array_string_store_ir():
	table, int_ty, uint_ty, str_ty = _types()

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="a"),
			ConstString(dest="t1", value="bb"),
			ConstInt(dest="i0", value=0),
			ConstInt(dest="i1", value=1),
			ConstInt(dest="tlen0", value=0),
			ConstInt(dest="tlen", value=2),
			ConstInt(dest="tcap", value=2),
			ArrayAlloc(dest="t2", elem_ty=str_ty, length="tlen0", cap="tcap"),
			ArrayElemInitUnchecked(elem_ty=str_ty, array="t2", index="i0", value="t0"),
			ArrayElemInitUnchecked(elem_ty=str_ty, array="t2", index="i1", value="t1"),
			ArraySetLen(dest="t2_len", array="t2", length="tlen"),
			ConstInt(dest="idx", value=0),
			ConstString(dest="t3", value="ccc"),
			CopyValue(dest="t3_copy", ty=str_ty, value="t3"),
			ArrayElemAssign(elem_ty=str_ty, array="t2_len", index="idx", value="t3_copy"),
		],
		terminator=Return(value="t3"),
	)
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	func = MirFunc(
		fn_id=fn_id,
		name="main",
		params=[],
		locals=["t0", "t1", "t2", "t2_len", "t3", "t3_copy", "idx", "i0", "i1", "tlen0", "tlen", "tcap"],
		blocks={"entry": block},
		entry="entry",
	)
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="main", param_type_ids=[], return_type_id=str_ty)
	info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	mod = lower_module_to_llvm({fn_id: func}, {fn_id: ssa}, {fn_id: info}, type_table=table, word_bits=word_bits)
	ir = mod.render()

	assert f"%DriftString = type {{ {word_ty}, i8*" in ir
	assert "declare i8* @drift_alloc_array" in ir
	assert "call i8* @drift_alloc_array" in ir
	assert "getelementptr %DriftString" in ir
	assert "call %DriftString @drift_string_retain" in ir
	assert "call void @drift_string_release" in ir
	assert "store %DriftString %t3_copy" in ir or "store %DriftString" in ir
	assert "call void @drift_bounds_check" in ir


def test_array_string_take_zeros_slot():
	table, int_ty, uint_ty, str_ty = _types()

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="a"),
			ConstInt(dest="i0", value=0),
			ConstInt(dest="tlen0", value=0),
			ConstInt(dest="tlen", value=1),
			ConstInt(dest="tcap", value=1),
			ArrayAlloc(dest="t1", elem_ty=str_ty, length="tlen0", cap="tcap"),
			ArrayElemInitUnchecked(elem_ty=str_ty, array="t1", index="i0", value="t0"),
			ArraySetLen(dest="t1_len", array="t1", length="tlen"),
			ArrayElemTake(dest="t2", elem_ty=str_ty, array="t1_len", index="i0"),
		],
		terminator=Return(value="t2"),
	)
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	func = MirFunc(
		fn_id=fn_id,
		name="main",
		params=[],
		locals=["t0", "t1", "t1_len", "t2", "i0", "tlen0", "tlen", "tcap"],
		blocks={"entry": block},
		entry="entry",
	)
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="main", param_type_ids=[], return_type_id=str_ty)
	info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	mod = lower_module_to_llvm({fn_id: func}, {fn_id: ssa}, {fn_id: info}, type_table=table, word_bits=word_bits)
	ir = mod.render()

	assert f"%DriftString = type {{ {word_ty}, i8*" in ir
	match = re.search(
		r"call void @drift_bounds_check[^\n]*\n\s*(%[\w\.]+) = getelementptr[^\n]*\n(?s:.*?store %DriftString [^,]+, %DriftString\* \1)",
		ir,
	)
	assert match is not None


def test_array_optional_string_take_uses_tombstone_ctor():
	table, int_ty, uint_ty, str_ty = _types()
	base = table.ensure_optional_base()
	opt_str = table.ensure_variant_instantiated(base, [str_ty])

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="a"),
			ConstInt(dest="i0", value=0),
			ConstInt(dest="tlen0", value=0),
			ConstInt(dest="tlen", value=1),
			ConstInt(dest="tcap", value=1),
			ConstructVariant(dest="v0", variant_ty=opt_str, ctor="Some", args=["t0"]),
			ArrayAlloc(dest="arr", elem_ty=opt_str, length="tlen0", cap="tcap"),
			ArrayElemInitUnchecked(elem_ty=opt_str, array="arr", index="i0", value="v0"),
			ArraySetLen(dest="arr_len", array="arr", length="tlen"),
			ArrayElemTake(dest="taken", elem_ty=opt_str, array="arr_len", index="i0"),
		],
		terminator=Return(value="tlen"),
	)
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	func = MirFunc(
		fn_id=fn_id,
		name="main",
		params=[],
		locals=["t0", "i0", "tlen0", "tlen", "tcap", "v0", "arr", "arr_len", "taken"],
		blocks={"entry": block},
		entry="entry",
	)
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="main", param_type_ids=[], return_type_id=int_ty)
	info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, signature=sig, return_type_id=int_ty)

	mod = lower_module_to_llvm({fn_id: func}, {fn_id: ssa}, {fn_id: info}, type_table=table, word_bits=host_word_bits())
	ir = mod.render()

	assert "store i8 2" in ir
	match = re.search(
		r"call void @drift_bounds_check[^\n]*\n\s*(%[\w\.]+) = getelementptr[^\n]*\n(?s:.*?store [^\n]*, [^\n]*\* \1)",
		ir,
	)
	assert match is not None
