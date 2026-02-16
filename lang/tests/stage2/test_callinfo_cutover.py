# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc import stage1 as H
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.types_core import TypeTable, TypeKind
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget, call_abi_ret_type
from lang.driftc.stage1.node_ids import assign_node_ids, assign_callsite_ids
from lang.driftc.stage2 import HIRToMIR, Call, CallIndirect, make_builder
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.mir_validate import validate_mir_call_invariants


def _fn_id(name: str) -> FunctionId:
	return FunctionId(module="main", name=name, ordinal=0)


def _collect_calls_in_block(block: H.HBlock) -> list[H.HExpr]:
	calls: list[H.HExpr] = []
	def walk_expr(expr: H.HExpr) -> None:
		if isinstance(expr, (H.HCall, H.HMethodCall, H.HInvoke)):
			calls.append(expr)
			if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HLambda):
				walk_lambda(expr.fn)
			if isinstance(expr, H.HInvoke):
				walk_expr(expr.callee)
			if isinstance(expr, H.HMethodCall):
				walk_expr(expr.receiver)
			for arg in getattr(expr, "args", []) or []:
				walk_expr(arg)
			for kw in getattr(expr, "kwargs", []) or []:
				walk_expr(kw.value)
			return
		if isinstance(expr, H.HLambda):
			walk_lambda(expr)
			return
		if isinstance(expr, H.HUnary):
			walk_expr(expr.expr)
		if isinstance(expr, H.HBinary):
			walk_expr(expr.left)
			walk_expr(expr.right)
		if isinstance(expr, H.HTernary):
			walk_expr(expr.cond)
			walk_expr(expr.then_expr)
			walk_expr(expr.else_expr)
		if isinstance(expr, H.HField):
			walk_expr(expr.subject)
		if isinstance(expr, H.HIndex):
			walk_expr(expr.subject)
			walk_expr(expr.index)
		if hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
			walk_expr(expr.attempt)
			for arm in expr.arms:
				walk_block(arm.block)
				if arm.result is not None:
					walk_expr(arm.result)
		if hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
			walk_expr(expr.subject)
			for arm in expr.arms:
				walk_block(arm.block)
				if getattr(arm, "result", None) is not None:
					walk_expr(arm.result)

	def walk_block(inner: H.HBlock) -> None:
		for stmt in inner.statements:
			if isinstance(stmt, H.HExprStmt):
				walk_expr(stmt.expr)
			if isinstance(stmt, H.HReturn) and stmt.value is not None:
				walk_expr(stmt.value)
			if isinstance(stmt, H.HIf):
				walk_expr(stmt.cond)
				walk_block(stmt.then_block)
				if stmt.else_block is not None:
					walk_block(stmt.else_block)
			if hasattr(H, "HLoop") and isinstance(stmt, getattr(H, "HLoop")):
				walk_block(stmt.body)
			if hasattr(H, "HBlock") and isinstance(stmt, getattr(H, "HBlock")):
				walk_block(stmt)

	def walk_lambda(lam: H.HLambda) -> None:
		if lam.body_expr is not None:
			walk_expr(lam.body_expr)
		if lam.body_block is not None:
			walk_block(lam.body_block)

	walk_block(block)
	return calls


def _lower_typed_callsite(
	block: H.HBlock,
	*,
	call_info_by_callsite_id: dict[int, CallInfo],
	type_table: TypeTable | None = None,
	signatures_by_id: dict[FunctionId, FnSignature] | None = None,
	typed_mode: bool = True,
) -> HIRToMIR:
	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	table = type_table or TypeTable()
	hir_norm = block
	assign_node_ids(hir_norm)
	assign_callsite_ids(hir_norm)
	lower = HIRToMIR(
		builder,
		type_table=table,
		call_info_by_callsite_id=call_info_by_callsite_id,
		signatures_by_id=signatures_by_id or {},
		typed_mode=typed_mode,
	)
	lower.lower_block(hir_norm)
	if typed_mode:
		for call in _collect_calls_in_block(hir_norm):
			csid = getattr(call, "callsite_id", None)
			if isinstance(csid, int) and csid not in call_info_by_callsite_id:
				raise AssertionError(f"missing callsite CallInfo for id {csid}")
	return lower




def _call_info(
	*,
	fn_id: FunctionId,
	param_types: tuple[int, ...],
	return_type: int,
	can_throw: bool,
) -> CallInfo:
	return CallInfo(
		target=CallTarget.direct(fn_id),
		sig=CallSig(param_types=param_types, user_ret_type=return_type, can_throw=can_throw),
	)


