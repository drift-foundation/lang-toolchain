from __future__ import annotations

from lang.codegen.llvm import LlvmModuleBuilder, lower_ssa_func_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import BasicBlock, ConstBool, DropValue, IfTerminator, MirFunc, Return
from lang.driftc.stage4 import MirToSSA


def test_dv_drop_uses_helper_not_inline_alloca_in_branch() -> None:
	table = TypeTable()
	dv_ty = table.ensure_diagnostic_value()
	void_ty = table.ensure_void()
	fn_id = FunctionId(module="main", name="dv_drop_branch", ordinal=0)
	sig = FnSignature(name="dv_drop_branch", param_type_ids=[dv_ty], return_type_id=void_ty, declared_can_throw=False)
	fn_info = FnInfo(fn_id=fn_id, name="dv_drop_branch", declared_can_throw=False, return_type_id=void_ty, signature=sig)

	entry = BasicBlock(
		name="entry",
		instructions=[ConstBool(dest="cond", value=True)],
		terminator=IfTerminator(cond="cond", then_target="then", else_target="else"),
	)
	then_block = BasicBlock(
		name="then",
		instructions=[DropValue(value="p", ty=dv_ty)],
		terminator=Return(value=None),
	)
	else_block = BasicBlock(name="else", instructions=[], terminator=Return(value=None))
	mir = MirFunc(fn_id=fn_id, name="dv_drop_branch", params=["p"], locals=[], blocks={"entry": entry, "then": then_block, "else": else_block}, entry="entry")
	ssa = MirToSSA().run(mir)

	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits()))
	ir = mod.render()

	assert "define void @__drift_dv_drop_helper(" in ir
	assert "call void @__drift_dv_drop_helper(" in ir
	assert ir.count("call void @drift_dv_release(") == 1

