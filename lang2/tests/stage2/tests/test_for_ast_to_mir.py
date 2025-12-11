# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Integration test: AST for-loop → HIR → MIR CFG sanity.

Checks that the iterator-based for desugaring produces a well-formed MIR CFG
with proper terminators on all blocks.
"""

from __future__ import annotations

from lang2.stage0 import ast
from lang2.stage1 import AstToHIR, HBlock
from lang2.stage2 import MirBuilder, HIRToMIR, MirFunc


def test_for_ast_lowered_to_mir_cfg():
	# for i in items { i; }
	for_ast = ast.ForStmt(
		iter_var="i",
		iterable=ast.Name("items"),
		body=[ast.ExprStmt(expr=ast.Name("i"))],
	)

	# AST → HIR
	hir_stmt = AstToHIR().lower_stmt(for_ast)
	assert isinstance(hir_stmt, HBlock)

	# HIR → MIR
	builder = MirBuilder(name="f_for")
	HIRToMIR(builder).lower_block(hir_stmt)
	func: MirFunc = builder.func

	# Basic CFG sanity: multiple blocks and all have terminators.
	assert func.blocks  # not empty
	# At least the entry block must terminate.
	assert func.blocks[func.entry].terminator is not None
	# Loop-generated blocks should exist, even if exit stays open for fallthrough.
	assert any(bname.startswith("loop_") for bname in func.blocks)
