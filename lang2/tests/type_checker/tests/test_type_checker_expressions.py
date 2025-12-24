#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Expression typing coverage: unary/binary/index/array/ternary."""

from lang2.driftc import stage1 as H
from lang2.driftc.type_checker import TypeChecker
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.core.types_core import TypeKind, TypeParamId, TypeTable
from lang2.driftc.checker import FnSignature, TypeParam
from lang2.driftc.method_registry import CallableRegistry, CallableSignature, Visibility, SelfMode
from lang2.driftc.method_resolver import MethodResolution
from lang2.driftc.parser import ast as parser_ast
from lang2.driftc.core.span import Span


def _tc() -> TypeChecker:
	return TypeChecker(TypeTable())


def _fn_id(name: str) -> FunctionId:
	return FunctionId(module="main", name=name, ordinal=0)


def test_binary_int_ops_infer_int():
	tc = _tc()
	block = H.HBlock(
		statements=[
			H.HExprStmt(
				expr=H.HBinary(left=H.HLiteralInt(1), op=H.BinaryOp.ADD, right=H.HLiteralInt(2)),
			)
		]
	)
	res = tc.check_function(_fn_id("f"), block)
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
	res = tc.check_function(_fn_id("g"), block)
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
	res = tc.check_function(_fn_id("u"), block)
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
	res = tc.check_function(_fn_id("arr"), block)
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
	res = tc.check_function(_fn_id("tern"), block)
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
	res = tc.check_function(_fn_id("c"), block, param_types=None, call_signatures={"foo": sig})
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
		_fn_id("c"),
		block,
		param_types={"x": int_ty},
		callable_registry=registry,
		visible_modules=(0,),
		current_module=0,
	)
	assert res.diagnostics == []
	assert ret_ty in res.typed_fn.expr_types.values()


def test_call_with_explicit_type_args_instantiates_signature():
	table = TypeTable()
	tc = TypeChecker(table)
	fn_id = _fn_id("id")
	type_param_id = TypeParamId(owner=fn_id, index=0)
	type_param = TypeParam(id=type_param_id, name="T", span=Span())
	type_var = table.ensure_typevar(type_param_id, name="T")
	sig = FnSignature(
		name="id",
		return_type_id=type_var,
		param_type_ids=[type_var],
		type_params=[type_param],
		module="main",
	)
	registry = CallableRegistry()
	registry.register_free_function(
		callable_id=1,
		name="id",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(type_var,), result_type=type_var),
		fn_id=fn_id,
		is_generic=True,
	)
	call = H.HCall(
		fn=H.HVar("id"),
		args=[H.HLiteralInt(1)],
		type_args=[
			parser_ast.TypeExpr(
				name="Int",
				args=[],
				module_alias=None,
				module_id=None,
				loc=parser_ast.Located(line=1, column=1),
			)
		],
	)
	block = H.HBlock(statements=[H.HExprStmt(expr=call)])
	res = tc.check_function(
		_fn_id("caller"),
		block,
		callable_registry=registry,
		signatures_by_id={fn_id: sig},
		visible_modules=(0,),
		current_module=0,
	)
	assert res.diagnostics == []
	assert table.ensure_int() in res.typed_fn.expr_types.values()


def test_call_infers_type_args_from_args():
	table = TypeTable()
	tc = TypeChecker(table)
	fn_id = _fn_id("id")
	type_param_id = TypeParamId(owner=fn_id, index=0)
	type_param = TypeParam(id=type_param_id, name="T", span=Span())
	type_var = table.ensure_typevar(type_param_id, name="T")
	sig = FnSignature(
		name="id",
		return_type_id=type_var,
		param_type_ids=[type_var],
		type_params=[type_param],
		module="main",
	)
	registry = CallableRegistry()
	registry.register_free_function(
		callable_id=1,
		name="id",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(type_var,), result_type=type_var),
		fn_id=fn_id,
		is_generic=True,
	)
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HCall(fn=H.HVar("id"), args=[H.HLiteralInt(1)])),
		]
	)
	res = tc.check_function(
		_fn_id("caller"),
		block,
		callable_registry=registry,
		signatures_by_id={fn_id: sig},
		visible_modules=(0,),
		current_module=0,
	)
	assert res.diagnostics == []
	assert table.ensure_int() in res.typed_fn.expr_types.values()


def test_call_infers_type_args_from_expected_return_type():
	table = TypeTable()
	tc = TypeChecker(table)
	fn_id = _fn_id("make")
	type_param_id = TypeParamId(owner=fn_id, index=0)
	type_param = TypeParam(id=type_param_id, name="T", span=Span())
	type_var = table.ensure_typevar(type_param_id, name="T")
	sig = FnSignature(
		name="make",
		return_type_id=type_var,
		param_type_ids=[],
		type_params=[type_param],
		module="main",
	)
	registry = CallableRegistry()
	registry.register_free_function(
		callable_id=1,
		name="make",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(), result_type=type_var),
		fn_id=fn_id,
		is_generic=True,
	)
	block = H.HBlock(
		statements=[
			H.HReturn(value=H.HCall(fn=H.HVar("make"), args=[])),
		]
	)
	res = tc.check_function(
		_fn_id("caller"),
		block,
		return_type=table.ensure_int(),
		callable_registry=registry,
		signatures_by_id={fn_id: sig},
		visible_modules=(0,),
		current_module=0,
	)
	assert res.diagnostics == []
	assert table.ensure_int() in res.typed_fn.expr_types.values()