def test_direct_call_requires_call_info() -> None:
	block = H.HBlock(statements=[H.HExprStmt(expr=H.HCall(fn=H.HVar("f"), args=[H.HLiteralInt(1)]))])
	assign_node_ids(block)
	with pytest.raises(AssertionError, match=r"missing call info for HCall callsite_id="):
		_lower_typed_callsite(block, call_info_by_callsite_id={})


def test_method_call_requires_call_info() -> None:
	call = H.HMethodCall(receiver=H.HVar("obj"), method_name="m", args=[])
	block = H.HBlock(statements=[H.HExprStmt(expr=call)])
	assign_node_ids(block)
	with pytest.raises(AssertionError, match=r"missing call info for HMethodCall callsite_id="):
		_lower_typed_callsite(block, call_info_by_callsite_id={})


def test_invoke_requires_call_info() -> None:
	call = H.HInvoke(callee=H.HVar("fp"), args=[H.HLiteralInt(1)])
	block = H.HBlock(statements=[H.HExprStmt(expr=call)])
	assign_node_ids(block)
	with pytest.raises(AssertionError, match=r"missing call info for HInvoke callsite_id="):
		_lower_typed_callsite(block, call_info_by_callsite_id={})


def test_invoke_rejects_non_indirect_call_target() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	call = H.HInvoke(callee=H.HVar("fp"), args=[H.HLiteralInt(1)])
	block = H.HBlock(statements=[H.HExprStmt(expr=call)])
	assign_node_ids(block)
	assign_callsite_ids(block)
	assert isinstance(call.callsite_id, int)
	call_info_by_callsite_id = {
		call.callsite_id: CallInfo(
			target=CallTarget.direct(_fn_id("f")),
			sig=CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=False),
		)
	}
	with pytest.raises(AssertionError, match=r"requires INDIRECT CallTarget"):
		_lower_typed_callsite(block, call_info_by_callsite_id=call_info_by_callsite_id, type_table=table)


def test_invoke_rejects_includes_callee_flag() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	call = H.HInvoke(callee=H.HVar("fp"), args=[H.HLiteralInt(1)])
	block = H.HBlock(statements=[H.HExprStmt(expr=call)])
	assign_node_ids(block)
	assign_callsite_ids(block)
	assert isinstance(call.callsite_id, int)
	call_info_by_callsite_id = {
		call.callsite_id: CallInfo(
			target=CallTarget.indirect(call.callee.node_id),
			sig=CallSig(param_types=(int_ty, int_ty), user_ret_type=int_ty, can_throw=False, includes_callee=True),
		)
	}
	with pytest.raises(AssertionError, match=r"must not set includes_callee"):
		_lower_typed_callsite(block, call_info_by_callsite_id=call_info_by_callsite_id, type_table=table)


def test_method_call_rejects_constructor_call_target() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	struct_ty = table.declare_struct("main", "Point", ["x"])
	call = H.HMethodCall(receiver=H.HVar("obj"), method_name="m", args=[H.HLiteralInt(1)])
	block = H.HBlock(statements=[H.HExprStmt(expr=call)])
	assign_node_ids(block)
	assign_callsite_ids(block)
	assert isinstance(call.callsite_id, int)
	call_info_by_callsite_id = {
		call.callsite_id: CallInfo(
			target=CallTarget.constructor_struct(struct_ty),
			sig=CallSig(param_types=(int_ty, int_ty), user_ret_type=struct_ty, can_throw=False),
		)
	}
	with pytest.raises(AssertionError, match=r"method call has constructor CallTarget"):
		_lower_typed_callsite(block, call_info_by_callsite_id=call_info_by_callsite_id, type_table=table)


def test_missing_call_info_in_control_flow_and_lambda() -> None:
	call_if = H.HCall(fn=H.HVar("f"), args=[H.HLiteralInt(1)])
	call_loop = H.HCall(fn=H.HVar("g"), args=[])
	call_inner = H.HCall(fn=H.HVar("h"), args=[])
	lam = H.HLambda(
		params=[],
		body_block=H.HBlock(
			statements=[
				H.HExprStmt(expr=call_inner),
				H.HReturn(value=H.HLiteralInt(0)),
			]
		),
	)
	block = H.HBlock(
		statements=[
			H.HIf(
				cond=H.HLiteralBool(True),
				then_block=H.HBlock(statements=[H.HExprStmt(expr=call_if)]),
				else_block=None,
			),
			H.HLoop(body=H.HBlock(statements=[H.HExprStmt(expr=call_loop), H.HBreak()])),
			H.HExprStmt(expr=H.HCall(fn=lam, args=[])),
		]
	)
	table = TypeTable()
	int_ty = table.ensure_int()
	assign_node_ids(block)
	assign_callsite_ids(block)
	call_info_by_callsite_id = {
		call_if.callsite_id: _call_info(fn_id=_fn_id("f"), param_types=(int_ty,), return_type=int_ty, can_throw=False),
		call_loop.callsite_id: _call_info(fn_id=_fn_id("g"), param_types=(), return_type=int_ty, can_throw=False),
	}
	with pytest.raises(AssertionError, match=r"lambda missing can_throw_effective"):
		_lower_typed_callsite(
			block,
			call_info_by_callsite_id=call_info_by_callsite_id,
			type_table=table,
		)


