"""
Type-aware check: scalar return types should match the declared TypeId.
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.stage2 import BasicBlock, MirFunc, Return
from lang.driftc.stage4.throw_checks import FuncThrowInfo, enforce_fnresult_returns_typeaware


class DummyTypeEnv:
	"""
	Minimal TypeEnv stub for this test: reports type_of_ssa_value and is_fnresult.
	"""

	def __init__(self, ty_map: dict[tuple[FunctionId, str], object]) -> None:
		self._ty_map = ty_map

	def type_of_ssa_value(self, fn_id: FunctionId, value_id: str) -> object:
		return self._ty_map[(fn_id, value_id)]

	def is_fnresult(self, ty: object) -> bool:
		return isinstance(ty, tuple) and ty and ty[0] == "FnResult"

	def fnresult_parts(self, ty: object) -> tuple[object, object]:
		if not self.is_fnresult(ty):
			raise TypeError("not a FnResult")
		return ty[1], ty[2]


def test_scalar_return_mismatch_reports_error():
	"""Non-can-throw scalar function returning wrong scalar type should be flagged."""
	fn_id = FunctionId(module="main", name="f_scalar", ordinal=0)
	ret_val = "v0"
	ssa_func = DummyNamespace(
		func=MirFunc(
			fn_id=fn_id,
			name=fn_id.name,
			params=[],
			locals=[],
			blocks={"entry": BasicBlock(name="entry", instructions=[], terminator=Return(value=ret_val))},
			entry="entry",
		)
	)

	# Declared return type id = 1; actual type = 2 (mismatch).
	env = DummyTypeEnv({(fn_id, ret_val): 2})
	func_infos = {
		fn_id: FuncThrowInfo(
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
		ssa_funcs={fn_id: ssa_func},
		type_env=env,
		diagnostics=diagnostics,
	)

	assert diagnostics
	assert any("signature return type" in d.message for d in diagnostics)


class DummyNamespace:
	def __init__(self, **kwargs):
		self.__dict__.update(kwargs)
