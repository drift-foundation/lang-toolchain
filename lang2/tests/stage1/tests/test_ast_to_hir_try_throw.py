# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
AST→HIR lowering tests for try/throw.
"""

import pytest

from lang2.driftc.stage0 import ast
from lang2.driftc.stage1 import AstToHIR, HThrow, HTry, HBlock, HExprStmt, HVar, HCatchArm


def test_throw_stmt_to_hthrow():
	l = AstToHIR()
	stmt = ast.ThrowStmt(value=ast.Name("err"))
	hir = l.lower_stmt(stmt)
	assert isinstance(hir, HThrow)
	assert isinstance(hir.value, HVar)
	assert hir.value.name == "err"


def test_try_stmt_to_htry():
	l = AstToHIR()
	stmt = ast.TryStmt(
		body=[ast.ExprStmt(expr=ast.Name("body"))],
		catches=[
			ast.CatchExprArm(event=None, binder="e", block=[ast.ExprStmt(expr=ast.Name("handler"))])
		],
	)
	hir = l.lower_stmt(stmt)
	assert isinstance(hir, HTry)
	assert isinstance(hir.catches, list) and len(hir.catches) == 1
	catch_arm = hir.catches[0]
	assert isinstance(catch_arm, HCatchArm)
	assert catch_arm.event_fqn is None
	assert catch_arm.binder == "e"
	assert isinstance(hir.body, HBlock)
	assert isinstance(hir.body.statements[0], HExprStmt)
	assert isinstance(catch_arm.block, HBlock)
	assert isinstance(catch_arm.block.statements[0], HExprStmt)


def test_raise_stmt_still_not_supported():
	l = AstToHIR()
	stmt = ast.RaiseStmt(value=ast.Name("x"))
	with pytest.raises(NotImplementedError):
		l.lower_stmt(stmt)


def test_try_stmt_multiple_catches_to_htry():
	l = AstToHIR()
	stmt = ast.TryStmt(
		body=[ast.ExprStmt(expr=ast.Name("body"))],
		catches=[
			ast.CatchExprArm(event="m:EvtA", binder="a", block=[ast.ExprStmt(expr=ast.Name("h1"))]),
			ast.CatchExprArm(event=None, binder=None, block=[ast.ExprStmt(expr=ast.Name("h2"))]),
		],
	)
	hir = l.lower_stmt(stmt)
	assert isinstance(hir, HTry)
	assert len(hir.catches) == 2
	first, second = hir.catches
	assert first.event_fqn == "m:EvtA" and first.binder == "a"
	assert second.event_fqn is None and second.binder is None
