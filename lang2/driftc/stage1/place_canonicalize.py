# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-15
"""
Canonicalize place expressions in HIR (stage1).

Goal
----
Ensure that all *place contexts* use `HPlaceExpr` instead of legacy expression
shapes (`HVar`/`HField`/`HIndex`/`HUnary(DEREF)`).

This is a deliberately narrow pass: it does not rewrite arbitrary expressions
into places. It only canonicalizes places in contexts that *must* be addressable:

  - `HBorrow.subject`
  - `HAssign.target`
  - `HAugAssign.target`
  - `HMove.subject`
  - `swap(a, b)` / `replace(place, new)` builtin place operands

Why stage1?
-----------
Borrow checking and MIR lowering are much simpler (and more robust) when they
can assume a single canonical lvalue representation at the stage1→stage2
boundary.

Diagnostics policy
------------------
This pass does not emit diagnostics. If a place context contains an rvalue
(e.g., `&(foo())` without materialization), we leave it unchanged; the typed
checker is the authority that rejects such programs with a source span.

Stage2/MIR lowering may still assert if an invalid rvalue place reaches it,
but in a production pipeline we expect the checker to stop compilation on
errors before lowering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from lang2.driftc.core.span import Span
from lang2.driftc.stage1 import hir_nodes as H
from lang2.driftc.stage1.place_expr import place_expr_from_lvalue_expr


@dataclass
class PlaceCanonicalizeRewriter:
	"""Rewrite place contexts to use `HPlaceExpr`."""

	def rewrite_block(self, block: H.HBlock) -> H.HBlock:
		new_stmts: List[H.HStmt] = []
		for stmt in block.statements:
			new_stmts.extend(self._rewrite_stmt(stmt))
		return H.HBlock(statements=new_stmts)

	def _rewrite_stmt(self, stmt: H.HStmt) -> List[H.HStmt]:
		if isinstance(stmt, H.HExprStmt):
			pfx, expr = self._rewrite_expr(stmt.expr)
			return pfx + [H.HExprStmt(expr=expr)]
		if isinstance(stmt, H.HThrow):
			pfx, expr = self._rewrite_expr(stmt.value)
			return pfx + [H.HThrow(value=expr)]
		if isinstance(stmt, H.HLet):
			pfx, expr = self._rewrite_expr(stmt.value)
			return pfx + [
				H.HLet(
					name=stmt.name,
					value=expr,
					declared_type_expr=stmt.declared_type_expr,
					binding_id=stmt.binding_id,
					is_mutable=stmt.is_mutable,
				)
			]
		if isinstance(stmt, H.HAssign):
			pfx_t, tgt = self._rewrite_expr(stmt.target)
			place = place_expr_from_lvalue_expr(tgt)
			if place is not None:
				tgt = place
			pfx_v, val = self._rewrite_expr(stmt.value)
			return pfx_t + pfx_v + [H.HAssign(target=tgt, value=val)]
		if hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
			pfx_t, tgt = self._rewrite_expr(stmt.target)
			place = place_expr_from_lvalue_expr(tgt)
			if place is not None:
				tgt = place
			pfx_v, val = self._rewrite_expr(stmt.value)
			return pfx_t + pfx_v + [
				H.HAugAssign(target=tgt, op=getattr(stmt, "op", "+="), value=val, loc=getattr(stmt, "loc", Span()))
			]
		if isinstance(stmt, H.HReturn):
			if stmt.value is None:
				return [stmt]
			pfx, expr = self._rewrite_expr(stmt.value)
			return pfx + [H.HReturn(value=expr)]
		if isinstance(stmt, H.HIf):
			pfx, cond = self._rewrite_expr(stmt.cond)
			then_block = self.rewrite_block(stmt.then_block)
			else_block = self.rewrite_block(stmt.else_block) if stmt.else_block else None
			return pfx + [H.HIf(cond=cond, then_block=then_block, else_block=else_block)]
		if isinstance(stmt, H.HLoop):
			return [H.HLoop(body=self.rewrite_block(stmt.body))]
		# Block statements introduce a nested statement scope (used by desugarings
		# like `for` which need to introduce hidden temporaries without leaking
		# them to the outer scope). Canonicalization must recurse into them.
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
		if isinstance(stmt, (H.HBreak, H.HContinue, H.HRethrow)):
			return [stmt]
		raise NotImplementedError(f"PlaceCanonicalizeRewriter does not handle stmt {type(stmt).__name__}")

	def _rewrite_expr(self, expr: H.HExpr) -> Tuple[List[H.HStmt], H.HExpr]:
		# This pass is canonicalization-only: it does not introduce new prefixes.
		if isinstance(expr, H.HBorrow):
			pfx, subj = self._rewrite_expr(expr.subject)
			place = place_expr_from_lvalue_expr(subj)
			if place is not None:
				subj = place
			return pfx, H.HBorrow(subject=subj, is_mut=expr.is_mut)
		if isinstance(expr, getattr(H, "HMove", ())):
			pfx, subj = self._rewrite_expr(expr.subject)
			place = place_expr_from_lvalue_expr(subj)
			if place is not None:
				subj = place
			return pfx, H.HMove(subject=subj, loc=getattr(expr, "loc", Span()))

		# Most expressions are unchanged; we only recurse to preserve structure.
		if isinstance(expr, H.HVar):
			return [], expr
		if hasattr(H, "HQualifiedMember") and isinstance(expr, getattr(H, "HQualifiedMember")):
			return [], expr
		if isinstance(expr, (H.HLiteralInt, H.HLiteralFloat, H.HLiteralString, H.HLiteralBool)):
			return [], expr
		if isinstance(expr, H.HCall):
			_, fn = self._rewrite_expr(expr.fn)
			new_args: List[H.HExpr] = []
			for a in expr.args:
				_, av = self._rewrite_expr(a)
				new_args.append(av)
			new_kwargs: list[H.HKwArg] = []
			for kw in getattr(expr, "kwargs", []) or []:
				_, kv = self._rewrite_expr(kw.value)
				new_kwargs.append(H.HKwArg(name=kw.name, value=kv, loc=kw.loc))
			# Builtins that operate on *places* should receive canonical `HPlaceExpr`
			# operands so downstream passes never have to reconstruct lvalues from
			# arbitrary expression trees.
			if isinstance(fn, H.HVar) and fn.name in ("swap", "replace"):
				# swap(a, b): both operands are place contexts.
				if fn.name == "swap" and len(new_args) >= 2:
					pa = place_expr_from_lvalue_expr(new_args[0])
					pb = place_expr_from_lvalue_expr(new_args[1])
					if pa is not None:
						new_args[0] = pa
					if pb is not None:
						new_args[1] = pb
				# replace(place, new): first operand is a place context.
				if fn.name == "replace" and len(new_args) >= 1:
					pa = place_expr_from_lvalue_expr(new_args[0])
					if pa is not None:
						new_args[0] = pa
			return [], H.HCall(fn=fn, args=new_args, kwargs=new_kwargs)
		if isinstance(expr, H.HMethodCall):
			_, recv = self._rewrite_expr(expr.receiver)
			new_args: List[H.HExpr] = []
			for a in expr.args:
				_, av = self._rewrite_expr(a)
				new_args.append(av)
			new_kwargs: list[H.HKwArg] = []
			for kw in getattr(expr, "kwargs", []) or []:
				_, kv = self._rewrite_expr(kw.value)
				new_kwargs.append(H.HKwArg(name=kw.name, value=kv, loc=kw.loc))
			return [], H.HMethodCall(receiver=recv, method_name=expr.method_name, args=new_args, kwargs=new_kwargs)
		if isinstance(expr, H.HField):
			_, subj = self._rewrite_expr(expr.subject)
			return [], H.HField(subject=subj, name=expr.name)
		if isinstance(expr, H.HIndex):
			_, subj = self._rewrite_expr(expr.subject)
			_, idx = self._rewrite_expr(expr.index)
			return [], H.HIndex(subject=subj, index=idx)
		if isinstance(expr, H.HUnary):
			_, inner = self._rewrite_expr(expr.expr)
			return [], H.HUnary(op=expr.op, expr=inner)
		if isinstance(expr, H.HBinary):
			_, left = self._rewrite_expr(expr.left)
			_, right = self._rewrite_expr(expr.right)
			return [], H.HBinary(op=expr.op, left=left, right=right)
		if isinstance(expr, H.HTernary):
			_, cond = self._rewrite_expr(expr.cond)
			_, then = self._rewrite_expr(expr.then_expr)
			_, els = self._rewrite_expr(expr.else_expr)
			return [], H.HTernary(cond=cond, then_expr=then, else_expr=els)
		if isinstance(expr, H.HArrayLiteral):
			new_elems: List[H.HExpr] = []
			for e in expr.elements:
				_, ev = self._rewrite_expr(e)
				new_elems.append(ev)
			return [], H.HArrayLiteral(elements=new_elems)
		if isinstance(expr, H.HFString):
			new_holes: List[H.HFStringHole] = []
			for hole in expr.holes:
				_, hev = self._rewrite_expr(hole.expr)
				new_holes.append(H.HFStringHole(expr=hev, spec=hole.spec, loc=hole.loc))
			return [], H.HFString(parts=list(expr.parts), holes=new_holes, loc=expr.loc)
		if isinstance(expr, H.HMatchExpr):
			# `match` is an expression; canonicalization must recurse into the
			# scrutinee and into each arm's block/result so lvalue contexts inside
			# arms (e.g. `sum = sum + x`) become canonical `HPlaceExpr` nodes.
			_, scrutinee = self._rewrite_expr(expr.scrutinee)
			new_arms: list[H.HMatchArm] = []
			for arm in expr.arms:
				arm_block = self.rewrite_block(arm.block)
				arm_result = None
				if arm.result is not None:
					_, arm_result = self._rewrite_expr(arm.result)
				new_arms.append(
					H.HMatchArm(
						ctor=arm.ctor,
						pattern_arg_form=getattr(arm, "pattern_arg_form", "positional"),
						binders=list(arm.binders),
						binder_fields=getattr(arm, "binder_fields", None),
						binder_field_indices=getattr(arm, "binder_field_indices", []),
						block=arm_block,
						result=arm_result,
						loc=arm.loc,
					)
				)
			return [], H.HMatchExpr(scrutinee=scrutinee, arms=new_arms, loc=expr.loc)
		if isinstance(expr, H.HDVInit):
			new_args: List[H.HExpr] = []
			for a in expr.args:
				_, av = self._rewrite_expr(a)
				new_args.append(av)
			return [], H.HDVInit(dv_type_name=expr.dv_type_name, args=new_args)
		if isinstance(expr, H.HExceptionInit):
			new_pos: List[H.HExpr] = []
			for a in expr.pos_args:
				_, av = self._rewrite_expr(a)
				new_pos.append(av)
			new_kw: List[H.HKwArg] = []
			for kw in expr.kw_args:
				_, kv = self._rewrite_expr(kw.value)
				new_kw.append(H.HKwArg(name=kw.name, value=kv, loc=kw.loc))
			return [], H.HExceptionInit(event_fqn=expr.event_fqn, pos_args=new_pos, kw_args=new_kw, loc=expr.loc)
		if hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
			_, attempt = self._rewrite_expr(expr.attempt)
			new_arms: List[H.HTryExprArm] = []
			for arm in expr.arms:
				arm_block = self.rewrite_block(arm.block)
				arm_result = None
				if arm.result is not None:
					_, arm_result = self._rewrite_expr(arm.result)
				new_arms.append(
					H.HTryExprArm(
						event_fqn=arm.event_fqn,
						binder=arm.binder,
						block=arm_block,
						result=arm_result,
						loc=arm.loc,
					)
				)
			return [], H.HTryExpr(attempt=attempt, arms=new_arms, loc=expr.loc)

		# Default: leave unchanged.
		return [], expr


__all__ = ["PlaceCanonicalizeRewriter"]
