# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

import lang.driftc.stage1.hir_nodes as H
from lang.driftc.stage2 import HIRToMIR, mir_nodes as M, make_builder
from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.types_core import TypeTable


def test_lambda_immediate_call_lowered_inline_with_param_and_capture() -> None:
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	type_table = TypeTable()
	lower = HIRToMIR(builder, type_table=type_table)
	# let x = 1; (outer capture)
	let_outer = H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1)
	# (|x| => x + outer_x)(2) where parameter x shadows outer x
	lambda_expr = H.HLambda(
		params=[H.HParam(name="x", binding_id=10)],
		body_expr=H.HBinary(
			op=H.BinaryOp.ADD,
			left=H.HVar(name="x", binding_id=1),
			right=H.HVar(name="x", binding_id=10),
		),
	)
	call_expr = H.HCall(fn=lambda_expr, args=[H.HLiteralInt(2)], kwargs=[])
	ret = H.HReturn(value=call_expr)
	block = H.HBlock(statements=[let_outer, H.HExprStmt(expr=call_expr), ret])

	lower.lower_block(block)
	instrs = builder.func.blocks["entry"].instructions
	assert any(isinstance(i, M.ConstructStruct) for i in instrs)
	call_instrs = [i for i in instrs if isinstance(i, M.Call)]
	assert call_instrs
	assert function_symbol(call_instrs[-1].fn_id).split("::")[-1].startswith("__lambda_")
	specs = lower.hidden_lambda_specs()
	assert specs
	spec = specs[0]
	assert spec.has_captures is True
	assert spec.param_names and spec.param_names[0].startswith("__env_")
	assert spec.capture_map
	assert len(spec.capture_map) == len(spec.lambda_expr.captures)
	assert not lambda_expr.captures


def test_lambda_block_body_returns_trailing_expr() -> None:
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	type_table = TypeTable()
	lower = HIRToMIR(builder, type_table=type_table)
	block_body = H.HBlock(statements=[H.HExprStmt(expr=H.HLiteralInt(7))])
	lambda_expr = H.HLambda(params=[], body_expr=None, body_block=block_body)
	call_expr = H.HCall(fn=lambda_expr, args=[], kwargs=[])
	lower.lower_block(H.HBlock(statements=[H.HExprStmt(expr=call_expr)]))

	specs = lower.hidden_lambda_specs()
	assert specs
	assert specs[0].has_captures is False
	assert not specs[0].capture_map
	assert not specs[0].param_names or not specs[0].param_names[0].startswith("__env_")


def test_lambda_block_body_explicit_return_is_respected() -> None:
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	type_table = TypeTable()
	lower = HIRToMIR(builder, type_table=type_table)
	block_body = H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(3))])
	lambda_expr = H.HLambda(params=[], body_expr=None, body_block=block_body)
	call_expr = H.HCall(fn=lambda_expr, args=[], kwargs=[])
	lower.lower_block(H.HBlock(statements=[H.HExprStmt(expr=call_expr)]))

	specs = lower.hidden_lambda_specs()
	assert specs
	assert specs[0].has_captures is False
	assert not specs[0].capture_map
	assert not specs[0].param_names or not specs[0].param_names[0].startswith("__env_")
