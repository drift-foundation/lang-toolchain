#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-15
"""Stage1 borrow materialization tests."""

from lang2.driftc import stage1 as H
from lang2.driftc.stage1.normalize import normalize_hir


def test_shared_borrow_of_rvalue_is_materialized():
	"""
	`&(<rvalue>)` should be rewritten to `val tmp = <rvalue>; &tmp` so the operand is a place.
	"""
	block = H.HBlock(
		statements=[
			H.HLet(name="p", value=H.HBorrow(subject=H.HLiteralString("abc"), is_mut=False), declared_type_expr=None),
		]
	)
	norm = normalize_hir(block)
	assert len(norm.statements) == 2
	assert isinstance(norm.statements[0], H.HLet)
	assert isinstance(norm.statements[0].value, H.HLiteralString)
	assert isinstance(norm.statements[1], H.HLet)
	assert isinstance(norm.statements[1].value, H.HBorrow)
	assert isinstance(norm.statements[1].value.subject, H.HVar)


def test_mut_borrow_of_rvalue_is_not_materialized():
	"""
	`&mut (<rvalue>)` remains as-is so the typed checker can reject it.
	"""
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HBorrow(subject=H.HLiteralInt(1), is_mut=True)),
		]
	)
	norm = normalize_hir(block)
	assert len(norm.statements) == 1
	stmt = norm.statements[0]
	assert isinstance(stmt, H.HExprStmt)
	assert isinstance(stmt.expr, H.HBorrow)
	assert isinstance(stmt.expr.subject, H.HLiteralInt)

