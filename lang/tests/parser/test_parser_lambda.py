# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import parser as p
from lang.driftc.parser.ast import Lambda, Name, Call


def test_parse_lambda_expr_body() -> None:
	expr = p._parse_expr_fragment("|x: Int| => x")
	assert isinstance(expr, Lambda)
	assert expr.params and expr.params[0].name == "x"
	assert expr.params[0].type_expr is not None
	assert isinstance(expr.body_expr, Name)
	assert expr.body_block is None


def test_parse_lambda_block_body() -> None:
	expr = p._parse_expr_fragment("| | => { 1 }")
	assert isinstance(expr, Lambda)
	assert expr.params == []
	assert expr.ret_type is None
	assert expr.body_expr is None
	assert expr.body_block is not None


def test_parse_lambda_with_returns_expr_body() -> None:
	expr = p._parse_expr_fragment("|x: Int| -> Int => x")
	assert isinstance(expr, Lambda)
	assert expr.params and expr.params[0].name == "x"
	assert expr.ret_type is not None
	assert expr.ret_type.name == "Int"
	assert isinstance(expr.body_expr, Name)
	assert expr.body_block is None


def test_parse_lambda_with_returns_block_body() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
    return (|x: Int| -> Int => { return x; })(1);
}
"""
	)
	fn = prog.functions[0]
	call = fn.body.statements[0].value
	assert isinstance(call, Call)
	assert isinstance(call.func, Lambda)
	expr = call.func
	assert expr.params and expr.params[0].name == "x"
	assert expr.ret_type is not None
	assert expr.ret_type.name == "Int"
	assert expr.body_expr is None
	assert expr.body_block is not None


def test_parse_lambda_with_captures_list() -> None:
	expr = p._parse_expr_fragment("|x: Int| captures (copy i, &mut y, z) => x")
	assert isinstance(expr, Lambda)
	assert expr.captures is not None
	assert [cap.name for cap in expr.captures] == ["i", "y", "z"]
	assert [cap.kind for cap in expr.captures] == ["copy", "ref_mut", "auto"]


def test_parse_lambda_with_nothrow_modifier() -> None:
	expr = p._parse_expr_fragment("| | nothrow => 1")
	assert isinstance(expr, Lambda)
	assert expr.declared_nothrow is True
