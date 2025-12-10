#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Minimal typed checker skeleton for lang2.

This is a real checker scaffold that:
- Allocates ParamId/LocalId/BindingId for bindings.
- Infers types for basic expressions (literals, vars, lets, borrows, calls).
- Produces a TypedFn record with expression TypeIds and binding identity.

It is intentionally small; it will grow to cover full Drift semantics. Borrow
checker integration will consume TypedFn once this matures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Mapping

from lang2 import stage1 as H
from lang2.core.diagnostics import Diagnostic
from lang2.core.types_core import TypeId, TypeTable

# Identifier aliases for clarity.
ParamId = int
LocalId = int


@dataclass
class TypedFn:
	"""Typed view of a single function's HIR."""

	name: str
	params: List[ParamId]
	param_bindings: List[int]
	locals: List[LocalId]
	body: H.HBlock
	expr_types: Dict[int, TypeId]  # keyed by id(expr)
	binding_for_var: Dict[int, int]  # keyed by id(HVar)
	binding_types: Dict[int, TypeId]  # binding_id -> TypeId
	binding_names: Dict[int, str]  # binding_id -> name


@dataclass
class TypeCheckResult:
	"""Result of type checking a function."""

	typed_fn: TypedFn
	diagnostics: List[Diagnostic] = field(default_factory=list)


class TypeChecker:
	"""
	Minimal HIR type checker that assigns binding IDs and basic types.

	This is a skeleton: it understands literals, vars, lets, borrows, and calls.
	More constructs will be added as the language matures.
	"""

	def __init__(self, type_table: Optional[TypeTable] = None):
		self.type_table = type_table or TypeTable()
		self._int = self.type_table.ensure_int()
		self._bool = self.type_table.ensure_bool()
		self._unknown = self.type_table.ensure_unknown()
		self._next_param_id: ParamId = 1
		self._next_local_id: LocalId = 1

	def check_function(
		self,
		name: str,
		body: H.HBlock,
		param_types: Mapping[str, TypeId] | None = None,
	) -> TypeCheckResult:
		scope_env: List[Dict[str, TypeId]] = [dict()]
		scope_bindings: List[Dict[str, int]] = [dict()]
		expr_types: Dict[int, TypeId] = {}
		binding_for_var: Dict[int, int] = {}
		binding_types: Dict[int, TypeId] = {}
		binding_names: Dict[int, str] = {}
		diagnostics: List[Diagnostic] = []

		params: List[ParamId] = []
		param_bindings: List[int] = []
		locals: List[LocalId] = []

		# Seed parameters if provided.
		for pname, pty in (param_types or {}).items():
			pid = self._alloc_param_id()
			params.append(pid)
			param_bindings.append(pid)
			scope_env[-1][pname] = pty
			scope_bindings[-1][pname] = pid
			binding_types[pid] = pty
			binding_names[pid] = pname

		def record_expr(expr: H.HExpr, ty: TypeId) -> TypeId:
			expr_id = id(expr)
			expr_types[expr_id] = ty
			return ty

		def type_expr(expr: H.HExpr) -> TypeId:
			if isinstance(expr, H.HLiteralInt):
				return record_expr(expr, self._int)
			if isinstance(expr, H.HLiteralBool):
				return record_expr(expr, self._bool)
			if isinstance(expr, H.HLiteralString):
				return record_expr(expr, self.type_table.ensure_string())
			if isinstance(expr, H.HVar):
				if expr.binding_id is None:
					for scope in reversed(scope_bindings):
						if expr.name in scope:
							expr.binding_id = scope[expr.name]
							break
				for scope in reversed(scope_env):
					if expr.name in scope:
						if expr.binding_id is not None:
							binding_for_var[id(expr)] = expr.binding_id
						return record_expr(expr, scope[expr.name])
				diagnostics.append(Diagnostic(message=f"unknown variable '{expr.name}'", severity="error", span=None))
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HBorrow):
				inner_ty = type_expr(expr.subject)
				ref_ty = self.type_table.ensure_ref_mut(inner_ty) if expr.is_mut else self.type_table.ensure_ref(inner_ty)
				return record_expr(expr, ref_ty)
			if isinstance(expr, H.HCall):
				type_expr(expr.fn)
				for a in expr.args:
					type_expr(a)
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HMethodCall):
				type_expr(expr.receiver)
				for a in expr.args:
					type_expr(a)
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HField):
				type_expr(expr.subject)
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HIndex):
				type_expr(expr.subject)
				type_expr(expr.index)
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HUnary):
				type_expr(expr.expr)
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HBinary):
				type_expr(expr.left)
				type_expr(expr.right)
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HArrayLiteral):
				for e in expr.elements:
					type_expr(e)
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HTernary):
				type_expr(expr.cond)
				type_expr(expr.then_expr)
				type_expr(expr.else_expr)
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HDVInit):
				for a in expr.args:
					type_expr(a)
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HResultOk):
				type_expr(expr.value)
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HTryResult):
				type_expr(expr.expr)
				return record_expr(expr, self._unknown)
			# Fallback: unknown type.
			return record_expr(expr, self._unknown)

		def type_stmt(stmt: H.HStmt) -> None:
			if isinstance(stmt, H.HLet):
				if stmt.binding_id is None:
					stmt.binding_id = self._alloc_local_id()
				locals.append(stmt.binding_id)
				val_ty = type_expr(stmt.value)
				scope_env[-1][stmt.name] = val_ty
				scope_bindings[-1][stmt.name] = stmt.binding_id
				binding_types[stmt.binding_id] = val_ty
				binding_names[stmt.binding_id] = stmt.name
			elif isinstance(stmt, H.HAssign):
				type_expr(stmt.value)
				type_expr(stmt.target)
			elif isinstance(stmt, H.HExprStmt):
				type_expr(stmt.expr)
			elif isinstance(stmt, H.HReturn):
				if stmt.value is not None:
					type_expr(stmt.value)
			elif isinstance(stmt, H.HIf):
				type_expr(stmt.cond)
				type_block(stmt.then_block)
				if stmt.else_block:
					type_block(stmt.else_block)
			elif isinstance(stmt, H.HLoop):
				type_block(stmt.body)
			elif isinstance(stmt, H.HTry):
				type_block(stmt.body)
				for arm in stmt.catches:
					type_block(arm.block)
			elif isinstance(stmt, H.HThrow):
				type_expr(stmt.value)
			# HBreak/HContinue are typeless here.

		def type_block(block: H.HBlock) -> None:
			scope_env.append(dict())
			scope_bindings.append(dict())
			try:
				for s in block.statements:
					type_stmt(s)
			finally:
				scope_env.pop()
				scope_bindings.pop()

		type_block(body)

		typed = TypedFn(
			name=name,
			params=params,
			param_bindings=param_bindings,
			locals=locals,
			body=body,
			expr_types={ref: ty for ref, ty in expr_types.items()},
			binding_for_var=binding_for_var,
			binding_types=binding_types,
			binding_names=binding_names,
		)
		return TypeCheckResult(typed_fn=typed, diagnostics=diagnostics)

	def _alloc_param_id(self) -> ParamId:
		pid = self._next_param_id
		self._next_param_id += 1
		return pid

	def _alloc_local_id(self) -> LocalId:
		lid = self._next_local_id
		self._next_local_id += 1
		return lid
