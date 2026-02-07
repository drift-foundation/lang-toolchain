#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Basic typed checker coverage: bindings, literals, borrows."""

from lang.driftc import stage1 as H
from lang.driftc.type_checker import TypeChecker
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeKind, TypeTable


def _checker():
	return TypeChecker(TypeTable())


def _fn_id(name: str) -> FunctionId:
	return FunctionId(module="main", name=name, ordinal=0)


def test_literal_and_var_types():
	tc = _checker()
	block = H.HBlock(statements=[H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None)])
	result = tc.check_function(_fn_id("f"), block)
	assert result.diagnostics == []
	x_binding = block.statements[0].binding_id
	assert x_binding is not None
	assert result.typed_fn.binding_for_var == {}
	assert block.statements[0].binding_id in result.typed_fn.locals
	# Var lookup
	block2 = H.HBlock(statements=[H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None), H.HExprStmt(expr=H.HVar("x"))])
	result2 = tc.check_function(_fn_id("g"), block2)
	var_expr = block2.statements[1].expr
	assert isinstance(var_expr, H.HVar)
	assert tc.type_table.ensure_int() in result2.typed_fn.expr_types.values()
	# binding_for_var should map this HVar to the same binding id as its let.
	let_binding = block2.statements[0].binding_id
	assert let_binding is not None
	assert result2.typed_fn.binding_for_var[var_expr.node_id] == let_binding


def test_borrow_types():
	tc = _checker()
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, is_mutable=True),
			H.HLet(name="r", value=H.HBorrow(subject=H.HVar("x"), is_mut=False), declared_type_expr=None),
			H.HLet(name="m", value=H.HBorrow(subject=H.HVar("x"), is_mut=True), declared_type_expr=None),
		]
	)
	res = tc.check_function(_fn_id("h"), block)
	assert res.diagnostics == []
	r_let = block.statements[1]
	m_let = block.statements[2]
	assert isinstance(r_let, H.HLet)
	assert isinstance(m_let, H.HLet)
	vals = list(res.typed_fn.expr_types.values())
	assert tc.type_table.ensure_ref(tc.type_table.ensure_int()) in vals
	assert tc.type_table.ensure_ref_mut(tc.type_table.ensure_int()) in vals
	# binding metadata captured
	assert res.typed_fn.binding_types
	assert res.typed_fn.binding_names


def test_shadowing_respects_lexical_scope():
	tc = _checker()
	outer_let = H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None)
	then_block = H.HBlock(statements=[H.HLet(name="x", value=H.HLiteralBool(True), declared_type_expr=None)])
	block = H.HBlock(
		statements=[
			outer_let,
			H.HIf(cond=H.HLiteralBool(True), then_block=then_block, else_block=H.HBlock(statements=[])),
			H.HExprStmt(expr=H.HVar("x")),
		]
	)
	res = tc.check_function(_fn_id("shadow"), block)
	assert res.diagnostics == []
	var_expr = block.statements[2].expr
	assert isinstance(var_expr, H.HVar)
	outer_bid = outer_let.binding_id
	assert outer_bid is not None
	assert res.typed_fn.binding_for_var[var_expr.node_id] == outer_bid
	assert tc.type_table.ensure_int() in res.typed_fn.expr_types.values()


def test_param_binding_and_type_tracked():
	tc = _checker()
	int_ty = tc.type_table.ensure_int()
	fn_body = H.HBlock(statements=[H.HExprStmt(expr=H.HVar("x"))])
	res = tc.check_function(_fn_id("p"), fn_body, param_types={"x": int_ty})
	assert res.diagnostics == []
	assert res.typed_fn.params
	assert res.typed_fn.param_bindings
	var_expr = fn_body.statements[0].expr
	assert isinstance(var_expr, H.HVar)
	p_binding = res.typed_fn.binding_for_var[var_expr.node_id]
	assert p_binding in res.typed_fn.param_bindings
	assert res.typed_fn.binding_types[p_binding] == int_ty


def test_binding_types_capture_ref_mut():
	tc = _checker()
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1, is_mutable=True),
			H.HLet(name="m", value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True), declared_type_expr=None, binding_id=2),
		]
	)
	res = tc.check_function(_fn_id("mutref"), block)
	assert res.diagnostics == []
	ref_ty = res.typed_fn.binding_types[2]
	td = tc.type_table.get(ref_ty)
	assert td.kind is TypeKind.REF
	assert td.ref_mut is True


def test_mut_borrow_of_val_is_rejected():
	tc = _checker()
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, is_mutable=False),
			H.HLet(name="m", value=H.HBorrow(subject=H.HVar("x"), is_mut=True), declared_type_expr=None),
		]
	)
	res = tc.check_function(_fn_id("bad_mut_borrow"), block)
	assert any("cannot take &mut of an immutable binding" in d.message for d in res.diagnostics)


def test_conflicting_borrows_overlap_in_same_stmt():
	tc = _checker()
	base = H.HVar("s", binding_id=1)
	field = H.HPlaceExpr(base=H.HVar("s", binding_id=1), projections=[H.HPlaceField("a")])
	block = H.HBlock(
		statements=[
			H.HLet(name="s", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1, is_mutable=True),
			H.HExprStmt(
				expr=H.HArrayLiteral(
					elements=[
						H.HBorrow(subject=base, is_mut=True),
						H.HBorrow(subject=field, is_mut=False),
					]
				)
			),
		]
	)
	res = tc.check_function(_fn_id("borrow_overlap"), block)
	assert any("conflicting borrows in the same statement" in d.message for d in res.diagnostics)


def test_disjoint_field_borrows_ok_in_same_stmt():
	tc = _checker()
	field_a = H.HPlaceExpr(base=H.HVar("s", binding_id=1), projections=[H.HPlaceField("a")])
	field_b = H.HPlaceExpr(base=H.HVar("s", binding_id=1), projections=[H.HPlaceField("b")])
	block = H.HBlock(
		statements=[
			H.HLet(name="s", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1, is_mutable=True),
			H.HExprStmt(
				expr=H.HArrayLiteral(
					elements=[
						H.HBorrow(subject=field_a, is_mut=True),
						H.HBorrow(subject=field_b, is_mut=False),
					]
				)
			),
		]
	)
	res = tc.check_function(_fn_id("borrow_disjoint"), block)
	assert not any("conflicting borrows in the same statement" in d.message for d in res.diagnostics)
