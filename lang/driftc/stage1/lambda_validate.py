# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.span import Span
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.method_registry import CallableDecl
from lang.driftc.method_resolver import MethodResolution
from lang.driftc.stage1 import closures as C
from lang.driftc.stage1.capture_discovery import discover_captures
from lang.driftc.stage1 import hir_nodes as H


# Lambda validation diagnostics are typecheck-phase.
def _lambda_diag(*args, **kwargs):
	if "phase" not in kwargs or kwargs.get("phase") is None:
		kwargs["phase"] = "typecheck"
	return Diagnostic(*args, **kwargs)


@dataclass
class LambdaValidationResult:
	diagnostics: list[Diagnostic]


def validate_lambdas_non_retaining(
	node: H.HNode,
	*,
	signatures_by_id: Mapping[FunctionId, FnSignature] | None = None,
	call_resolutions: Mapping[int, object] | None = None,
) -> LambdaValidationResult:
	"""
	Validate that borrowed-capture lambdas are non-escaping in v0.

	Allowed forms:
	- immediate invocation: (|...| => ...)(...)
	- passing to a callee param proven non-retaining
	"""
	diags: list[Diagnostic] = []
	signatures_by_id = signatures_by_id or {}
	call_resolutions = call_resolutions or {}
	method_sig_by_key: dict[tuple[int, str], FnSignature] = {}
	for sig in signatures_by_id.values():
		if sig.is_method and sig.impl_target_type_id is not None:
			method_sig_by_key[(sig.impl_target_type_id, sig.method_name or sig.name)] = sig

	def _emit_error(span: Span) -> None:
		diags.append(
			_lambda_diag(
				message="closures with borrowed captures are non-escaping in v0; only immediate invocation or proven non-retaining params are supported",
				severity="error",
				span=span,
				notes=["wrap it like: (|...| => ...)(...)"],
			)
		)

	def _iter_expr_children(e: H.HExpr) -> list[H.HExpr]:
		children: list[H.HExpr] = []
		for field_name in getattr(e, "__dataclass_fields__", {}) or {}:
			val = getattr(e, field_name, None)
			if isinstance(val, H.HExpr):
				children.append(val)
			elif isinstance(val, list):
				for item in val:
					if isinstance(item, H.HExpr):
						children.append(item)
		return children

	def _resolve_sig_for_call(call: H.HExpr) -> FnSignature | None:
		if isinstance(call, H.HCall):
			res = call_resolutions.get(call.node_id)
			if isinstance(res, CallableDecl):
				if res.fn_id is None:
					return None
				return signatures_by_id.get(res.fn_id)
			return None
		if isinstance(call, H.HMethodCall):
			res = call_resolutions.get(call.node_id)
			if isinstance(res, MethodResolution):
				if res.decl.fn_id is not None:
					return signatures_by_id.get(res.decl.fn_id)
				impl_target = res.decl.impl_target_type_id
				if impl_target is None:
					return None
				return method_sig_by_key.get((impl_target, res.decl.name))
		return None

	def _param_index_for_call(
		sig: FnSignature,
		*,
		arg_index: int | None = None,
		kw_name: str | None = None,
	) -> int | None:
		if kw_name is not None:
			if not sig.param_names:
				return None
			try:
				idx = sig.param_names.index(kw_name)
			except ValueError:
				return None
			if sig.is_method and idx == 0:
				return None
			return idx
		if arg_index is None:
			return None
		if sig.is_method:
			return arg_index + 1
		return arg_index

	def _allow_lambda_arg(call: H.HExpr, *, arg_index: int | None = None, kw_name: str | None = None) -> bool:
		sig = _resolve_sig_for_call(call)
		if sig is None or not sig.param_nonretaining:
			return False
		param_index = _param_index_for_call(sig, arg_index=arg_index, kw_name=kw_name)
		if param_index is None:
			return False
		if param_index >= len(sig.param_nonretaining):
			return False
		return sig.param_nonretaining[param_index] is True

	def _walk_expr(e: H.HExpr, allow_lambda: bool) -> None:
		if isinstance(e, H.HLambda):
			res = discover_captures(e)
			diags.extend(res.diagnostics)
			has_borrow = any(
				cap.kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT)
				for cap in res.captures
			)
			if not allow_lambda and has_borrow:
				_emit_error(e.span)
			if e.body_expr is not None:
				_walk_expr(e.body_expr, allow_lambda=False)
			if e.body_block is not None:
				for stmt in e.body_block.statements:
					_walk_stmt(stmt)
			return
		if isinstance(e, H.HPlaceExpr):
			_walk_expr(e.base, allow_lambda=False)
			for proj in e.projections:
				if isinstance(proj, H.HPlaceIndex):
					_walk_expr(proj.index, allow_lambda=False)
			return
		if isinstance(e, H.HCall):
			_walk_expr(e.fn, allow_lambda=True)
			for idx, arg in enumerate(e.args):
				_walk_expr(arg, allow_lambda=_allow_lambda_arg(e, arg_index=idx))
			for kw in e.kwargs:
				_walk_expr(kw.value, allow_lambda=_allow_lambda_arg(e, kw_name=kw.name))
			return
		if isinstance(e, getattr(H, "HInvoke", ())):
			_walk_expr(e.callee, allow_lambda=True)
			for arg in e.args:
				_walk_expr(arg, allow_lambda=False)
			for kw in e.kwargs:
				_walk_expr(kw.value, allow_lambda=False)
			return
		if isinstance(e, H.HMethodCall):
			allow_receiver_lambda = e.method_name == "call" and isinstance(e.receiver, H.HLambda)
			_walk_expr(e.receiver, allow_lambda=allow_receiver_lambda)
			for idx, arg in enumerate(e.args):
				_walk_expr(arg, allow_lambda=_allow_lambda_arg(e, arg_index=idx))
			for kw in e.kwargs:
				_walk_expr(kw.value, allow_lambda=_allow_lambda_arg(e, kw_name=kw.name))
			return
		if isinstance(e, H.HExceptionInit):
			for arg in e.pos_args:
				_walk_expr(arg, allow_lambda=False)
			for kw in e.kw_args:
				_walk_expr(kw.value, allow_lambda=False)
			return
		if isinstance(e, H.HMatchExpr):
			_walk_expr(e.scrutinee, allow_lambda=False)
			for arm in e.arms:
				for stmt in arm.block.statements:
					_walk_stmt(stmt)
				if arm.result is not None:
					_walk_expr(arm.result, allow_lambda=False)
			return
		if isinstance(e, H.HTryExpr):
			_walk_expr(e.attempt, allow_lambda=False)
			for arm in e.arms:
				for stmt in arm.block.statements:
					_walk_stmt(stmt)
				if arm.result is not None:
					_walk_expr(arm.result, allow_lambda=False)
			return
		if isinstance(e, H.HTernary):
			_walk_expr(e.cond, allow_lambda=False)
			_walk_expr(e.then_expr, allow_lambda=False)
			_walk_expr(e.else_expr, allow_lambda=False)
			return
		for child in _iter_expr_children(e):
			_walk_expr(child, allow_lambda=False)

	def _walk_stmt(s: H.HStmt) -> None:
		if isinstance(s, H.HBlock):
			for stmt in s.statements:
				_walk_stmt(stmt)
		elif hasattr(H, "HUnsafeBlock") and isinstance(s, getattr(H, "HUnsafeBlock")):
			for stmt in s.block.statements:
				_walk_stmt(stmt)
		elif isinstance(s, H.HExprStmt):
			_walk_expr(s.expr, allow_lambda=False)
		elif isinstance(s, H.HLet):
			_walk_expr(s.value, allow_lambda=False)
		elif isinstance(s, H.HAssign):
			_walk_expr(s.target, allow_lambda=False)
			_walk_expr(s.value, allow_lambda=False)
		elif isinstance(s, H.HAugAssign):
			_walk_expr(s.target, allow_lambda=False)
			_walk_expr(s.value, allow_lambda=False)
		elif isinstance(s, H.HIf):
			_walk_expr(s.cond, allow_lambda=False)
			for stmt in s.then_block.statements:
				_walk_stmt(stmt)
			if s.else_block:
				for stmt in s.else_block.statements:
					_walk_stmt(stmt)
		elif isinstance(s, H.HReturn):
			if s.value is not None:
				_walk_expr(s.value, allow_lambda=False)
		elif isinstance(s, H.HLoop):
			for stmt in s.body.statements:
				_walk_stmt(stmt)
		elif isinstance(s, H.HTry):
			for stmt in s.body.statements:
				_walk_stmt(stmt)
			for arm in s.catches:
				for stmt in arm.block.statements:
					_walk_stmt(stmt)
		elif isinstance(s, H.HThrow):
			_walk_expr(s.value, allow_lambda=False)
		elif isinstance(s, H.HMatchExpr):
			_walk_expr(s, allow_lambda=False)
		elif isinstance(s, H.HTryExpr):
			_walk_expr(s, allow_lambda=False)

	if isinstance(node, H.HExpr):
		_walk_expr(node, allow_lambda=False)
	elif isinstance(node, H.HStmt):
		_walk_stmt(node)
	return LambdaValidationResult(diagnostics=diags)
