# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-10
"""
LLVM lowering for Array<String> literals and indexing.
"""

from lang2.driftc.checker import FnInfo, FnSignature
from lang2.driftc.core.types_core import TypeTable
from lang2.driftc.stage2 import ArrayIndexLoad, ArrayIndexStore, ArrayLit, BasicBlock, ConstInt, ConstString, MirFunc, Return
from lang2.driftc.stage4.ssa import MirToSSA
from lang2.codegen.llvm import lower_module_to_llvm


def _types():
    table = TypeTable()
    int_ty = table.new_scalar("Int")
    str_ty = table.new_scalar("String")
    table._int_type = int_ty  # type: ignore[attr-defined]
    table._string_type = str_ty  # type: ignore[attr-defined]
    return table, int_ty, str_ty


def test_array_string_literal_and_index_ir():
	table, int_ty, str_ty = _types()

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="a"),
			ConstString(dest="t1", value="bb"),
			ArrayLit(dest="t2", elem_ty=str_ty, elements=["t0", "t1"]),
			ConstInt(dest="idx", value=1),
			ArrayIndexLoad(dest="t3", elem_ty=str_ty, array="t2", index="idx"),
		],
		terminator=Return(value="t3"),
	)
	func = MirFunc(name="main", params=[], locals=["t0", "t1", "t2", "t3", "idx"], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="main", param_type_ids=[], return_type_id=str_ty)
	info = FnInfo(name="main", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	mod = lower_module_to_llvm({"main": func}, {"main": ssa}, {"main": info}, type_table=table)
	ir = mod.render()

	assert "declare ptr @drift_alloc_array" in ir
	assert "%drift.size = type i64" in ir
	assert "%DriftString = type { %drift.size, i8*" in ir
	assert "getelementptr inbounds %DriftString" in ir
	assert "call void @drift_bounds_check_fail" in ir


def test_array_string_store_ir():
	table, int_ty, str_ty = _types()

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="a"),
			ConstString(dest="t1", value="bb"),
			ArrayLit(dest="t2", elem_ty=str_ty, elements=["t0", "t1"]),
			ConstInt(dest="idx", value=0),
			ConstString(dest="t3", value="ccc"),
			ArrayIndexStore(elem_ty=str_ty, array="t2", index="idx", value="t3"),
		],
		terminator=Return(value="t3"),
	)
	func = MirFunc(name="main", params=[], locals=["t0", "t1", "t2", "t3", "idx"], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="main", param_type_ids=[], return_type_id=str_ty)
	info = FnInfo(name="main", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	mod = lower_module_to_llvm({"main": func}, {"main": ssa}, {"main": info}, type_table=table)
	ir = mod.render()

	assert "%drift.size = type i64" in ir
	assert "%DriftString = type { %drift.size, i8*" in ir
	assert "declare ptr @drift_alloc_array" in ir
	assert "call ptr @drift_alloc_array" in ir
	assert "getelementptr inbounds %DriftString" in ir
	assert "store %DriftString %t3" in ir or "store %DriftString" in ir
	assert "call void @drift_bounds_check_fail" in ir
