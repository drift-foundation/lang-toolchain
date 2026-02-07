#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-15
"""Stage1 borrow materialization tests."""

from lang.driftc import stage1 as H
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.core.span import Span


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
	# Normalization canonicalizes lvalue contexts to `HPlaceExpr`.
	assert isinstance(norm.statements[1].value.subject, H.HPlaceExpr)
	assert isinstance(norm.statements[1].value.subject.base, H.HVar)
	assert norm.statements[1].value.subject.base.name.startswith("__tmp")
	assert norm.statements[1].value.subject.projections == []


def test_mut_borrow_of_rvalue_is_materialized():
	"""
	`&mut (<rvalue>)` should be rewritten to `var tmp = <rvalue>; &mut tmp`.
	"""
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HBorrow(subject=H.HLiteralInt(1), is_mut=True)),
		]
	)
	norm = normalize_hir(block)
	assert len(norm.statements) == 2
	tmp_stmt = norm.statements[0]
	assert isinstance(tmp_stmt, H.HLet)
	assert tmp_stmt.is_mutable is True
	assert isinstance(tmp_stmt.value, H.HLiteralInt)
	stmt = norm.statements[1]
	assert isinstance(stmt, H.HExprStmt)
	assert isinstance(stmt.expr, H.HBorrow)
	assert stmt.expr.is_mut is True
	assert isinstance(stmt.expr.subject, H.HPlaceExpr)


def test_move_operand_is_canonicalized_to_place_expr():
	"""normalize_hir must canonicalize `move` operands to HPlaceExpr for stage2."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1)),
			H.HExprStmt(expr=H.HMove(subject=H.HVar("x"), loc=Span())),
		]
	)
	norm = normalize_hir(block)
	assert len(norm.statements) == 2
	stmt = norm.statements[1]
	assert isinstance(stmt, H.HExprStmt)
	assert isinstance(stmt.expr, H.HMove)
	assert isinstance(stmt.expr.subject, H.HPlaceExpr)
