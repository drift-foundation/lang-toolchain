# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: OpenAI; created: 2026-01-21
from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.checker import Checker, make_fn_info
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage1.node_ids import assign_callsite_ids, assign_node_ids
from lang.driftc.checker import FnSignature


def test_validate_calls_rejects_trait_target_in_typed_mode():
	table = TypeTable()
	fn_id = FunctionId(module="m_main", name="f", ordinal=0)
	sig = FnSignature(
		name="f",
		param_type_ids=[],
		return_type_id=table.ensure_int(),
		type_params=[],
		module="m_main",
		declared_can_throw=False,
	)
	tc = Checker(
		signatures_by_id={fn_id: sig},
		hir_blocks_by_id={},
		call_info_by_callsite_id={},
		exception_catalog={},
		type_table=table,
	)
	fn_info = make_fn_info(fn_id, sig)
	call_method = H.HMethodCall(receiver=H.HLiteralInt(1), method_name="m", args=[])
	block_method = H.HBlock(statements=[H.HExprStmt(expr=call_method)])
	assign_node_ids(block_method)
	assign_callsite_ids(block_method)
	call_info_by_callsite_id_method = {
		call_method.callsite_id: CallInfo(
			target=CallTarget.trait(trait_key="traitkey", method_name="m"),
			sig=CallSig(param_types=(table.ensure_int(),), user_ret_type=table.ensure_int(), can_throw=False),
		)
	}
	diagnostics = []
	tc._validate_calls(block_method, {fn_id: fn_info}, diagnostics, call_info_by_callsite_id_method, current_fn=fn_info)
	assert any("resolved to trait target in typed mode" in d.message for d in diagnostics)

	call_direct = H.HCall(fn=H.HVar("f"), args=[])
	block_direct = H.HBlock(statements=[H.HExprStmt(expr=call_direct)])
	assign_node_ids(block_direct)
	assign_callsite_ids(block_direct)
	call_info_by_callsite_id_direct = {
		call_direct.callsite_id: CallInfo(
			target=CallTarget.trait(trait_key="traitkey", method_name="f"),
			sig=CallSig(param_types=(), user_ret_type=table.ensure_int(), can_throw=False),
		)
	}
	diagnostics = []
	tc._validate_calls(block_direct, {fn_id: fn_info}, diagnostics, call_info_by_callsite_id_direct, current_fn=fn_info)
	assert any("resolved to trait target in typed mode" in d.message for d in diagnostics)

	call_invoke = H.HInvoke(callee=H.HVar("f"), args=[])
	block_invoke = H.HBlock(statements=[H.HExprStmt(expr=call_invoke)])
	assign_node_ids(block_invoke)
	assign_callsite_ids(block_invoke)
	call_info_by_callsite_id_invoke = {
		call_invoke.callsite_id: CallInfo(
			target=CallTarget.trait(trait_key="traitkey", method_name="f"),
			sig=CallSig(param_types=(table.ensure_int(),), user_ret_type=table.ensure_int(), can_throw=False, includes_callee=True),
		)
	}
	diagnostics = []
	tc._validate_calls(block_invoke, {fn_id: fn_info}, diagnostics, call_info_by_callsite_id_invoke, current_fn=fn_info)
	assert any("resolved to trait target in typed mode" in d.message for d in diagnostics)
