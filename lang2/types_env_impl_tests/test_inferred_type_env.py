from __future__ import annotations

import pytest

from lang2.stage2 import (
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
from lang2.stage4 import MirToSSA, run_throw_checks
from lang2.stage3 import ThrowSummaryBuilder
from lang2.checker import FnSignature
from lang2.test_support import declared_from_signatures, make_signatures
from lang2.core.types_env_impl import build_type_env_from_ssa, SimpleTypeEnv


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
		name="f_alias",
		params=[],
		locals=["x"],
		blocks={"entry": entry},
		entry="entry",
	)

	ssa = _ssa_for_func(mir_func)
	type_env = build_type_env_from_ssa({"f_alias": ssa})

	# Return value should be recognized as FnResult via propagation.
	ty = type_env.type_of_ssa_value("f_alias", "tmp")
	assert type_env.is_fnresult(ty)

	# Type-aware throw checks should pass (structural guard would fail here).
	summaries = ThrowSummaryBuilder().build({"f_alias": mir_func}, code_to_exc={})
	decl = declared_from_signatures(make_signatures({"f_alias": "FnResult<Int, Error>"}))
	run_throw_checks(
		{"f_alias": mir_func},
		summaries,
		declared_can_throw=decl,
		ssa_funcs={"f_alias": ssa},
		type_env=type_env,
	)


def test_inferred_type_env_uses_signatures_for_call_results():
	"""Calls with FnResult return types should be marked as FnResult in the env."""
	entry = BasicBlock(
		name="entry",
		instructions=[
			Call(dest="call_res", fn="foo", args=[]),
		],
		terminator=Return(value="call_res"),
	)
	mir_func = MirFunc(
		name="caller",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	ssa = _ssa_for_func(mir_func)
	sigs = {"foo": FnSignature(name="foo", return_type="FnResult<Int, Error>")}
	type_env = build_type_env_from_ssa({"caller": ssa}, signatures=sigs)

	ty = type_env.type_of_ssa_value("caller", "call_res")
	assert type_env.is_fnresult(ty)

	# Type-aware throw check should pass for caller marked as can-throw.
	summaries = ThrowSummaryBuilder().build({"caller": mir_func}, code_to_exc={})
	decl = declared_from_signatures(make_signatures({"caller": "FnResult<Int, Error>"}))
	run_throw_checks(
		{"caller": mir_func},
		summaries,
		declared_can_throw=decl,
		ssa_funcs={"caller": ssa},
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
		name="f_bad_type",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	ssa = _ssa_for_func(mir_func)

	# Deliberately tag return value as non-FnResult.
	tenv = SimpleTypeEnv()
	tenv.set_ssa_type("f_bad_type", "r0", "Int")

	summaries = ThrowSummaryBuilder().build({"f_bad_type": mir_func}, code_to_exc={})
	decl = declared_from_signatures(make_signatures({"f_bad_type": "FnResult<Int, Error>"}))

	with pytest.raises(RuntimeError):
		run_throw_checks(
			{"f_bad_type": mir_func},
			summaries,
			declared_can_throw=decl,
			ssa_funcs={"f_bad_type": ssa},
			type_env=tenv,
		)
