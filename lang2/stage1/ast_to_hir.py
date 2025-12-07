# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
AST → HIR lowering (sugar removal entry point).

Pipeline placement:
  AST (lang2/stage0/ast.py) → HIR (lang2/stage1/hir_nodes.py) → MIR → SSA → LLVM/obj

This pass takes the parsed AST and produces the sugar-free HIR defined in
`lang2/stage1/hir_nodes.py`. It currently lowers expressions/statements:
  - literals, vars, unary/binary ops, field/index
  - let/assign/if/while/for/return/break/continue/expr-stmt
  - plain/method calls, exception ctors, ternary, try/throw
Remaining sugar (raise/rethrow, TryCatchExpr, array literals) still fails loudly
so it can be filled in incrementally.

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

	def __init__(self):
		# Simple counter for internal temporaries (e.g., for-loop bindings).
		self._temp_counter = 0

	def _fresh_temp(self, prefix: str = "__tmp") -> str:
		"""Allocate a unique temporary name with a given prefix."""
		self._temp_counter += 1
		return f"{prefix}{self._temp_counter}"

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
		  - Result.Ok(...) sugar -> HResultOk
		  - otherwise: plain HCall(fn_expr, args)
		DV-specific constructors are handled in _visit_expr_ExceptionCtor.
		"""
		# Recognize Result.Ok constructor in source -> HResultOk for FnResult.
		if isinstance(expr.func, ast.Name) and expr.func.ident == "Ok" and len(expr.args) == 1:
			return H.HResultOk(value=self.lower_expr(expr.args[0]))

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
		"""Lower ternary expression: cond ? then_expr : else_expr."""
		cond_h = self.lower_expr(expr.cond)
		then_h = self.lower_expr(expr.then_expr)
		else_h = self.lower_expr(expr.else_expr)
		return H.HTernary(cond=cond_h, then_expr=then_h, else_expr=else_h)

	def _visit_expr_TryExpr(self, expr: ast.TryExpr) -> H.HExpr:
		"""
		Result-driven try sugar marker (expr? / try expr).

		We lower to HTryResult and leave desugaring to a dedicated HIR rewrite
		pass (see stage1/try_result_rewrite.py). That keeps this pass sugar-only.
		"""
		return H.HTryResult(expr=self.lower_expr(expr.expr))

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
		"""
		Lower statement-form try/catch with multiple arms into HTry + HCatchArm.

		  try { body }
		  catch EventName(e) { handler }
		  catch (e) { handler }
		  catch { handler }

		Arms are preserved in source order.
		"""
		body_block = self.lower_block(stmt.body)
		if not stmt.catches:
			raise NotImplementedError("Try lowering requires at least one catch arm")

		catch_arms: list[H.HCatchArm] = []
		for arm in stmt.catches:
			event_name = arm.event
			binder = arm.binder
			handler_block = self.lower_block(arm.block)
			catch_arms.append(H.HCatchArm(event_name=event_name, binder=binder, block=handler_block))

		return H.HTry(body=body_block, catches=catch_arms)

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
		Desugar:
		  for iter_var in iterable { body }

		into the iterator protocol using Optional:
		  let __for_iterable = iterable
		  let __for_iter = __for_iterable.iter()
		  loop {
		    let __for_next = __for_iter.next()
		    if __for_next.is_some() {
		      let iter_var = __for_next.unwrap()
		      body
		    } else {
		      break
		    }
		  }

		Notes:
		  - iter_var is currently an identifier (pattern support can be added later).
		  - iterable expression is evaluated exactly once.
		"""
		# 1) Evaluate iterable once and bind.
		iterable_expr = self.lower_expr(stmt.iterable)
		iterable_name = self._fresh_temp("__for_iterable")
		iterable_let = H.HLet(name=iterable_name, value=iterable_expr)

		# 2) Build iterator: __for_iter = __for_iterable.iter()
		iter_name = self._fresh_temp("__for_iter")
		iter_call = H.HMethodCall(receiver=H.HVar(iterable_name), method_name="iter", args=[])
		iter_let = H.HLet(name=iter_name, value=iter_call)

		# 3) In loop: __for_next = __for_iter.next()
		next_name = self._fresh_temp("__for_next")
		next_call = H.HMethodCall(receiver=H.HVar(iter_name), method_name="next", args=[])
		next_let = H.HLet(name=next_name, value=next_call)

		# 4) Condition: __for_next.is_some()
		cond = H.HMethodCall(receiver=H.HVar(next_name), method_name="is_some", args=[])

		# 5) Then: let iter_var = __for_next.unwrap(); body...
		unwrap_call = H.HMethodCall(receiver=H.HVar(next_name), method_name="unwrap", args=[])
		bind_iter = H.HLet(name=stmt.iter_var, value=unwrap_call)
		body_block = self.lower_block(stmt.body)
		then_block = H.HBlock(statements=[bind_iter] + body_block.statements)

		# 6) Else: break
		else_block = H.HBlock(statements=[H.HBreak()])

		if_stmt = H.HIf(cond=cond, then_block=then_block, else_block=else_block)
		loop_body = H.HBlock(statements=[next_let, if_stmt])
		loop_stmt = H.HLoop(body=loop_body)

		# 7) Wrap iterable/iter bindings in a block to scope them.
		return H.HBlock(statements=[iterable_let, iter_let, loop_stmt])

	def _visit_stmt_BreakStmt(self, stmt: ast.BreakStmt) -> H.HStmt:
		return H.HBreak()

	def _visit_stmt_ContinueStmt(self, stmt: ast.ContinueStmt) -> H.HStmt:
		return H.HContinue()

	def _visit_stmt_ThrowStmt(self, stmt: ast.ThrowStmt) -> H.HStmt:
		"""Lower throw statement to HThrow; semantics are implemented in later stages."""
		value_h = self.lower_expr(stmt.value)
		return H.HThrow(value=value_h)

	def _visit_stmt_RaiseStmt(self, stmt: ast.RaiseStmt) -> H.HStmt:
		raise NotImplementedError("Raise lowering not implemented yet")


__all__ = ["AstToHIR"]
