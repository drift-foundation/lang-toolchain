# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId, FunctionRefId, FunctionRefKind
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage1.node_ids import assign_node_ids, assign_callsite_ids
from lang.driftc.stage2 import HIRToMIR, CallIndirect, FnPtrConst, make_builder


def test_fnptr_const_lowers_and_indirect_call_uses_callsig() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	fn_id = FunctionId(module="main", name="add1", ordinal=0)
	call_sig = CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=False)
	fn_ref = FunctionRefId(fn_id=fn_id, kind=FunctionRefKind.IMPL, has_wrapper=False)
	fnptr = H.HFnPtrConst(fn_ref=fn_ref, call_sig=call_sig)
	let_stmt = H.HLet(name="f", value=fnptr, binding_id=1)
	invoke = H.HInvoke(callee=H.HVar("f", binding_id=1), args=[H.HLiteralInt(3)])
	block = H.HBlock(statements=[let_stmt, H.HExprStmt(expr=invoke)])
	assign_node_ids(block)
	assign_callsite_ids(block)

	call_info = CallInfo(
		target=CallTarget.indirect(invoke.callee.node_id),
		sig=call_sig,
	)
	call_info_by_callsite_id = {}
	csid = getattr(invoke, "callsite_id", None)
	if isinstance(csid, int):
		call_info_by_callsite_id[csid] = call_info

	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	lower = HIRToMIR(
		builder,
		type_table=table,
		call_info_by_callsite_id=call_info_by_callsite_id,
	)
	lower.lower_block(block)

	instrs = list(builder.func.blocks[builder.func.entry].instructions)
	fnptr_instrs = [instr for instr in instrs if isinstance(instr, FnPtrConst)]
	assert fnptr_instrs
	assert fnptr_instrs[0].fn_ref == fn_ref
	call_instrs = [instr for instr in instrs if isinstance(instr, CallIndirect)]
	assert call_instrs
	assert call_instrs[0].can_throw is False
	assert call_instrs[0].user_ret_type == int_ty
