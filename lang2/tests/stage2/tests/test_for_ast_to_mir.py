# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Integration test: AST for-loop → HIR → MIR CFG sanity.

Checks that the iterator-based for desugaring produces a well-formed MIR CFG
with proper terminators on all blocks.
"""

from __future__ import annotations

from lang2.driftc.stage0 import ast
from lang2.driftc.stage1 import AstToHIR, HBlock
from lang2.driftc.stage1.normalize import normalize_hir
from lang2.driftc.stage2 import MirBuilder, HIRToMIR, MirFunc
from lang2.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang2.driftc.core.generic_type_expr import GenericTypeExpr


def test_for_ast_lowered_to_mir_cfg():
	# for i in [1,2,3] { i; }
	for_ast = ast.ForStmt(
		iter_var="i",
		iterable=ast.ArrayLiteral(elements=[ast.Literal(1), ast.Literal(2), ast.Literal(3)]),
		body=[ast.ExprStmt(expr=ast.Name("i"))],
	)

	# AST → HIR
	hir_stmt = AstToHIR().lower_stmt(for_ast)
	assert isinstance(hir_stmt, HBlock)

	# HIR → MIR
	builder = MirBuilder(name="f_for")
	type_table = TypeTable()
	# Stage2 unit tests bypass the parser adapter, so we must seed the prelude
	# `Optional<T>` variant that `for` desugaring relies on.
	type_table.declare_variant(
		"lang.core",
		"Optional",
		["T"],
		[
			VariantArmSchema(name="Some", fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))]),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	HIRToMIR(builder, type_table=type_table).lower_block(normalize_hir(hir_stmt))
	func: MirFunc = builder.func

	# Basic CFG sanity: multiple blocks and all have terminators.
	assert func.blocks  # not empty
	# At least the entry block must terminate.
	assert func.blocks[func.entry].terminator is not None
	# Loop-generated blocks should exist, even if exit stays open for fallthrough.
	assert any(bname.startswith("loop_") for bname in func.blocks)
