# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
AST → HIR lowering (sugar removal entry point).

Pipeline placement:
  AST (lang2/stage0/ast.py) → HIR (lang2/stage1/hir_nodes.py) → MIR → SSA → LLVM/obj

This pass takes the parsed AST and produces the sugar-free HIR defined in
`lang2/stage1/hir_nodes.py`. It currently lowers basic expressions/statements
(literals, vars, unary/binary ops, field/index, let/assign/if/return/break/continue/expr-stmt,
plain/method calls); control-flow sugar and other complex constructs still fail
loudly so they can be filled in incrementally.

Entry points (stage API):
  - lower_expr: lower a single expression to HIR
  - lower_stmt: lower a single statement to HIR
  - lower_block: lower a list of statements into an HBlock
"""

from __future__ import annotations

from typing import List

# Import stage0 AST via package API to keep stage layering explicit.
from lang2.stage0 import ast
from . import hir_nodes as H


class AstToHIR:
	"""
	AST → HIR lowering (sugar removal happens here).

	Entry points (stage API):
	  - lower_expr: lower a single expression to HIR
	  - lower_stmt: lower a single statement to HIR
	  - lower_block: lower a list of statements into an HBlock
	Helper visitors are prefixed with an underscore; anything without a leading
	underscore is intended for callers of this stage.
	"""

	def lower_expr(self, expr: ast.Expr) -> H.HExpr:
		"""
		Dispatch an AST expression to a per-type visitor.

		Fail-loud behavior is intentional: new AST node types should add
		a visitor rather than being silently ignored.

		Public stage API: callers should use lower_expr/stmt/block; helper
		visitors are private (_visit_*).
		"""
		method = getattr(self, f"_visit_expr_{type(expr).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No HIR lowering for expr type {type(expr).__name__}")
		return method(expr)

	def lower_stmt(self, stmt: ast.Stmt) -> H.HStmt:
		"""
		Dispatch an AST statement to a per-type visitor.

		Fail-loud behavior is intentional: new AST node types should add
		a visitor rather than being silently ignored.

		Public stage API: callers should use lower_expr/stmt/block; helper
		visitors are private (_visit_*).
		"""
		method = getattr(self, f"_visit_stmt_{type(stmt).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No HIR lowering for stmt type {type(stmt).__name__}")
		return method(stmt)

	def lower_block(self, stmts: List[ast.Stmt]) -> H.HBlock:
		"""Lower a list of AST statements into an HIR block."""
		return H.HBlock(statements=[self.lower_stmt(s) for s in stmts])

	# --- minimal implemented handlers (trivial cases only) ---

	def _visit_expr_Name(self, expr: ast.Name) -> H.HExpr:
		"""Names become HVar; binding resolution happens later."""
		return H.HVar(name=expr.ident)

	def _visit_expr_Literal(self, expr: ast.Literal) -> H.HExpr:
		"""Map literal to the appropriate HIR literal node."""
		if isinstance(expr.value, bool):
			return H.HLiteralBool(value=bool(expr.value))
		if isinstance(expr.value, int):
			return H.HLiteralInt(value=int(expr.value))
		if isinstance(expr.value, str):
			return H.HLiteralString(value=str(expr.value))
		raise NotImplementedError(f"Literal of unsupported type: {type(expr.value).__name__}")

	def _visit_stmt_LetStmt(self, stmt: ast.LetStmt) -> H.HStmt:
		"""Immutable binding introduction."""
		return H.HLet(name=stmt.name, value=self.lower_expr(stmt.value))

	def _visit_stmt_ReturnStmt(self, stmt: ast.ReturnStmt) -> H.HStmt:
		"""Return with optional value."""
		val = self.lower_expr(stmt.value) if stmt.value is not None else None
		return H.HReturn(value=val)

	def _visit_stmt_ExprStmt(self, stmt: ast.ExprStmt) -> H.HStmt:
		"""Expression as statement (value discarded)."""
		return H.HExprStmt(expr=self.lower_expr(stmt.expr))

	# --- stubs for remaining nodes ---

	def _visit_expr_Call(self, expr: ast.Call) -> H.HExpr:
		"""
		Lower calls:
		  - method sugar: Call(func=Attr(receiver, name), args=...) → HMethodCall
		  - otherwise: plain HCall(fn_expr, args)
		DV-specific constructors are handled in _visit_expr_ExceptionCtor.
		"""
		# Method call sugar: receiver.method(args)
		if isinstance(expr.func, ast.Attr):
			receiver = self.lower_expr(expr.func.value)
			args = [self.lower_expr(a) for a in expr.args]
			return H.HMethodCall(receiver=receiver, method_name=expr.func.attr, args=args)

		# Plain function call.
		fn_expr = self.lower_expr(expr.func)
		args = [self.lower_expr(a) for a in expr.args]
		return H.HCall(fn=fn_expr, args=args)

	def _visit_expr_Attr(self, expr: ast.Attr) -> H.HExpr:
		"""Field access: subject.name (no method/placeholder sugar here)."""
		subject = self.lower_expr(expr.value)
		return H.HField(subject=subject, name=expr.attr)

	def _visit_expr_Index(self, expr: ast.Index) -> H.HExpr:
		"""Indexing: subject[index] (no placeholder/index sugar here)."""
		subject = self.lower_expr(expr.value)
		index = self.lower_expr(expr.index)
		return H.HIndex(subject=subject, index=index)

	def _visit_expr_Unary(self, expr: ast.Unary) -> H.HExpr:
		"""
		Unary op lowering. Only maps the simple ops; more exotic ops can be
		added later with explicit enum entries.
		"""
		op_map = {
			"-": H.UnaryOp.NEG,
			"!": H.UnaryOp.NOT,
			"~": H.UnaryOp.BIT_NOT,
		}
		try:
			op = op_map[expr.op]
		except KeyError:
			raise NotImplementedError(f"Unsupported unary op: {expr.op}")
		return H.HUnary(op=op, expr=self.lower_expr(expr.operand))

	def _visit_expr_Binary(self, expr: ast.Binary) -> H.HExpr:
		"""
		Binary op lowering. Short-circuit behavior for &&/|| is NOT lowered
		here; that can be desugared later if needed.
		"""
		op_map = {
			"+": H.BinaryOp.ADD,
			"-": H.BinaryOp.SUB,
			"*": H.BinaryOp.MUL,
			"/": H.BinaryOp.DIV,
			"%": H.BinaryOp.MOD,
			"&": H.BinaryOp.BIT_AND,
			"|": H.BinaryOp.BIT_OR,
			"^": H.BinaryOp.BIT_XOR,
			"<<": H.BinaryOp.SHL,
			">>": H.BinaryOp.SHR,
			"==": H.BinaryOp.EQ,
			"!=": H.BinaryOp.NE,
			"<": H.BinaryOp.LT,
			"<=": H.BinaryOp.LE,
			">": H.BinaryOp.GT,
			">=": H.BinaryOp.GE,
			"&&": H.BinaryOp.AND,
			"||": H.BinaryOp.OR,
		}
		try:
			op = op_map[expr.op]
		except KeyError:
			raise NotImplementedError(f"Unsupported binary op: {expr.op}")
		left = self.lower_expr(expr.left)
		right = self.lower_expr(expr.right)
		return H.HBinary(op=op, left=left, right=right)

	def _visit_expr_ArrayLiteral(self, expr: ast.ArrayLiteral) -> H.HExpr:
		raise NotImplementedError("Array literal lowering not implemented yet")

	def _visit_expr_ExceptionCtor(self, expr: ast.ExceptionCtor) -> H.HExpr:
		"""
		Exception/diagnostic constructor → HDVInit placeholder.

		Fields are a mapping name -> expression; ordering is not enforced yet.
		"""
		arg_exprs = [self.lower_expr(v) for _, v in expr.fields.items()]
		return H.HDVInit(dv_type_name=expr.name, args=arg_exprs)

	def _visit_expr_Ternary(self, expr: ast.Ternary) -> H.HExpr:
		raise NotImplementedError("Ternary lowering not implemented yet")

	def _visit_expr_TryCatchExpr(self, expr: ast.TryCatchExpr) -> H.HExpr:
		raise NotImplementedError("Try/catch expr lowering not implemented yet")

	def _visit_stmt_AssignStmt(self, stmt: ast.AssignStmt) -> H.HStmt:
		target = self.lower_expr(stmt.target)
		value = self.lower_expr(stmt.value)
		return H.HAssign(target=target, value=value)

	def _visit_stmt_IfStmt(self, stmt: ast.IfStmt) -> H.HStmt:
		cond = self.lower_expr(stmt.cond)
		then_block = self.lower_block(stmt.then_block)
		else_block = self.lower_block(stmt.else_block) if stmt.else_block else None
		return H.HIf(cond=cond, then_block=then_block, else_block=else_block)

	def _visit_stmt_TryStmt(self, stmt: ast.TryStmt) -> H.HStmt:
		raise NotImplementedError("Try lowering not implemented yet")

	def _visit_stmt_WhileStmt(self, stmt: ast.WhileStmt) -> H.HStmt:
		"""
		Desugar:
		  while cond { body }

		into:
		  loop {
		    if cond { body } else { break }
		  }
		so HIR only needs HLoop/HIf/HBreak, and MIR reuses existing lowering.
		"""
		cond_hir = self.lower_expr(stmt.cond)
		then_block = self.lower_block(stmt.body)
		else_block = H.HBlock(statements=[H.HBreak()])
		if_stmt = H.HIf(cond=cond_hir, then_block=then_block, else_block=else_block)
		loop_body = H.HBlock(statements=[if_stmt])
		return H.HLoop(body=loop_body)

	def _visit_stmt_ForStmt(self, stmt: ast.ForStmt) -> H.HStmt:
		"""
		Desugar a simple for-each style loop:
		  for iter_var in iterable { body }

		into:
		  var __iter = iterable
		  loop {
		    if __iter.has_next() {
		      var iter_var = __iter.next()
		      body
		    } else {
		      break
		    }
		  }

		This is a placeholder desugaring; the actual iteration protocol will
		be defined later. For now we keep it simple and reuse loop/if/break.
		"""
		# TODO: Replace with a real iterator protocol; for now, synthesize a trivial
		# pattern: if iterable { body } else break. This keeps HIR legal without
		# committing to iteration semantics.
		iter_hir = self.lower_expr(stmt.iterable)
		# Fake condition: iterable itself (truthy) – placeholder.
		cond_hir = iter_hir
		then_block = self.lower_block(stmt.body)
		else_block = H.HBlock(statements=[H.HBreak()])
		if_stmt = H.HIf(cond=cond_hir, then_block=then_block, else_block=else_block)
		loop_stmt = H.HLoop(body=H.HBlock(statements=[if_stmt]))
		return loop_stmt

	def _visit_stmt_BreakStmt(self, stmt: ast.BreakStmt) -> H.HStmt:
		return H.HBreak()

	def _visit_stmt_ContinueStmt(self, stmt: ast.ContinueStmt) -> H.HStmt:
		return H.HContinue()

	def _visit_stmt_ThrowStmt(self, stmt: ast.ThrowStmt) -> H.HStmt:
		raise NotImplementedError("Throw lowering not implemented yet")

	def _visit_stmt_RaiseStmt(self, stmt: ast.RaiseStmt) -> H.HStmt:
		raise NotImplementedError("Raise lowering not implemented yet")


__all__ = ["AstToHIR"]
