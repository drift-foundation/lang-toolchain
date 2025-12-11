"""
Type-aware check: scalar return types should match the declared TypeId.
"""

from __future__ import annotations

from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.stage2 import BasicBlock, MirFunc, Return
from lang2.driftc.stage4.throw_checks import FuncThrowInfo, enforce_fnresult_returns_typeaware


class DummyTypeEnv:
	"""
	Minimal TypeEnv stub for this test: reports type_of_ssa_value and is_fnresult.
	"""

	def __init__(self, ty_map: dict[tuple[str, str], object]) -> None:
		self._ty_map = ty_map

	def type_of_ssa_value(self, fn_name: str, value_id: str) -> object:
		return self._ty_map[(fn_name, value_id)]

	def is_fnresult(self, ty: object) -> bool:
		return isinstance(ty, tuple) and ty and ty[0] == "FnResult"

	def fnresult_parts(self, ty: object) -> tuple[object, object]:
		if not self.is_fnresult(ty):
			raise TypeError("not a FnResult")
		return ty[1], ty[2]


def test_scalar_return_mismatch_reports_error():
	"""Non-can-throw scalar function returning wrong scalar type should be flagged."""
	fn_name = "f_scalar"
	ret_val = "v0"
	ssa_func = DummyNamespace(
		func=MirFunc(
			name=fn_name,
			params=[],
			locals=[],
			blocks={"entry": BasicBlock(name="entry", instructions=[], terminator=Return(value=ret_val))},
			entry="entry",
		)
	)

	# Declared return type id = 1; actual type = 2 (mismatch).
	env = DummyTypeEnv({(fn_name, ret_val): 2})
	func_infos = {
		fn_name: FuncThrowInfo(
			constructs_error=False,
			exception_types=set(),
			may_fail_sites=set(),
			declared_can_throw=False,
			return_type_id=1,
		)
	}

	diagnostics: list[Diagnostic] = []
	enforce_fnresult_returns_typeaware(
		func_infos=func_infos,
		ssa_funcs={fn_name: ssa_func},
		type_env=env,
		diagnostics=diagnostics,
	)

	assert diagnostics
	assert any("signature return type" in d.message for d in diagnostics)


class DummyNamespace:
	def __init__(self, **kwargs):
		self.__dict__.update(kwargs)
