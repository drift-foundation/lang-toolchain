"""Type-aware FnResult return checks using SimpleTypeEnv and SSA."""

from __future__ import annotations

import pytest

from lang.driftc.stage2 import BasicBlock, ConstructResultOk, MirFunc, Return
from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage3 import ThrowSummaryBuilder
from lang.driftc.stage4 import MirToSSA, run_throw_checks
from lang.driftc.core.types_env_impl import SimpleTypeEnv


def _build_simple_mir_fn(name: str, ret_value: str) -> MirFunc:
	"""
	Build a single-block MIR function that -> `ret_value` produced
	by ConstructResultOk. Used to exercise the type-aware throw check.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[ConstructResultOk(dest=ret_value, value="v0")],
		terminator=Return(value=ret_value),
	)
	return MirFunc(
		fn_id=FunctionId(module="main", name=name, ordinal=0),
		name=name,
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)


def test_typeaware_accepts_fnresult_return():
	"""Type-aware check should pass when the returned SSA value has FnResult type."""
	fn_id = FunctionId(module="main", name="f_type_ok", ordinal=0)
	mir_func = _build_simple_mir_fn(fn_id.name, ret_value="r0")

	# SSA for the function (single block).
	ssa_func = MirToSSA().run(mir_func)

	# SimpleTypeEnv: treat a 2-tuple as FnResult; tag r0 as FnResult(Int, Error).
	tenv = SimpleTypeEnv()
	tenv.set_ssa_type(fn_id, "r0", ("Int", "Error"))

	summaries = ThrowSummaryBuilder().build({fn_id: mir_func}, code_to_exc={})

	# Should not raise: declared can-throw and return value is typed as FnResult.
	run_throw_checks(
		{fn_id: mir_func},
		summaries,
		declared_can_throw={fn_id: True},
		ssa_funcs={fn_id: ssa_func},
		type_env=tenv,
	)


def test_typeaware_rejects_non_fnresult_return():
	"""
	Type-aware check should reject can-throw functions returning non-FnResult
	values even if structural checks would pass.
	"""
	fn_id = FunctionId(module="main", name="f_type_bad", ordinal=0)
	mir_func = _build_simple_mir_fn(fn_id.name, ret_value="r0")
	ssa_func = MirToSSA().run(mir_func)

	# Tag r0 as a non-FnResult type.
	tenv = SimpleTypeEnv()
	tenv.set_ssa_type(fn_id, "r0", "Int")

	summaries = ThrowSummaryBuilder().build({fn_id: mir_func}, code_to_exc={})

	with pytest.raises(RuntimeError):
		run_throw_checks(
			{fn_id: mir_func},
			summaries,
			declared_can_throw={fn_id: True},
			ssa_funcs={fn_id: ssa_func},
			type_env=tenv,
		)
