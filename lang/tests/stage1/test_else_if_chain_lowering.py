# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Robustness regression: stage1 `_visit_stmt_IfStmt` must not RecursionError
on long else-if chains.

Surfaced by `work/robustness/robustness-matrix.md` row #5: even after the
parser-converter fix in `lang/driftc/parser/__init__.py::_convert_if`, the
stage1 lowering visitor `_visit_stmt_IfStmt` was still recursive on
`else_block` and crashed at depth ~2000 under the row #4 recursion-limit
bump (8192).

The fix is an iterative else-if chain flattener in `_visit_stmt_IfStmt`:
walk the chain from outer to inner collecting `(cond, then_block, loc)`
tuples, then build the resulting `H.HIf` nodes from innermost out (each
wrapped in a singleton `HBlock` to match the recursive shape downstream
HIR consumers see).

This test pins the stage1 fix in isolation by building a synthetic stage0
AST chain and lowering it under `sys.setrecursionlimit(1000)`.
"""
from __future__ import annotations

import sys

from lang.driftc.stage0 import ast as s0_ast
from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.stage1.ast_to_hir import AstToHIR


def _build_else_if_chain(n: int) -> s0_ast.IfStmt:
	"""Build a stage0 IfStmt with `n` chained else-if levels and a final else."""
	# Innermost: a final-else block returning -1.
	final_return = s0_ast.ReturnStmt(value=s0_ast.Literal(value=-1))
	current_else: list[s0_ast.Stmt] = [final_return]
	for i in reversed(range(n)):
		then_ret = s0_ast.ReturnStmt(value=s0_ast.Literal(value=i))
		stmt = s0_ast.IfStmt(
			cond=s0_ast.Literal(value=True),
			then_block=[then_ret],
			else_block=current_else,
		)
		current_else = [stmt]
	outer = current_else[0]
	assert isinstance(outer, s0_ast.IfStmt)
	return outer


def test_visit_stmt_ifstmt_chain_5000_no_recursion_error() -> None:
	"""5000 else-if levels must lower without crashing under default limit."""
	prev = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		stmt = _build_else_if_chain(5000)
		lowering = AstToHIR()
		# Push a scope so lower_block calls inside the visitor have a stack
		# to push onto. The visitor itself does not push the entry scope.
		lowering._push_scope()
		try:
			result = lowering.lower_stmt(stmt)
		finally:
			lowering._pop_scope()
		# Sanity: the result is an HIf with the same chain depth.
		assert isinstance(result, H.HIf)
		depth = 0
		node: object = result
		while isinstance(node, H.HIf):
			depth += 1
			eb = node.else_block
			if (
				eb is not None
				and len(eb.statements) == 1
				and isinstance(eb.statements[0], H.HIf)
			):
				node = eb.statements[0]
			else:
				break
		assert depth == 5000, f"expected 5000 chained HIf, got {depth}"
	finally:
		sys.setrecursionlimit(prev)


def _build_chain_with_let_per_arm(n: int) -> s0_ast.IfStmt:
	"""Build a chain `if true {let x_i = i; ...} else if true {let x_i+1 = i+1; ...} else {let x_n = -1; ...}`.

	Each arm declares a single uniquely-named `let` binding so the test can
	read back the binding ids and check they are allocated in outer-first
	declaration order.
	"""
	# Innermost: terminating else block with `let x_n = -1; return x_n;`.
	# (We need a return after the let so the block has the same shape as the
	# arms; the return value isn't checked.)
	final_let = s0_ast.LetStmt(name=f"x{n}", value=s0_ast.Literal(value=-1))
	final_ret = s0_ast.ReturnStmt(value=s0_ast.Literal(value=-1))
	current_else: list[s0_ast.Stmt] = [final_let, final_ret]
	for i in reversed(range(n)):
		then_let = s0_ast.LetStmt(name=f"x{i}", value=s0_ast.Literal(value=i))
		then_ret = s0_ast.ReturnStmt(value=s0_ast.Literal(value=i))
		stmt = s0_ast.IfStmt(
			cond=s0_ast.Literal(value=True),
			then_block=[then_let, then_ret],
			else_block=current_else,
		)
		current_else = [stmt]
	outer = current_else[0]
	assert isinstance(outer, s0_ast.IfStmt)
	return outer


def _collect_let_binding_ids_in_order(node: H.HStmt) -> list[tuple[str, int]]:
	"""Walk the chain top-down and return (let_name, binding_id) for every
	`let xN` encountered, in the order they appear from outermost arm to
	innermost arm to terminating-else arm. Iterative walk so the helper
	itself does not blow recursion on long chains.
	"""
	out: list[tuple[str, int]] = []
	stack: list[object] = [node]
	while stack:
		obj = stack.pop()
		if isinstance(obj, H.HLet):
			out.append((obj.name, int(obj.binding_id) if obj.binding_id is not None else -1))
			continue
		if isinstance(obj, H.HBlock):
			# Push in reverse so list-order processing matches declaration order.
			for s in reversed(obj.statements):
				stack.append(s)
			continue
		if isinstance(obj, H.HIf):
			# Visit then block first (declaration order: then before else).
			# Push in reverse: else last so it pops after then's contents.
			if obj.else_block is not None:
				stack.append(obj.else_block)
			stack.append(obj.then_block)
			continue
		# Other statement kinds we don't recurse into for this test.
	return out


def test_visit_stmt_ifstmt_chain_preserves_outer_first_binding_id_allocation() -> None:
	"""The iterative else-if flattener must allocate binding ids in the same
	outer-first order the original recursive lowering did.

	This pins the determinism property surfaced in 0.27.162 review:
	the first iterative draft built the HIf tree innermost-out, which
	silently reversed binding-id allocation across chain arms (inner arm's
	let got id 1, outer arm's let got id 2, etc.). The corrected
	implementation lowers in forward order and only constructs HIf nodes
	innermost-out as a pure post-step.

	Test shape: a 4-level chain with `let x0..x4` (4 chain arms + 1 terminating
	else). Lower it, walk the resulting HIR, and assert that `x0`'s
	binding_id < `x1`'s binding_id < ... < `x4`'s binding_id, where `x0` is
	the outermost arm's let.
	"""
	stmt = _build_chain_with_let_per_arm(4)
	lowering = AstToHIR()
	lowering._push_scope()
	try:
		result = lowering.lower_stmt(stmt)
	finally:
		lowering._pop_scope()
	pairs = _collect_let_binding_ids_in_order(result)
	# Expect 5 lets in declaration order: x0 (outer), x1, x2, x3, x4 (terminating else).
	names = [p[0] for p in pairs]
	ids = [p[1] for p in pairs]
	assert names == ["x0", "x1", "x2", "x3", "x4"], f"unexpected let order: {names}"
	# All ids must be distinct positive integers.
	assert all(i > 0 for i in ids), f"missing/invalid binding ids: {ids}"
	# Crucially, ids must be monotonically increasing in outer-first
	# declaration order. Pre-fix shape: ids would be reversed (x4 lowest,
	# x0 highest) because the first iterative draft lowered innermost-out.
	assert ids == sorted(ids), (
		f"binding ids are not in outer-first allocation order — the iterative "
		f"chain flattener has regressed to lowering innermost-first.\n"
		f"  declared order: {names}\n"
		f"  binding ids:    {ids}"
	)
