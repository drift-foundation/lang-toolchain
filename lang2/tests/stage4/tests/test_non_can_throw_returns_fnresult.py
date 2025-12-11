"""
Type-aware invariant: non-can-throw functions must not return FnResult.
"""

from __future__ import annotations

from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.stage2 import MirFunc, BasicBlock, Return
from lang2.driftc.stage4.throw_checks import FuncThrowInfo, enforce_fnresult_returns_typeaware
from lang2.driftc.core.types_env_impl import SimpleTypeEnv


def test_non_can_throw_returning_fnresult_is_rejected():
	"""
	Even on typed paths, a non-can-throw function returning FnResult should fail.
	"""
	fn_name = "f_plain"
	ssa_return_val = "ret"
	ssa_func = SimpleNamespace(
		func=MirFunc(
			name=fn_name,
			params=[],
			locals=[],
			blocks={
				"entry": BasicBlock(
					name="entry",
					instructions=[],
					terminator=Return(value=ssa_return_val),
				)
			},
			entry="entry",
		)
	)

	# Tag the return value as FnResult in the TypeEnv.
	env = SimpleTypeEnv()
	env.set_ssa_type(fn_name, ssa_return_val, ("Int", "Error"))

	func_infos = {
		fn_name: FuncThrowInfo(
			constructs_error=False,
			exception_types=set(),
			may_fail_sites=set(),
			declared_can_throw=False,
		)
	}

	diagnostics: list[Diagnostic] = []
	enforce_fnresult_returns_typeaware(
		func_infos=func_infos,
		ssa_funcs={fn_name: ssa_func},
		type_env=env,
		diagnostics=diagnostics,
	)

	assert diagnostics, "Expected a diagnostic for returning FnResult in non-can-throw fn"
	assert any("not declared can-throw" in d.message for d in diagnostics)


# Helper: lightweight SimpleNamespace to mimic SsaFunc shape for this test.
class SimpleNamespace:
	def __init__(self, **kwargs):
		self.__dict__.update(kwargs)
