# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.parser import ast as parser_ast
from lang.driftc.parser import parser as p


def test_parser_match_qualified_ctor_pattern() -> None:
	prog = p.parse_program(
		"""
import std.concurrent as conc;

fn main() -> Int {
	val e = conc.ConcurrencyError::Closed();
	return match e {
		conc.ConcurrencyError::Closed() => { 7 },
		default => { 1 },
	};
}
"""
	)
	assert isinstance(prog, parser_ast.Program)
	fn = prog.functions[0]
	ret_stmt = fn.body.statements[-1]
	assert isinstance(ret_stmt, parser_ast.ReturnStmt)
	assert isinstance(ret_stmt.value, parser_ast.MatchExpr)
	match_expr = ret_stmt.value
	assert len(match_expr.arms) == 2
	arm = match_expr.arms[0]
	assert arm.ctor == "Closed"
	assert arm.pattern_arg_form == "paren"
	assert arm.ctor_base is not None
	assert arm.ctor_base.name == "ConcurrencyError"
	assert arm.ctor_base.module_alias == "conc"


def test_parser_match_qualified_ctor_pattern_malformed() -> None:
	with pytest.raises(Exception):
		p.parse_program(
			"""
import std.concurrent as conc;

fn main() -> Int {
	val e = conc.ConcurrencyError::Closed();
	return match e {
		conc.ConcurrencyError::Closed( => { 7 },
		default => { 1 },
	};
}
"""
		)


def test_parser_type_expr_module_path_module_id() -> None:
	prog = p.parse_program(
		"""
fn main(x: std.err.ResultError) -> Int {
	return 0;
}
"""
	)
	fn = prog.functions[0]
	param = fn.params[0]
	assert isinstance(param.type_expr, parser_ast.TypeExpr)
	assert param.type_expr.name == "ResultError"
	assert param.type_expr.module_id == "std.err"
