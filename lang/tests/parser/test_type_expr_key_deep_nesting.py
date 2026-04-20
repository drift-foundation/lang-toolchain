# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Robustness regression: `_type_expr_key` must not RecursionError on deep
type nesting.

Surfaced by a robustness-matrix re-probe cleanup pass: nested generic types
in fn-parameter position with the corrected source shape revealed a real
recursion failure at d≥5000 in `lang/driftc/parser/__init__.py::_type_expr_key`,
the recursive type-expression key builder used by the parser-to-HIR pipeline.
The original probe artifact had been hiding this finding behind a
syntactic error.

The fix is the same iterative post-order walker pattern used in rows #2
and #5: process children first, store their results in an `id(node)`-keyed
cache, build parent keys from cached child results.

This test pins the fix in isolation by building a synthetic deeply-nested
parser-AST `TypeExpr` directly and running it through `_type_expr_key`
under `sys.setrecursionlimit(1000)`.
"""
from __future__ import annotations

import sys

from lang.driftc.parser import ast as p_ast
from lang.driftc.parser import _type_expr_key  # type: ignore[attr-defined]


_LOC = p_ast.Located(line=1, column=1)


def _build_nested_array_type(n: int) -> p_ast.TypeExpr:
	"""Build `Array<Array<...<Int>>>` with n levels of `Array<>` nesting."""
	t = p_ast.TypeExpr(loc=_LOC, name="Int", args=[])
	for _ in range(n):
		t = p_ast.TypeExpr(loc=_LOC, name="Array", args=[t])
	return t


def test_type_expr_key_deep_nested_no_recursion_error() -> None:
	"""5000 levels of nested Array<...> must produce a key without crashing.

	The recursive walker would die at ~250 levels under Python's default
	recursion limit. The iterative post-order builder has no such ceiling.
	"""
	prev = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		typ = _build_nested_array_type(5000)
		key = _type_expr_key(typ)
		# Sanity: the result is a tuple. Walk down its structure
		# iteratively (also under the tight recursion limit) and confirm
		# the depth matches.
		depth = 0
		node: object = key
		while isinstance(node, tuple) and len(node) >= 3 and node[1] == "Array":
			depth += 1
			children = node[2]
			if not children:
				break
			node = children[0]
		assert depth == 5000, f"expected 5000 nested Array<>, got {depth}"
	finally:
		sys.setrecursionlimit(prev)


def test_type_expr_key_shallow_nested_unchanged() -> None:
	"""Sanity: a small nested type still produces the same shape of key.

	Pins that the iterative refactor does not change the structure of the
	resulting tuple key for typical inputs.
	"""
	# Array<Array<Int>>
	inner = p_ast.TypeExpr(loc=_LOC, name="Int", args=[])
	mid = p_ast.TypeExpr(loc=_LOC, name="Array", args=[inner])
	outer = p_ast.TypeExpr(loc=_LOC, name="Array", args=[mid])
	key = _type_expr_key(outer)
	# Expected shape: (qual=None, "Array", ((qual=None, "Array", ((qual=None, "Int", ()),)),))
	assert isinstance(key, tuple)
	assert key[1] == "Array"
	assert isinstance(key[2], tuple)
	assert len(key[2]) == 1
	mid_key = key[2][0]
	assert mid_key[1] == "Array"
	assert mid_key[2][0][1] == "Int"
	assert mid_key[2][0][2] == ()
