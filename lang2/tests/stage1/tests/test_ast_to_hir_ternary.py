# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
AST→HIR ternary lowering tests.
"""

from lang2.stage0 import ast
from lang2.stage1 import AstToHIR, HTernary, HVar, HLiteralInt


def test_ternary_expr_lowered():
	l = AstToHIR()
	tern_ast = ast.Ternary(
		cond=ast.Name("c"),
		then_expr=ast.Literal(1),
		else_expr=ast.Literal(2),
	)
	hir = l.lower_expr(tern_ast)
	assert isinstance(hir, HTernary)
	assert isinstance(hir.cond, HVar)
	assert hir.cond.name == "c"
	assert isinstance(hir.then_expr, HLiteralInt)
	assert hir.then_expr.value == 1
	assert isinstance(hir.else_expr, HLiteralInt)
	assert hir.else_expr.value == 2
