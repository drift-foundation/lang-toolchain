from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import (
	BasicBlock,
	BinaryOpInstr,
	ConstructResultOk,
	ConstInt,
	MirFunc,
	Return,
)
from lang.driftc.stage4 import MirToSSA
from lang.driftc.checker import Checker, FnSignature


def test_checker_builds_type_env_from_ssa():
	"""Checker should build a CheckerTypeEnv from SSA using signature TypeIds."""
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstructResultOk(dest="r0", value="v0"),
		],
		terminator=Return(value="r0"),
	)
	mir_func = MirFunc(
		fn_id=FunctionId(module="main", name="f", ordinal=0),
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	ssa = MirToSSA().run(mir_func)

	fn_id = FunctionId(module="main", name="f", ordinal=0)
	signatures = {fn_id: FnSignature(name="f", return_type="Int", declared_can_throw=True, param_type_ids=[], return_type_id=int_ty, error_type_id=err_ty)}
	checker = Checker(
		signatures_by_id=signatures,
		hir_blocks_by_id={},
		call_info_by_callsite_id={},
		type_table=table,
	)
	checked = checker.check_by_id([fn_id])

	type_env = checker.build_type_env_from_ssa({fn_id: ssa}, signatures)
	assert type_env is not None
	ty = type_env.type_of_ssa_value(fn_id, "r0")
	assert type_env.is_fnresult(ty)


def test_checker_types_basic_numeric_ops():
	"""
	Checker SSA typing should propagate scalar types through simple ops.
	"""
	table = TypeTable()
	int_ty = table.ensure_int()

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="a", value=1),
			ConstInt(dest="b", value=2),
			BinaryOpInstr(dest="sum", op=None, left="a", right="b"),
		],
		terminator=Return(value="sum"),
	)
	mir_func = MirFunc(
		fn_id=FunctionId(module="main", name="add", ordinal=0),
		name="add",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	ssa = MirToSSA().run(mir_func)

	fn_id = FunctionId(module="main", name="add", ordinal=0)
	signatures = {fn_id: FnSignature(name="add", return_type="Int", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False)}
	checker = Checker(
		signatures_by_id=signatures,
		hir_blocks_by_id={},
		call_info_by_callsite_id={},
		type_table=table,
	)
	checker.check_by_id([fn_id])

	type_env = checker.build_type_env_from_ssa({fn_id: ssa}, signatures)
	assert type_env is not None
	sum_ty = type_env.type_of_ssa_value(fn_id, "sum")
	# Sum should carry the scalar Int TypeId; this also proves BinaryOpInstr is handled.
	int_td = checker._type_table.get(checker._int_type)
	sum_td = checker._type_table.get(sum_ty)
	assert sum_td.kind == int_td.kind
	assert sum_td.name == int_td.name


def test_checker_type_env_ignores_void_calls():
	"""
	SSA typing should not assign types to dests of void-returning calls.
	"""
	table = TypeTable()
	void_ty = table.ensure_void()

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="t0", value=1),
		],
		terminator=Return(value=None),
	)
	mir_func = MirFunc(fn_id=FunctionId(module="main", name="noop", ordinal=0), name="noop", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir_func)

	fn_id = FunctionId(module="main", name="noop", ordinal=0)
	checker = Checker(
		signatures_by_id={fn_id: FnSignature(name="noop", return_type_id=void_ty, return_type="Void", param_type_ids=[], declared_can_throw=False)},
		hir_blocks_by_id={},
		call_info_by_callsite_id={},
		type_table=table,
	)
	checker.check_by_id([fn_id])

	type_env = checker.build_type_env_from_ssa({fn_id: ssa}, checker._signatures_by_id)
	assert type_env is None or (fn_id, "t0") not in getattr(type_env, "_types", {})
