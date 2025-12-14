"""Type-aware FnResult check should allow forwarding/aliasing when types are known."""

from __future__ import annotations

import pytest

from lang2.driftc.stage2 import BasicBlock, MirFunc, Return
from lang2.driftc.stage3 import ThrowSummaryBuilder
from lang2.driftc.stage4 import MirToSSA, run_throw_checks
from lang2.driftc.core.types_env_impl import SimpleTypeEnv


def test_fnresult_forwarding_passes_with_type_env():
	"""
	With SSA + TypeEnv supplied, a can-throw function returning a forwarded
	FnResult value should pass (structural check is skipped in typed mode).
	"""
	fn_name = "f_forward_typed"
	# MIR: directly return parameter p0 (already a FnResult).
	entry = BasicBlock(name="entry", instructions=[], terminator=Return(value="p0"))
	mir_func = MirFunc(
		name=fn_name,
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)

	ssa_func = MirToSSA().run(mir_func)

	tenv = SimpleTypeEnv()
	# Tag p0 as a FnResult
	tenv.set_ssa_type(fn_name, "p0", ("Int", "Error"))

	summaries = ThrowSummaryBuilder().build({fn_name: mir_func}, code_to_exc={})

	# Should not raise: type-aware path allows forwarding/aliasing of FnResult.
	run_throw_checks(
		{fn_name: mir_func},
		summaries,
		declared_can_throw={fn_name: True},
		ssa_funcs={fn_name: ssa_func},
		type_env=tenv,
	)


def test_fnresult_forwarding_still_rejected_without_types():
	"""
	Structural guard remains for untyped paths: forwarding without ConstructResultOk/Err
	is rejected when SSA/TypeEnv are not supplied.
	"""
	fn_name = "f_forward_untyped"
	entry = BasicBlock(name="entry", instructions=[], terminator=Return(value="p0"))
	mir_func = MirFunc(
		name=fn_name,
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	summaries = ThrowSummaryBuilder().build({fn_name: mir_func}, code_to_exc={})

	with pytest.raises(RuntimeError):
		run_throw_checks(
			{fn_name: mir_func},
			summaries,
			declared_can_throw={fn_name: True},
			ssa_funcs=None,
			type_env=None,
		)
