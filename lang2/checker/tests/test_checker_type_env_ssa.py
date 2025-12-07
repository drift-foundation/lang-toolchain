from __future__ import annotations

from lang2.stage2 import BasicBlock, ConstructResultOk, MirFunc, Return
from lang2.stage4 import MirToSSA
from lang2.checker import Checker, FnSignature


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

	signatures = {"f": FnSignature(name="f", return_type="FnResult<Int, Error>")}
	checker = Checker(signatures=signatures)
	checked = checker.check(["f"])

	type_env = checker.build_type_env_from_ssa({"f": ssa}, signatures)
	assert type_env is not None
	ty = type_env.type_of_ssa_value("f", "r0")
	assert type_env.is_fnresult(ty)
