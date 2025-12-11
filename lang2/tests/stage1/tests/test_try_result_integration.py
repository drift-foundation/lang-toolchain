# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
End-to-end sanity: TryExpr -> HTryResult -> desugared HIR -> HIR→MIR lowers cleanly.

We do not perform type checks here; this just proves the sugar rewrite composes with
the existing lowering pipeline and does not leave HTryResult behind.
"""

from __future__ import annotations

from lang2 import stage1 as H
from lang2.stage1 import normalize_hir
from lang2.stage2 import HIRToMIR, MirBuilder


def _contains_try_result(block: H.HBlock) -> bool:
	"""Walk a block to see if any HTryResult remains (should be False after rewrite)."""
	for stmt in block.statements:
		if isinstance(stmt, H.HExprStmt) and isinstance(stmt.expr, H.HTryResult):
			return True
		if isinstance(stmt, H.HIf):
			if _contains_try_result(stmt.then_block):
				return True
			if stmt.else_block and _contains_try_result(stmt.else_block):
				return True
		if isinstance(stmt, H.HLoop):
			if _contains_try_result(stmt.body):
				return True
		if isinstance(stmt, H.HTry):
			if _contains_try_result(stmt.body):
				return True
			for arm in stmt.catches:
				if _contains_try_result(arm.block):
					return True
	return False


def test_try_result_rewrite_and_lowering_round_trip():
	"""
	Build a tiny HIR block containing HTryResult, rewrite it to explicit HIR,
	and ensure HIR→MIR lowering succeeds without leaving HTryResult nodes.
	"""
	# HIR before rewrite: val x = (res)? as an expression statement.
	hir = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HTryResult(expr=H.HVar(name="res"))),
		]
	)

	rewritten = normalize_hir(hir)
	assert not _contains_try_result(rewritten), "rewrite should eliminate all HTryResult nodes"

	# Lower to MIR; we only care that it compiles and produces an If/throw/unwrap shape.
	builder = MirBuilder(name="try_result_lowering")
	HIRToMIR(builder).lower_block(rewritten)
	func = builder.func

	# There should be at least one IfTerminator from the desugared is_err check.
	assert any(
		block.terminator and block.terminator.__class__.__name__ == "IfTerminator"
		for block in func.blocks.values()
	), "desugared try-result should introduce conditional branching"
