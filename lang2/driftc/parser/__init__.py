"""
lang2 parser copy (self-contained, no runtime dependency on lang/).
Parses Drift source and adapts to lang2.driftc.stage0 AST + FnSignatures for the
lang2 pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Tuple, Optional, List

from lark.exceptions import UnexpectedInput

from . import parser as _parser
from . import ast as parser_ast
from lang2.driftc.stage0 import ast as s0
from lang2.driftc.stage1 import AstToHIR
from lang2.driftc import stage1 as H
from lang2.driftc.checker import FnSignature
from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.core.span import Span
from lang2.driftc.core.event_codes import event_code, PAYLOAD_MASK
from lang2.driftc.core.types_core import TypeTable


def _type_expr_to_str(typ: parser_ast.TypeExpr) -> str:
	"""Render a TypeExpr into a string (e.g., Array<Int>, Result<Int, Error>)."""
	if not typ.args:
		return typ.name
	args = ", ".join(_type_expr_to_str(a) for a in typ.args)
	return f"{typ.name}<{args}>"


def _convert_expr(expr: parser_ast.Expr) -> s0.Expr:
	"""Convert parser AST expressions into lang2.driftc.stage0 AST expressions."""
	if isinstance(expr, parser_ast.Literal):
		return s0.Literal(value=expr.value, loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.Name):
		return s0.Name(ident=expr.ident, loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.Call):
		return s0.Call(
			func=_convert_expr(expr.func),
			args=[_convert_expr(a) for a in expr.args],
			kwargs=[
				s0.KwArg(
					name=kw.name,
					value=_convert_expr(kw.value),
					loc=Span.from_loc(getattr(kw, "loc", None)),
				)
				for kw in getattr(expr, "kwargs", [])
			],
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Attr):
		return s0.Attr(
			value=_convert_expr(expr.value),
			attr=expr.attr,
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Index):
		return s0.Index(
			value=_convert_expr(expr.value),
			index=_convert_expr(expr.index),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Binary):
		return s0.Binary(
			op=expr.op,
			left=_convert_expr(expr.left),
			right=_convert_expr(expr.right),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Unary):
		return s0.Unary(op=expr.op, operand=_convert_expr(expr.operand), loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.ArrayLiteral):
		return s0.ArrayLiteral(elements=[_convert_expr(e) for e in expr.elements], loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.Move):
		return _convert_expr(expr.value)
	if isinstance(expr, parser_ast.Placeholder):
		return s0.Placeholder(loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.Ternary):
		return s0.Ternary(
			cond=_convert_expr(expr.condition),
			then_expr=_convert_expr(expr.then_value),
			else_expr=_convert_expr(expr.else_value),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.TryCatchExpr):
		catch_arms = [
			s0.CatchExprArm(
				event=arm.event,
				binder=arm.binder,
				block=_convert_block(arm.block),
				loc=Span.from_loc(getattr(arm, "loc", None)),
			)
			for arm in expr.catch_arms
		]
		return s0.TryCatchExpr(
			attempt=_convert_expr(expr.attempt),
			catch_arms=catch_arms,
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.ExceptionCtor):
		return s0.ExceptionCtor(
			name=expr.name,
			args=[_convert_expr(a) for a in expr.args],
			kwargs=[
				s0.KwArg(
					name=kw.name,
					value=_convert_expr(kw.value),
					loc=Span.from_loc(getattr(kw, "loc", None)),
				)
				for kw in expr.kwargs
			],
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.FString):
		return s0.FString(
			parts=list(expr.parts),
			holes=[
				s0.FStringHole(
					expr=_convert_expr(h.expr),
					spec=h.spec,
					loc=Span.from_loc(getattr(h, "loc", None)),
				)
				for h in expr.holes
			],
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	raise NotImplementedError(f"Unsupported expression in adapter: {expr!r}")


def _convert_return(stmt: parser_ast.ReturnStmt) -> s0.Stmt:
	return s0.ReturnStmt(value=_convert_expr(stmt.value) if stmt.value is not None else None, loc=Span.from_loc(stmt.loc))


def _convert_expr_stmt(stmt: parser_ast.ExprStmt) -> s0.Stmt:
	return s0.ExprStmt(expr=_convert_expr(stmt.value), loc=Span.from_loc(stmt.loc))


def _convert_let(stmt: parser_ast.LetStmt) -> s0.Stmt:
	return s0.LetStmt(
		name=stmt.name,
		value=_convert_expr(stmt.value),
		type_expr=getattr(stmt, "type_expr", None),
		mutable=bool(getattr(stmt, "mutable", False)),
		loc=Span.from_loc(stmt.loc),
	)


def _convert_assign(stmt: parser_ast.AssignStmt) -> s0.Stmt:
	return s0.AssignStmt(target=_convert_expr(stmt.target), value=_convert_expr(stmt.value), loc=Span.from_loc(stmt.loc))


def _convert_if(stmt: parser_ast.IfStmt) -> s0.Stmt:
	return s0.IfStmt(
		cond=_convert_expr(stmt.condition),
		then_block=_convert_block(stmt.then_block),
		else_block=_convert_block(stmt.else_block) if stmt.else_block else [],
		loc=Span.from_loc(stmt.loc),
	)


def _convert_break(stmt: parser_ast.BreakStmt) -> s0.Stmt:
	return s0.BreakStmt(loc=Span.from_loc(stmt.loc))


def _convert_continue(stmt: parser_ast.ContinueStmt) -> s0.Stmt:
	return s0.ContinueStmt(loc=Span.from_loc(stmt.loc))


def _convert_while(stmt: parser_ast.WhileStmt) -> s0.Stmt:
	return s0.WhileStmt(cond=_convert_expr(stmt.condition), body=_convert_block(stmt.body), loc=Span.from_loc(stmt.loc))


def _convert_for(stmt: parser_ast.ForStmt) -> s0.Stmt:
	return s0.ForStmt(iter_var=stmt.var, iterable=_convert_expr(stmt.iter_expr), body=_convert_block(stmt.body), loc=Span.from_loc(stmt.loc))


def _convert_throw(stmt: parser_ast.ThrowStmt) -> s0.Stmt:
	return s0.ThrowStmt(value=_convert_expr(stmt.expr), loc=Span.from_loc(stmt.loc))


def _convert_raise(stmt: parser_ast.RaiseStmt) -> s0.Stmt:
	# TODO: when rethrow semantics are defined, map RaiseStmt appropriately.
	# For now, treat parser RaiseStmt as a plain throw of the expression.
	expr = getattr(stmt, "expr", None) or getattr(stmt, "value")
	return s0.ThrowStmt(value=_convert_expr(expr), loc=Span.from_loc(stmt.loc))


def _convert_rethrow(stmt: parser_ast.RethrowStmt) -> s0.Stmt:
	return s0.RethrowStmt(loc=Span.from_loc(stmt.loc))


def _convert_try(stmt: parser_ast.TryStmt) -> s0.Stmt:
	catches = [
		s0.CatchExprArm(
			event=c.event,
			binder=c.binder,
			block=_convert_block(c.block),
			loc=Span.from_loc(getattr(c, "loc", None)),
		)
		for c in stmt.catches
	]
	return s0.TryStmt(body=_convert_block(stmt.body), catches=catches, loc=Span.from_loc(stmt.loc))


def _convert_import(stmt: parser_ast.ImportStmt) -> s0.Stmt:
	path = ".".join(stmt.path)
	return s0.ImportStmt(path=path, loc=Span.from_loc(stmt.loc))


_STMT_DISPATCH: dict[type[parser_ast.Stmt], Callable[[parser_ast.Stmt], s0.Stmt]] = {
	parser_ast.ReturnStmt: _convert_return,
	parser_ast.ExprStmt: _convert_expr_stmt,
	parser_ast.LetStmt: _convert_let,
	parser_ast.AssignStmt: _convert_assign,
	parser_ast.IfStmt: _convert_if,
	parser_ast.BreakStmt: _convert_break,
	parser_ast.ContinueStmt: _convert_continue,
	parser_ast.WhileStmt: _convert_while,
	parser_ast.ForStmt: _convert_for,
	parser_ast.ThrowStmt: _convert_throw,
	parser_ast.RaiseStmt: _convert_raise,
	parser_ast.RethrowStmt: _convert_rethrow,
	parser_ast.TryStmt: _convert_try,
	parser_ast.ImportStmt: _convert_import,
}


def _convert_stmt(stmt: parser_ast.Stmt) -> s0.Stmt:
	"""Convert parser AST statements into lang2.driftc.stage0 AST statements."""
	fn = _STMT_DISPATCH.get(type(stmt))
	if fn is None:
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


def _diagnostic(message: str, loc: object | None) -> Diagnostic:
	"""Helper to create a Diagnostic from a parser location."""
	return Diagnostic(message=message, severity="error", span=Span.from_loc(loc))


def _typeexpr_uses_internal_fnresult(typ: parser_ast.TypeExpr) -> bool:
	"""
	Return True if a surface type annotation mentions `FnResult` anywhere.

	`FnResult<T, Error>` is an internal ABI carrier used by lang2 for can-throw
	functions. It is not a surface type in the Drift language: user code should
	write `returns T` and use exceptions/try/catch for control flow.
	"""
	if typ.name == "FnResult":
		return True
	for arg in getattr(typ, "args", []) or []:
		if _typeexpr_uses_internal_fnresult(arg):
			return True
	return False


def _report_internal_fnresult_in_surface_type(
	*,
	kind: str,
	symbol: str,
	loc: object | None,
	diagnostics: list[Diagnostic],
) -> None:
	diagnostics.append(
		_diagnostic(
			f"{kind} '{symbol}' uses internal-only type 'FnResult' in a surface annotation; "
			"write `returns T` and use exceptions/try-catch instead",
			loc,
		)
	)


def _build_exception_catalog(exceptions: list[parser_ast.ExceptionDef], module_name: str | None, diagnostics: list[Diagnostic]) -> dict[str, int]:
	"""
	Assign deterministic event codes to exception declarations using the shared ABI hash.

	Collisions on the payload bits are reported as errors and the colliding
	exceptions are omitted from the catalog to avoid undefined dispatch.
	"""
	catalog: dict[str, int] = {}
	payload_seen: dict[int, str] = {}
	seen_names: set[str] = set()
	for exc in exceptions:
		if exc.name in seen_names:
			diagnostics.append(_diagnostic(f"duplicate exception '{exc.name}'", getattr(exc, "loc", None)))
			continue
		seen_names.add(exc.name)
		fqn = f"{module_name}:{exc.name}" if module_name else exc.name
		code = event_code(fqn)
		payload = code & PAYLOAD_MASK
		if payload in payload_seen and payload_seen[payload] != fqn:
			other = payload_seen[payload]
			diagnostics.append(
				_diagnostic(
					f"exception code collision between '{other}' and '{fqn}' (payload {payload})",
					getattr(exc, "loc", None),
				)
			)
			continue
		payload_seen[payload] = fqn
		catalog[fqn] = code
	return catalog


def parse_drift_to_hir(path: Path) -> Tuple[Dict[str, H.HBlock], Dict[str, FnSignature], "TypeTable", Dict[str, int], List[Diagnostic]]:
	"""
	Parse a Drift source file into lang2 HIR blocks + FnSignatures + TypeTable.

	Collects parser/adapter diagnostics (e.g., duplicate functions) instead of
	throwing, so callers can report them alongside later pipeline checks.
	"""
	source = path.read_text()
	try:
		prog = _parser.parse_program(source)
	except _parser.FStringParseError as err:
		# f-string syntax errors should be reported as normal parser diagnostics,
		# not hard crashes (they are user errors, not internal bugs).
		diagnostics: list[Diagnostic] = [
			Diagnostic(message=str(err), severity="error", span=Span.from_loc(err.loc))
		]
		return {}, {}, TypeTable(), {}, diagnostics
	except UnexpectedInput as err:
		# Parse errors are user errors; do not crash the compiler pipeline.
		#
		# The Span best-effort extracts `line`/`column` from the lark exception, and
		# we also thread the file name so diagnostics are source-anchored.
		span = Span(
			file=str(path),
			line=getattr(err, "line", None),
			column=getattr(err, "column", None),
			raw=err,
		)
		diagnostics = [Diagnostic(message=str(err), severity="error", span=span)]
		return {}, {}, TypeTable(), {}, diagnostics
	module_name = getattr(prog, "module", None)
	func_hirs: Dict[str, H.HBlock] = {}
	decls: list[_FrontendDecl] = []
	signatures: Dict[str, FnSignature] = {}
	lowerer = AstToHIR()
	lowerer._module_name = module_name
	seen: set[str] = set()
	method_keys: set[tuple[str, str]] = set()  # (impl_target_repr, method_name)
	diagnostics: list[Diagnostic] = []
	exception_schemas: dict[str, tuple[str, list[str]]] = {}
	exception_catalog: dict[str, int] = _build_exception_catalog(prog.exceptions, module_name, diagnostics)
	for exc in prog.exceptions:
		fqn = f"{module_name}:{exc.name}" if module_name else exc.name
		field_names = [arg.name for arg in getattr(exc, "args", [])]
		exception_schemas[fqn] = (fqn, field_names)
	for fn in prog.functions:
		if fn.name in seen:
			diagnostics.append(
				Diagnostic(
					message=f"duplicate function definition for '{fn.name}'",
					severity="error",
					span=Span.from_loc(getattr(fn, "loc", None)),
				)
			)
			# Skip adding a duplicate; keep the first definition.
			continue
		seen.add(fn.name)
		decl_decl = _decl_from_parser_fn(fn)
		decl_decl.module = module_name
		# Reject FnResult in surface type annotations (return or parameter types).
		# FnResult is an internal ABI carrier in lang2, not a user-facing type.
		if _typeexpr_uses_internal_fnresult(decl_decl.return_type):
			_report_internal_fnresult_in_surface_type(
				kind="function",
				symbol=fn.name,
				loc=getattr(fn.return_type, "loc", getattr(fn, "loc", None)),
				diagnostics=diagnostics,
			)
		for p in getattr(fn, "params", []) or []:
			if _typeexpr_uses_internal_fnresult(p.type_expr):
				_report_internal_fnresult_in_surface_type(
					kind="parameter",
					symbol=f"{fn.name}({p.name})",
					loc=getattr(p.type_expr, "loc", getattr(p, "loc", None)),
					diagnostics=diagnostics,
				)
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
					span=Span.from_loc(getattr(impl, "loc", None)),
				)
			)
			continue
		for fn in impl.methods:
			if not fn.params:
				diagnostics.append(
					Diagnostic(
						message=f"method '{fn.name}' must have a receiver parameter",
						severity="error",
						span=Span.from_loc(getattr(fn, "loc", None)),
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
						span=Span.from_loc(getattr(fn, "loc", None)),
					)
				)
				continue

			# Compute the canonical symbol for this method early so any diagnostics
			# (including type-annotation validation) can reference it.
			target_str = _type_expr_to_str(impl.target)
			symbol_name = f"{target_str}::{fn.name}"

			params = [_FrontendParam(p.name, p.type_expr, getattr(p, "loc", None)) for p in fn.params]
			# Reject FnResult in method surface type annotations too.
			if _typeexpr_uses_internal_fnresult(fn.return_type):
				_report_internal_fnresult_in_surface_type(
					kind="method",
					symbol=symbol_name,
					loc=getattr(fn.return_type, "loc", getattr(fn, "loc", None)),
					diagnostics=diagnostics,
				)
			for p in getattr(fn, "params", []) or []:
				if _typeexpr_uses_internal_fnresult(p.type_expr):
					_report_internal_fnresult_in_surface_type(
						kind="parameter",
						symbol=f"{symbol_name}({p.name})",
						loc=getattr(p.type_expr, "loc", getattr(p, "loc", None)),
						diagnostics=diagnostics,
					)
			if fn.name in seen:
				diagnostics.append(
					Diagnostic(
						message=f"method '{fn.name}' conflicts with existing free function of the same name",
						severity="error",
						span=Span.from_loc(getattr(fn, "loc", None)),
					)
				)
				continue
			key = (target_str, fn.name)
			if key in method_keys:
				diagnostics.append(
					Diagnostic(
						message=f"duplicate method definition '{fn.name}' for type '{target_str}'",
						severity="error",
						span=Span.from_loc(getattr(fn, "loc", None)),
					)
				)
				continue
			method_keys.add(key)
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
	# Thread exception schemas through the shared type table for downstream validators.
	type_table.exception_schemas = exception_schemas
	return func_hirs, signatures, type_table, exception_catalog, diagnostics


__all__ = ["parse_drift_to_hir"]
