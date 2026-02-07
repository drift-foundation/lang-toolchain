from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
import pytest

from lang.driftc.stage2 import (
	BasicBlock,
	ConstructResultOk,
	ConstructResultErr,
	Call,
	ConstInt,
	LoadLocal,
	MirFunc,
	Return,
	StoreLocal,
)
from lang.driftc.stage4 import MirToSSA, run_throw_checks
from lang.driftc.stage3 import ThrowSummaryBuilder
from lang.driftc.checker import FnSignature
from lang.test_support import declared_from_signatures, make_signatures
from lang.driftc.core.types_env_impl import build_type_env_from_ssa, SimpleTypeEnv


def _ssa_for_func(func: MirFunc):
	"""Helper to SSA-ify a MIR func for these tests."""
	return MirToSSA().run(func)


def test_inferred_type_env_tracks_constructresult_and_copies():
	"""
	InferredTypeEnv should mark FnResult values constructed in MIR and propagate
	through AssignSSA copies produced by SSA.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="v0", value=1),
			ConstructResultOk(dest="r0", value="v0"),
			StoreLocal(local="x", value="r0"),
			LoadLocal(dest="tmp", local="x"),
		],
		terminator=Return(value="tmp"),
	)
	mir_func = MirFunc(
		fn_id=FunctionId(module="main", name="f_alias", ordinal=0),
		name="f_alias",
		params=[],
		locals=["x"],
		blocks={"entry": entry},
		entry="entry",
	)

	ssa = _ssa_for_func(mir_func)
	fn_id = FunctionId(module="main", name="f_alias", ordinal=0)
	type_env = build_type_env_from_ssa({fn_id: ssa})

	# Return value should be recognized as FnResult via propagation.
	ty = type_env.type_of_ssa_value(fn_id, "tmp")
	assert type_env.is_fnresult(ty)

	# Type-aware throw checks should pass (structural guard would fail here).
	summaries = ThrowSummaryBuilder().build({fn_id: mir_func}, code_to_exc={})
	decl = declared_from_signatures(make_signatures({fn_id: "Int"}, declared_can_throw={fn_id: True}))
	run_throw_checks(
		{fn_id: mir_func},
		summaries,
		declared_can_throw=decl,
		ssa_funcs={fn_id: ssa},
		type_env=type_env,
	)


def test_inferred_type_env_uses_signatures_for_call_results():
	"""Calls with FnResult return types should be marked as FnResult in the env."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			Call(dest="call_res", fn_id=FunctionId(module="main", name="foo", ordinal=0), args=[], can_throw=True),
		],
		terminator=Return(value="call_res"),
	)
	mir_func = MirFunc(
		fn_id=FunctionId(module="main", name="caller", ordinal=0),
		name="caller",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	ssa = _ssa_for_func(mir_func)
	fn_id = FunctionId(module="main", name="caller", ordinal=0)
	foo_id = FunctionId(module="main", name="foo", ordinal=0)
	sigs = {foo_id: FnSignature(name="foo", return_type="Int", declared_can_throw=True)}
	type_env = build_type_env_from_ssa({fn_id: ssa}, signatures=sigs)

	ty = type_env.type_of_ssa_value(fn_id, "call_res")
	assert type_env.is_fnresult(ty)

	# Type-aware throw check should pass for caller marked as can-throw.
	summaries = ThrowSummaryBuilder().build({fn_id: mir_func}, code_to_exc={})
	decl = declared_from_signatures(make_signatures({fn_id: "Int"}, declared_can_throw={fn_id: True}))
	run_throw_checks(
		{fn_id: mir_func},
		summaries,
		declared_can_throw=decl,
		ssa_funcs={fn_id: ssa},
		type_env=type_env,
	)


def test_typeaware_check_rejects_non_fnresult_type():
	"""
	Type-aware throw check should fail when the TypeEnv says return value is not FnResult.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="v0", value=1),
			ConstructResultErr(dest="r0", error="v0"),
		],
		terminator=Return(value="r0"),
	)
	mir_func = MirFunc(
		fn_id=FunctionId(module="main", name="f_bad_type", ordinal=0),
		name="f_bad_type",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	ssa = _ssa_for_func(mir_func)
	fn_id = FunctionId(module="main", name="f_bad_type", ordinal=0)

	# Deliberately tag return value as non-FnResult.
	tenv = SimpleTypeEnv()
	tenv.set_ssa_type(fn_id, "r0", "Int")

	summaries = ThrowSummaryBuilder().build({fn_id: mir_func}, code_to_exc={})
	decl = declared_from_signatures(make_signatures({fn_id: "Int"}, declared_can_throw={fn_id: True}))

	with pytest.raises(RuntimeError):
		run_throw_checks(
			{fn_id: mir_func},
			summaries,
			declared_can_throw=decl,
			ssa_funcs={fn_id: ssa},
			type_env=tenv,
		)
