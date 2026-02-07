# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import ast as parser_ast
from lang.driftc.parser import parser as p


def test_parser_cast_expr_builds_cast_node() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
	val f = cast<Fn(Int) nothrow -> Int>(abs);
	return 0;
}
"""
	)
	assert isinstance(prog, parser_ast.Program)
	fn = prog.functions[0]
	let_stmt = fn.body.statements[0]
	assert isinstance(let_stmt, parser_ast.LetStmt)
	assert isinstance(let_stmt.value, parser_ast.Cast)
	cast_expr = let_stmt.value
	assert isinstance(cast_expr.target_type, parser_ast.TypeExpr)
	assert cast_expr.target_type.name == "fn"
	assert [arg.name for arg in cast_expr.target_type.args] == ["Int", "Int"]
	assert cast_expr.target_type.fn_throws is False
	assert isinstance(cast_expr.expr, parser_ast.Name)
	assert cast_expr.expr.ident == "abs"
