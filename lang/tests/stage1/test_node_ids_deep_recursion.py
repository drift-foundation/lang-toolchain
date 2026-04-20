# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Robustness regression: stage1 node-id walkers must not RecursionError on
deeply nested HIR.

Surfaced by a robustness audit: ~200 levels of
nested if-statements crashes driftc with Python `RecursionError` in
`lang/driftc/stage1/node_ids.py` mutually-recursive `walk` / `walk_value`
walkers (three independent pairs across `assign_node_ids`,
`assign_callsite_ids`, `validate_callsite_ids`).

This test builds a synthetic deeply-nested HIR tree directly (no parser /
AST-builder dependence) so it pins the stage1 walker behavior in isolation.
"""
from __future__ import annotations

import sys

from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.stage1.node_ids import (
	assign_callsite_ids,
	assign_node_ids,
	validate_callsite_ids,
)


def _build_deep_block(n: int) -> H.HBlock:
	"""Build n levels of nested HBlock around a single HReturn."""
	inner: H.HStmt = H.HReturn(value=H.HLiteralInt(0))
	for _ in range(n):
		inner = H.HBlock(statements=[inner])
	# `inner` is the outermost HBlock at this point.
	return inner if isinstance(inner, H.HBlock) else H.HBlock(statements=[inner])


def test_assign_node_ids_deep_block_no_recursion_error() -> None:
	"""3000 levels of nested HBlock must be ID-assigned without crashing.

	The recursive walker would die at ~250 levels under Python's default
	recursion limit. The iterative walker has no such ceiling.
	"""
	# Make sure we don't accidentally rely on the parser's recursion-limit
	# bump from 0.27.155 — restore the default before this test runs.
	prev = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		root = _build_deep_block(3000)
		next_id = assign_node_ids(root)
		# Sanity: every block should have received an id.
		assert next_id > 3000
	finally:
		sys.setrecursionlimit(prev)


def test_assign_callsite_ids_deep_block_no_recursion_error() -> None:
	prev = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		root = _build_deep_block(3000)
		# No call nodes in this tree, so the result is just `start` unchanged,
		# but the walker still has to traverse all 3000 levels without crashing.
		assert assign_callsite_ids(root) == 0
	finally:
		sys.setrecursionlimit(prev)


def test_validate_callsite_ids_deep_block_no_recursion_error() -> None:
	prev = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		root = _build_deep_block(3000)
		# No call nodes → vacuously valid; the walker must reach the bottom.
		validate_callsite_ids(root)
	finally:
		sys.setrecursionlimit(prev)
