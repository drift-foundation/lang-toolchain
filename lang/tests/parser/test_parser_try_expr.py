# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import ast as parser_ast
from lang.driftc.parser import parser as p


def test_parser_try_expr_binds_catch_after_binary() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
	val x = try 1 + 2 catch { 0 };
	return x;
}
"""
	)
	assert isinstance(prog, parser_ast.Program)
	fn = prog.functions[0]
	let_stmt = fn.body.statements[0]
	assert isinstance(let_stmt, parser_ast.LetStmt)
	assert isinstance(let_stmt.value, parser_ast.TryCatchExpr)
	try_expr = let_stmt.value
	assert isinstance(try_expr.attempt, parser_ast.Binary)
	assert try_expr.attempt.op == "+"
	assert len(try_expr.catch_arms) == 1
	arm = try_expr.catch_arms[0]
	assert arm.event is None
	assert arm.binder is None
	assert isinstance(arm.block, parser_ast.Block)
	assert isinstance(arm.block.statements[-1], parser_ast.ExprStmt)
