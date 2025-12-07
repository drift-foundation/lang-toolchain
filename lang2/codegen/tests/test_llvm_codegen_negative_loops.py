"""
Loops/backedges should be rejected by the codegen path in v1.
"""

from __future__ import annotations

import pytest

from lang2.codegen.llvm import lower_ssa_func_to_llvm
from lang2.checker import FnInfo
from lang2.stage2 import BasicBlock, MirFunc, Goto, Return
from lang2.stage4 import MirToSSA
from lang2.types_core import TypeTable


def test_loop_backedge_rejected():
	"""
	A function with a backedge should cause SSA to raise before codegen.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[],
		terminator=Goto(target="loop"),
	)
	loop_block = BasicBlock(
		name="loop",
		instructions=[],
		terminator=Goto(target="loop"),
	)
	mir = MirFunc(
		name="loopy",
		params=[],
		locals=[],
		blocks={"entry": entry, "loop": loop_block},
		entry="entry",
	)
	with pytest.raises(NotImplementedError, match="loops/backedges not supported"):
		MirToSSA().run(mir)

	# As a safety check, if a synthetic GENERAL cfg_kind were passed, codegen should reject.
	table = TypeTable()
	int_ty = table.new_scalar("Int")
	fn_info = FnInfo(name="loopy", declared_can_throw=False, return_type_id=int_ty)
	FakeEnum = type("Enum", (), {"GENERAL": object()})
	fake_ssa = type("FakeSsa", (), {"cfg_kind": FakeEnum.GENERAL, "block_order": []})
	with pytest.raises(NotImplementedError):
		lower_ssa_func_to_llvm(mir, fake_ssa, fn_info, {"loopy": fn_info})
