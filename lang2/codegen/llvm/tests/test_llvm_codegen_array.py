# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-07
"""
LLVM lowering for array MIR ops: ArrayLit + ArrayIndexLoad/Store.
"""

from lang2.checker import FnInfo, FnSignature
from lang2.driftc.core.types_core import TypeTable
from lang2.driftc.stage2 import (
	ArrayIndexLoad,
	ArrayIndexStore,
	ArrayLit,
	ArrayLen,
	BasicBlock,
	ConstInt,
	MirFunc,
	Return,
)
from lang2.driftc.stage4.ssa import MirToSSA
from lang2.codegen.llvm import lower_ssa_func_to_llvm


def _int_types():
	table = TypeTable()
	int_ty = table.new_scalar("Int")
	table._int_type = int_ty  # type: ignore[attr-defined]
	return table, int_ty


def test_array_literal_and_index_ir_contains_alloc_and_load():
	table, int_ty = _int_types()
	# MIR: t1=1; t2=2; t3=[t1,t2]; t4=t3[1]; return t4
	block = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="t1", value=1),
			ConstInt(dest="t2", value=2),
			ArrayLit(dest="t3", elem_ty=int_ty, elements=["t1", "t2"]),
			ArrayIndexLoad(dest="t4", elem_ty=int_ty, array="t3", index="t1"),
			ArrayLen(dest="t5", array="t3"),
		],
		terminator=Return(value="t4"),
	)
	func = MirFunc(name="f", params=[], locals=[], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="f", return_type_id=int_ty, param_type_ids=[])
	fn_info = FnInfo(name="f", declared_can_throw=False, signature=sig, return_type_id=int_ty)
	ir = lower_ssa_func_to_llvm(func, ssa, fn_info, {"f": fn_info})
	assert "declare ptr @drift_alloc_array" in ir
	assert "call ptr @drift_alloc_array" in ir
	assert "drift_bounds_check_fail" in ir
	assert "br i1" in ir
	assert "unreachable" in ir
	assert "sext" not in ir
	assert "getelementptr inbounds i64" in ir
	assert "extractvalue { %drift.size, %drift.size, i64* } %t3, 0" in ir


def test_array_index_store_ir_contains_store():
	table, int_ty = _int_types()
	block = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="t1", value=1),
			ConstInt(dest="t2", value=2),
			ArrayLit(dest="t3", elem_ty=int_ty, elements=["t1", "t2"]),
			ArrayIndexStore(elem_ty=int_ty, array="t3", index="t1", value="t2"),
		],
		terminator=Return(value="t1"),
	)
	func = MirFunc(name="f", params=[], locals=[], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="f", return_type_id=int_ty, param_type_ids=[])
	fn_info = FnInfo(name="f", declared_can_throw=False, signature=sig, return_type_id=int_ty)
	ir = lower_ssa_func_to_llvm(func, ssa, fn_info, {"f": fn_info})
	assert "drift_bounds_check_fail" in ir
	assert "store i64 %t2" in ir
	assert "sext" not in ir
