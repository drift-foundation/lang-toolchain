#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Expression typing coverage: unary/binary/index/array/ternary."""

from lang2 import stage1 as H
from lang2.type_checker import TypeChecker
from lang2.core.types_core import TypeTable


def _tc() -> TypeChecker:
	return TypeChecker(TypeTable())


def test_binary_int_ops_infer_int():
	tc = _tc()
	block = H.HBlock(
		statements=[
			H.HExprStmt(
				expr=H.HBinary(left=H.HLiteralInt(1), op=H.BinaryOp.ADD, right=H.HLiteralInt(2)),
			)
		]
	)
	res = tc.check_function("f", block)
	assert res.diagnostics == []
	assert tc.type_table.ensure_int() in res.typed_fn.expr_types.values()


def test_logical_ops_infer_bool():
	tc = _tc()
	block = H.HBlock(
		statements=[
			H.HExprStmt(
				expr=H.HBinary(left=H.HLiteralBool(True), op=H.BinaryOp.AND, right=H.HLiteralBool(False)),
			)
		]
	)
	res = tc.check_function("g", block)
	assert res.diagnostics == []
	assert tc.type_table.ensure_bool() in res.typed_fn.expr_types.values()


def test_unary_ops():
	tc = _tc()
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HUnary(op=H.UnaryOp.NEG, expr=H.HLiteralInt(1))),
			H.HExprStmt(expr=H.HUnary(op=H.UnaryOp.NOT, expr=H.HLiteralBool(True))),
		]
	)
	res = tc.check_function("u", block)
	assert res.diagnostics == []
	assert tc.type_table.ensure_int() in res.typed_fn.expr_types.values()
	assert tc.type_table.ensure_bool() in res.typed_fn.expr_types.values()


def test_array_literal_and_index():
	tc = _tc()
	block = H.HBlock(
		statements=[
			H.HLet(
				name="xs",
				value=H.HArrayLiteral(elements=[H.HLiteralInt(1), H.HLiteralInt(2)]),
				declared_type_expr=None,
			),
			H.HExprStmt(expr=H.HIndex(subject=H.HVar("xs"), index=H.HLiteralInt(0))),
		]
	)
	res = tc.check_function("arr", block)
	assert res.diagnostics == []
	int_ty = tc.type_table.ensure_int()
	arr_ty = tc.type_table.new_array(int_ty)
	assert arr_ty in res.typed_fn.expr_types.values()
	assert int_ty in res.typed_fn.expr_types.values()


def test_ternary_prefers_common_type():
	tc = _tc()
	block = H.HBlock(
		statements=[
			H.HExprStmt(
				expr=H.HTernary(
					cond=H.HLiteralBool(True),
					then_expr=H.HLiteralInt(1),
					else_expr=H.HLiteralInt(2),
				)
			)
		]
	)
	res = tc.check_function("tern", block)
	assert res.diagnostics == []
	assert tc.type_table.ensure_int() in res.typed_fn.expr_types.values()
