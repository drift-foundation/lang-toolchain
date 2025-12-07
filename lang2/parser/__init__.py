"""
lang2 parser copy (self-contained, no runtime dependency on lang/).
Parses Drift source and adapts to lang2.stage0 AST + FnSignatures for the
lang2 pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

from . import parser as _parser
from . import ast as parser_ast
from lang2.stage0 import ast as s0
from lang2.stage1 import AstToHIR
from lang2.checker import FnSignature


def _type_expr_to_str(typ: parser_ast.TypeExpr) -> str:
	"""Render a TypeExpr into a string (e.g., FnResult<Int, Error>)."""
	if not typ.args:
		return typ.name
	args = ", ".join(_type_expr_to_str(a) for a in typ.args)
	return f"{typ.name}<{args}>"


def _convert_expr(expr: parser_ast.Expr) -> s0.Expr:
	"""Convert parser AST expressions into lang2.stage0 AST expressions."""
	if isinstance(expr, parser_ast.Literal):
		return s0.Literal(value=expr.value)
	if isinstance(expr, parser_ast.Name):
		return s0.Name(ident=expr.ident)
	if isinstance(expr, parser_ast.Call):
		return s0.Call(func=_convert_expr(expr.func), args=[_convert_expr(a) for a in expr.args], kwargs=[], loc=None)
	if isinstance(expr, parser_ast.Attr):
		return s0.Attr(value=_convert_expr(expr.value), attr=expr.attr)
	if isinstance(expr, parser_ast.Index):
		return s0.Index(value=_convert_expr(expr.value), index=_convert_expr(expr.index))
	if isinstance(expr, parser_ast.Binary):
		return s0.Binary(op=expr.op, left=_convert_expr(expr.left), right=_convert_expr(expr.right))
	if isinstance(expr, parser_ast.Unary):
		return s0.Unary(op=expr.op, operand=_convert_expr(expr.operand))
	if isinstance(expr, parser_ast.Move):
		return _convert_expr(expr.value)
	raise NotImplementedError(f"Unsupported expression in adapter: {expr!r}")


def _convert_stmt(stmt: parser_ast.Stmt) -> s0.Stmt:
	"""Convert parser AST statements into lang2.stage0 AST statements."""
	if isinstance(stmt, parser_ast.ReturnStmt):
		return s0.ReturnStmt(value=_convert_expr(stmt.value) if stmt.value is not None else None)
	if isinstance(stmt, parser_ast.ExprStmt):
		return s0.ExprStmt(expr=_convert_expr(stmt.value))
	if isinstance(stmt, parser_ast.LetStmt):
		return s0.LetStmt(name=stmt.name, value=_convert_expr(stmt.value))
	if isinstance(stmt, parser_ast.AssignStmt):
		return s0.AssignStmt(target=_convert_expr(stmt.target), value=_convert_expr(stmt.value))
	if isinstance(stmt, parser_ast.IfStmt):
		return s0.IfStmt(
			cond=_convert_expr(stmt.condition),
			then_block=_convert_block(stmt.then_block),
			else_block=_convert_block(stmt.else_block) if stmt.else_block else [],
		)
	if isinstance(stmt, parser_ast.BreakStmt):
		return s0.BreakStmt()
	if isinstance(stmt, parser_ast.ContinueStmt):
		return s0.ContinueStmt()
	if isinstance(stmt, parser_ast.ThrowStmt):
		return s0.ThrowStmt(value=_convert_expr(stmt.value))
	# While/For/Try not yet needed for current e2e cases.
	raise NotImplementedError(f"Unsupported statement in adapter: {stmt!r}")


def _convert_block(block: parser_ast.Block) -> list[s0.Stmt]:
	return [_convert_stmt(s) for s in block.statements]


def parse_drift_to_hir(path: Path) -> Tuple[Dict[str, s0.HBlock], Dict[str, FnSignature]]:
	"""Parse a Drift source file into lang2 HIR blocks + FnSignatures."""
	source = path.read_text()
	prog = _parser.parse_program(source)
	func_hirs: Dict[str, s0.HBlock] = {}
	signatures: Dict[str, FnSignature] = {}
	lowerer = AstToHIR()
	for fn in prog.functions:
		stmt_block = _convert_block(fn.body)
		hir_block = lowerer.lower_block(stmt_block)
		func_hirs[fn.name] = hir_block
		ret_str = _type_expr_to_str(fn.return_type)
		signatures[fn.name] = FnSignature(name=fn.name, return_type=ret_str)
	return func_hirs, signatures


__all__ = ["parse_drift_to_hir"]
