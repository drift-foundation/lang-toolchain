# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
AST → HIR lowering (sugar removal entry point).

This pass takes the parsed AST and produces the sugar-free HIR defined in
`lang2/hir_nodes.py`. Only the trivial leaf cases are implemented right now
to keep the initial commit small; all other nodes fail loudly so they can be
filled in incrementally.
"""

from __future__ import annotations

from typing import List

from . import ast  # local copy of AST node definitions for the refactor
from . import hir_nodes as H


class AstToHIR:
	"""AST → HIR lowering (sugar removal happens here)."""

	def lower_expr(self, expr: ast.Expr) -> H.HExpr:
		"""Dispatch an AST expression to a per-type visitor."""
		method = getattr(self, f"visit_expr_{type(expr).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No HIR lowering for expr type {type(expr).__name__}")
		return method(expr)

	def lower_stmt(self, stmt: ast.Stmt) -> H.HStmt:
		"""Dispatch an AST statement to a per-type visitor."""
		method = getattr(self, f"visit_stmt_{type(stmt).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No HIR lowering for stmt type {type(stmt).__name__}")
		return method(stmt)

	def lower_block(self, stmts: List[ast.Stmt]) -> H.HBlock:
		"""Lower a list of AST statements into an HIR block."""
		return H.HBlock(statements=[self.lower_stmt(s) for s in stmts])

	# --- minimal implemented handlers (trivial cases only) ---

	def visit_expr_Name(self, expr: ast.Name) -> H.HExpr:
		"""Names become HVar; binding resolution happens later."""
		return H.HVar(name=expr.ident)

	def visit_expr_Literal(self, expr: ast.Literal) -> H.HExpr:
		"""Map literal to the appropriate HIR literal node."""
		if isinstance(expr.value, bool):
			return H.HLiteralBool(value=bool(expr.value))
		if isinstance(expr.value, int):
			return H.HLiteralInt(value=int(expr.value))
		if isinstance(expr.value, str):
			return H.HLiteralString(value=str(expr.value))
		raise NotImplementedError(f"Literal of unsupported type: {type(expr.value).__name__}")

	def visit_stmt_LetStmt(self, stmt: ast.LetStmt) -> H.HStmt:
		"""Immutable binding introduction."""
		return H.HLet(name=stmt.name, value=self.lower_expr(stmt.value))

	def visit_stmt_ReturnStmt(self, stmt: ast.ReturnStmt) -> H.HStmt:
		"""Return with optional value."""
		val = self.lower_expr(stmt.value) if stmt.value is not None else None
		return H.HReturn(value=val)

	def visit_stmt_ExprStmt(self, stmt: ast.ExprStmt) -> H.HStmt:
		"""Expression as statement (value discarded)."""
		return H.HExprStmt(expr=self.lower_expr(stmt.expr))

	# --- stubs for remaining nodes ---

	def visit_expr_Call(self, expr: ast.Call) -> H.HExpr:
		raise NotImplementedError("Call lowering not implemented yet")

	def visit_expr_Attr(self, expr: ast.Attr) -> H.HExpr:
		raise NotImplementedError("Attr lowering not implemented yet")

	def visit_expr_Index(self, expr: ast.Index) -> H.HExpr:
		raise NotImplementedError("Index lowering not implemented yet")

	def visit_expr_Unary(self, expr: ast.Unary) -> H.HExpr:
		raise NotImplementedError("Unary lowering not implemented yet")

	def visit_expr_Binary(self, expr: ast.Binary) -> H.HExpr:
		raise NotImplementedError("Binary lowering not implemented yet")

	def visit_expr_ArrayLiteral(self, expr: ast.ArrayLiteral) -> H.HExpr:
		raise NotImplementedError("Array literal lowering not implemented yet")

	def visit_expr_ExceptionCtor(self, expr: ast.ExceptionCtor) -> H.HExpr:
		raise NotImplementedError("Exception ctor lowering not implemented yet")

	def visit_expr_Ternary(self, expr: ast.Ternary) -> H.HExpr:
		raise NotImplementedError("Ternary lowering not implemented yet")

	def visit_expr_TryCatchExpr(self, expr: ast.TryCatchExpr) -> H.HExpr:
		raise NotImplementedError("Try/catch expr lowering not implemented yet")

	def visit_stmt_AssignStmt(self, stmt: ast.AssignStmt) -> H.HStmt:
		raise NotImplementedError("Assign lowering not implemented yet")

	def visit_stmt_IfStmt(self, stmt: ast.IfStmt) -> H.HStmt:
		raise NotImplementedError("If lowering not implemented yet")

	def visit_stmt_TryStmt(self, stmt: ast.TryStmt) -> H.HStmt:
		raise NotImplementedError("Try lowering not implemented yet")

	def visit_stmt_WhileStmt(self, stmt: ast.WhileStmt) -> H.HStmt:
		raise NotImplementedError("While lowering not implemented yet")

	def visit_stmt_ForStmt(self, stmt: ast.ForStmt) -> H.HStmt:
		raise NotImplementedError("For lowering not implemented yet")

	def visit_stmt_BreakStmt(self, stmt: ast.BreakStmt) -> H.HStmt:
		return H.HBreak()

	def visit_stmt_ContinueStmt(self, stmt: ast.ContinueStmt) -> H.HStmt:
		return H.HContinue()

	def visit_stmt_ThrowStmt(self, stmt: ast.ThrowStmt) -> H.HStmt:
		raise NotImplementedError("Throw lowering not implemented yet")

	def visit_stmt_RaiseStmt(self, stmt: ast.RaiseStmt) -> H.HStmt:
		raise NotImplementedError("Raise lowering not implemented yet")


__all__ = ["AstToHIR"]


__all__ = ["AstToHIR"]
