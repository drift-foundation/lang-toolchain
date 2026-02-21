# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import lang.driftc.stage1.hir_nodes as H
from lang.driftc.stage1.node_ids import assign_node_ids
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.method_registry import CallableDecl, CallableKind, CallableSignature, Visibility
from lang.driftc.method_resolver import MethodResolution
from lang.driftc.stage1.lambda_validate import validate_lambdas_non_retaining
from lang.driftc.stage1.non_retaining_analysis import analyze_non_retaining_params
from lang.driftc.type_checker import TypedFn


def test_immediate_call_lambda_allowed() -> None:
	lam = H.HLambda(params=[], body_expr=H.HLiteralInt(1), body_block=None)
	call = H.HCall(fn=lam, args=[], kwargs=[])
	res = validate_lambdas_non_retaining(call)
	assert res.diagnostics == []


def _typed_fn_with_direct_call(fn_id: FunctionId, *, param_name: str, binding_id: int = 1) -> TypedFn:
	call = H.HCall(fn=H.HVar(name=param_name, binding_id=binding_id), args=[], kwargs=[])
	body = H.HBlock(statements=[H.HExprStmt(expr=call)])
	return TypedFn(
		fn_id=fn_id,
		name=fn_id.name,
		params=[binding_id],
		param_bindings=[binding_id],
		locals=[],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={binding_id: 0},
		binding_names={binding_id: param_name},
		binding_mutable={binding_id: False},
		call_resolutions={},
	)


def _typed_method_with_direct_call(fn_id: FunctionId, *, self_name: str, param_name: str) -> TypedFn:
	self_id = 1
	param_id = 2
	call = H.HCall(fn=H.HVar(name=param_name, binding_id=param_id), args=[], kwargs=[])
	body = H.HBlock(statements=[H.HExprStmt(expr=call)])
	return TypedFn(
		fn_id=fn_id,
		name=fn_id.name,
		params=[self_id, param_id],
		param_bindings=[self_id, param_id],
		locals=[],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={self_id: 0, param_id: 0},
		binding_names={self_id: self_name, param_id: param_name},
		binding_mutable={self_id: False, param_id: False},
		call_resolutions={},
	)


def _analyze_signatures(
	signatures_by_id: dict[FunctionId, FnSignature],
	typed_fns: dict[FunctionId, TypedFn],
) -> dict[FunctionId, FnSignature]:
	return analyze_non_retaining_params(typed_fns, signatures_by_id)


def test_lambda_in_call_arg_allowed_for_nonretaining_param() -> None:
	lam = H.HLambda(params=[], body_expr=H.HVar(name="x", binding_id=1), body_block=None)
	call = H.HCall(fn=H.HVar(name="f"), args=[lam], kwargs=[])
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	signatures_by_id = {fn_id: FnSignature(name="f", param_names=["f"])}
	typed_fns = {fn_id: _typed_fn_with_direct_call(fn_id, param_name="f")}
	signatures = _analyze_signatures(signatures_by_id, typed_fns)
	assign_node_ids(call)
	call_resolutions = {
		call.node_id: CallableDecl(
			callable_id=1,
			name="f",
			kind=CallableKind.FREE_FUNCTION,
			module_id=0,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=(), result_type=0),
			fn_id=fn_id,
		)
	}
	res = validate_lambdas_non_retaining(call, signatures_by_id=signatures, call_resolutions=call_resolutions)
	assert res.diagnostics == []


def test_borrowed_capture_allowed_for_nonretaining_param() -> None:
	lam = H.HLambda(
		params=[],
		body_expr=H.HVar(name="x", binding_id=1),
		body_block=None,
		explicit_captures=[H.HExplicitCapture(name="x", kind="ref_mut", binding_id=1)],
	)
	call = H.HCall(fn=H.HVar(name="f"), args=[lam], kwargs=[])
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	signatures_by_id = {fn_id: FnSignature(name="f", param_names=["f"])}
	typed_fns = {fn_id: _typed_fn_with_direct_call(fn_id, param_name="f")}
	signatures = _analyze_signatures(signatures_by_id, typed_fns)
	assign_node_ids(call)
	call_resolutions = {
		call.node_id: CallableDecl(
			callable_id=1,
			name="f",
			kind=CallableKind.FREE_FUNCTION,
			module_id=0,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=(), result_type=0),
			fn_id=fn_id,
		)
	}
	res = validate_lambdas_non_retaining(call, signatures_by_id=signatures, call_resolutions=call_resolutions)
	assert res.diagnostics == []


def test_lambda_immediate_call_nested_in_expr_allowed() -> None:
	inner = H.HCall(fn=H.HLambda(params=[], body_expr=H.HLiteralInt(1), body_block=None), args=[], kwargs=[])
	outer = H.HCall(fn=H.HVar(name="g"), args=[inner], kwargs=[])
	res = validate_lambdas_non_retaining(outer)
	assert res.diagnostics == []


def test_lambda_immediate_method_call_allowed() -> None:
	lam = H.HLambda(params=[], body_expr=H.HVar(name="x", binding_id=1), body_block=None)
	call = H.HMethodCall(receiver=lam, method_name="call", args=[], kwargs=[])
	res = validate_lambdas_non_retaining(call)
	assert res.diagnostics == []


def _method_resolution(
	method_name: str,
	*,
	impl_target_type_id: int = 1,
	fn_id: FunctionId | None = None,
) -> MethodResolution:
	decl = CallableDecl(
		callable_id=1,
		name=method_name,
		kind=CallableKind.METHOD_INHERENT,
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(), result_type=0),
		impl_id=1,
		impl_target_type_id=impl_target_type_id,
		fn_id=fn_id,
	)
	return MethodResolution(decl=decl, receiver_autoborrow=None)


def test_lambda_in_method_call_arg_allowed_for_nonretaining_param() -> None:
	lam = H.HLambda(params=[], body_expr=H.HVar(name="x", binding_id=1), body_block=None)
	call = H.HMethodCall(receiver=H.HVar(name="xs"), method_name="for_each", args=[lam], kwargs=[])
	fn_id = FunctionId(module="main", name="Vec::for_each", ordinal=0)
	signatures_by_id = {
		fn_id: FnSignature(
			name="Vec::for_each",
			method_name="for_each",
			is_method=True,
			impl_target_type_id=1,
			param_names=["self", "f"],
		),
	}
	typed_fns = {fn_id: _typed_method_with_direct_call(fn_id, self_name="self", param_name="f")}
	signatures = _analyze_signatures(signatures_by_id, typed_fns)
	assign_node_ids(call)
	call_resolutions = {call.node_id: _method_resolution("for_each", impl_target_type_id=1, fn_id=fn_id)}
	res = validate_lambdas_non_retaining(call, signatures_by_id=signatures, call_resolutions=call_resolutions)
	assert res.diagnostics == []


def test_lambda_non_call_allowed_without_borrow_capture() -> None:
	lam = H.HLambda(params=[], body_expr=H.HLiteralInt(1), body_block=None)
	res = validate_lambdas_non_retaining(lam)
	assert res.diagnostics == []
