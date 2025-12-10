#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Expression typing coverage: unary/binary/index/array/ternary."""

from lang2 import stage1 as H
from lang2.type_checker import TypeChecker
from lang2.core.types_core import TypeTable, TypeKind
from lang2.checker import FnSignature
from lang2.method_registry import CallableRegistry, CallableSignature, Visibility, SelfMode


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


def test_call_return_type_uses_signature():
	table = TypeTable()
	tc = TypeChecker(table)
	ret_ty = table.ensure_string()
	sig = FnSignature(name="foo", return_type_id=ret_ty, param_type_ids=[table.ensure_int()])
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=H.HCall(fn=H.HVar("foo"), args=[H.HVar("x", binding_id=1)])),
		]
	)
	res = tc.check_function("c", block, param_types=None, call_signatures={"foo": sig})
	assert res.diagnostics == []
	assert ret_ty in res.typed_fn.expr_types.values()


def test_call_resolution_uses_registry_and_types():
	table = TypeTable()
	tc = TypeChecker(table)
	int_ty = table.ensure_int()
	ret_ty = table.ensure_string()
	registry = CallableRegistry()
	registry.register_free_function(
		callable_id=1,
		name="foo",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(int_ty,), result_type=ret_ty),
	)
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=H.HCall(fn=H.HVar("foo"), args=[H.HVar("x", binding_id=1)])),
		]
	)
	res = tc.check_function(
		"c",
		block,
		param_types={"x": int_ty},
		callable_registry=registry,
		visible_modules=(0,),
		current_module=0,
	)
	assert res.diagnostics == []
	assert ret_ty in res.typed_fn.expr_types.values()


def test_method_resolution_uses_registry_and_types():
	table = TypeTable()
	tc = TypeChecker(table)
	int_ty = table.ensure_int()
	str_ty = table.ensure_string()
	registry = CallableRegistry()
	recv_ty = table.ensure_ref(int_ty)
	registry.register_inherent_method(
		callable_id=1,
		name="m",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(recv_ty,), result_type=str_ty),
		impl_id=1,
		impl_target_type_id=int_ty,
		self_mode=SelfMode.SELF_BY_REF,
	)
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=H.HMethodCall(receiver=H.HVar("x", binding_id=1), method_name="m", args=[])),
		]
	)
	res = tc.check_function(
		"mcall",
		block,
		param_types={"x": int_ty},
		callable_registry=registry,
		visible_modules=(0,),
		current_module=0,
	)
	assert res.diagnostics == []
	assert str_ty in res.typed_fn.expr_types.values()


def test_result_ok_uses_fnresult_type():
	tc = _tc()
	block = H.HBlock(statements=[H.HExprStmt(expr=H.HResultOk(value=H.HLiteralInt(1)))])
	res = tc.check_function("res", block)
	assert res.diagnostics == []
	assert any(tc.type_table.get(ty).kind is TypeKind.FNRESULT for ty in res.typed_fn.expr_types.values())


def test_try_result_propagates_inner_type():
	tc = _tc()
	block = H.HBlock(statements=[H.HExprStmt(expr=H.HTryResult(expr=H.HLiteralInt(2)))])
	res = tc.check_function("tr", block)
	assert res.diagnostics == []
	assert tc.type_table.ensure_int() in res.typed_fn.expr_types.values()
