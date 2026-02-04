# Debug info: keep alive unused locals so they appear in line tables.

from __future__ import annotations

from lang2.codegen.llvm import lower_ssa_func_to_llvm
from lang2.codegen.llvm.test_utils import host_word_bits
from lang2.driftc.checker import FnInfo, FnSignature
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.core.span import Span
from lang2.driftc.core.types_core import TypeTable
from lang2.driftc.stage2 import BasicBlock, ConstInt, MirFunc, Return, StoreLocal
from lang2.driftc.stage4 import MirToSSA


def test_debug_keepalive_store_for_unused_local() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="t0", value=1),
			StoreLocal(local="p", value="t0"),
			ConstInt(dest="t1", value=2),
		],
		terminator=Return(value="t1"),
	)
	block.instructions[1].span = Span(file="src/main.drift", line=3, column=2)
	block.terminator.span = Span(file="src/main.drift", line=4, column=2)

	fn_id = FunctionId(module="main", name="main", ordinal=0)
	sig = FnSignature(
		name="main",
		return_type_id=int_ty,
		param_type_ids=[],
		declared_can_throw=False,
		loc=Span(file="src/main.drift", line=1, column=1),
	)
	fn_info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, signature=sig, return_type_id=int_ty)
	func = MirFunc(
		fn_id=fn_id,
		name="main",
		params=[],
		locals=["p"],
		blocks={"entry": block},
		entry="entry",
		local_types={"p": int_ty, "t0": int_ty, "t1": int_ty},
	)

	ssa = MirToSSA().run(func)
	ir = lower_ssa_func_to_llvm(func, ssa, fn_info, {fn_id: fn_info}, type_table=table, word_bits=host_word_bits())

	assert "__dbg_keepalive_p__addr" in ir
	assert "store" in ir