def test_direct_call_abi_return_types() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HCall(fn=H.HVar("may_throw"), args=[H.HLiteralInt(1)])),
			H.HExprStmt(expr=H.HCall(fn=H.HVar("nothrow"), args=[H.HLiteralInt(2)])),
		]
	)
	assign_node_ids(block)
	assign_callsite_ids(block)
	call_info_by_callsite_id = {}
	info_by_name = {}
	for stmt in block.statements:
		expr = getattr(stmt, "expr", None)
		if not isinstance(expr, H.HCall):
			continue
		is_throw = expr.fn.name == "may_throw"
		info = CallInfo(
			target=CallTarget.direct(_fn_id(expr.fn.name)),
			sig=CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=is_throw),
		)
		csid = getattr(expr, "callsite_id", None)
		if isinstance(csid, int):
			call_info_by_callsite_id[csid] = info
		info_by_name[expr.fn.name] = info
	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	lower = HIRToMIR(
		builder,
		type_table=table,
		call_info_by_callsite_id=call_info_by_callsite_id,
	)
	lower.lower_block(block)
	calls = []
	for block in builder.func.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, Call):
				calls.append(instr)
	assert len(calls) >= 2
	for instr in calls:
		ret_ty = lower._local_types.get(instr.dest)
		fn_name = function_symbol(instr.fn_id)
		if fn_name == "may_throw":
			assert ret_ty == call_abi_ret_type(info_by_name["may_throw"].sig, table)
			assert table.get(ret_ty).kind is TypeKind.FNRESULT
		elif fn_name == "nothrow":
			assert ret_ty == int_ty


def test_call_abi_return_types_extended() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	void_ty = table.ensure_void()
	obj_init = H.HLet(name="obj", value=H.HLiteralInt(0))
	obj_init.binding_id = 0
	call_throw = H.HCall(fn=H.HVar("may_throw"), args=[H.HLiteralInt(1)])
	call_void = H.HCall(fn=H.HVar("nothrow_void"), args=[])
	call_throw_void = H.HCall(fn=H.HVar("may_throw_void"), args=[])
	call_inner = H.HCall(fn=H.HVar("inner"), args=[])
	call_outer = H.HCall(fn=H.HVar("outer"), args=[call_inner])
	method_call = H.HMethodCall(receiver=H.HVar("obj", binding_id=0), method_name="m", args=[])
	block = H.HBlock(
		statements=[
			obj_init,
			H.HExprStmt(expr=call_throw),
			H.HExprStmt(expr=call_void),
			H.HExprStmt(expr=call_throw_void),
			H.HExprStmt(expr=method_call),
			H.HExprStmt(expr=call_outer),
		]
	)
	assign_node_ids(block)
	assign_callsite_ids(block)
	call_info_by_callsite = {
		call_throw.callsite_id: _call_info(fn_id=_fn_id("may_throw"), param_types=(int_ty,), return_type=int_ty, can_throw=True),
		call_void.callsite_id: _call_info(fn_id=_fn_id("nothrow_void"), param_types=(), return_type=void_ty, can_throw=False),
		call_throw_void.callsite_id: _call_info(
			fn_id=_fn_id("may_throw_void"), param_types=(), return_type=void_ty, can_throw=True
		),
		call_inner.callsite_id: _call_info(fn_id=_fn_id("inner"), param_types=(), return_type=int_ty, can_throw=True),
		call_outer.callsite_id: _call_info(fn_id=_fn_id("outer"), param_types=(int_ty,), return_type=int_ty, can_throw=False),
		method_call.callsite_id: _call_info(
			fn_id=FunctionId(module="mod", name="m", ordinal=0),
			param_types=(int_ty,),
			return_type=int_ty,
			can_throw=True,
		),
	}
	signatures_by_id = {
		FunctionId(module="mod", name="m", ordinal=0): FnSignature(
			name="mod::m",
			method_name="m",
			param_type_ids=[int_ty],
			return_type_id=int_ty,
			is_method=True,
			self_mode="value",
		)
	}
	lower = _lower_typed_callsite(
		block,
		call_info_by_callsite_id=call_info_by_callsite,
		type_table=table,
		signatures_by_id=signatures_by_id,
	)
	calls = []
	for b in lower.b.func.blocks.values():
		for instr in b.instructions:
			if isinstance(instr, Call):
				calls.append(instr)
	calls_by_fn = {function_symbol(instr.fn_id): instr for instr in calls}
	assert calls_by_fn["may_throw"].can_throw is True
	assert calls_by_fn["nothrow_void"].can_throw is False
	assert calls_by_fn["may_throw_void"].can_throw is True
	assert calls_by_fn["inner"].can_throw is True
	assert calls_by_fn["outer"].can_throw is False
	assert calls_by_fn["mod::m"].can_throw is True

	assert lower._local_types[calls_by_fn["may_throw"].dest] == call_abi_ret_type(
		call_info_by_callsite[call_throw.callsite_id].sig, table
	)
	assert lower._local_types[calls_by_fn["may_throw_void"].dest] == call_abi_ret_type(
		call_info_by_callsite[call_throw_void.callsite_id].sig, table
	)
	assert lower._local_types[calls_by_fn["inner"].dest] == call_abi_ret_type(
		call_info_by_callsite[call_inner.callsite_id].sig, table
	)
	assert lower._local_types[calls_by_fn["outer"].dest] == int_ty
	assert calls_by_fn["nothrow_void"].dest is None


