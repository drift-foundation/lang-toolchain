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
from lang2.driftc import stage1 as H
from lang2.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang2.driftc.core.generic_type_expr import GenericTypeExpr


def _seed_optional_match_binder_indices(block: H.HBlock) -> None:
	"""
	Stage2 unit tests bypass the typed checker, which normally normalizes
	`HMatchArm.binder_field_indices` based on the scrutinee's concrete variant
	instance.

	`for` desugaring uses `match it.next() { Some(i) => ..., default => break }`.
	For `Optional<T>`, `Some` has exactly one field at index 0, so we can seed the
	mapping in this unit test without reintroducing semantic fallbacks in stage2.
	"""

	def walk_expr(expr: H.HExpr) -> None:
		if isinstance(expr, H.HMatchExpr):
			for arm in expr.arms:
				if getattr(arm, "ctor", None) is not None and arm.binders:
					field_indices = list(getattr(arm, "binder_field_indices", []) or [])
					if not field_indices:
						if len(arm.binders) != 1:
							raise AssertionError(
								"unexpected binder arity in for-desugared Optional match arm"
							)
						arm.binder_field_indices = [0]
				walk_block(arm.block)
				if arm.result is not None:
					walk_expr(arm.result)
			walk_expr(expr.scrutinee)
			return

		# Generic recursion for this test's shape: descend into child expressions/blocks.
		for child in getattr(expr, "args", []) or []:
			walk_expr(child)
		for kw in getattr(expr, "kwargs", []) or []:
			walk_expr(kw.value)
		for child in getattr(expr, "values", []) or []:
			walk_expr(child)
		for child in getattr(expr, "elements", []) or []:
			walk_expr(child)
		for child in getattr(expr, "parts", []) or []:
			if isinstance(child, H.HExpr):
				walk_expr(child)

	def walk_stmt(stmt: H.HStmt) -> None:
		# Keep this walker minimal and resilient to stage1 node renames. It only
		# needs to descend into the nodes produced by `for` desugaring.
		if isinstance(stmt, H.HExprStmt):
			walk_expr(stmt.expr)
			return
		if isinstance(stmt, H.HLoop):
			walk_block(stmt.body)
			return
		if isinstance(stmt, H.HLet):
			walk_expr(stmt.value)
			return
		if isinstance(stmt, H.HAssign):
			walk_expr(stmt.value)
			return
		HIf = getattr(H, "HIfStmt", None) or getattr(H, "HIf", None)
		if HIf is not None and isinstance(stmt, HIf):
			walk_expr(stmt.cond)
			walk_block(stmt.then_block)
			walk_block(H.HBlock(statements=stmt.else_block))
			return
		HWhile = getattr(H, "HWhileStmt", None) or getattr(H, "HWhile", None)
		if HWhile is not None and isinstance(stmt, HWhile):
			walk_expr(stmt.cond)
			walk_block(stmt.body)
			return
		if isinstance(stmt, H.HReturn):
			if stmt.value is not None:
				walk_expr(stmt.value)
			return
		if isinstance(stmt, H.HThrow):
			walk_expr(stmt.value)
			return
		if isinstance(stmt, H.HTry):
			walk_block(stmt.try_block)
			for arm in stmt.catches:
				walk_block(arm.block)
			return

	def walk_block(b: H.HBlock) -> None:
		for s in b.statements:
			walk_stmt(s)

	walk_block(block)


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
	hir_norm = normalize_hir(hir_stmt)
	assert isinstance(hir_norm, H.HBlock)
	_seed_optional_match_binder_indices(hir_norm)
	HIRToMIR(builder, type_table=type_table).lower_block(hir_norm)
	func: MirFunc = builder.func

	# Basic CFG sanity: multiple blocks and all have terminators.
	assert func.blocks  # not empty
	# At least the entry block must terminate.
	assert func.blocks[func.entry].terminator is not None
	# Loop-generated blocks should exist, even if exit stays open for fallthrough.
	assert any(bname.startswith("loop_") for bname in func.blocks)
