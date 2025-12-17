# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage1 (AST → HIR) unit tests.

These tests exercise the currently supported lowering:
  - literals, names, unary/binary ops
  - field/index
  - plain calls, method calls
  - exception ctor → HExceptionInit
  - let/assign/if/return/break/continue/expr-stmt
They run purely in-memory; no parser or codegen required.
"""

from __future__ import annotations

import pytest

from lang2.driftc.stage0 import ast
from lang2.driftc.stage1 import (
	AstToHIR,
	HVar,
	HLiteralInt,
	HLiteralBool,
	HLiteralString,
	HLiteralFloat,
	HBinary,
	HField,
	HIndex,
	HCall,
	HMethodCall,
	HExceptionInit,
	HLet,
	HAssign,
	HIf,
	HReturn,
	HBreak,
	HContinue,
	HExprStmt,
	HLoop,
	HBlock,
	HMatchExpr,
)


def test_literals_and_names():
	l = AstToHIR()
	assert isinstance(l.lower_expr(ast.Literal(1)), HLiteralInt)
	assert isinstance(l.lower_expr(ast.Literal(True)), HLiteralBool)
	assert isinstance(l.lower_expr(ast.Literal("s")), HLiteralString)
	assert isinstance(l.lower_expr(ast.Literal(1.25)), HLiteralFloat)
	v = l.lower_expr(ast.Name("x"))
	assert isinstance(v, HVar)
	assert v.name == "x"


def test_binary_and_field_index():
	l = AstToHIR()
	bin_hir = l.lower_expr(ast.Binary("+", ast.Name("a"), ast.Literal(2)))
	assert isinstance(bin_hir, HBinary)
	assert isinstance(bin_hir.left, HVar) and bin_hir.left.name == "a"
	field_hir = l.lower_expr(ast.Attr(ast.Name("obj"), "f"))
	assert isinstance(field_hir, HField)
	assert field_hir.name == "f"
	index_hir = l.lower_expr(ast.Index(ast.Name("arr"), ast.Literal(1)))
	assert isinstance(index_hir, HIndex)


def test_plain_and_method_calls():
	l = AstToHIR()
	call_hir = l.lower_expr(ast.Call(func=ast.Name("f"), args=[ast.Literal(1)], kwargs=[]))
	assert isinstance(call_hir, HCall)
	assert isinstance(call_hir.fn, HVar) and call_hir.fn.name == "f"
	assert isinstance(call_hir.args[0], HLiteralInt)

	meth_hir = l.lower_expr(
		ast.Call(func=ast.Attr(ast.Name("obj"), "m"), args=[ast.Literal(True)], kwargs=[])
	)
	assert isinstance(meth_hir, HMethodCall)
	assert meth_hir.method_name == "m"
	assert isinstance(meth_hir.receiver, HVar) and meth_hir.receiver.name == "obj"
	assert isinstance(meth_hir.args[0], HLiteralBool)


def test_exception_ctor_to_dvinit():
	l = AstToHIR()
	l._module_name = "m"
	ctor = ast.ExceptionCtor(
		name="MyErr",
		args=[],
		kwargs=[
			ast.KwArg(name="x", value=ast.Literal(1)),
			ast.KwArg(name="y", value=ast.Literal(2)),
		],
	)
	hir = l.lower_expr(ctor)
	assert isinstance(hir, HExceptionInit)
	assert hir.event_fqn == "m:MyErr"
	assert hir.pos_args == []
	assert [kw.name for kw in hir.kw_args] == ["x", "y"]
	assert len(hir.kw_args) == 2
	assert all(isinstance(kw.value, HLiteralInt) for kw in hir.kw_args)


def test_basic_statements_and_if():
	l = AstToHIR()
	let_hir = l.lower_stmt(ast.LetStmt(name="x", value=ast.Literal(1)))
	assert isinstance(let_hir, HLet) and isinstance(let_hir.value, HLiteralInt)

	assign_hir = l.lower_stmt(ast.AssignStmt(target=ast.Name("x"), value=ast.Literal(2)))
	assert isinstance(assign_hir, HAssign)

	if_hir = l.lower_stmt(
		ast.IfStmt(
			cond=ast.Binary("<", ast.Name("x"), ast.Literal(0)),
			then_block=[ast.ReturnStmt(value=None)],
			else_block=[],
		)
	)
	assert isinstance(if_hir, HIf)
	assert if_hir.else_block is None  # empty else becomes None
	assert isinstance(if_hir.then_block.statements[0], HReturn)

	break_hir = l.lower_stmt(ast.BreakStmt())
	assert isinstance(break_hir, HBreak)
	cont_hir = l.lower_stmt(ast.ContinueStmt())
	assert isinstance(cont_hir, HContinue)
	expr_stmt_hir = l.lower_stmt(ast.ExprStmt(expr=ast.Literal("x")))
	assert isinstance(expr_stmt_hir, HExprStmt)


def test_while_desugars_to_loop_if_break():
	l = AstToHIR()
	ast_while = ast.WhileStmt(cond=ast.Name("x"), body=[ast.ExprStmt(expr=ast.Literal(1))])
	hir = l.lower_stmt(ast_while)
	assert isinstance(hir, HLoop)
	assert len(hir.body.statements) == 1
	if_stmt = hir.body.statements[0]
	assert isinstance(if_stmt, HIf)
	assert isinstance(if_stmt.then_block, HBlock)
	assert isinstance(if_stmt.else_block, HBlock)
	assert isinstance(if_stmt.else_block.statements[0], HBreak)


def test_for_desugars_to_iter_loop_match():
	l = AstToHIR()
	for_ast = ast.ForStmt(iter_var="i", iterable=ast.Name("items"), body=[ast.ExprStmt(expr=ast.Name("i"))])
	hir = l.lower_stmt(for_ast)
	assert isinstance(hir, HBlock)
	assert len(hir.statements) == 3  # iterable let, iter let, loop
	iterable_let, iter_let, loop = hir.statements
	assert isinstance(iterable_let, HLet)
	assert iterable_let.name.startswith("__for_iterable")
	assert isinstance(iterable_let.value, HVar)
	assert iterable_let.value.name == "items"
	assert isinstance(iter_let, HLet)
	assert iter_let.name.startswith("__for_iter")
	assert isinstance(iter_let.value, HMethodCall)
	assert iter_let.value.method_name == "iter"
	assert isinstance(loop, HLoop)
	assert len(loop.body.statements) == 1
	stmt = loop.body.statements[0]
	assert isinstance(stmt, HExprStmt)
	assert isinstance(stmt.expr, HMatchExpr)
	match = stmt.expr
	assert isinstance(match.scrutinee, HMethodCall)
	assert match.scrutinee.method_name == "next"
	assert len(match.arms) == 2
	some, default = match.arms
	assert some.ctor == "Some"
	assert some.binders == ["i"]
	assert isinstance(some.block, HBlock)
	assert isinstance(some.block.statements[0], HExprStmt)
	assert default.ctor is None
	assert default.binders == []
	assert isinstance(default.block.statements[0], HBreak)


def test_for_with_missing_cond_step_defaults():
	l = AstToHIR()
	for_ast = ast.ForStmt(iter_var="x", iterable=ast.Name("it"), body=[ast.ExprStmt(expr=ast.Literal(1))])
	hir = l.lower_stmt(for_ast)
	assert isinstance(hir, HBlock)
	assert isinstance(hir.statements[2], HLoop)


def test_fail_loud_on_unhandled_nodes():
	l = AstToHIR()
	with pytest.raises(NotImplementedError):
		l.lower_stmt(ast.RaiseStmt(value=ast.Literal(1)))
