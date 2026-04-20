# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Robustness regression: parser-AST → stage0-AST converter must not
RecursionError on long else-if chains.

Surfaced by a robustness audit: a Drift source
with thousands of `else if` clauses crashes driftc with Python
`RecursionError` in `lang/driftc/parser/__init__.py::_convert_if`, which
recursed via `_convert_block` → `_convert_stmt` → `_convert_if` (~4 frames
per source else-if level).

The fix is an iterative else-if chain flattener in `_convert_if`: walk the
chain from outer to inner collecting `(cond, then_block, loc)` tuples, then
build the resulting `s0.IfStmt` nodes from innermost out.

This test pins the parser-converter fix in isolation by building a synthetic
parser-AST `IfStmt` chain directly and running it through `_convert_stmt`
under `sys.setrecursionlimit(1000)`.
"""
from __future__ import annotations

import sys

from lang.driftc.parser import ast as p_ast
from lang.driftc.parser import _convert_stmt  # type: ignore[attr-defined]
from lang.driftc.stage0 import ast as s0_ast


_LOC = p_ast.Located(line=1, column=1)


def _build_else_if_chain(n: int) -> p_ast.IfStmt:
	"""Build `if x==0 {} else if x==1 {} else if x==2 {} ... else { }` with n branches."""
	# Innermost: a final else block with a single return.
	final_return = p_ast.ReturnStmt(loc=_LOC, value=p_ast.Literal(loc=_LOC, value=-1))
	current_else: p_ast.Block = p_ast.Block(statements=[final_return])
	# Build from innermost out.
	for i in reversed(range(n)):
		then_ret = p_ast.ReturnStmt(loc=_LOC, value=p_ast.Literal(loc=_LOC, value=i))
		then_blk = p_ast.Block(statements=[then_ret])
		stmt = p_ast.IfStmt(
			loc=_LOC,
			condition=p_ast.Literal(loc=_LOC, value=True),
			then_block=then_blk,
			else_block=current_else,
		)
		current_else = p_ast.Block(statements=[stmt])
	# After the loop, `current_else` wraps the outermost IfStmt.
	# The outermost IfStmt is the only statement in the wrapper block.
	outer = current_else.statements[0]
	assert isinstance(outer, p_ast.IfStmt)
	return outer


def test_convert_if_chain_5000_no_recursion_error() -> None:
	"""5000 else-if levels must convert without crashing under default limit."""
	prev = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		stmt = _build_else_if_chain(5000)
		result = _convert_stmt(stmt)
		# Sanity: the result is an s0.IfStmt with the same chain depth.
		assert isinstance(result, s0_ast.IfStmt)
		depth = 0
		node: object = result
		while isinstance(node, s0_ast.IfStmt):
			depth += 1
			# Walk down the else-if chain (the else_block of an else-if is
			# `[IfStmt]`, terminating block has different shape).
			eb = node.else_block
			if len(eb) == 1 and isinstance(eb[0], s0_ast.IfStmt):
				node = eb[0]
			else:
				break
		assert depth == 5000, f"expected 5000 chained s0.IfStmt, got {depth}"
	finally:
		sys.setrecursionlimit(prev)
