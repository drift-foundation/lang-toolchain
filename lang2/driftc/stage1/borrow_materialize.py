# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-15
"""
Borrow materialization (stage1).

Goal:
  Allow `&(<rvalue>)` by materializing the rvalue into a hidden local first.

Why stage1?
  - The borrow checker operates on HIR before MIR lowering; if we kept rvalue
    borrows as-is, borrow checking would have to invent a temporary model.
  - MIR lowering for borrows expects the operand to be a place-like expression.

MVP rules:
  - Shared borrows of rvalues are materialized:
      `&(<expr>)`  ->  `val __tmpN = <expr>; &__tmpN`
  - Mutable borrows of rvalues are also materialized:
      `&mut (<expr>)`  ->  `var __tmp_mutN = <expr>; &mut __tmp_mutN`
    This gives `&mut` a real storage slot to point at. The temp is a normal
    local from the borrow checker's point of view: it is frozen while borrowed
    and lives for the surrounding scope.
  - Materialization is purely structural and does not perform typing. The
    typed checker remains the authority for rejecting illegal borrow operands
    (e.g. non-place borrows when materialization is disabled, or `&mut` of an
    expression containing an explicit `move`).
  - This pass is structural and does not perform type checking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from lang2.driftc.core.span import Span
from . import hir_nodes as H
from .place_expr import place_expr_from_lvalue_expr


@dataclass
class BorrowMaterializeRewriter:
	"""Rewrite `&(<rvalue>)` into `val tmp = <rvalue>; &tmp` via prefix statements."""

	_temp_counter: int = 0

	def _fresh(self, prefix: str = "__tmp") -> str:
		self._temp_counter += 1
		return f"{prefix}{self._temp_counter}"

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
			# Preserve HLet metadata (mutability, declared types, binding identity).
			# Normalization passes must not silently change `val` ↔ `var` semantics.
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
			pfx_v, val = self._rewrite_expr(stmt.value)
			return pfx_t + pfx_v + [H.HAssign(target=tgt, value=val)]
		if hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
			pfx_t, tgt = self._rewrite_expr(stmt.target)
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
		# them to the outer scope). This pass must recurse into them.
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
		raise NotImplementedError(f"BorrowMaterializeRewriter does not handle stmt {type(stmt).__name__}")

	def _to_place(self, expr: H.HExpr) -> H.HPlaceExpr | None:
		"""
		Best-effort conversion from legacy place-like expressions to `HPlaceExpr`.

		This pass is purely structural (no type info), so we only recognize the
		shapes that are syntactically addressable:
		  - `HVar`
		  - `HField(place, name)`
		  - `HIndex(place, idx)`
		  - `HUnary(DEREF, place)`

		All other expressions are treated as rvalues.
		"""
		return place_expr_from_lvalue_expr(expr)

	def _contains_move(self, expr: H.HExpr) -> bool:
		"""
		Conservatively detect explicit `move` inside an expression.

		We use this to avoid materializing `&(move x)` / `&mut (move x)` into a
		temp. Doing so would implicitly change "consume + invalidate" into "store
		then borrow", which is a semantic expansion we want to keep explicit.
		"""
		if hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
			return True
		if isinstance(expr, H.HUnary):
			return self._contains_move(expr.expr)
		if isinstance(expr, H.HBinary):
			return self._contains_move(expr.left) or self._contains_move(expr.right)
		if isinstance(expr, H.HTernary):
			return (
				self._contains_move(expr.cond)
				or self._contains_move(expr.then_expr)
				or self._contains_move(expr.else_expr)
			)
		if isinstance(expr, H.HCall):
			return (
				self._contains_move(expr.fn)
				or any(self._contains_move(a) for a in expr.args)
				or any(self._contains_move(k.value) for k in getattr(expr, "kwargs", []) or [])
			)
		if isinstance(expr, H.HMethodCall):
			return (
				self._contains_move(expr.receiver)
				or any(self._contains_move(a) for a in expr.args)
				or any(self._contains_move(k.value) for k in getattr(expr, "kwargs", []) or [])
			)
		if isinstance(expr, H.HField):
			return self._contains_move(expr.subject)
		if isinstance(expr, H.HIndex):
			return self._contains_move(expr.subject) or self._contains_move(expr.index)
		if isinstance(expr, getattr(H, "HPlaceExpr", ())):
			return False
		if isinstance(expr, H.HArrayLiteral):
			return any(self._contains_move(e) for e in expr.elements)
		if isinstance(expr, H.HDVInit):
			return any(self._contains_move(a) for a in expr.args)
		if isinstance(expr, H.HExceptionInit):
			return any(self._contains_move(a) for a in expr.pos_args) or any(
				self._contains_move(k.value) for k in expr.kw_args
			)
		if isinstance(expr, getattr(H, "HTryExpr", ())):
			if self._contains_move(expr.attempt):
				return True
			for arm in expr.arms:
				if any(
					self._contains_move(s.expr) for s in arm.block.statements if isinstance(s, H.HExprStmt)
				):
					return True
				if arm.result is not None and self._contains_move(arm.result):
					return True
			return False
		return False

	def _rewrite_expr(self, expr: H.HExpr) -> Tuple[List[H.HStmt], H.HExpr]:
		if isinstance(expr, H.HVar):
			return [], expr
		if isinstance(expr, (H.HLiteralInt, H.HLiteralFloat, H.HLiteralString, H.HLiteralBool)):
			return [], expr
		if isinstance(expr, H.HCall):
			pfx_fn, fn = self._rewrite_expr(expr.fn)
			pfx_args: List[H.HStmt] = []
			new_args: List[H.HExpr] = []
			for a in expr.args:
				apfx, av = self._rewrite_expr(a)
				pfx_args.extend(apfx)
				new_args.append(av)
			pfx_kwargs: List[H.HStmt] = []
			new_kwargs: list[H.HKwArg] = []
			for kw in getattr(expr, "kwargs", []) or []:
				kpfx, kv = self._rewrite_expr(kw.value)
				pfx_kwargs.extend(kpfx)
				new_kwargs.append(H.HKwArg(name=kw.name, value=kv, loc=kw.loc))
			return pfx_fn + pfx_args + pfx_kwargs, H.HCall(fn=fn, args=new_args, kwargs=new_kwargs)
		if isinstance(expr, H.HMethodCall):
			pfx_recv, recv = self._rewrite_expr(expr.receiver)
			pfx_args: List[H.HStmt] = []
			new_args: List[H.HExpr] = []
			for a in expr.args:
				apfx, av = self._rewrite_expr(a)
				pfx_args.extend(apfx)
				new_args.append(av)
			pfx_kwargs: List[H.HStmt] = []
			new_kwargs: list[H.HKwArg] = []
			for kw in getattr(expr, "kwargs", []) or []:
				kpfx, kv = self._rewrite_expr(kw.value)
				pfx_kwargs.extend(kpfx)
				new_kwargs.append(H.HKwArg(name=kw.name, value=kv, loc=kw.loc))
			return pfx_recv + pfx_args + pfx_kwargs, H.HMethodCall(
				receiver=recv, method_name=expr.method_name, args=new_args, kwargs=new_kwargs
			)
		if isinstance(expr, H.HField):
			pfx, subj = self._rewrite_expr(expr.subject)
			return pfx, H.HField(subject=subj, name=expr.name)
		if isinstance(expr, H.HIndex):
			pfx_s, subj = self._rewrite_expr(expr.subject)
			pfx_i, idx = self._rewrite_expr(expr.index)
			return pfx_s + pfx_i, H.HIndex(subject=subj, index=idx)
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
		if isinstance(expr, H.HBorrow):
			pfx, subj = self._rewrite_expr(expr.subject)
			place = self._to_place(subj)
			# Materialize rvalue borrows into a hidden temp so borrow checking and
			# MIR lowering can treat borrow operands as places.
			if place is None:
				# Guardrail: do not materialize borrows of expressions that contain an
				# explicit `move`. The typed checker is responsible for rejecting these
				# with a targeted diagnostic.
				if self._contains_move(subj):
					return pfx, H.HBorrow(subject=subj, is_mut=expr.is_mut)
				tmp = self._fresh("__tmp_borrow_mut" if expr.is_mut else "__tmp_borrow")
				return (
					pfx
					+ [
						H.HLet(
							name=tmp,
							value=subj,
							declared_type_expr=None,
							binding_id=None,
							is_mutable=bool(expr.is_mut),
						)
					],
					H.HBorrow(subject=H.HPlaceExpr(base=H.HVar(tmp), projections=[]), is_mut=expr.is_mut),
				)
			# Canonicalize to a place expression when possible; this makes later
			# phases less dependent on re-deriving place structure from trees.
			return pfx, H.HBorrow(subject=place if place is not None else subj, is_mut=expr.is_mut)
		if isinstance(expr, getattr(H, "HMove", ())):
			pfx, subj = self._rewrite_expr(expr.subject)
			return pfx, H.HMove(subject=subj, loc=getattr(expr, "loc", Span()))
		if isinstance(expr, H.HArrayLiteral):
			pfx: List[H.HStmt] = []
			new_elems: List[H.HExpr] = []
			for e in expr.elements:
				epfx, ev = self._rewrite_expr(e)
				pfx.extend(epfx)
				new_elems.append(ev)
			return pfx, H.HArrayLiteral(elements=new_elems)
		if isinstance(expr, H.HFString):
			pfx: List[H.HStmt] = []
			new_holes: List[H.HFStringHole] = []
			for hole in expr.holes:
				hpfx, hev = self._rewrite_expr(hole.expr)
				pfx.extend(hpfx)
				new_holes.append(H.HFStringHole(expr=hev, spec=hole.spec, loc=hole.loc))
			return pfx, H.HFString(parts=list(expr.parts), holes=new_holes, loc=expr.loc)
		if isinstance(expr, H.HMatchExpr):
			# `match` is an expression; borrows (and other rewrites) may appear
			# inside the scrutinee, in arm block statements, or in the arm result
			# expression. Prefix statements produced while rewriting the scrutinee
			# must run before dispatch, and prefixes produced while rewriting an
			# arm result must execute inside that arm.
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
		if isinstance(expr, H.HDVInit):
			pfx: List[H.HStmt] = []
			new_args: List[H.HExpr] = []
			for a in expr.args:
				apfx, av = self._rewrite_expr(a)
				pfx.extend(apfx)
				new_args.append(av)
			return pfx, H.HDVInit(dv_type_name=expr.dv_type_name, args=new_args)
		if isinstance(expr, H.HExceptionInit):
			pfx: List[H.HStmt] = []
			new_pos: List[H.HExpr] = []
			for a in expr.pos_args:
				apfx, av = self._rewrite_expr(a)
				pfx.extend(apfx)
				new_pos.append(av)
			new_kw: List[H.HKwArg] = []
			for kw in expr.kw_args:
				kpfx, kv = self._rewrite_expr(kw.value)
				pfx.extend(kpfx)
				new_kw.append(H.HKwArg(name=kw.name, value=kv, loc=kw.loc))
			return pfx, H.HExceptionInit(event_fqn=expr.event_fqn, pos_args=new_pos, kw_args=new_kw, loc=expr.loc)
		if hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
			pfx_attempt, attempt = self._rewrite_expr(expr.attempt)
			new_arms: List[H.HTryExprArm] = []
			for arm in expr.arms:
				arm_block = self.rewrite_block(arm.block)
				arm_result = None
				if arm.result is not None:
					rpfx, rv = self._rewrite_expr(arm.result)
					if rpfx:
						arm_block = H.HBlock(statements=arm_block.statements + rpfx)
					arm_result = rv
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
		# Leave other expressions unchanged (or handled by other normalizers).
		return [], expr
