# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Robustness regression: long binary-op chains must not crash stage1 lowering.

Surfaced by a robustness audit: a Drift source like
`return 1+1+1+...+1;` with hundreds of operands crashes driftc with Python
`RecursionError` in `lang/driftc/stage1/ast_to_hir.py::_visit_expr_Binary`,
which descends `expr.left` recursively for left-leaning chains.

The fix is an iterative left-spine flattener in `_visit_expr_Binary`: it
collects the spine `(op, right_ast)` pairs in O(1) stack depth and rebuilds
the HIR `HBinary` tree from the leftmost leaf outward without recursing on
the chain length.

This file pins the stage1 fix in isolation by building a synthetic deeply-
left-leaning AST `Binary` tree and lowering it under
`sys.setrecursionlimit(1000)` to prove the iterative path has no stack
ceiling, independent of any other recursion-limit bumps in the system.
"""
from __future__ import annotations

import sys

from lang.driftc.stage0 import ast as s0_ast
from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.stage1.ast_to_hir import AstToHIR


def _build_left_leaning_add_chain(n: int) -> s0_ast.Expr:
	"""Build `((((1+1)+1)+1)...+1)` with `n` `+` operations.

	Note: stage1 lowers stage0 AST nodes (not parser AST nodes); see the
	`from lang.driftc.stage0 import ast` import in
	`lang/driftc/stage1/ast_to_hir.py`. The dispatch keys on
	`type(expr).__name__`, so building parser AST `Binary` would route to
	the same `_visit_expr_Binary` method but `isinstance(node, ast.Binary)`
	checks inside the visitor would fail (different class) — leading to a
	silent infinite loop in the iterative spine walk. Use stage0 AST.
	"""
	expr: s0_ast.Expr = s0_ast.Literal(value=1)
	for _ in range(n):
		expr = s0_ast.Binary(op="+", left=expr, right=s0_ast.Literal(value=1))
	return expr


def test_long_binary_chain_lowers_without_recursion_error() -> None:
	"""5000 left-leaning binary adds must lower without crashing.

	The recursive walker would die at ~400 levels under Python's default
	recursion limit. The iterative spine flattener has no such ceiling.
	Pinned under `sys.setrecursionlimit(1000)` so this test does not
	silently rely on the row #4 driftc.py recursion-limit bump.
	"""
	prev = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		expr = _build_left_leaning_add_chain(5000)
		lowering = AstToHIR()
		result = lowering.lower_expr(expr)
		# Sanity: the result is an HBinary tree with the same shape.
		assert isinstance(result, H.HBinary)
		# Walk down the left spine and count levels iteratively (must not
		# recurse here either, hence iterative).
		depth = 0
		node: H.HExpr = result
		while isinstance(node, H.HBinary):
			depth += 1
			node = node.left
		assert depth == 5000, f"expected 5000 chained HBinary, got {depth}"
	finally:
		sys.setrecursionlimit(prev)
