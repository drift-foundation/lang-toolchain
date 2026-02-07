# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import ast as parser_ast
from lang.driftc.parser import parser as p


def test_parser_assert_stmt_basic() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
	assert(1 == 1);
	return 0;
}
"""
	)
	assert isinstance(prog, parser_ast.Program)
	fn = prog.functions[0]
	stmt = fn.body.statements[0]
	assert isinstance(stmt, parser_ast.AssertStmt)
	assert isinstance(stmt.cond, parser_ast.Binary)
	assert stmt.msg is None


def test_parser_assert_stmt_with_msg() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
	assert(1 == 1, "ok");
	return 0;
}
"""
	)
	assert isinstance(prog, parser_ast.Program)
	fn = prog.functions[0]
	stmt = fn.body.statements[0]
	assert isinstance(stmt, parser_ast.AssertStmt)
	assert isinstance(stmt.cond, parser_ast.Binary)
	assert isinstance(stmt.msg, parser_ast.Literal)
	assert stmt.msg.value == "ok"
