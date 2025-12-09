#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Basic typed checker coverage: bindings, literals, borrows."""

from lang2 import stage1 as H
from lang2.type_checker import TypeChecker
from lang2.core.types_core import TypeTable


def _checker():
	return TypeChecker(TypeTable())


def test_literal_and_var_types():
	tc = _checker()
	block = H.HBlock(statements=[H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None)])
	result = tc.check_function("f", block)
	assert result.diagnostics == []
	x_binding = block.statements[0].binding_id
	assert x_binding is not None
	assert result.typed_fn.binding_for_var == {}
	assert block.statements[0].binding_id in result.typed_fn.locals
	# Var lookup
	block2 = H.HBlock(statements=[H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None), H.HExprStmt(expr=H.HVar("x"))])
	result2 = tc.check_function("g", block2)
	var_expr = block2.statements[1].expr
	assert isinstance(var_expr, H.HVar)
	etypes = {k: v for k, v in result2.typed_fn.expr_types.items()}
	assert tc.type_table.ensure_int() in etypes.values()


def test_borrow_types():
	tc = _checker()
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HLet(name="r", value=H.HBorrow(subject=H.HVar("x"), is_mut=False), declared_type_expr=None),
			H.HLet(name="m", value=H.HBorrow(subject=H.HVar("x"), is_mut=True), declared_type_expr=None),
		]
	)
	res = tc.check_function("h", block)
	assert res.diagnostics == []
	r_let = block.statements[1]
	m_let = block.statements[2]
	assert isinstance(r_let, H.HLet)
	assert isinstance(m_let, H.HLet)
	vals = list(res.typed_fn.expr_types.values())
	assert tc.type_table.ensure_ref(tc.type_table.ensure_int()) in vals
	assert tc.type_table.ensure_ref_mut(tc.type_table.ensure_int()) in vals
