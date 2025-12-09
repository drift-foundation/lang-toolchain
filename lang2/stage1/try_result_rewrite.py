# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Try-result sugar desugaring (stage1).

Pipeline placement:
  AST (with TryExpr/expr?) → HIR (HTryResult) → [this pass expands HTryResult] → MIR → SSA → LLVM/obj

Goal:
  HTryResult is a syntactic marker for result-driven try (`expr?`). This pass
  rewrites it into explicit HIR using normal constructs so lowering/SSA do not
  need to know about the sugar.

Canonical expansion:

    val __res = expr
    if __res.is_err() {
        throw __res.unwrap_err();
    }
    val tmp = __res.unwrap();
    // tmp is the value of the sugar expression

Notes:
  * We always evaluate the operand once (temp __resN).
  * We reuse normal throw semantics, so nested try/catch/unwind work unchanged.
  * This pass is purely structural; no type checking is done here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from . import hir_nodes as H


@dataclass
class _RewriteResult:
	"""
	Helper to carry a sequence of statements plus a rewritten expression.

	`stmts` should be inserted before evaluating `expr`.
	"""
	stmts: List[H.HStmt]
	expr: H.HExpr


class TryResultRewriter:
	"""
	Rewrite HTryResult expressions into explicit HIR.

	This pass walks HIR blocks/statements/expressions, expanding HTryResult
	into the canonical `if is_err { throw unwrap_err } else { unwrap }` pattern,
	inserting the necessary temporaries to preserve single evaluation of the
	operand. All other nodes are left intact apart from recursive rewriting of
	their children.
	"""

	def __init__(self) -> None:
		self._temp_counter = 0

	def _fresh(self, prefix: str) -> str:
		"""Generate a deterministic fresh SSA-style name."""
		self._temp_counter += 1
		return f"{prefix}{self._temp_counter}"

	# Public entry point -------------------------------------------------

	def rewrite_block(self, block: H.HBlock) -> H.HBlock:
		"""Rewrite an HBlock in-place, returning a new HBlock with HTryResult expanded."""
		new_stmts: List[H.HStmt] = []
		for stmt in block.statements:
			new_stmts.extend(self._rewrite_stmt(stmt))
		return H.HBlock(statements=new_stmts)

	# Statement rewriting -----------------------------------------------

	def _rewrite_stmt(self, stmt: H.HStmt) -> List[H.HStmt]:
		"""Return a list of rewritten statements replacing the input stmt."""
		if isinstance(stmt, H.HExprStmt):
			prefix, expr = self._rewrite_expr(stmt.expr)
			return prefix + [H.HExprStmt(expr=expr)]
		if isinstance(stmt, H.HThrow):
			# throw expr? => prefixes + throw rewritten expr
			prefix, expr = self._rewrite_expr(stmt.value)
			return prefix + [H.HThrow(value=expr)]
		if isinstance(stmt, H.HLet):
			prefix, expr = self._rewrite_expr(stmt.value)
			return prefix + [H.HLet(name=stmt.name, value=expr)]
		if isinstance(stmt, H.HAssign):
			prefix_target, target = self._rewrite_expr(stmt.target)
			prefix_value, value = self._rewrite_expr(stmt.value)
			return prefix_target + prefix_value + [H.HAssign(target=target, value=value)]
		if isinstance(stmt, H.HReturn):
			if stmt.value is None:
				return [stmt]
			prefix, expr = self._rewrite_expr(stmt.value)
			return prefix + [H.HReturn(value=expr)]
		if isinstance(stmt, H.HIf):
			prefix, cond = self._rewrite_expr(stmt.cond)
			then_block = self.rewrite_block(stmt.then_block)
			else_block = self.rewrite_block(stmt.else_block) if stmt.else_block else None
			return prefix + [H.HIf(cond=cond, then_block=then_block, else_block=else_block)]
		if isinstance(stmt, H.HLoop):
			body = self.rewrite_block(stmt.body)
			return [H.HLoop(body=body)]
		if isinstance(stmt, H.HTry):
			body = self.rewrite_block(stmt.body)
			catches = [
				H.HCatchArm(event_name=arm.event_name, binder=arm.binder, block=self.rewrite_block(arm.block))
				for arm in stmt.catches
			]
			return [H.HTry(body=body, catches=catches)]
		# Simple statements unchanged
		if isinstance(stmt, (H.HBreak, H.HContinue)):
			return [stmt]
		raise NotImplementedError(f"TryResultRewriter does not handle stmt {type(stmt).__name__}")

	# Expression rewriting ----------------------------------------------

	def _rewrite_expr(self, expr: H.HExpr) -> Tuple[List[H.HStmt], H.HExpr]:
		"""Return (prefix_stmts, rewritten_expr) for a given expression."""
		if isinstance(expr, H.HTryResult):
			return self._expand_try_result(expr)
		if isinstance(expr, H.HVar):
			return [], expr
		if isinstance(expr, (H.HLiteralInt, H.HLiteralString, H.HLiteralBool)):
			return [], expr
		if isinstance(expr, H.HCall):
			prefix_fn, fn = self._rewrite_expr(expr.fn)
			arg_prefixes: List[H.HStmt] = []
			new_args: List[H.HExpr] = []
			for a in expr.args:
				pfx, arg = self._rewrite_expr(a)
				arg_prefixes.extend(pfx)
				new_args.append(arg)
			return prefix_fn + arg_prefixes, H.HCall(fn=fn, args=new_args)
		if isinstance(expr, H.HMethodCall):
			prefix_recv, recv = self._rewrite_expr(expr.receiver)
			arg_prefixes: List[H.HStmt] = []
			new_args: List[H.HExpr] = []
			for a in expr.args:
				pfx, arg = self._rewrite_expr(a)
				arg_prefixes.extend(pfx)
				new_args.append(arg)
			return prefix_recv + arg_prefixes, H.HMethodCall(receiver=recv, method_name=expr.method_name, args=new_args)
		if isinstance(expr, H.HField):
			prefix_subj, subj = self._rewrite_expr(expr.subject)
			return prefix_subj, H.HField(subject=subj, name=expr.name)
		if isinstance(expr, H.HIndex):
			pfx_subj, subj = self._rewrite_expr(expr.subject)
			pfx_idx, idx = self._rewrite_expr(expr.index)
			return pfx_subj + pfx_idx, H.HIndex(subject=subj, index=idx)
		if isinstance(expr, H.HUnary):
			pfx, inner = self._rewrite_expr(expr.expr)
			return pfx, H.HUnary(op=expr.op, expr=inner)
		if isinstance(expr, H.HBinary):
			pfx_l, left = self._rewrite_expr(expr.left)
			pfx_r, right = self._rewrite_expr(expr.right)
			return pfx_l + pfx_r, H.HBinary(op=expr.op, left=left, right=right)
		if isinstance(expr, H.HTernary):
			pfx_c, cond = self._rewrite_expr(expr.cond)
			pfx_t, then = self._rewrite_expr(expr.then_expr)
			pfx_e, els = self._rewrite_expr(expr.else_expr)
			return pfx_c + pfx_t + pfx_e, H.HTernary(cond=cond, then_expr=then, else_expr=els)
		if isinstance(expr, H.HResultOk):
			pfx, inner = self._rewrite_expr(expr.value)
			return pfx, H.HResultOk(value=inner)
		if isinstance(expr, H.HDVInit):
			new_args: List[H.HExpr] = []
			pfx: List[H.HStmt] = []
			for a in expr.args:
				arg_pfx, arg = self._rewrite_expr(a)
				pfx.extend(arg_pfx)
				new_args.append(arg)
			return pfx, H.HDVInit(dv_type_name=expr.dv_type_name, args=new_args)
		if isinstance(expr, H.HArrayLiteral):
			elem_pfx: List[H.HStmt] = []
			new_elems: List[H.HExpr] = []
			for e in expr.elements:
				pfx, el = self._rewrite_expr(e)
				elem_pfx.extend(pfx)
				new_elems.append(el)
			return elem_pfx, H.HArrayLiteral(elements=new_elems)
		raise NotImplementedError(f"TryResultRewriter does not handle expr {type(expr).__name__}")

	def _expand_try_result(self, expr: H.HTryResult) -> Tuple[List[H.HStmt], H.HExpr]:
		"""
		Desugar HTryResult(expr) into explicit HIR:

		  val __res = <expr>
		  if __res.is_err() { throw __res.unwrap_err(); }
		  val __val = __res.unwrap();
		  // expression value is HVar(__val)
		"""
		pfx, inner = self._rewrite_expr(expr.expr)
		res_name = self._fresh("__res")
		val_name = self._fresh("__val")

		# __res = expr
		assign_res = H.HLet(name=res_name, value=inner)

		# if __res.is_err() { throw __res.unwrap_err(); }
		is_err_call = H.HMethodCall(receiver=H.HVar(name=res_name), method_name="is_err", args=[])
		unwrap_err_call = H.HMethodCall(receiver=H.HVar(name=res_name), method_name="unwrap_err", args=[])
		throw_stmt = H.HThrow(value=unwrap_err_call)
		if_stmt = H.HIf(
			cond=is_err_call,
			then_block=H.HBlock(statements=[throw_stmt]),
			else_block=None,
		)

		# __val = __res.unwrap()
		unwrap_call = H.HMethodCall(receiver=H.HVar(name=res_name), method_name="unwrap", args=[])
		assign_val = H.HLet(name=val_name, value=unwrap_call)

		stmts: List[H.HStmt] = []
		stmts.extend(pfx)
		stmts.extend([assign_res, if_stmt, assign_val])
		return stmts, H.HVar(name=val_name)
