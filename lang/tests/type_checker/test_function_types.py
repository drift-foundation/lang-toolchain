# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

"""Unit tests for the type checker (not Stage-1 pipeline validation)."""

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility
from lang.driftc.checker import FnSignature
from lang.driftc.type_checker import TypeChecker


def _fn_id(name: str) -> FunctionId:
	return FunctionId(module="main", name=name, ordinal=0)


def test_pretty_function_type_name():
	table = TypeTable()
	tc = TypeChecker(table)
	int_ty = table.ensure_int()
	fn_nothrow = table.ensure_function([int_ty], int_ty, can_throw=False)
	fn_can_throw = table.ensure_function([int_ty], int_ty, can_throw=True)

	assert tc._pretty_type_name(fn_nothrow, current_module="main") == "Fn(Int) nothrow -> Int"
	assert tc._pretty_type_name(fn_can_throw, current_module="main") == "Fn(Int) -> Int"


def test_function_type_identity_includes_throw_mode():
	table = TypeTable()
	int_ty = table.ensure_int()
	fn_nothrow = table.ensure_function([int_ty], int_ty, can_throw=False)
	fn_nothrow_2 = table.ensure_function([int_ty], int_ty, can_throw=False)
	fn_can_throw = table.ensure_function([int_ty], int_ty, can_throw=True)
	fn_can_throw_2 = table.ensure_function([int_ty], int_ty, can_throw=True)

	assert fn_nothrow == fn_nothrow_2
	assert fn_can_throw == fn_can_throw_2
	assert fn_nothrow != fn_can_throw
	assert table.get(fn_nothrow).fn_throws is False
	assert table.get(fn_can_throw).fn_throws is True


def test_call_resolution_respects_function_throw_mode():
	table = TypeTable()
	tc = TypeChecker(table)
	int_ty = table.ensure_int()
	bool_ty = table.ensure_bool()
	fn_nothrow = table.ensure_function([int_ty], int_ty, can_throw=False)
	fn_can_throw = table.ensure_function([int_ty], int_ty, can_throw=True)

	registry = CallableRegistry()
	takes_nothrow_id = FunctionId(module="main", name="takes", ordinal=0)
	takes_can_throw_id = FunctionId(module="main", name="takes", ordinal=1)
	sig_nothrow = FnSignature(
		name="takes",
		param_type_ids=[fn_nothrow],
		return_type_id=bool_ty,
		declared_can_throw=False,
	)
	sig_can_throw = FnSignature(
		name="takes",
		param_type_ids=[fn_can_throw],
		return_type_id=int_ty,
		declared_can_throw=False,
	)
	registry.register_free_function(
		callable_id=1,
		name="takes",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(fn_nothrow,), result_type=bool_ty),
		fn_id=takes_nothrow_id,
	)
	registry.register_free_function(
		callable_id=2,
		name="takes",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(fn_can_throw,), result_type=int_ty),
		fn_id=takes_can_throw_id,
	)

	call = H.HCall(fn=H.HVar("takes"), args=[H.HVar("g")])
	block = H.HBlock(statements=[H.HExprStmt(expr=call)])
	res = tc.check_function(
		_fn_id("caller"),
		block,
		param_types={"g": fn_nothrow},
		callable_registry=registry,
		signatures_by_id={takes_nothrow_id: sig_nothrow, takes_can_throw_id: sig_can_throw},
		visible_modules=(0,),
		current_module=0,
	)
	assert res.diagnostics == []
	resolutions = list(res.typed_fn.call_resolutions.values())
	assert len(resolutions) == 1
	assert resolutions[0].signature.result_type == bool_ty


def test_call_resolution_respects_function_throw_mode_can_throw():
	table = TypeTable()
	tc = TypeChecker(table)
	int_ty = table.ensure_int()
	bool_ty = table.ensure_bool()
	fn_nothrow = table.ensure_function([int_ty], int_ty, can_throw=False)
	fn_can_throw = table.ensure_function([int_ty], int_ty, can_throw=True)

	registry = CallableRegistry()
	takes_nothrow_id = FunctionId(module="main", name="takes", ordinal=0)
	takes_can_throw_id = FunctionId(module="main", name="takes", ordinal=1)
	sig_nothrow = FnSignature(
		name="takes",
		param_type_ids=[fn_nothrow],
		return_type_id=bool_ty,
		declared_can_throw=False,
	)
	sig_can_throw = FnSignature(
		name="takes",
		param_type_ids=[fn_can_throw],
		return_type_id=int_ty,
		declared_can_throw=False,
	)
	registry.register_free_function(
		callable_id=1,
		name="takes",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(fn_nothrow,), result_type=bool_ty),
		fn_id=takes_nothrow_id,
	)
	registry.register_free_function(
		callable_id=2,
		name="takes",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(fn_can_throw,), result_type=int_ty),
		fn_id=takes_can_throw_id,
	)

	call = H.HCall(fn=H.HVar("takes"), args=[H.HVar("g")])
	block = H.HBlock(statements=[H.HExprStmt(expr=call)])
	res = tc.check_function(
		_fn_id("caller"),
		block,
		param_types={"g": fn_can_throw},
		callable_registry=registry,
		signatures_by_id={takes_nothrow_id: sig_nothrow, takes_can_throw_id: sig_can_throw},
		visible_modules=(0,),
		current_module=0,
	)
	assert res.diagnostics == []
	resolutions = list(res.typed_fn.call_resolutions.values())
	assert len(resolutions) == 1
	assert resolutions[0].signature.result_type == int_ty