def test_invoke_call_abi_return_types() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	void_ty = table.ensure_void()
	call_throw = H.HInvoke(callee=H.HVar("fp1", binding_id=0), args=[H.HLiteralInt(1)])
	call_void = H.HInvoke(callee=H.HVar("fp2", binding_id=1), args=[])
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=call_throw),
			H.HExprStmt(expr=call_void),
		]
	)
	assign_node_ids(block)
	assign_callsite_ids(block)
	call_info_by_callsite = {
		call_throw.callsite_id: CallInfo(
			target=CallTarget.indirect(call_throw.callee.node_id),
			sig=CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=True),
		),
		call_void.callsite_id: CallInfo(
			target=CallTarget.indirect(call_void.callee.node_id),
			sig=CallSig(param_types=(), user_ret_type=void_ty, can_throw=False),
		),
	}
	lower = _lower_typed_callsite(
		block,
		call_info_by_callsite_id=call_info_by_callsite,
		type_table=table,
	)
	calls = []
	for b in lower.b.func.blocks.values():
		for instr in b.instructions:
			if isinstance(instr, CallIndirect):
				calls.append(instr)
	assert len(calls) == 2
	for instr in calls:
		if instr.can_throw:
			assert lower._local_types[instr.dest] == call_abi_ret_type(
				call_info_by_callsite[call_throw.callsite_id].sig, table
			)
		else:
			assert instr.dest is None


def test_call_target_uses_canonical_symbol() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	obj_init = H.HLet(name="obj", value=H.HLiteralInt(0))
	obj_init.binding_id = 0
	free_call = H.HCall(fn=H.HVar("foo"), args=[H.HLiteralInt(1)])
	method_call = H.HMethodCall(receiver=H.HVar("obj", binding_id=0), method_name="m", args=[])
	block = H.HBlock(statements=[obj_init, H.HExprStmt(expr=free_call), H.HExprStmt(expr=method_call)])
	assign_node_ids(block)
	assign_callsite_ids(block)
	call_info_by_callsite = {
		free_call.callsite_id: _call_info(
			fn_id=FunctionId(module="mod", name="foo", ordinal=2),
			param_types=(int_ty,),
			return_type=int_ty,
			can_throw=False,
		),
		method_call.callsite_id: _call_info(
			fn_id=FunctionId(module="mod2", name="m", ordinal=1),
			param_types=(int_ty,),
			return_type=int_ty,
			can_throw=False,
		),
	}
	signatures_by_id = {
		FunctionId(module="mod2", name="m", ordinal=1): FnSignature(
			name="mod2::m#1",
			method_name="m",
			param_type_ids=[int_ty],
			return_type_id=int_ty,
			is_method=True,
			self_mode="value",
		)
	}
	lower = _lower_typed_callsite(
		block,
		call_info_by_callsite_id=call_info_by_callsite,
		type_table=table,
		signatures_by_id=signatures_by_id,
	)
	call_targets = set()
	for b in lower.b.func.blocks.values():
		for instr in b.instructions:
			if isinstance(instr, Call):
				call_targets.add(function_symbol(instr.fn_id))
	assert "mod::foo#2" in call_targets
	assert "mod2::m#1" in call_targets


def test_mir_call_requires_can_throw_flag() -> None:
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	builder = make_builder(fn_id)
	dest = builder.new_temp()
	builder.emit(M.Call(dest=dest, fn_id=fn_id, args=[], can_throw=None))
	with pytest.raises(AssertionError, match=r"missing can_throw"):
		validate_mir_call_invariants({fn_id: builder.func})
