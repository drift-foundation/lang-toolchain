# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import parser as p
from lang.driftc.parser import ast as parser_ast


def test_parser_builds_function_type_exprs() -> None:
	prog = p.parse_program(
		"""
fn takes(f: Fn(Int, String) -> Bool, g: Fn(Int) nothrow -> Int) -> Void {
	return;
}
"""
	)
	assert isinstance(prog, parser_ast.Program)
	assert len(prog.functions) == 1
	fn = prog.functions[0]
	assert len(fn.params) == 2
	f_ty = fn.params[0].type_expr
	g_ty = fn.params[1].type_expr
	assert isinstance(f_ty, parser_ast.TypeExpr)
	assert isinstance(g_ty, parser_ast.TypeExpr)
	assert f_ty.name == "fn"
	assert [a.name for a in f_ty.args] == ["Int", "String", "Bool"]
	assert f_ty.fn_throws is True
	assert g_ty.name == "fn"
	assert [a.name for a in g_ty.args] == ["Int", "Int"]
	assert g_ty.fn_throws is False
