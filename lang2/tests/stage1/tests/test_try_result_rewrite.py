# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Tests for result-driven try sugar desugaring (HTryResult → explicit HIR).

We do not touch MIR/SSA here; the rewriter must expand HTryResult into the
canonical `if is_err { throw unwrap_err } else { unwrap }` shape using
normal HIR nodes. This keeps the sugar out of lowering.
"""

from lang2 import stage1 as H
from lang2.stage1.try_result_rewrite import TryResultRewriter


def test_try_result_desugars_to_if_throw_unwrap():
	"""HTryResult(expr) expands to eval once + is_err throw + unwrap."""
	rewriter = TryResultRewriter()
	# Build HIR with a single expression statement containing HTryResult.
	hir = H.HBlock(statements=[H.HExprStmt(expr=H.HTryResult(expr=H.HVar(name="res_expr")))])

	rewritten = rewriter.rewrite_block(hir)

	# Expect three statements before the final expression result:
	# 1) __resN = res_expr
	# 2) if __resN.is_err() { throw __resN.unwrap_err(); }
	# 3) __valN = __resN.unwrap()
	stmts = rewritten.statements
	assert len(stmts) == 4
	let_res, if_stmt, let_val, expr_stmt = stmts

	assert isinstance(let_res, H.HLet)
	res_name = let_res.name
	assert isinstance(let_res.value, H.HVar)
	assert let_res.value.name == "res_expr"

	assert isinstance(if_stmt, H.HIf)
	assert isinstance(if_stmt.cond, H.HMethodCall)
	assert if_stmt.cond.method_name == "is_err"
	assert isinstance(if_stmt.then_block, H.HBlock)
	throw_stmt = if_stmt.then_block.statements[0]
	assert isinstance(throw_stmt, H.HThrow)
	assert isinstance(throw_stmt.value, H.HMethodCall)
	assert throw_stmt.value.method_name == "unwrap_err"

	assert isinstance(let_val, H.HLet)
	val_name = let_val.name
	assert isinstance(let_val.value, H.HMethodCall)
	assert let_val.value.method_name == "unwrap"

	assert isinstance(expr_stmt, H.HExprStmt)
	assert isinstance(expr_stmt.expr, H.HVar)
	assert expr_stmt.expr.name == val_name
	# is_err/unwrap/unwrap_err should all target the same __resN temp.
	assert if_stmt.cond.receiver.name == res_name
	assert throw_stmt.value.receiver.name == res_name
	assert let_val.value.receiver.name == res_name


def test_try_result_inside_return_desugars_cleanly():
	"""HTryResult in a Return gets expanded with prefix statements before the return."""
	rewriter = TryResultRewriter()
	hir = H.HBlock(statements=[H.HReturn(value=H.HTryResult(expr=H.HVar(name="res_expr")))])

	rewritten = rewriter.rewrite_block(hir)
	stmts = rewritten.statements
	# Expect: let __res, if is_err { throw ... }, let __val, return __val
	assert len(stmts) == 4
	assert isinstance(stmts[-1], H.HReturn)
	assert isinstance(stmts[-1].value, H.HVar)


def test_try_result_in_throw_rewrites_with_prefixes():
	"""throw expr? should expand prefixes and throw the rewritten value temp."""
	rewriter = TryResultRewriter()
	hir = H.HBlock(statements=[H.HThrow(value=H.HTryResult(expr=H.HVar(name="e_res")))])

	rewritten = rewriter.rewrite_block(hir)
	# Expect: let __res, if is_err { throw __res.unwrap_err }, let __val, throw __val
	assert len(rewritten.statements) == 4
	assert isinstance(rewritten.statements[-1], H.HThrow)
	assert isinstance(rewritten.statements[-1].value, H.HVar)
	assert rewritten.statements[-1].value.name.startswith("__val")


def test_try_result_in_call_argument_rewrites_and_unwraps():
	"""
	Call with a try-result argument should insert prefixes before the call and
	pass the unwrapped value to the callee.
	"""
	rewriter = TryResultRewriter()
	hir = H.HBlock(
		statements=[
			H.HExprStmt(
				expr=H.HCall(
					fn=H.HVar(name="f"),
					args=[H.HTryResult(expr=H.HVar(name="arg_res"))],
				)
			)
		]
	)

	rewritten = rewriter.rewrite_block(hir)
	# Expect: let __res, if is_err { throw ... }, let __val, call f(__val)
	assert len(rewritten.statements) == 4
	call_stmt = rewritten.statements[-1]
	assert isinstance(call_stmt, H.HExprStmt)
	call_expr = call_stmt.expr
	assert isinstance(call_expr, H.HCall)
	assert isinstance(call_expr.args[0], H.HVar)
	assert call_expr.args[0].name.startswith("__val")
