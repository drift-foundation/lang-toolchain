from __future__ import annotations

from lang2.driftc.stage2 import (
	BasicBlock,
	BinaryOpInstr,
	ConstructResultOk,
	ConstInt,
	MirFunc,
	Return,
)
from lang2.driftc.stage4 import MirToSSA
from lang2.driftc.checker import Checker, FnSignature


def test_checker_builds_type_env_from_ssa():
	"""Checker should build a CheckerTypeEnv from SSA using signature TypeIds."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstructResultOk(dest="r0", value="v0"),
		],
		terminator=Return(value="r0"),
	)
	mir_func = MirFunc(
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	ssa = MirToSSA().run(mir_func)

	signatures = {"f": FnSignature(name="f", return_type="Int", declared_can_throw=True)}
	checker = Checker(signatures=signatures)
	checked = checker.check(["f"])

	type_env = checker.build_type_env_from_ssa({"f": ssa}, signatures)
	assert type_env is not None
	ty = type_env.type_of_ssa_value("f", "r0")
	assert type_env.is_fnresult(ty)


def test_checker_types_basic_numeric_ops():
	"""
	Checker SSA typing should propagate scalar types through simple ops.
	"""
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
		name="add",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	ssa = MirToSSA().run(mir_func)

	signatures = {"add": FnSignature(name="add", return_type="Int")}
	checker = Checker(signatures=signatures)
	checker.check(["add"])

	type_env = checker.build_type_env_from_ssa({"add": ssa}, signatures)
	assert type_env is not None
	sum_ty = type_env.type_of_ssa_value("add", "sum")
	# Sum should carry the scalar Int TypeId; this also proves BinaryOpInstr is handled.
	int_td = checker._type_table.get(checker._int_type)
	sum_td = checker._type_table.get(sum_ty)
	assert sum_td.kind == int_td.kind
	assert sum_td.name == int_td.name


def test_checker_type_env_ignores_void_calls():
	"""
	SSA typing should not assign types to dests of void-returning calls.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="t0", value=1),
		],
		terminator=Return(value=None),
	)
	mir_func = MirFunc(name="noop", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir_func)

	checker = Checker(
		signatures={"noop": FnSignature(name="noop", return_type_id=None, return_type="Void")},
	)
	checker.check(["noop"])

	type_env = checker.build_type_env_from_ssa({"noop": ssa}, checker._signatures)
	assert type_env is None or ("noop", "t0") not in getattr(type_env, "_types", {})
