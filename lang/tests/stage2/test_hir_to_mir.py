# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage2 (HIR → MIR) unit tests.

These cover the currently supported lowering:
  - straight-line code (literals/vars/unary/binary/field/index)
  - let/assign/expr/return
  - `if` and `loop` with break/continue
  - basic calls/method calls/DV construction

The tests build HIR by hand and assert on the MIR blocks/instructions emitted
by HIRToMIR + MirBuilder.
"""

from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage1 import (
	HBlock,
	HLet,
	HAssign,
	HReturn,
	HBinary,
	HVar,
	HLiteralInt,
	HLiteralFloat,
	HIf,
	HLoop,
	HBreak,
	HContinue,
	HCall,
	HDVInit,
	HFString,
	HFStringHole,
	HExprStmt,
	HQualifiedMember,
	HKwArg,
	BinaryOp,
	assign_node_ids,
	assign_callsite_ids,
)
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.stage2 import (
	HIRToMIR,
	make_builder,
	ConstInt,
	ConstFloat,
	StringFromFloat,
	BinaryOpInstr,
	StoreLocal,
	LoadLocal,
	Return,
	IfTerminator,
	Goto,
	Call,
	ConstructDV,
)


def _fn_id(name: str) -> FunctionId:
	return FunctionId(module="main", name=name, ordinal=0)


def _build_and_lower(block: HBlock):
	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	type_table = TypeTable()
	hir_norm = normalize_hir(block)
	assign_node_ids(hir_norm)
	assign_callsite_ids(hir_norm)
	call_info_by_callsite_id: dict[int, CallInfo] = {}
	def _walk_expr(expr: object) -> None:
		if isinstance(expr, HCall):
			if isinstance(expr.fn, HVar):
				int_ty = type_table.ensure_int()
				param_types = tuple(int_ty for _ in expr.args)
				info = CallInfo(
					target=CallTarget.direct(_fn_id(expr.fn.name)),
					sig=CallSig(param_types=param_types, user_ret_type=int_ty, can_throw=False),
				)
				csid = getattr(expr, "callsite_id", None)
				if isinstance(csid, int):
					call_info_by_callsite_id[csid] = info
			for arg in list(expr.args):
				_walk_expr(arg)
			for kw in list(expr.kwargs or []):
				_walk_expr(kw.value)
			return

	def _walk_stmt(stmt: object) -> None:
		if isinstance(stmt, HExprStmt):
			_walk_expr(stmt.expr)
		elif isinstance(stmt, HLet):
			_walk_expr(stmt.value)
		elif isinstance(stmt, HAssign):
			_walk_expr(stmt.value)
		elif isinstance(stmt, HReturn):
			if stmt.value is not None:
				_walk_expr(stmt.value)
	for stmt in hir_norm.statements:
		if isinstance(stmt, HExprStmt) and isinstance(stmt.expr, HCall) and isinstance(stmt.expr.fn, HVar):
			int_ty = type_table.ensure_int()
			param_types = tuple(int_ty for _ in stmt.expr.args)
			info = CallInfo(
				target=CallTarget.direct(_fn_id(stmt.expr.fn.name)),
				sig=CallSig(param_types=param_types, user_ret_type=int_ty, can_throw=False),
			)
			csid = getattr(stmt.expr, "callsite_id", None)
			if isinstance(csid, int):
				call_info_by_callsite_id[csid] = info
	lowerer = HIRToMIR(
		builder,
		type_table=type_table,
		call_info_by_callsite_id=call_info_by_callsite_id,
	)
	lowerer.lower_block(hir_norm)
	return builder.func


def _lower_with_call_info(block: HBlock, type_table: TypeTable) -> object:
	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	hir_norm = normalize_hir(block)
	assign_node_ids(hir_norm)
	assign_callsite_ids(hir_norm)
	call_info_by_callsite_id: dict[int, CallInfo] = {}

	def _walk_expr(expr: object) -> None:
		if isinstance(expr, HCall):
			if isinstance(expr.fn, HVar):
				int_ty = type_table.ensure_int()
				param_types = tuple(int_ty for _ in expr.args)
				info = CallInfo(
					target=CallTarget.direct(_fn_id(expr.fn.name)),
					sig=CallSig(param_types=param_types, user_ret_type=int_ty, can_throw=False),
				)
				csid = getattr(expr, "callsite_id", None)
				if isinstance(csid, int):
					call_info_by_callsite_id[csid] = info
			elif isinstance(expr.fn, HQualifiedMember):
				base_te = expr.fn.base_type_expr
				base_tid = resolve_opaque_type(base_te, type_table, module_id=getattr(base_te, "module_id", None))
				inst_tid = base_tid
				if type_table.get_variant_instance(inst_tid) is None:
					inst_tid = type_table.ensure_instantiated(base_tid, [])
				inst = type_table.get_variant_instance(inst_tid)
				if inst is not None:
					arm_def = inst.arms_by_name.get(expr.fn.member)
					if arm_def is not None:
						info = CallInfo(
							target=CallTarget.constructor(inst_tid, expr.fn.member),
							sig=CallSig(param_types=tuple(arm_def.field_types), user_ret_type=inst_tid, can_throw=False),
						)
						csid = getattr(expr, "callsite_id", None)
						if isinstance(csid, int):
							call_info_by_callsite_id[csid] = info
			for arg in list(expr.args):
				_walk_expr(arg)
			for kw in list(expr.kwargs or []):
				_walk_expr(kw.value)
			return

	def _walk_stmt(stmt: object) -> None:
		if isinstance(stmt, HExprStmt):
			_walk_expr(stmt.expr)
		elif isinstance(stmt, HLet):
			_walk_expr(stmt.value)
		elif isinstance(stmt, HAssign):
			_walk_expr(stmt.value)
		elif isinstance(stmt, HReturn):
			if stmt.value is not None:
				_walk_expr(stmt.value)

	for stmt in hir_norm.statements:
		_walk_stmt(stmt)

	lowerer = HIRToMIR(
		builder,
		type_table=type_table,
		call_info_by_callsite_id=call_info_by_callsite_id,
	)
	lowerer.lower_block(hir_norm)
	return builder.func


def test_straight_line():
	# let x = 1; x = x + 2; return x;
	block = HBlock(
		statements=[
			HLet(name="x", value=HLiteralInt(1)),
			HAssign(
				target=HVar("x"),
				value=HBinary(op=BinaryOp.ADD, left=HVar("x"), right=HLiteralInt(2)),
			),
			HReturn(value=HVar("x")),
		]
	)
	func = _build_and_lower(block)
	entry = func.blocks[func.entry]
	ops = entry.instructions
	assert any(isinstance(op, ConstInt) for op in ops)
	assert any(isinstance(op, StoreLocal) for op in ops)
	assert any(isinstance(op, BinaryOpInstr) for op in ops)
	assert isinstance(entry.terminator, Return)


def test_if_lowering():
	# if (x) { x = x + 1; }
	block = HBlock(
		statements=[
			HIf(
				cond=HVar("x"),
				then_block=HBlock(
					statements=[
						HAssign(
							target=HVar("x"),
							value=HBinary(op=BinaryOp.ADD, left=HVar("x"), right=HLiteralInt(1)),
						)
					]
				),
				else_block=None,
			)
		]
	)
	func = _build_and_lower(block)
	assert "if_then" in func.blocks
	assert "if_join" in func.blocks
	entry = func.blocks[func.entry]
	assert isinstance(entry.terminator, IfTerminator)
	then_block = func.blocks["if_then"]
	assert isinstance(then_block.terminator, Goto)


def test_variant_ctor_args_eval_order_is_source_order() -> None:
	type_table = TypeTable()
	int_ty = type_table.ensure_int()

	pair_base = type_table.declare_variant(
		module_id="main",
		name="Pair",
		type_params=[],
		arms=[
			VariantArmSchema(
				name="Mk",
				fields=[
					VariantFieldSchema(name="a", type_expr=GenericTypeExpr.named("Int")),
					VariantFieldSchema(name="b", type_expr=GenericTypeExpr.named("Int")),
				],
			)
		],
	)
	type_table.ensure_instantiated(pair_base, [])

	pair_te = TypeExpr(name="Pair", module_id="main")
	ctor = HQualifiedMember(base_type_expr=pair_te, member="Mk")
	call = HCall(
		fn=ctor,
		args=[
			HCall(fn=HVar("f"), args=[HLiteralInt(1)], kwargs=[]),
			HCall(fn=HVar("f"), args=[HLiteralInt(2)], kwargs=[]),
		],
		kwargs=[],
	)
	block = HBlock(statements=[HExprStmt(expr=call)])
	assign_node_ids(block)
	assign_callsite_ids(block)
	callsite_id = getattr(call, "callsite_id", None)
	call_info_by_callsite_id: dict[int, CallInfo] = {}
	def _walk_expr(expr: object) -> None:
		if isinstance(expr, HCall):
			if isinstance(expr.fn, HVar):
				int_ty = type_table.ensure_int()
				param_types = tuple(int_ty for _ in expr.args)
				info = CallInfo(
					target=CallTarget.direct(_fn_id(expr.fn.name)),
					sig=CallSig(param_types=param_types, user_ret_type=int_ty, can_throw=False),
				)
				csid = getattr(expr, "callsite_id", None)
				if isinstance(csid, int):
					call_info_by_callsite_id[csid] = info
			for arg in list(expr.args):
				_walk_expr(arg)
			for kw in list(expr.kwargs or []):
				_walk_expr(kw.value)
			return

	def _walk_stmt(stmt: object) -> None:
		if isinstance(stmt, HExprStmt):
			_walk_expr(stmt.expr)
		elif isinstance(stmt, HLet):
			_walk_expr(stmt.value)
		elif isinstance(stmt, HAssign):
			_walk_expr(stmt.value)
		elif isinstance(stmt, HReturn):
			if stmt.value is not None:
				_walk_expr(stmt.value)
	inst_id = type_table.ensure_instantiated(pair_base, [])
	inst = type_table.get_variant_instance(inst_id)
	assert inst is not None
	arm_def = inst.arms_by_name["Mk"]
	info = CallInfo(
		target=CallTarget.constructor(
			inst_id,
			"Mk",
			ctor_arg_field_indices=(1, 0),
		),
		sig=CallSig(param_types=tuple(arm_def.field_types), user_ret_type=inst_id, can_throw=False),
	)
	if isinstance(callsite_id, int):
		call_info_by_callsite_id[callsite_id] = info
	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	hir_norm = normalize_hir(block)
	for stmt in hir_norm.statements:
		_walk_stmt(stmt)
	lowerer = HIRToMIR(builder, type_table=type_table, call_info_by_callsite_id=call_info_by_callsite_id)
	lowerer.lower_block(hir_norm)
	func = builder.func
	entry = func.blocks[func.entry]
	const_vals = {op.dest: op.value for op in entry.instructions if isinstance(op, ConstInt)}
	calls = [op for op in entry.instructions if isinstance(op, Call) and op.fn_id.name == "f"]
	assert len(calls) == 2
	assert const_vals[calls[0].args[0]] == 1
	assert const_vals[calls[1].args[0]] == 2


def test_loop_and_break_continue():
	# loop { continue; break; }
	loop_block = HBlock(statements=[HContinue(), HBreak()])
	block = HBlock(statements=[HLoop(body=loop_block)])
	func = _build_and_lower(block)
	assert "loop_header" in func.blocks
	assert "loop_body" in func.blocks
	assert "loop_exit" in func.blocks
	body_term = func.blocks["loop_body"].terminator
	assert isinstance(body_term, Goto)


def test_calls_and_dv():
	# f(1); MyDV(3)
	block = HBlock(
		statements=[
			HExprStmt(expr=HCall(fn=HVar("f"), args=[HLiteralInt(1)])),
			HExprStmt(expr=HDVInit(dv_type_name="MyDV", args=[HLiteralInt(3)])),
		]
	)
	func = _build_and_lower(block)
	entry = func.blocks[func.entry]
	kinds = {type(instr) for instr in entry.instructions}
	assert Call in kinds
	assert ConstructDV in kinds


def test_float_literal_lowers_to_const_float():
	block = HBlock(statements=[HExprStmt(expr=HLiteralFloat(1.25))])
	func = _build_and_lower(block)
	entry = func.blocks[func.entry]
	assert any(isinstance(op, ConstFloat) for op in entry.instructions)


def test_fstring_float_hole_emits_string_from_float():
	f = HFString(parts=["x=", ""], holes=[HFStringHole(expr=HLiteralFloat(1.25))])
	block = HBlock(statements=[HExprStmt(expr=f)])
	func = _build_and_lower(block)
	entry = func.blocks[func.entry]
	assert any(isinstance(op, StringFromFloat) for op in entry.instructions)


def test_call_with_non_var_fn_raises():
	call_hir = HCall(fn=HBinary(op=BinaryOp.ADD, left=HLiteralInt(1), right=HLiteralInt(2)), args=[])
	with pytest.raises(NotImplementedError):
		_build_and_lower(HBlock(statements=[HExprStmt(expr=call_hir)]))
