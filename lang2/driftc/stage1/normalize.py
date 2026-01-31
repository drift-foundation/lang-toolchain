# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage1 HIR normalization helpers.

At this stage we normalize HIR into canonical forms expected by stage2:
- materialize borrows of rvalues into temps
- canonicalize lvalue contexts into HPlaceExpr
"""

from __future__ import annotations

from . import hir_nodes as H
from .borrow_materialize import BorrowMaterializeRewriter
from .node_ids import assign_node_ids, assign_callsite_ids, validate_callsite_ids
from .place_canonicalize import PlaceCanonicalizeRewriter
from lang2.driftc.core.span import Span


class DVInitRewriter:
	"""
	Rewrite DiagnosticValue qualified-member calls into HDVInit.

	Example: DiagnosticValue::Int(1) -> HDVInit(dv_type_name="Int", args=[1])
	"""

	def rewrite_block(self, block: H.HBlock) -> H.HBlock:
		new_stmts: list[H.HStmt] = []
		for stmt in block.statements:
			new_stmts.append(self._rewrite_stmt(stmt))
		return H.HBlock(statements=new_stmts)

	def _rewrite_stmt(self, stmt: H.HStmt) -> H.HStmt:
		if isinstance(stmt, H.HExprStmt):
			return H.HExprStmt(expr=self._rewrite_expr(stmt.expr))
		if isinstance(stmt, H.HThrow):
			return H.HThrow(value=self._rewrite_expr(stmt.value))
		if isinstance(stmt, H.HReturn):
			return H.HReturn(value=self._rewrite_expr(stmt.value) if stmt.value is not None else None)
		if isinstance(stmt, H.HLet):
			return H.HLet(
				name=stmt.name,
				value=self._rewrite_expr(stmt.value),
				is_mutable=stmt.is_mutable,
				declared_type_expr=stmt.declared_type_expr,
				binding_id=stmt.binding_id,
			)
		if isinstance(stmt, H.HAssign):
			return H.HAssign(
				target=self._rewrite_expr(stmt.target),
				value=self._rewrite_expr(stmt.value),
			)
		if isinstance(stmt, H.HIf):
			return H.HIf(
				cond=self._rewrite_expr(stmt.cond),
				then_block=self.rewrite_block(stmt.then_block),
				else_block=self.rewrite_block(stmt.else_block) if stmt.else_block else None,
			)
		if isinstance(stmt, H.HLoop):
			return H.HLoop(body=self.rewrite_block(stmt.body))
		if isinstance(stmt, H.HTry):
			new_arms = []
			for arm in stmt.catches:
				new_arms.append(
					H.HCatchArm(
						event_fqn=arm.event_fqn,
						binder=arm.binder,
						block=self.rewrite_block(arm.block),
					)
				)
			return H.HTry(body=self.rewrite_block(stmt.body), catches=new_arms)
		if isinstance(stmt, H.HBlock):
			return self.rewrite_block(stmt)
		if hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
			return H.HUnsafeBlock(block=self.rewrite_block(stmt.block))
		return stmt

	def _rewrite_expr(self, expr: H.HExpr) -> H.HExpr:
		if isinstance(expr, H.HCall):
			fn = expr.fn
			if hasattr(H, "HQualifiedMember") and isinstance(fn, getattr(H, "HQualifiedMember")):
				base_te = getattr(fn, "base_type_expr", None)
				base_name = getattr(base_te, "name", None) if base_te is not None else None
				base_mod = None
				if base_te is not None:
					base_mod = getattr(base_te, "module_id", None) or getattr(base_te, "module_alias", None)
				if base_name == "DiagnosticValue":
					if not (getattr(expr, "kwargs", None) or []):
						new_args = [self._rewrite_expr(a) for a in expr.args]
						return H.HDVInit(dv_type_name=fn.member, args=new_args)
			return H.HCall(
				fn=self._rewrite_expr(expr.fn),
				args=[self._rewrite_expr(a) for a in expr.args],
				kwargs=[
					H.HKwArg(name=kw.name, value=self._rewrite_expr(kw.value))
					for kw in getattr(expr, "kwargs", []) or []
				],
				type_args=getattr(expr, "type_args", None),
				origin=getattr(expr, "origin", None),
			)
		if isinstance(expr, H.HMethodCall):
			return H.HMethodCall(
				receiver=self._rewrite_expr(expr.receiver),
				method_name=expr.method_name,
				args=[self._rewrite_expr(a) for a in expr.args],
				kwargs=[
					H.HKwArg(name=kw.name, value=self._rewrite_expr(kw.value))
					for kw in getattr(expr, "kwargs", []) or []
				],
			)
		if isinstance(expr, H.HInvoke):
			return H.HInvoke(
				callee=self._rewrite_expr(expr.callee),
				args=[self._rewrite_expr(a) for a in expr.args],
				kwargs=[
					H.HKwArg(name=kw.name, value=self._rewrite_expr(kw.value))
					for kw in getattr(expr, "kwargs", []) or []
				],
			)
		if isinstance(expr, H.HBinary):
			return H.HBinary(
				left=self._rewrite_expr(expr.left),
				op=expr.op,
				right=self._rewrite_expr(expr.right),
			)
		if isinstance(expr, H.HUnary):
			return H.HUnary(op=expr.op, expr=self._rewrite_expr(expr.expr))
		if isinstance(expr, H.HTernary):
			return H.HTernary(
				cond=self._rewrite_expr(expr.cond),
				then_expr=self._rewrite_expr(expr.then_expr),
				else_expr=self._rewrite_expr(expr.else_expr),
			)
		if isinstance(expr, H.HField):
			return H.HField(subject=self._rewrite_expr(expr.subject), name=expr.name)
		if isinstance(expr, H.HIndex):
			return H.HIndex(subject=self._rewrite_expr(expr.subject), index=self._rewrite_expr(expr.index))
		if isinstance(expr, H.HBorrow):
			return H.HBorrow(subject=self._rewrite_expr(expr.subject), is_mut=expr.is_mut)
		if hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
			return H.HMove(
				subject=self._rewrite_expr(expr.subject),
				loc=getattr(expr, "loc", Span()),
				is_implicit=bool(getattr(expr, "is_implicit", False)),
			)
		if hasattr(H, "HCopy") and isinstance(expr, getattr(H, "HCopy")):
			return H.HCopy(subject=self._rewrite_expr(expr.subject))
		if isinstance(expr, H.HArrayLiteral):
			return H.HArrayLiteral(elements=[self._rewrite_expr(e) for e in expr.elements])
		if isinstance(expr, H.HExceptionInit):
			return H.HExceptionInit(
				event_fqn=expr.event_fqn,
				pos_args=[self._rewrite_expr(a) for a in expr.pos_args],
				kw_args=[
					H.HKwArg(name=kw.name, value=self._rewrite_expr(kw.value))
					for kw in expr.kw_args
				],
				loc=getattr(expr, "loc", None),
			)
		if hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
			new_arms = []
			for arm in expr.arms:
				new_arms.append(
					H.HTryExprArm(
						event_fqn=arm.event_fqn,
						binder=arm.binder,
						block=self.rewrite_block(arm.block),
						result=self._rewrite_expr(arm.result) if arm.result is not None else None,
					)
				)
			return H.HTryExpr(attempt=self._rewrite_expr(expr.attempt), arms=new_arms)
		if hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
			new_arms = []
			for arm in expr.arms:
				new_arms.append(
					H.HMatchArm(
						ctor=arm.ctor,
						ctor_base=getattr(arm, "ctor_base", None),
						binders=list(arm.binders),
						block=self.rewrite_block(arm.block),
						result=self._rewrite_expr(arm.result) if arm.result is not None else None,
						pattern_arg_form=arm.pattern_arg_form,
						binder_fields=arm.binder_fields,
						binder_field_indices=list(arm.binder_field_indices),
						binder_is_mutable=getattr(arm, "binder_is_mutable", None),
					)
				)
			return H.HMatchExpr(scrutinee=self._rewrite_expr(expr.scrutinee), arms=new_arms)
		if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
			new_projs = []
			for proj in expr.projections:
				if isinstance(proj, H.HPlaceIndex):
					new_projs.append(H.HPlaceIndex(index=self._rewrite_expr(proj.index)))
				else:
					new_projs.append(proj)
			return H.HPlaceExpr(base=expr.base, projections=new_projs)
		return expr

def _assign_missing_binding_ids(block: H.HBlock) -> None:
	"""
	Assign binding ids to normalized temps introduced without ids.

	Borrow materialization can introduce `HLet` + `HVar` pairs with `binding_id=None`.
	We assign stable ids here so later passes can track types/borrows.
	"""
	max_id = 0

	def _scan_expr(expr: H.HExpr) -> None:
		nonlocal max_id
		if isinstance(expr, H.HVar) and expr.binding_id is not None:
			max_id = max(max_id, expr.binding_id)
		if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
			if isinstance(expr.base, H.HVar) and expr.base.binding_id is not None:
				max_id = max(max_id, expr.base.binding_id)
			for proj in expr.projections:
				if isinstance(proj, H.HPlaceIndex):
					_scan_expr(proj.index)
			return
		if isinstance(expr, H.HBinary):
			_scan_expr(expr.left)
			_scan_expr(expr.right)
		elif isinstance(expr, H.HUnary):
			_scan_expr(expr.expr)
		elif isinstance(expr, H.HTernary):
			_scan_expr(expr.cond)
			_scan_expr(expr.then_expr)
			_scan_expr(expr.else_expr)
		elif isinstance(expr, H.HCall):
			_scan_expr(expr.fn)
			for a in expr.args:
				_scan_expr(a)
			for kw in getattr(expr, "kwargs", []) or []:
				_scan_expr(kw.value)
		elif isinstance(expr, H.HMethodCall):
			_scan_expr(expr.receiver)
			for a in expr.args:
				_scan_expr(a)
			for kw in getattr(expr, "kwargs", []) or []:
				_scan_expr(kw.value)
		elif isinstance(expr, H.HInvoke):
			_scan_expr(expr.callee)
			for a in expr.args:
				_scan_expr(a)
			for kw in getattr(expr, "kwargs", []) or []:
				_scan_expr(kw.value)
		elif isinstance(expr, H.HField):
			_scan_expr(expr.subject)
		elif isinstance(expr, H.HIndex):
			_scan_expr(expr.subject)
			_scan_expr(expr.index)
		elif isinstance(expr, H.HBorrow):
			_scan_expr(expr.subject)
		elif hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
			_scan_expr(expr.subject)
		elif hasattr(H, "HCopy") and isinstance(expr, getattr(H, "HCopy")):
			_scan_expr(expr.subject)
		elif isinstance(expr, H.HArrayLiteral):
			for el in expr.elements:
				_scan_expr(el)
		elif isinstance(expr, H.HDVInit):
			for a in expr.args:
				_scan_expr(a)
		elif isinstance(expr, H.HExceptionInit):
			for a in expr.pos_args:
				_scan_expr(a)
			for kw in getattr(expr, "kw_args", []) or []:
				_scan_expr(kw.value)
		elif hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
			_scan_expr(expr.attempt)
			for arm in expr.arms:
				_scan_block(arm.block)
				if arm.result is not None:
					_scan_expr(arm.result)
		elif hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
			_scan_expr(expr.scrutinee)
			for arm in expr.arms:
				_scan_block(arm.block)
				if arm.result is not None:
					_scan_expr(arm.result)

	def _scan_block(b: H.HBlock) -> None:
		nonlocal max_id
		for stmt in b.statements:
			if isinstance(stmt, H.HLet) and stmt.binding_id is not None:
				max_id = max(max_id, stmt.binding_id)
			if isinstance(stmt, H.HLet):
				_scan_expr(stmt.value)
			elif isinstance(stmt, H.HAssign):
				_scan_expr(stmt.target)
				_scan_expr(stmt.value)
			elif isinstance(stmt, H.HReturn) and stmt.value is not None:
				_scan_expr(stmt.value)
			elif isinstance(stmt, H.HExprStmt):
				_scan_expr(stmt.expr)
			elif isinstance(stmt, H.HIf):
				_scan_expr(stmt.cond)
				_scan_block(stmt.then_block)
				if stmt.else_block:
					_scan_block(stmt.else_block)
			elif isinstance(stmt, H.HLoop):
				_scan_block(stmt.body)
			elif isinstance(stmt, H.HTry):
				_scan_block(stmt.body)
				for arm in stmt.catches:
					_scan_block(arm.block)
			elif isinstance(stmt, H.HBlock):
				_scan_block(stmt)
			elif hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
				_scan_block(stmt.block)

	_scan_block(block)
	next_id = max_id + 1

	scope_stack: list[dict[str, int]] = [{}]

	def _bind(name: str, bid: int) -> None:
		scope_stack[-1][name] = bid

	def _lookup(name: str) -> int | None:
		for scope in reversed(scope_stack):
			if name in scope:
				return scope[name]
		return None

	def _assign_expr(expr: H.HExpr) -> None:
		nonlocal next_id
		if isinstance(expr, H.HVar) and expr.binding_id is None:
			bid = _lookup(expr.name)
			if bid is not None:
				expr.binding_id = bid
			return
		if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
			if isinstance(expr.base, H.HVar) and expr.base.binding_id is None:
				bid = _lookup(expr.base.name)
				if bid is not None:
					expr.base.binding_id = bid
			for proj in expr.projections:
				if isinstance(proj, H.HPlaceIndex):
					_assign_expr(proj.index)
			return
		if isinstance(expr, H.HBinary):
			_assign_expr(expr.left)
			_assign_expr(expr.right)
		elif isinstance(expr, H.HUnary):
			_assign_expr(expr.expr)
		elif isinstance(expr, H.HTernary):
			_assign_expr(expr.cond)
			_assign_expr(expr.then_expr)
			_assign_expr(expr.else_expr)
		elif isinstance(expr, H.HCall):
			_assign_expr(expr.fn)
			for a in expr.args:
				_assign_expr(a)
			for kw in getattr(expr, "kwargs", []) or []:
				_assign_expr(kw.value)
		elif isinstance(expr, H.HMethodCall):
			_assign_expr(expr.receiver)
			for a in expr.args:
				_assign_expr(a)
			for kw in getattr(expr, "kwargs", []) or []:
				_assign_expr(kw.value)
		elif isinstance(expr, H.HInvoke):
			_assign_expr(expr.callee)
			for a in expr.args:
				_assign_expr(a)
			for kw in getattr(expr, "kwargs", []) or []:
				_assign_expr(kw.value)
		elif isinstance(expr, H.HField):
			_assign_expr(expr.subject)
		elif isinstance(expr, H.HIndex):
			_assign_expr(expr.subject)
			_assign_expr(expr.index)
		elif isinstance(expr, H.HBorrow):
			_assign_expr(expr.subject)
		elif hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
			_assign_expr(expr.subject)
		elif hasattr(H, "HCopy") and isinstance(expr, getattr(H, "HCopy")):
			_assign_expr(expr.subject)
		elif isinstance(expr, H.HArrayLiteral):
			for el in expr.elements:
				_assign_expr(el)
		elif isinstance(expr, H.HDVInit):
			for a in expr.args:
				_assign_expr(a)
		elif isinstance(expr, H.HExceptionInit):
			for a in expr.pos_args:
				_assign_expr(a)
			for kw in getattr(expr, "kw_args", []) or []:
				_assign_expr(kw.value)
		elif hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
			_assign_expr(expr.attempt)
			for arm in expr.arms:
				_assign_block(arm.block)
				if arm.result is not None:
					_assign_expr(arm.result)
		elif hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
			_assign_expr(expr.scrutinee)
			for arm in expr.arms:
				_assign_block(arm.block)
				if arm.result is not None:
					_assign_expr(arm.result)

	def _assign_block(b: H.HBlock) -> None:
		nonlocal next_id
		scope_stack.append({})
		try:
			for stmt in b.statements:
				if isinstance(stmt, H.HLet):
					_assign_expr(stmt.value)
					if stmt.binding_id is None:
						stmt.binding_id = next_id
						next_id += 1
					_bind(stmt.name, stmt.binding_id)
					continue
				if isinstance(stmt, H.HAssign):
					_assign_expr(stmt.target)
					_assign_expr(stmt.value)
					continue
				if isinstance(stmt, H.HReturn) and stmt.value is not None:
					_assign_expr(stmt.value)
					continue
				if isinstance(stmt, H.HExprStmt):
					_assign_expr(stmt.expr)
					continue
				if isinstance(stmt, H.HIf):
					_assign_expr(stmt.cond)
					_assign_block(stmt.then_block)
					if stmt.else_block:
						_assign_block(stmt.else_block)
					continue
				if isinstance(stmt, H.HLoop):
					_assign_block(stmt.body)
					continue
				if isinstance(stmt, H.HTry):
					_assign_block(stmt.body)
					for arm in stmt.catches:
						_assign_block(arm.block)
					continue
				if isinstance(stmt, H.HBlock):
					_assign_block(stmt)
					continue
				if hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
					_assign_block(stmt.block)
					continue
		finally:
			scope_stack.pop()

	_assign_block(block)


def normalize_hir(block: H.HBlock) -> H.HBlock:
	"""
	Normalize an HIR block into canonical forms expected by stage2.
	Additional normalization passes can be added here as needed.
	"""
	param_binding_ids = getattr(block, "param_binding_ids", None)
	# Order matters:
	# 1) Materialize shared borrows of rvalues into temps so borrow checking and
	#    MIR lowering can treat borrow operands as places.
	# 2) Canonicalize lvalue contexts so stage2 sees `HPlaceExpr` instead of
	#    re-deriving place-ness from arbitrary expression trees.
	block = BorrowMaterializeRewriter().rewrite_block(block)
	block = PlaceCanonicalizeRewriter().rewrite_block(block)
	block = DVInitRewriter().rewrite_block(block)
	_assign_missing_binding_ids(block)
	# Ensure stable per-function NodeIds for typed side tables.
	assign_node_ids(block, start=1)
	assign_callsite_ids(block, start=0)
	if __debug__:
		validate_callsite_ids(block)
	if param_binding_ids is not None:
		block.param_binding_ids = param_binding_ids
	return block
