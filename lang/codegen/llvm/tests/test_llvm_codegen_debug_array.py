# SSA-first LLVM debug info test for array header types.

from __future__ import annotations

from lang.codegen.llvm import lower_module_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import ArrayLit, BasicBlock, ConstInt, MirFunc, Return, StoreLocal
from lang.driftc.stage4 import MirToSSA


def test_debug_array_emits_header_fields() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	arr_ty = table.new_array(int_ty)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="v0", value=1),
			ConstInt(dest="v1", value=2),
			ArrayLit(dest="arrval", elem_ty=int_ty, elements=["v0", "v1"]),
			StoreLocal(local="arr", value="arrval"),
		],
		terminator=Return(value=None),
	)
	for idx, instr in enumerate(entry.instructions, start=1):
		instr.span = Span(file="main.drift", line=idx, column=1)
	entry.terminator.span = Span(file="main.drift", line=5, column=1)

	fn_id = FunctionId(module="main", name="main", ordinal=0)
	sig = FnSignature(name="main", return_type_id=table.ensure_void(), declared_can_throw=False)
	fn_info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, return_type_id=table.ensure_void(), signature=sig)
	mir = MirFunc(
		fn_id=fn_id,
		name="main",
		params=[],
		locals=["arr"],
		blocks={"entry": entry},
		entry="entry",
		local_types={"arr": arr_ty},
	)
	ssa = MirToSSA().run(mir)
	mod = lower_module_to_llvm(
		funcs={fn_id: mir},
		ssa_funcs={fn_id: ssa},
		fn_infos={fn_id: fn_info},
		type_table=table,
		word_bits=host_word_bits(),
	)
	ir = mod.render()

	assert 'name: "len"' in ir
	assert 'name: "cap"' in ir
	assert 'name: "gen"' in ir
	assert 'name: "data"' in ir
