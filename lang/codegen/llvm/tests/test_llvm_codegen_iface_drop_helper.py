from __future__ import annotations

from lang.codegen.llvm import LlvmModuleBuilder, lower_ssa_func_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import BasicBlock, DropValue, MirFunc, Return
from lang.driftc.stage4 import MirToSSA


def test_iface_drop_uses_helper_not_inline_in_function_body() -> None:
	table = TypeTable()
	iface_ty = table.declare_interface("m", "I", type_params=[])
	void_ty = table.ensure_void()
	fn_id = FunctionId(module="main", name="iface_drop", ordinal=0)
	sig = FnSignature(name="iface_drop", param_type_ids=[iface_ty], return_type_id=void_ty, declared_can_throw=False)
	fn_info = FnInfo(fn_id=fn_id, name="iface_drop", declared_can_throw=False, return_type_id=void_ty, signature=sig)

	entry = BasicBlock(
		name="entry",
		instructions=[DropValue(value="p", ty=iface_ty)],
		terminator=Return(value=None),
	)
	mir = MirFunc(fn_id=fn_id, name="iface_drop", params=["p"], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits()))
	ir = mod.render()

	assert "define void @__drift_iface_drop_helper(" in ir
	assert "call void @__drift_iface_drop_helper(" in ir
	assert ir.count("call void @drift_iface_free(") == 1
