"""Type-aware FnResult return checks using SimpleTypeEnv and SSA."""

from __future__ import annotations

import pytest

from lang2.driftc.stage2 import BasicBlock, ConstructResultOk, MirFunc, Return
from lang2.driftc.stage3 import ThrowSummaryBuilder
from lang2.driftc.stage4 import MirToSSA, run_throw_checks
from lang2.driftc.core.types_env_impl import SimpleTypeEnv


def _build_simple_mir_fn(name: str, ret_value: str) -> MirFunc:
	"""
	Build a single-block MIR function that returns `ret_value` produced
	by ConstructResultOk. Used to exercise the type-aware throw check.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[ConstructResultOk(dest=ret_value, value="v0")],
		terminator=Return(value=ret_value),
	)
	return MirFunc(
		name=name,
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)


def test_typeaware_accepts_fnresult_return():
	"""Type-aware check should pass when the returned SSA value has FnResult type."""
	fn_name = "f_type_ok"
	mir_func = _build_simple_mir_fn(fn_name, ret_value="r0")

	# SSA for the function (single block).
	ssa_func = MirToSSA().run(mir_func)

	# SimpleTypeEnv: treat a 2-tuple as FnResult; tag r0 as FnResult(Int, Error).
	tenv = SimpleTypeEnv()
	tenv.set_ssa_type(fn_name, "r0", ("Int", "Error"))

	summaries = ThrowSummaryBuilder().build({fn_name: mir_func}, code_to_exc={})

	# Should not raise: declared can-throw and return value is typed as FnResult.
	run_throw_checks(
		{fn_name: mir_func},
		summaries,
		declared_can_throw={fn_name: True},
		ssa_funcs={fn_name: ssa_func},
		type_env=tenv,
	)


def test_typeaware_rejects_non_fnresult_return():
	"""
	Type-aware check should reject can-throw functions returning non-FnResult
	values even if structural checks would pass.
	"""
	fn_name = "f_type_bad"
	mir_func = _build_simple_mir_fn(fn_name, ret_value="r0")
	ssa_func = MirToSSA().run(mir_func)

	# Tag r0 as a non-FnResult type.
	tenv = SimpleTypeEnv()
	tenv.set_ssa_type(fn_name, "r0", "Int")

	summaries = ThrowSummaryBuilder().build({fn_name: mir_func}, code_to_exc={})

	with pytest.raises(RuntimeError):
		run_throw_checks(
			{fn_name: mir_func},
			summaries,
			declared_can_throw={fn_name: True},
			ssa_funcs={fn_name: ssa_func},
			type_env=tenv,
		)
