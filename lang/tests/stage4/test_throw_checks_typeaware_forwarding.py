"""Type-aware FnResult check should allow forwarding/aliasing when types are known."""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
import pytest

from lang.driftc.stage2 import BasicBlock, MirFunc, Return
from lang.driftc.stage3 import ThrowSummaryBuilder
from lang.driftc.stage4 import MirToSSA, run_throw_checks
from lang.driftc.core.types_env_impl import SimpleTypeEnv


def test_fnresult_forwarding_passes_with_type_env():
	"""
	With SSA + TypeEnv supplied, a can-throw function returning a forwarded
	FnResult value should pass (structural check is skipped in typed mode).
	"""
	fn_id = FunctionId(module="main", name="f_forward_typed", ordinal=0)
	# MIR: directly return parameter p0 (already a FnResult).
	entry = BasicBlock(name="entry", instructions=[], terminator=Return(value="p0"))
	mir_func = MirFunc(
		fn_id=fn_id,
		name=fn_id.name,
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)

	ssa_func = MirToSSA().run(mir_func)

	tenv = SimpleTypeEnv()
	# Tag p0 as a FnResult
	tenv.set_ssa_type(fn_id, "p0", ("Int", "Error"))

	summaries = ThrowSummaryBuilder().build({fn_id: mir_func}, code_to_exc={})

	# Should not raise: type-aware path allows forwarding/aliasing of FnResult.
	run_throw_checks(
		{fn_id: mir_func},
		summaries,
		declared_can_throw={fn_id: True},
		ssa_funcs={fn_id: ssa_func},
		type_env=tenv,
	)


def test_fnresult_forwarding_still_rejected_without_types():
	"""
	Structural guard remains for untyped paths: forwarding without ConstructResultOk/Err
	is rejected when SSA/TypeEnv are not supplied.
	"""
	fn_id = FunctionId(module="main", name="f_forward_untyped", ordinal=0)
	entry = BasicBlock(name="entry", instructions=[], terminator=Return(value="p0"))
	mir_func = MirFunc(
		fn_id=fn_id,
		name=fn_id.name,
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	summaries = ThrowSummaryBuilder().build({fn_id: mir_func}, code_to_exc={})

	with pytest.raises(RuntimeError):
		run_throw_checks(
			{fn_id: mir_func},
			summaries,
			declared_can_throw={fn_id: True},
			ssa_funcs=None,
			type_env=None,
		)
