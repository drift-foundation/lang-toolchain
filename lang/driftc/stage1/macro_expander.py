# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.stage0 import ast


def expand_macro_call(expr: ast.MacroCall) -> ast.Call:
	"""
	Expand an MVP macro call into a normal stage0 call expression.

	Current built-ins:
	- log.info!(logger, ev[, arg3[, arg4]])  -> log.macro_info(...)
	- log.debug!(logger, ev[, arg3[, arg4]]) -> log.macro_debug(...)
	- log.error!(logger, ev[, arg3[, arg4]]) -> log.macro_error(...)
	"""
	if getattr(expr, "kwargs", None):
		raise ValueError("macro calls do not support keyword arguments in v1")
	macro_suffix = _macro_call_suffix_name(expr.func)
	if macro_suffix is None:
		raise ValueError("unsupported macro call target in v1")
	if len(expr.args) < 2 or len(expr.args) > 4:
		raise ValueError(f"macro '{macro_suffix}!' expects 2-4 positional args: (logger, ev[, arg3[, arg4]])")
	rewrite = {
		"info": "macro_info",
		"debug": "macro_debug",
		"error": "macro_error",
	}
	target_method = rewrite.get(macro_suffix)
	if target_method is None:
		raise ValueError(f"unknown macro '{macro_suffix}!'")
	return ast.Call(
		loc=expr.loc,
		func=_macro_call_rewrite_target(expr.func, target_method),
		args=list(expr.args) + [_caller_expr(expr)],
		kwargs=[],
		type_args=None,
	)


def _macro_call_suffix_name(fn_expr: ast.Expr) -> str | None:
	if isinstance(fn_expr, ast.Attr):
		return fn_expr.attr
	return None


def _macro_call_rewrite_target(fn_expr: ast.Expr, target_method: str) -> ast.Expr:
	if not isinstance(fn_expr, ast.Attr):
		raise ValueError("unsupported macro call target shape")
	return ast.Attr(loc=fn_expr.loc, value=fn_expr.value, attr=target_method)


def _caller_expr(expr: ast.MacroCall) -> ast.Call:
	return ast.Call(
		loc=expr.loc,
		func=ast.Attr(loc=expr.loc, value=ast.Name(loc=expr.loc, ident="meta"), attr="caller"),
		args=[],
		kwargs=[],
		type_args=None,
	)
