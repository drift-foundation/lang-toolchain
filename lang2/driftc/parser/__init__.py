"""
lang2 parser copy (self-contained, no runtime dependency on lang/).
Parses Drift source and adapts to lang2.driftc.stage0 AST + FnSignatures for the
lang2 pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Tuple, Optional, List

from . import parser as _parser
from . import ast as parser_ast
from lang2.driftc.stage0 import ast as s0
from lang2.driftc.stage1 import AstToHIR
from lang2.driftc import stage1 as H
from lang2.driftc.checker import FnSignature
from lang2.driftc.core.diagnostics import Diagnostic


def _type_expr_to_str(typ: parser_ast.TypeExpr) -> str:
	"""Render a TypeExpr into a string (e.g., FnResult<Int, Error>)."""
	if not typ.args:
		return typ.name
	args = ", ".join(_type_expr_to_str(a) for a in typ.args)
	return f"{typ.name}<{args}>"


def _convert_expr(expr: parser_ast.Expr) -> s0.Expr:
	"""Convert parser AST expressions into lang2.driftc.stage0 AST expressions."""
	if isinstance(expr, parser_ast.Literal):
		return s0.Literal(value=expr.value, loc=getattr(expr, "loc", None))
	if isinstance(expr, parser_ast.Name):
		return s0.Name(ident=expr.ident, loc=getattr(expr, "loc", None))
	if isinstance(expr, parser_ast.Call):
		return s0.Call(func=_convert_expr(expr.func), args=[_convert_expr(a) for a in expr.args], kwargs=[], loc=getattr(expr, "loc", None))
	if isinstance(expr, parser_ast.Attr):
		return s0.Attr(value=_convert_expr(expr.value), attr=expr.attr, loc=getattr(expr, "loc", None))
	if isinstance(expr, parser_ast.Index):
		return s0.Index(value=_convert_expr(expr.value), index=_convert_expr(expr.index), loc=getattr(expr, "loc", None))
	if isinstance(expr, parser_ast.Binary):
		return s0.Binary(op=expr.op, left=_convert_expr(expr.left), right=_convert_expr(expr.right), loc=getattr(expr, "loc", None))
	if isinstance(expr, parser_ast.Unary):
		return s0.Unary(op=expr.op, operand=_convert_expr(expr.operand), loc=getattr(expr, "loc", None))
	if isinstance(expr, parser_ast.ArrayLiteral):
		return s0.ArrayLiteral(elements=[_convert_expr(e) for e in expr.elements], loc=getattr(expr, "loc", None))
	if isinstance(expr, parser_ast.Move):
		return _convert_expr(expr.value)
	raise NotImplementedError(f"Unsupported expression in adapter: {expr!r}")


def _convert_return(stmt: parser_ast.ReturnStmt) -> s0.Stmt:
	return s0.ReturnStmt(value=_convert_expr(stmt.value) if stmt.value is not None else None, loc=stmt.loc)


def _convert_expr_stmt(stmt: parser_ast.ExprStmt) -> s0.Stmt:
	return s0.ExprStmt(expr=_convert_expr(stmt.value), loc=stmt.loc)


def _convert_let(stmt: parser_ast.LetStmt) -> s0.Stmt:
	return s0.LetStmt(
		name=stmt.name,
		value=_convert_expr(stmt.value),
		type_expr=getattr(stmt, "type_expr", None),
		loc=stmt.loc,
	)


def _convert_assign(stmt: parser_ast.AssignStmt) -> s0.Stmt:
	return s0.AssignStmt(target=_convert_expr(stmt.target), value=_convert_expr(stmt.value), loc=stmt.loc)


def _convert_if(stmt: parser_ast.IfStmt) -> s0.Stmt:
	return s0.IfStmt(
		cond=_convert_expr(stmt.condition),
		then_block=_convert_block(stmt.then_block),
		else_block=_convert_block(stmt.else_block) if stmt.else_block else [],
	)


def _convert_break(stmt: parser_ast.BreakStmt) -> s0.Stmt:
	return s0.BreakStmt(loc=stmt.loc)


def _convert_continue(stmt: parser_ast.ContinueStmt) -> s0.Stmt:
	return s0.ContinueStmt(loc=stmt.loc)


def _convert_throw(stmt: parser_ast.ThrowStmt) -> s0.Stmt:
	return s0.ThrowStmt(value=_convert_expr(stmt.expr), loc=stmt.loc)


def _convert_raise(stmt: parser_ast.RaiseStmt) -> s0.Stmt:
	# TODO: when rethrow semantics are defined, map RaiseStmt appropriately.
	# For now, treat parser RaiseStmt as a plain throw of the expression.
	expr = getattr(stmt, "expr", None) or getattr(stmt, "value")
	return s0.ThrowStmt(value=_convert_expr(expr), loc=stmt.loc)


_STMT_DISPATCH: dict[type[parser_ast.Stmt], Callable[[parser_ast.Stmt], s0.Stmt]] = {
	parser_ast.ReturnStmt: _convert_return,
	parser_ast.ExprStmt: _convert_expr_stmt,
	parser_ast.LetStmt: _convert_let,
	parser_ast.AssignStmt: _convert_assign,
	parser_ast.IfStmt: _convert_if,
	parser_ast.BreakStmt: _convert_break,
	parser_ast.ContinueStmt: _convert_continue,
	parser_ast.ThrowStmt: _convert_throw,
	parser_ast.RaiseStmt: _convert_raise,
}


def _convert_stmt(stmt: parser_ast.Stmt) -> s0.Stmt:
	"""Convert parser AST statements into lang2.driftc.stage0 AST statements."""
	fn = _STMT_DISPATCH.get(type(stmt))
	if fn is None:
		# While/For/Try not yet needed for current e2e cases.
		raise NotImplementedError(f"Unsupported statement in adapter: {stmt!r}")
	return fn(stmt)


def _convert_block(block: parser_ast.Block) -> list[s0.Stmt]:
	return [_convert_stmt(s) for s in block.statements]


class _FrontendParam:
	def __init__(self, name: str, type_expr: parser_ast.TypeExpr, loc: Optional[parser_ast.Located]) -> None:
		self.name = name
		# Preserve the parsed type expression so the resolver can build real TypeIds.
		self.type = type_expr
		self.loc = loc


class _FrontendDecl:
	def __init__(
		self,
		name: str,
		method_name: Optional[str],
		params: list[_FrontendParam],
		return_type: parser_ast.TypeExpr,
		loc: Optional[parser_ast.Located],
		is_method: bool = False,
		self_mode: Optional[str] = None,
		impl_target: Optional[parser_ast.TypeExpr] = None,
		module: Optional[str] = None,
	) -> None:
		self.name = name
		self.method_name = method_name
		self.params = params
		self.return_type = return_type
		self.throws = ()
		self.loc = loc
		self.is_extern = False
		self.is_intrinsic = False
		self.is_method = is_method
		self.self_mode = self_mode
		self.impl_target = impl_target
		self.module = module


def _decl_from_parser_fn(fn: parser_ast.FunctionDef) -> _FrontendDecl:
	params = [_FrontendParam(p.name, p.type_expr, getattr(p, "loc", None)) for p in fn.params]
	return _FrontendDecl(
		fn.name,
		fn.orig_name,
		params,
		fn.return_type,
		getattr(fn, "loc", None),
		fn.is_method,
		fn.self_mode,
		fn.impl_target,
	)


def parse_drift_to_hir(path: Path) -> Tuple[Dict[str, H.HBlock], Dict[str, FnSignature], "TypeTable", List[Diagnostic]]:
	"""
	Parse a Drift source file into lang2 HIR blocks + FnSignatures + TypeTable.

	Collects parser/adapter diagnostics (e.g., duplicate functions) instead of
	throwing, so callers can report them alongside later pipeline checks.
	"""
	source = path.read_text()
	prog = _parser.parse_program(source)
	module_name = getattr(prog, "module", None)
	func_hirs: Dict[str, H.HBlock] = {}
	decls: list[_FrontendDecl] = []
	signatures: Dict[str, FnSignature] = {}
	lowerer = AstToHIR()
	seen: set[str] = set()
	method_keys: set[tuple[str, str]] = set()  # (impl_target_repr, method_name)
	diagnostics: list[Diagnostic] = []
	for fn in prog.functions:
		if fn.name in seen:
			diagnostics.append(
				Diagnostic(
					message=f"duplicate function definition for '{fn.name}'",
					severity="error",
					span=getattr(fn, "loc", None),
				)
			)
			# Skip adding a duplicate; keep the first definition.
			continue
		seen.add(fn.name)
		decl_decl = _decl_from_parser_fn(fn)
		decl_decl.module = module_name
		decls.append(decl_decl)
		stmt_block = _convert_block(fn.body)
		hir_block = lowerer.lower_block(stmt_block)
		func_hirs[fn.name] = hir_block
	# Methods inside implement blocks.
	for impl in getattr(prog, "implements", []):
		# Reject reference-qualified impl headers in v1 (must be nominal types).
		if getattr(impl.target, "name", None) in {"&", "&mut"}:
			diagnostics.append(
				Diagnostic(
					message="implement header must use a nominal type, not a reference type",
					severity="error",
					span=getattr(impl, "loc", None),
				)
			)
			continue
		for fn in impl.methods:
			if not fn.params:
				diagnostics.append(
					Diagnostic(
						message=f"method '{fn.name}' must have a receiver parameter",
						severity="error",
						span=getattr(fn, "loc", None),
					)
				)
				continue
			receiver_ty = fn.params[0].type_expr
			self_mode = "value"
			if receiver_ty.name == "&":
				self_mode = "ref"
			elif receiver_ty.name == "&mut":
				self_mode = "ref_mut"
			# Reject nested refs for v1 (e.g., &&T).
			if receiver_ty.name in {"&", "&mut"} and receiver_ty.args and receiver_ty.args[0].name in {"&", "&mut"}:
				diagnostics.append(
					Diagnostic(
						message=f"unsupported receiver type for method '{fn.name}'",
						severity="error",
						span=getattr(fn, "loc", None),
					)
				)
				continue
			params = [_FrontendParam(p.name, p.type_expr, getattr(p, "loc", None)) for p in fn.params]
			if fn.name in seen:
				diagnostics.append(
					Diagnostic(
						message=f"method '{fn.name}' conflicts with existing free function of the same name",
						severity="error",
						span=getattr(fn, "loc", None),
					)
				)
				continue
			target_str = _type_expr_to_str(impl.target)
			key = (target_str, fn.name)
			if key in method_keys:
				diagnostics.append(
					Diagnostic(
						message=f"duplicate method definition '{fn.name}' for type '{target_str}'",
						severity="error",
						span=getattr(fn, "loc", None),
					)
				)
				continue
			method_keys.add(key)
			symbol_name = f"{target_str}::{fn.name}"
			decls.append(
				_FrontendDecl(
					symbol_name,
					fn.orig_name,
					params,
					fn.return_type,
					getattr(fn, "loc", None),
					is_method=True,
					self_mode=self_mode,
					impl_target=impl.target,
					module=module_name,
				)
			)
			stmt_block = _convert_block(fn.body)
			hir_block = lowerer.lower_block(stmt_block)
			func_hirs[symbol_name] = hir_block
	# Build signatures with resolved TypeIds from parser decls.
	from lang2.driftc.type_resolver import resolve_program_signatures

	type_table, sigs = resolve_program_signatures(decls)
	signatures.update(sigs)
	return func_hirs, signatures, type_table, diagnostics


__all__ = ["parse_drift_to_hir"]