def test_method_call_with_explicit_type_args_instantiates_signature():
	table = TypeTable()
	tc = TypeChecker(table)
	recv_ty = table.ensure_int()
	type_param_id = TypeParamId(owner=_fn_id("m"), index=0)
	type_param = TypeParam(id=type_param_id, name="T", span=Span())
	type_var = table.ensure_typevar(type_param_id, name="T")
	sig = FnSignature(
		name="m",
		param_type_ids=[recv_ty, type_var],
		return_type_id=type_var,
		type_params=[type_param],
		module="main",
	)
	registry = CallableRegistry()
	registry.register_inherent_method(
		callable_id=1,
		name="m",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(recv_ty, type_var), result_type=type_var),
		fn_id=_fn_id("m"),
		impl_id=1,
		impl_target_type_id=recv_ty,
		self_mode=SelfMode.SELF_BY_VALUE,
		is_generic=True,
	)
	call = H.HMethodCall(
		receiver=H.HVar("x", binding_id=1),
		method_name="m",
		args=[H.HLiteralInt(1)],
		type_args=[
			parser_ast.TypeExpr(
				name="Int",
				args=[],
				module_alias=None,
				module_id=None,
				loc=parser_ast.Located(line=1, column=1),
			)
		],
	)
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=call),
		]
	)
	res = tc.check_function(
		_fn_id("caller"),
		block,
		param_types={"x": recv_ty},
		callable_registry=registry,
		signatures_by_id={_fn_id("m"): sig},
		visible_modules=(0,),
		current_module=0,
	)
	assert res.diagnostics == []
	assert res.typed_fn.expr_types.get(id(call)) == table.ensure_int()


def test_method_type_args_skip_nongeneric_candidate():
	table = TypeTable()
	tc = TypeChecker(table)
	recv_ty = table.ensure_int()
	fn_id_gen = _fn_id("m")
	fn_id_plain = _fn_id("m_plain")

	tp_id = TypeParamId(owner=fn_id_gen, index=0)
	tp = TypeParam(id=tp_id, name="T", span=Span())
	tp_ty = table.ensure_typevar(tp_id, name="T")

	sig_plain = FnSignature(
		name="m",
		param_type_ids=[recv_ty, table.ensure_int()],
		return_type_id=table.ensure_int(),
		type_params=[],
		module="main",
	)
	sig_gen = FnSignature(
		name="m",
		param_type_ids=[recv_ty, tp_ty],
		return_type_id=tp_ty,
		type_params=[tp],
		module="main",
	)

	registry = CallableRegistry()
	registry.register_inherent_method(
		callable_id=1,
		name="m",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(recv_ty, table.ensure_int()), result_type=table.ensure_int()),
		fn_id=fn_id_plain,
		impl_id=1,
		impl_target_type_id=recv_ty,
		self_mode=SelfMode.SELF_BY_VALUE,
		is_generic=False,
	)
	registry.register_inherent_method(
		callable_id=2,
		name="m",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(recv_ty, tp_ty), result_type=tp_ty),
		fn_id=fn_id_gen,
		impl_id=1,
		impl_target_type_id=recv_ty,
		self_mode=SelfMode.SELF_BY_VALUE,
		is_generic=True,
	)

	call = H.HMethodCall(
		receiver=H.HVar("x", binding_id=1),
		method_name="m",
		args=[H.HLiteralInt(1)],
		type_args=[
			parser_ast.TypeExpr(
				name="Int",
				args=[],
				module_alias=None,
				module_id=None,
				loc=parser_ast.Located(line=1, column=1),
			)
		],
	)
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=call),
		]
	)
	res = tc.check_function(
		_fn_id("caller"),
		block,
		param_types={"x": recv_ty},
		callable_registry=registry,
		signatures_by_id={fn_id_plain: sig_plain, fn_id_gen: sig_gen},
		visible_modules=(0,),
		current_module=0,
	)
	assert res.diagnostics == []
	assert res.typed_fn.expr_types.get(id(call)) == table.ensure_int()

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
		_fn_id("mcall"),
		block,
		param_types={"x": int_ty},
		callable_registry=registry,
		visible_modules=(0,),
		current_module=0,
	)
	assert res.diagnostics == []
	assert str_ty in res.typed_fn.expr_types.values()
	# Verify resolved callee metadata is recorded on the call.
	call_expr = block.statements[1].expr
	resolution = res.typed_fn.call_resolutions.get(id(call_expr))
	assert isinstance(resolution, MethodResolution)
	assert resolution.decl.callable_id == 1
	assert resolution.decl.signature.result_type == str_ty
	assert resolution.receiver_autoborrow == SelfMode.SELF_BY_REF


def test_result_ok_uses_fnresult_type():
	tc = _tc()
	block = H.HBlock(statements=[H.HExprStmt(expr=H.HResultOk(value=H.HLiteralInt(1)))])
	res = tc.check_function(_fn_id("res"), block)
	assert res.diagnostics == []
	assert any(tc.type_table.get(ty).kind is TypeKind.FNRESULT for ty in res.typed_fn.expr_types.values())
