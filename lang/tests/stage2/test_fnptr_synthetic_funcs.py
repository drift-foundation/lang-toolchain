# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.types_core import TypeTable
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.parser.ast import TypeExpr


def _fn_type_expr(param_names: list[str], ret_name: str, *, nothrow: bool | None = None) -> TypeExpr:
	args = [TypeExpr(name=p) for p in param_names]
	args.append(TypeExpr(name=ret_name))
	return TypeExpr(name="fn", args=args, fn_throws=(not bool(nothrow)))


def test_thunk_ok_wrap_function_is_emitted() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	add1_id = FunctionId(module="main", name="add1", ordinal=0)
	main_id = FunctionId(module="main", name="main", ordinal=0)
	add1_block = H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(1))])
	main_block = H.HBlock(
		statements=[
			H.HLet(
				name="f",
				value=H.HVar("add1"),
				declared_type_expr=_fn_type_expr(["Int"], "Int"),
			),
			H.HReturn(value=H.HLiteralInt(0)),
		]
	)
	sigs = {
		add1_id: FnSignature(name="add1", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False),
		main_id: FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	mir_funcs = compile_stubbed_funcs(
		{add1_id: add1_block, main_id: main_block},
		signatures=sigs,
		type_table=table,
	)
	assert any("__thunk_ok_wrap" in function_symbol(fn_id) for fn_id in mir_funcs)


def test_captureless_lambda_function_is_emitted() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	main_id = FunctionId(module="main", name="main", ordinal=0)
	lam = H.HLambda(params=[H.HParam(name="x")], body_expr=H.HVar(name="x"), body_block=None)
	main_block = H.HBlock(
		statements=[
			H.HLet(
				name="f",
				value=lam,
				declared_type_expr=_fn_type_expr(["Int"], "Int", nothrow=True),
			),
			H.HReturn(value=H.HLiteralInt(0)),
		]
	)
	sigs = {main_id: FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False)}
	mir_funcs = compile_stubbed_funcs(
		{main_id: main_block},
		signatures=sigs,
		type_table=table,
	)
	assert any("__lambda_fn_" in function_symbol(fn_id) for fn_id in mir_funcs)


def test_captureless_lambda_functions_are_unique_per_enclosing_fn() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	main_id = FunctionId(module="main", name="main", ordinal=0)
	other_id = FunctionId(module="main", name="other", ordinal=0)
	lam_main = H.HLambda(params=[H.HParam(name="x")], body_expr=H.HVar(name="x"), body_block=None)
	lam_other = H.HLambda(params=[H.HParam(name="y")], body_expr=H.HVar(name="y"), body_block=None)
	main_block = H.HBlock(
		statements=[
			H.HLet(
				name="f",
				value=lam_main,
				declared_type_expr=_fn_type_expr(["Int"], "Int", nothrow=True),
			),
			H.HReturn(value=H.HLiteralInt(0)),
		]
	)
	other_block = H.HBlock(
		statements=[
			H.HLet(
				name="g",
				value=lam_other,
				declared_type_expr=_fn_type_expr(["Int"], "Int", nothrow=True),
			),
			H.HReturn(value=H.HLiteralInt(0)),
		]
	)
	sigs = {
		main_id: FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
		other_id: FnSignature(name="other", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	mir_funcs = compile_stubbed_funcs(
		{main_id: main_block, other_id: other_block},
		signatures=sigs,
		type_table=table,
	)
	lambda_names = [function_symbol(fn_id) for fn_id in mir_funcs if "__lambda_fn_" in function_symbol(fn_id)]
	assert len(lambda_names) == 2
