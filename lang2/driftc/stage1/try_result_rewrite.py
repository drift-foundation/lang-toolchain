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
from lang2.driftc.core.span import Span


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
			# Preserve HLet metadata. Normalization passes must not silently change
			# `val` ↔ `var` semantics, drop binding identity, or erase type annotations.
			return prefix + [
				H.HLet(
					name=stmt.name,
					value=expr,
					declared_type_expr=getattr(stmt, "declared_type_expr", None),
					binding_id=getattr(stmt, "binding_id", None),
					is_mutable=bool(getattr(stmt, "is_mutable", False)),
				)
			]
		if isinstance(stmt, H.HAssign):
			prefix_target, target = self._rewrite_expr(stmt.target)
			prefix_value, value = self._rewrite_expr(stmt.value)
			return prefix_target + prefix_value + [H.HAssign(target=target, value=value)]
		if hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
			prefix_target, target = self._rewrite_expr(stmt.target)
			prefix_value, value = self._rewrite_expr(stmt.value)
			return prefix_target + prefix_value + [
				H.HAugAssign(target=target, op=getattr(stmt, "op", "+="), value=value, loc=getattr(stmt, "loc", Span()))
			]
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
		# Block statements introduce a nested statement scope (used by desugarings
		# like `for` which need to introduce hidden temporaries without leaking
		# them to the outer scope). Normalization must recurse into them.
		if isinstance(stmt, H.HBlock):
			return [self.rewrite_block(stmt)]
		if isinstance(stmt, H.HTry):
			body = self.rewrite_block(stmt.body)
			catches = [
				H.HCatchArm(
					event_fqn=arm.event_fqn,
					binder=arm.binder,
					block=self.rewrite_block(arm.block),
					loc=arm.loc,
				)
				for arm in stmt.catches
			]
			return [H.HTry(body=body, catches=catches)]
		# Simple statements unchanged
		if isinstance(stmt, (H.HBreak, H.HContinue, H.HRethrow)):
			return [stmt]
		raise NotImplementedError(f"TryResultRewriter does not handle stmt {type(stmt).__name__}")

	# Expression rewriting ----------------------------------------------

	def _rewrite_expr(self, expr: H.HExpr) -> Tuple[List[H.HStmt], H.HExpr]:
		"""Return (prefix_stmts, rewritten_expr) for a given expression."""
		if isinstance(expr, H.HTryResult):
			return self._expand_try_result(expr)
		if isinstance(expr, H.HVar):
			return [], expr
		if isinstance(expr, (H.HLiteralInt, H.HLiteralFloat, H.HLiteralString, H.HLiteralBool)):
			return [], expr
		if isinstance(expr, H.HCall):
			prefix_fn, fn = self._rewrite_expr(expr.fn)
			arg_prefixes: List[H.HStmt] = []
			new_args: List[H.HExpr] = []
			for a in expr.args:
				pfx, arg = self._rewrite_expr(a)
				arg_prefixes.extend(pfx)
				new_args.append(arg)
			kw_prefixes: List[H.HStmt] = []
			new_kwargs: list[H.HKwArg] = []
			for kw in getattr(expr, "kwargs", []) or []:
				kpfx, kval = self._rewrite_expr(kw.value)
				kw_prefixes.extend(kpfx)
				new_kwargs.append(H.HKwArg(name=kw.name, value=kval, loc=kw.loc))
			return prefix_fn + arg_prefixes + kw_prefixes, H.HCall(fn=fn, args=new_args, kwargs=new_kwargs)
		if isinstance(expr, H.HMethodCall):
			prefix_recv, recv = self._rewrite_expr(expr.receiver)
			arg_prefixes: List[H.HStmt] = []
			new_args: List[H.HExpr] = []
			for a in expr.args:
				pfx, arg = self._rewrite_expr(a)
				arg_prefixes.extend(pfx)
				new_args.append(arg)
			kw_prefixes: List[H.HStmt] = []
			new_kwargs: list[H.HKwArg] = []
			for kw in getattr(expr, "kwargs", []) or []:
				kpfx, kval = self._rewrite_expr(kw.value)
				kw_prefixes.extend(kpfx)
				new_kwargs.append(H.HKwArg(name=kw.name, value=kval, loc=kw.loc))
			return prefix_recv + arg_prefixes + kw_prefixes, H.HMethodCall(
				receiver=recv, method_name=expr.method_name, args=new_args, kwargs=new_kwargs
			)
		if isinstance(expr, H.HField):
			prefix_subj, subj = self._rewrite_expr(expr.subject)
			return prefix_subj, H.HField(subject=subj, name=expr.name)
		if isinstance(expr, H.HIndex):
			pfx_subj, subj = self._rewrite_expr(expr.subject)
			pfx_idx, idx = self._rewrite_expr(expr.index)
			return pfx_subj + pfx_idx, H.HIndex(subject=subj, index=idx)
		if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
			# Canonical place expressions can still contain index sub-expressions.
			# We allow try-result sugar inside those index expressions by returning
			# any prefixes that must execute before evaluating the place.
			pfx: List[H.HStmt] = []
			new_projs: list[H.HPlaceProj] = []
			for proj in expr.projections:
				if isinstance(proj, H.HPlaceIndex):
					ipfx, idx = self._rewrite_expr(proj.index)
					pfx.extend(ipfx)
					new_projs.append(H.HPlaceIndex(index=idx))
				else:
					new_projs.append(proj)
			return pfx, H.HPlaceExpr(base=expr.base, projections=new_projs, loc=getattr(expr, "loc", Span()))
		if isinstance(expr, H.HBorrow):
			# Borrow expressions are pure (they only observe a place). We still
			# rewrite nested try-result sugar inside the subject expression so
			# `&try foo()?` behaves consistently once temp materialization exists.
			pfx, subj = self._rewrite_expr(expr.subject)
			return pfx, H.HBorrow(subject=subj, is_mut=expr.is_mut)
		if isinstance(expr, getattr(H, "HMove", ())):
			pfx, subj = self._rewrite_expr(expr.subject)
			return pfx, H.HMove(subject=subj, loc=getattr(expr, "loc", Span()))
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
		if isinstance(expr, H.HExceptionInit):
			pfx: List[H.HStmt] = []
			new_pos: List[H.HExpr] = []
			for a in expr.pos_args:
				apfx, av = self._rewrite_expr(a)
				pfx.extend(apfx)
				new_pos.append(av)
			new_kw_args: List[H.HKwArg] = []
			for kw in expr.kw_args:
				vpfx, vv = self._rewrite_expr(kw.value)
				pfx.extend(vpfx)
				new_kw_args.append(H.HKwArg(name=kw.name, value=vv, loc=getattr(kw, "loc", Span())))
			return pfx, H.HExceptionInit(
				event_fqn=expr.event_fqn,
				pos_args=new_pos,
				kw_args=new_kw_args,
				loc=expr.loc,
			)
		if isinstance(expr, H.HDVInit):
			new_args: List[H.HExpr] = []
			pfx: List[H.HStmt] = []
			for a in expr.args:
				arg_pfx, arg = self._rewrite_expr(a)
				pfx.extend(arg_pfx)
				new_args.append(arg)
			return pfx, H.HDVInit(
				dv_type_name=expr.dv_type_name,
				args=new_args,
			)
		if isinstance(expr, H.HArrayLiteral):
			elem_pfx: List[H.HStmt] = []
			new_elems: List[H.HExpr] = []
			for e in expr.elements:
				pfx, el = self._rewrite_expr(e)
				elem_pfx.extend(pfx)
				new_elems.append(el)
			return elem_pfx, H.HArrayLiteral(elements=new_elems)
		if isinstance(expr, H.HFString):
			# f-strings are expressions; try-result sugar may appear inside any
			# hole expression. Any prefixes produced while rewriting a hole must
			# execute before the f-string expression is evaluated.
			pfx: List[H.HStmt] = []
			new_holes: List[H.HFStringHole] = []
			for hole in expr.holes:
				hole_pfx, hole_expr = self._rewrite_expr(hole.expr)
				pfx.extend(hole_pfx)
				new_holes.append(H.HFStringHole(expr=hole_expr, spec=hole.spec, loc=hole.loc))
			return pfx, H.HFString(parts=list(expr.parts), holes=new_holes, loc=expr.loc)
		if isinstance(expr, H.HMatchExpr):
			# `match` is an expression; try-result sugar may appear inside the
			# scrutinee, in any arm block statement, or in the arm result
			# expression. Prefix statements emitted while rewriting the scrutinee
			# must execute before the match dispatch, and prefixes emitted while
			# rewriting an arm's result must execute within that arm.
			pfx_scrutinee, scrutinee = self._rewrite_expr(expr.scrutinee)
			new_arms: list[H.HMatchArm] = []
			for arm in expr.arms:
				arm_block = self.rewrite_block(arm.block)
				arm_result = None
				arm_result_pfx: List[H.HStmt] = []
				if arm.result is not None:
					arm_result_pfx, arm_result = self._rewrite_expr(arm.result)
				if arm_result_pfx:
					arm_block = H.HBlock(statements=[*arm_block.statements, *arm_result_pfx])
				new_arms.append(
					H.HMatchArm(
						ctor=arm.ctor,
						binders=list(arm.binders),
						block=arm_block,
						result=arm_result,
						loc=arm.loc,
					)
				)
			return pfx_scrutinee, H.HMatchExpr(scrutinee=scrutinee, arms=new_arms, loc=expr.loc)
		if hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
			# Expression-form try/catch is a control-flow expression. We rewrite
			# nested try-result sugar inside the attempt and inside catch bodies,
			# preserving the try-expr structure.
			pfx_attempt, attempt = self._rewrite_expr(expr.attempt)
			new_arms: List[H.HTryExprArm] = []
			for arm in expr.arms:
				arm_block = self.rewrite_block(arm.block)
				arm_result = None
				arm_result_pfx: List[H.HStmt] = []
				if getattr(arm, "result", None) is not None:
					arm_result_pfx, arm_result = self._rewrite_expr(arm.result)
				if arm_result_pfx:
					arm_block = H.HBlock(statements=[*arm_block.statements, *arm_result_pfx])
				new_arms.append(
					H.HTryExprArm(
						event_fqn=arm.event_fqn,
						binder=arm.binder,
						block=arm_block,
						result=arm_result,
						loc=arm.loc,
					)
				)
			return pfx_attempt, H.HTryExpr(attempt=attempt, arms=new_arms, loc=expr.loc)
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
