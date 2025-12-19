"""
lang2 parser copy (self-contained, no runtime dependency on lang/).
Parses Drift source and adapts to lang2.driftc.stage0 AST + FnSignatures for the
lang2 pipeline.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
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
from lang2.driftc.core.types_core import TypeKind
from lang2.driftc.core.event_codes import event_code, PAYLOAD_MASK
from lang2.driftc.core.types_core import (
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang2.driftc.core.type_resolve_common import resolve_opaque_type
from lang2.driftc.core.generic_type_expr import GenericTypeExpr


def _validate_module_id(mid: str, *, span: Span) -> list[Diagnostic]:
	"""
	Validate a module id per the language spec (format + reserved prefixes).

	This is shared by:
	- single-module builds (`parse_drift_files_to_hir`), and
	- workspace builds (`parse_drift_workspace_to_hir`), including inferred ids
	  from `-M/--module-path`.
	"""
	if not isinstance(mid, str) or not mid:
		return [
			Diagnostic(
				message="invalid module id (empty)",
				severity="error",
				span=span,
			)
		]
	raw_len = len(mid.encode("utf-8"))
	if raw_len > 254:
		return [
			Diagnostic(
				message=f"invalid module id '{mid}': length {raw_len} exceeds 254 UTF-8 bytes",
				severity="error",
				span=span,
			)
		]
	# Reserved module namespaces. Only the dotted namespace prefixes are reserved
	# (e.g. `std.foo`), not the bare segment itself (e.g. `lib` is allowed).
	forbidden_prefixes = ("lang", "abi", "std", "core", "lib")
	for pfx in forbidden_prefixes:
		if mid.startswith(pfx + "."):
			return [
				Diagnostic(
					message=f"invalid module id '{mid}': reserved prefix '{pfx}' is not allowed",
					severity="error",
					span=span,
				)
			]
	if mid.startswith(".") or mid.endswith(".") or ".." in mid:
		return [
			Diagnostic(
				message=f"invalid module id '{mid}': dots must separate non-empty segments",
				severity="error",
				span=span,
			)
		]
	if mid.startswith("_") or mid.endswith("_") or "__" in mid:
		return [
			Diagnostic(
				message=f"invalid module id '{mid}': underscores must not be leading/trailing or consecutive",
				severity="error",
				span=span,
			)
		]
	segments = mid.split(".")
	for seg in segments:
		if not seg:
			return [
				Diagnostic(
					message=f"invalid module id '{mid}': empty segment",
					severity="error",
					span=span,
				)
			]
		if seg.startswith("_") or seg.endswith("_") or "__" in seg:
			return [
				Diagnostic(
					message=f"invalid module id '{mid}': segment '{seg}' has invalid underscore placement",
					severity="error",
					span=span,
				)
			]
		# MVP: segments must start with a lowercase letter to avoid ambiguous module
		# names and to keep directory→module inference predictable.
		if not ("a" <= seg[0] <= "z"):
			return [
				Diagnostic(
					message=f"invalid module id '{mid}': segment '{seg}' must start with a lowercase letter",
					severity="error",
					span=span,
				)
			]
		for ch in seg:
			if not (("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "_"):
				return [
					Diagnostic(
						message=f"invalid module id '{mid}': segment '{seg}' contains invalid character '{ch}'",
						severity="error",
						span=span,
					)
				]
	return []


def _format_span_short(span: Span) -> str:
	"""
	Format a span as `file:line:column` for use in `Diagnostic.notes`.

	Notes are currently plain strings (no secondary-span support), so we keep the
	format stable and human-oriented.
	"""
	f = span.file or "<unknown>"
	l = span.line if span.line is not None else "?"
	c = span.column if span.column is not None else "?"
	return f"{f}:{l}:{c}"


def _prime_builtins(table: TypeTable) -> None:
	"""
	Ensure builtin TypeIds exist and are seeded in a stable order.

	This is required for package embedding in Milestone 4: until TypeId remapping
	exists, independently-produced artifacts must agree on builtin ids.
	"""
	table.ensure_unknown()
	table.ensure_int()
	table.ensure_uint()
	table.ensure_bool()
	table.ensure_float()
	table.ensure_string()
	table.ensure_void()
	table.ensure_error()
	table.ensure_diagnostic_value()
	# Seed commonly used derived types so TypeIds are stable across builds.
	#
	# MVP: DV accessors return Optional<Int/Bool/String>, so we ensure those
	# instantiations exist even if a particular module doesn't use them directly.
	table.new_optional(table.ensure_int())
	table.new_optional(table.ensure_bool())
	table.new_optional(table.ensure_string())


def _type_expr_to_str(typ: parser_ast.TypeExpr) -> str:
	"""Render a TypeExpr into a string (e.g., Array<Int>, Result<Int, Error>)."""
	if not typ.args:
		return typ.name
	args = ", ".join(_type_expr_to_str(a) for a in typ.args)
	return f"{typ.name}<{args}>"


def _generic_type_expr_from_parser(
	typ: parser_ast.TypeExpr,
	*,
	type_params: list[str],
) -> GenericTypeExpr:
	"""
	Convert a parser `TypeExpr` into a generic-aware core `GenericTypeExpr`.

	This is used for schema-bearing declarations (variants) where field types may
	refer to generic parameters (e.g. `Some(value: T)`).
	"""
	if typ.name in type_params and not typ.args:
		return GenericTypeExpr.param(type_params.index(typ.name))
	return GenericTypeExpr.named(
		typ.name,
		[_generic_type_expr_from_parser(a, type_params=type_params) for a in getattr(typ, "args", [])],
		module_id=getattr(typ, "module_id", None),
	)


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
		# Member-through-reference access (`p->field`) is normalized at the
		# parser→stage0 boundary by inserting an explicit deref.
		#
		# This keeps stage0/stage1 ASTs simple: later phases only need normal
		# member access plus unary deref (`*p`).
		base = _convert_expr(expr.value)
		if getattr(expr, "op", ".") == "->":
			base = s0.Unary(op="*", operand=base, loc=Span.from_loc(getattr(expr.value, "loc", None)))
		return s0.Attr(value=base, attr=expr.attr, loc=Span.from_loc(getattr(expr, "loc", None)))
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
		return s0.Move(value=_convert_expr(expr.value), loc=Span.from_loc(getattr(expr, "loc", None)))
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
	if isinstance(expr, parser_ast.MatchExpr):
		arms = [
			s0.MatchArm(
				ctor=arm.ctor,
				binders=list(arm.binders),
				block=_convert_block(arm.block),
				loc=Span.from_loc(getattr(arm, "loc", None)),
			)
			for arm in expr.arms
		]
		return s0.MatchExpr(
			scrutinee=_convert_expr(expr.scrutinee),
			arms=arms,
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


def _convert_aug_assign(stmt: "parser_ast.AugAssignStmt") -> s0.Stmt:
	"""
	Convert an augmented assignment statement.

	MVP supports:
	`+=`, `-=`, `*=`, `/=`, `%=`, `&=`, `|=`, `^=`, `<<=`, `>>=`.

	We preserve this as a distinct stage0 statement so later lowering can
	implement correct read-modify-write semantics for complex lvalues.
	"""
	return s0.AugAssignStmt(
		target=_convert_expr(stmt.target),
		op=str(getattr(stmt, "op", "+=")),
		value=_convert_expr(stmt.value),
		loc=Span.from_loc(stmt.loc),
	)

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
	parser_ast.AugAssignStmt: _convert_aug_assign,
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


def _span_in_file(path: Path, loc: object | None) -> Span:
	"""
	Construct a Span that is anchored to a specific source file.

	The parser AST location objects do not carry a filename; for multi-file module
	builds we need the file to be explicit so diagnostics can point at the right
	origin.
	"""
	if loc is None:
		return Span(file=str(path))
	span = Span.from_loc(loc)
	if span.file is None:
		return Span(
			file=str(path),
			line=span.line,
			column=span.column,
			end_line=span.end_line,
			end_column=span.end_column,
			raw=span.raw,
		)
	return span


def _diag_duplicate(
	*,
	kind: str,
	name: str,
	first_path: Path,
	first_loc: object | None,
	second_path: Path,
	second_loc: object | None,
) -> list[Diagnostic]:
	"""
	Build a primary error + secondary note diagnostic for a cross-file duplicate.

	The error is pinned to the second definition; the note is pinned to the first.
	"""
	first_span = _span_in_file(first_path, first_loc)
	second_span = _span_in_file(second_path, second_loc)
	return [
		Diagnostic(
			message=f"duplicate {kind} definition for '{name}'",
			severity="error",
			span=second_span,
		),
		Diagnostic(
			message=f"previous definition of '{name}' is here",
			severity="note",
			span=first_span,
		),
	]


def parse_drift_files_to_hir(
	paths: list[Path],
) -> Tuple[Dict[str, H.HBlock], Dict[str, FnSignature], "TypeTable", Dict[str, int], List[Diagnostic]]:
	"""
	Parse and lower a set of Drift source files into a single module unit.

	MVP (Milestone 1): accepts multiple files that all declare the same `module`
	id (or default to `main`). The module is lowered as if it were one merged file:
	- top-level declarations are combined,
	- cross-file collisions are diagnosed (with a pinned note pointing at the
	  first definition),
	- then the existing parser→stage0→HIR pipeline runs on the merged program.

	This does not implement cross-module imports yet; it only handles multiple
	files *within* the same module.
	"""
	diagnostics: list[Diagnostic] = []
	if not paths:
		return {}, {}, TypeTable(), {}, [Diagnostic(message="no input files", severity="error")]

	programs: list[tuple[Path, parser_ast.Program]] = []
	for path in paths:
		source = path.read_text()
		try:
			prog = _parser.parse_program(source)
		except _parser.ModuleDeclError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.FStringParseError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except UnexpectedInput as err:
			span = Span(
				file=str(path),
				line=getattr(err, "line", None),
				column=getattr(err, "column", None),
				raw=err,
			)
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=span))
			continue
		programs.append((path, prog))

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, TypeTable(), {}, {}, diagnostics

	# Enforce single-module membership across the file set.
	def _effective_module_id(p: parser_ast.Program) -> str:
		return getattr(p, "module", None) or "main"

	module_id = _effective_module_id(programs[0][1])
	for path, prog in programs:
		mid = _effective_module_id(prog)
		decl_span = _span_in_file(path, getattr(prog, "module_loc", None))
		diagnostics.extend(_validate_module_id(mid, span=decl_span))
	for path, prog in programs[1:]:
		mid = _effective_module_id(prog)
		if mid != module_id:
			diagnostics.append(
				Diagnostic(
					message=f"module id mismatch: expected '{module_id}', found '{mid}'",
					severity="error",
					span=Span(file=str(path), line=1, column=1),
				)
			)
	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, TypeTable(), {}, {}, diagnostics

	merged, _origins = _merge_module_files(module_id, programs, diagnostics)

	# Lower the merged program using the existing single-file pipeline.
	return _lower_parsed_program_to_hir(merged, diagnostics=diagnostics)


def _merge_module_files(
	module_id: str,
	files: list[tuple[Path, parser_ast.Program]],
	diagnostics: list[Diagnostic],
) -> tuple[parser_ast.Program, dict[str, Path]]:
	"""
	Merge a module's file set into a single parser AST `Program` (Milestone 1 rule set).

	This is the single source of truth for “multi-file module” merge behavior.
	Both `parse_drift_files_to_hir` (single-module build) and the workspace loader
	(Milestone 2) must call this helper to avoid drift.
	"""
	merged = parser_ast.Program(module=module_id)
	# Provenance map for module-local callable symbols (free functions and methods).
	#
	# Used by the workspace loader to implement per-file import environments:
	# we need to know which source file a given function body came from so we can
	# apply that file's imports while rewriting call sites.
	origin_by_symbol: dict[str, Path] = {}

	first_fn: dict[str, tuple[Path, object | None]] = {}
	for path, prog in files:
		for fn in getattr(prog, "functions", []) or []:
			if fn.name in first_fn:
				first_path, first_loc = first_fn[fn.name]
				diagnostics.extend(
					_diag_duplicate(
						kind="function",
						name=fn.name,
						first_path=first_path,
						first_loc=first_loc,
						second_path=path,
						second_loc=getattr(fn, "loc", None),
					)
				)
				continue
			first_fn[fn.name] = (path, getattr(fn, "loc", None))
			merged.functions.append(fn)
			origin_by_symbol.setdefault(fn.name, path)

	first_struct: dict[str, tuple[Path, object | None]] = {}
	for path, prog in files:
		for s in getattr(prog, "structs", []) or []:
			if s.name in first_struct:
				first_path, first_loc = first_struct[s.name]
				diagnostics.extend(
					_diag_duplicate(
						kind="struct",
						name=s.name,
						first_path=first_path,
						first_loc=first_loc,
						second_path=path,
						second_loc=getattr(s, "loc", None),
					)
				)
				continue
			first_struct[s.name] = (path, getattr(s, "loc", None))
			merged.structs.append(s)

	first_exc: dict[str, tuple[Path, object | None]] = {}
	for path, prog in files:
		for exc in getattr(prog, "exceptions", []) or []:
			if exc.name in first_exc:
				first_path, first_loc = first_exc[exc.name]
				diagnostics.extend(
					_diag_duplicate(
						kind="exception",
						name=exc.name,
						first_path=first_path,
						first_loc=first_loc,
						second_path=path,
						second_loc=getattr(exc, "loc", None),
					)
				)
				continue
			first_exc[exc.name] = (path, getattr(exc, "loc", None))
			merged.exceptions.append(exc)

	first_variant: dict[str, tuple[Path, object | None]] = {}
	for path, prog in files:
		for v in getattr(prog, "variants", []) or []:
			if v.name in first_variant:
				first_path, first_loc = first_variant[v.name]
				diagnostics.extend(
					_diag_duplicate(
						kind="variant",
						name=v.name,
						first_path=first_path,
						first_loc=first_loc,
						second_path=path,
						second_loc=getattr(v, "loc", None),
					)
				)
				continue
			first_variant[v.name] = (path, getattr(v, "loc", None))
			merged.variants.append(v)

	# Combine module directives (imports/exports).
	for _, prog in files:
		merged.imports.extend(getattr(prog, "imports", []) or [])
		merged.from_imports.extend(getattr(prog, "from_imports", []) or [])
		merged.exports.extend(getattr(prog, "exports", []) or [])

	# Merge implement blocks by target repr and de-duplicate methods.
	free_names = set(first_fn.keys())
	impls_by_target: dict[str, parser_ast.ImplementDef] = {}
	first_method: dict[tuple[str, str], tuple[Path, object | None]] = {}
	for path, prog in files:
		for impl in getattr(prog, "implements", []) or []:
			target_str = _type_expr_to_str(impl.target)
			dst = impls_by_target.get(target_str)
			if dst is None:
				dst = parser_ast.ImplementDef(target=impl.target, loc=getattr(impl, "loc", None), methods=[])
				impls_by_target[target_str] = dst
			for m in getattr(impl, "methods", []) or []:
				if m.name in free_names:
					first_path, _first_loc = first_fn[m.name]
					diagnostics.append(
						Diagnostic(
							message=f"method '{m.name}' conflicts with existing free function of the same name",
							severity="error",
							span=_span_in_file(path, getattr(m, "loc", None)),
							notes=[f"previous free function definition is in {first_path}"],
						)
					)
					continue
				key = (target_str, m.name)
				if key in first_method:
					first_path, first_loc = first_method[key]
					diagnostics.extend(
						_diag_duplicate(
							kind=f"method for type '{target_str}'",
							name=m.name,
							first_path=first_path,
							first_loc=first_loc,
							second_path=path,
							second_loc=getattr(m, "loc", None),
						)
					)
					continue
				first_method[key] = (path, getattr(m, "loc", None))
				dst.methods.append(m)
				origin_by_symbol.setdefault(f"{target_str}::{m.name}", path)
	merged.implements = list(impls_by_target.values())
	return merged, origin_by_symbol


def parse_drift_workspace_to_hir(
	paths: list[Path],
	*,
	module_paths: list[Path] | None = None,
	external_module_exports: dict[str, dict[str, set[str]]] | None = None,
) -> Tuple[
	Dict[str, H.HBlock],
	Dict[str, FnSignature],
	"TypeTable",
	Dict[str, int],
	Dict[str, Dict[str, List[str]]],
	List[Diagnostic],
]:
	"""
	Parse and lower a set of Drift source files that may belong to multiple modules.

	This is Milestone 2 (“module imports and cross-module resolution”) scaffolding:
	- input is an unordered set of files (typically all `*.drift` files in a build),
	- files are grouped by their declared `module <id>` (or default to `main`),
	- each module is merged from its file set (Milestone 1 behavior),
	- imports are resolved across modules (MVP: `from <module> import <symbol>`),
	- resulting HIR/signatures are returned as a single program unit suitable for
	  the existing HIR→MIR→SSA→LLVM pipeline.

		Important MVP constraints (pinned for clarity):
		- Imports are treated as **per-file** bindings:
		  - Duplicate identical imports in one file are idempotent (“no-op after first”).
		  - Conflicting aliases/bindings in one file are diagnosed as errors.
		  - Different files may import the same module/symbol freely; the module is still
		    parsed/merged/compiled once per build and referenced from all import sites.
		- Module-qualified access (`import m` then `m.foo()`) is parsed but not relied
		  upon yet; MVP import resolution focuses on `from m import foo`.
	- Cross-module name resolution is implemented for free functions (not types)
	  in MVP. Type-level imports are deferred (tests stay skipped).

	Returns:
	  (func_hirs, signatures, type_table, exception_catalog, module_exports, diagnostics)
	"""
	diagnostics: list[Diagnostic] = []
	if not paths:
		return {}, {}, TypeTable(), {}, {}, [Diagnostic(message="no input files", severity="error")]

	def _effective_module_id(p: parser_ast.Program) -> str:
		return getattr(p, "module", None) or "main"

	# Parse all files first.
	parsed: list[tuple[Path, parser_ast.Program]] = []
	for path in paths:
		source = path.read_text()
		try:
			prog = _parser.parse_program(source)
		except _parser.ModuleDeclError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.FStringParseError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except UnexpectedInput as err:
			span = Span(
				file=str(path),
				line=getattr(err, "line", None),
				column=getattr(err, "column", None),
				raw=err,
			)
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=span))
			continue
		parsed.append((path, prog))

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, TypeTable(), {}, {}, diagnostics

	def _infer_module_id_from_paths(path: Path) -> tuple[str, Path] | tuple[None, None]:
		"""
		Infer the module id for a file from the configured module roots.

		Rule (MVP, pinned in work-progress):
		- find the first module root that is a prefix of the file's absolute path,
		- module id is derived from the directory relative to the root:
		  - empty relative path => "main"
		  - otherwise path segments joined by '.' (platform-independent).
		"""
		if not module_paths:
			return None, None
		abs_path = path.resolve()
		candidates: list[tuple[Path, Path]] = []
		for root in module_paths:
			abs_root = root.resolve()
			try:
				rel_dir = abs_path.parent.relative_to(abs_root)
			except ValueError:
				continue
			candidates.append((abs_root, rel_dir))

		if not candidates:
			return None, None

		# Deterministic root selection: pick the most-specific root (longest prefix).
		# This prevents module identity from depending on CLI flag ordering when roots
		# overlap (e.g. `-M src` and `-M src/vendor`).
		candidates.sort(key=lambda r: len(r[0].parts), reverse=True)
		best_len = len(candidates[0][0].parts)
		best = [c for c in candidates if len(c[0].parts) == best_len]
		if len(best) != 1:
			# Ambiguous configuration: multiple roots at the same specificity match
			# the same file.
			return None, None

		abs_root, rel_dir = best[0]
		parts = list(rel_dir.parts)
		if not parts or parts == ["."]:
			return "main", abs_root
		for seg in parts:
			if seg in {".", ".."}:
				return None, None
			if not seg:
				return None, None
		return ".".join(parts), abs_root

	# Group by module id (declared or inferred).
	by_module: dict[str, list[tuple[Path, parser_ast.Program]]] = {}
	roots_by_module: dict[str, set[Path]] = {}
	# For pinned diagnostics, keep at least one representative file per (module, root).
	root_file_by_module: dict[str, dict[Path, Path]] = {}
	for path, prog in parsed:
		if module_paths:
			inferred, root = _infer_module_id_from_paths(path)
			if inferred is None or root is None:
				diagnostics.append(
					Diagnostic(
						message=f"file '{path}' is not under exactly one configured module root",
						severity="error",
						span=Span(file=str(path), line=1, column=1),
					)
				)
				continue
			# Validate inferred id before using it as a module-graph key.
			diagnostics.extend(_validate_module_id(inferred, span=Span(file=str(path), line=1, column=1)))
			declared = getattr(prog, "module", None)
			if declared is not None:
				decl_span = _span_in_file(path, getattr(prog, "module_loc", None))
				diagnostics.extend(_validate_module_id(declared, span=decl_span))
				if any(d.severity == "error" for d in diagnostics):
					continue
				if declared != inferred:
					notes = [f"inferred module id is '{inferred}' from root '{root}'"]
					diagnostics.append(
						Diagnostic(
							message=f"module id mismatch: expected '{inferred}', found '{declared}'",
							severity="error",
							span=decl_span,
							notes=notes,
						)
					)
					continue
			# Treat missing module header as implicit declaration of the inferred id.
			if declared is None:
				prog = replace(prog, module=inferred)
			by_module.setdefault(inferred, []).append((path, prog))
			roots_by_module.setdefault(inferred, set()).add(root)
			root_file_by_module.setdefault(inferred, {}).setdefault(root, path)
		else:
			mid = _effective_module_id(prog)
			decl_span = _span_in_file(path, getattr(prog, "module_loc", None))
			diagnostics.extend(_validate_module_id(mid, span=decl_span))
			by_module.setdefault(mid, []).append((path, prog))

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, TypeTable(), {}, {}, diagnostics

	# When module roots are used, reject ambiguous module ids coming from
	# multiple roots (prevents accidental shadowing/selection by search order).
	if module_paths:
		for mid, roots in roots_by_module.items():
			if len(roots) > 1:
				root_list = ", ".join(str(r) for r in sorted(roots))
				span_file = None
				# Anchor the diagnostic to a concrete file under one of the roots.
				for r in sorted(roots):
					span_file = root_file_by_module.get(mid, {}).get(r)
					if span_file is not None:
						break
				span = Span(file=str(span_file), line=1, column=1) if span_file else Span()
				diagnostics.append(
					Diagnostic(
						message=f"multiple module roots provide module '{mid}' ({root_list})",
						severity="error",
						span=span,
					)
				)
	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, TypeTable(), {}, {}, diagnostics
	# Merge each module (Milestone 1 rules) and retain callable provenance per file.
	merged_programs: dict[str, parser_ast.Program] = {}
	origin_by_module: dict[str, dict[str, Path]] = {}
	for mid, files in by_module.items():
		merged, origins = _merge_module_files(mid, files, diagnostics)
		merged_programs[mid] = merged
		origin_by_module[mid] = origins

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, TypeTable(), {}, {}, diagnostics

	def _build_export_interface(
		*,
		module_id: str,
		merged_prog: parser_ast.Program,
		module_files: list[tuple[Path, parser_ast.Program]],
	) -> tuple[dict[str, tuple[str, str]], set[str]]:
		"""
		Build the exported interface for a module.

		MVP visibility model:
		- items are private by default,
		- `export { Name, ... }` lists the exported names,
		- both values and types may be exported, but in separate namespaces:
		  - values: free functions
		  - types: structs, variants, exceptions

		Because `export { ... }` syntax is unqualified, exporting a name that exists
		in both namespaces is ambiguous. Until we add explicit qualifiers, that is
		a compile-time error.

		Spans are anchored to the source file that contained the `export { ... }`
		statement so diagnostics remain useful in multi-file modules.
		"""
		module_fn_names: set[str] = {fn.name for fn in getattr(merged_prog, "functions", []) or []}
		module_type_names: set[str] = {
			*(s.name for s in getattr(merged_prog, "structs", []) or []),
			*(v.name for v in getattr(merged_prog, "variants", []) or []),
			*(e.name for e in getattr(merged_prog, "exceptions", []) or []),
		}

		raw_export_entries: list[tuple[str, Span]] = []
		for path, parsed_prog in module_files:
			for ex in getattr(parsed_prog, "exports", []) or []:
				ex_span = _span_in_file(path, getattr(ex, "loc", None))
				for n in getattr(ex, "names", []) or []:
					raw_export_entries.append((n, ex_span))

		# MVP rule: exporting the same name multiple times within a module is a
		# deterministic user error (even if it would be a no-op). We treat it as a
		# duplicate declaration so the module interface remains crisp and tooling
		# never has to guess which export site is authoritative.
		seen_export_names: dict[str, Span] = {}

		# Exported values map exported local name -> underlying (module_id, symbol).
		#
		# This enables a minimal re-export mechanism without introducing a new
		# surface syntax yet:
		#
		#   from other.module import foo
		#   export { foo }
		#
		# Export entries always name symbols in the *current* module interface
		# (e.g., `a::foo`). When `foo` is re-exported (implemented by another module),
		# the workspace loader materializes a trampoline function `a::foo` that
		# forwards to the underlying target `other.module::foo`.
		exported_values: dict[str, tuple[str, str]] = {}
		exported_types: set[str] = set()

		# Collect re-export candidates from `from <module> import <symbol> [as alias]`
		# statements across files in the module. Imports are per-file, but exports
		# are module-level; to keep export resolution deterministic we require that
		# any *exported* imported binding be unambiguous across the module.
		reexport_candidates: dict[str, tuple[str, str]] = {}
		reexport_first_span: dict[str, Span] = {}
		reexport_conflicts: dict[str, tuple[tuple[str, str], tuple[str, str], Span, Span]] = {}
		for path, parsed_prog in module_files:
			for imp in getattr(parsed_prog, "from_imports", []) or []:
				local_name = getattr(imp, "alias", None) or getattr(imp, "symbol", "")
				mod = ".".join(getattr(imp, "module_path", []) or [])
				sym = getattr(imp, "symbol", "")
				if not local_name or not mod or not sym:
					continue
				target = (mod, sym)
				imp_span = _span_in_file(path, getattr(imp, "loc", None))
				prev = reexport_candidates.get(local_name)
				if prev is None:
					reexport_candidates[local_name] = target
					reexport_first_span[local_name] = imp_span
				elif prev != target:
					if local_name not in reexport_conflicts:
						reexport_conflicts[local_name] = (
							prev,
							target,
							reexport_first_span.get(local_name, Span()),
							imp_span,
						)

		for n, ex_span in raw_export_entries:
			first_span = seen_export_names.get(n)
			if first_span is None:
				seen_export_names[n] = ex_span
			else:
				diagnostics.append(
					Diagnostic(
						message=f"duplicate export of symbol '{n}' in module '{module_id}'",
						severity="error",
						span=ex_span,
						notes=[f"first export was here: {_format_span_short(first_span)}"],
					)
				)
				continue
			in_values = n in module_fn_names
			in_types = n in module_type_names
			if in_values and in_types:
				diagnostics.append(
					Diagnostic(
						message=f"exported name '{n}' is ambiguous (defined as both a value and a type in module '{module_id}')",
						severity="error",
						span=ex_span,
					)
				)
				continue
			if not in_values and not in_types:
				# Allow exporting an imported value (re-export) when it is present
				# unambiguously in the module's `from import` declarations.
				conflict = reexport_conflicts.get(n)
				if conflict is not None:
					prev, new, first_span, second_span = conflict
					diagnostics.append(
						Diagnostic(
							message=(
								f"exported name '{n}' is ambiguous due to conflicting imports "
								f"({prev[0]}::{prev[1]} vs {new[0]}::{new[1]}); cannot export it"
							),
							severity="error",
							span=ex_span,
							notes=[
								f"first import was here: {first_span}",
								f"conflicting import was here: {second_span}",
							],
						)
					)
					continue
				target = reexport_candidates.get(n)
				if target is not None:
					exported_values[n] = target
					continue
				diagnostics.append(
					Diagnostic(
						message=f"module '{module_id}' exports unknown symbol '{n}'",
						severity="error",
						span=ex_span,
					)
				)
				continue
			if in_values:
				exported_values[n] = (module_id, n)
			if in_types:
				exported_types.add(n)
		return exported_values, exported_types

	# Note: module-scoped nominal type identity is implemented in lang2.
	# Multiple modules may define types with the same short name without
	# colliding; identity is `(module_id, name, kind)`.

	# Export sets (private by default, explicit exports required).
	#
	# MVP supports exporting/importing both value-level and type-level symbols,
	# but keeps them in separate namespaces:
	# - values: currently just free functions
	# - types: structs, variants, exceptions
	#
	# Export lists are unqualified identifiers, so to avoid ambiguity we reject
	# any module that defines the same name in both namespaces (until the language
	# adds explicit `export type ...` / `export fn ...` syntax).
	exports_values_by_module: dict[str, dict[str, tuple[str, str]]] = {}
	exports_types_by_module: dict[str, set[str]] = {}
	for mid, prog in merged_programs.items():
		exported_values, exported_types = _build_export_interface(
			module_id=mid,
			merged_prog=prog,
			module_files=by_module.get(mid, []),
		)
		exports_values_by_module[mid] = exported_values
		exports_types_by_module[mid] = exported_types

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, TypeTable(), {}, {}, diagnostics

	# Export interface summary (used by package emission and future tooling).
	module_exports: dict[str, dict[str, list[str]]] = {}
	for mid in merged_programs.keys():
		vals = exports_values_by_module.get(mid, {})
		types = exports_types_by_module.get(mid, set())
		module_exports[mid] = {
			"values": sorted(list(vals.keys())),
			"types": sorted(list(types)),
		}

	# Resolve imports and build a dependency graph.
	#
	# MVP rule: import bindings are per-file. Module dependencies, however, are
	# computed at module granularity (a module depends on another module if any
	# of its files import it).
	#
	# Keep per-edge provenance so cycle diagnostics can be source-anchored.
	# Each edge is (to_module, span).
	dep_edges: dict[str, list[tuple[str, Span]]] = {mid: [] for mid in merged_programs}
	from_value_bindings_by_file: dict[Path, dict[str, tuple[str, str]]] = {}
	from_type_bindings_by_file: dict[Path, dict[str, tuple[str, str]]] = {}
	module_aliases_by_file: dict[Path, dict[str, str]] = {}
	for mid, files in by_module.items():
		for path, prog in files:
			file_seen_values: dict[str, tuple[str, str]] = {}
			file_seen_types: dict[str, tuple[str, str]] = {}
			file_value_bindings: dict[str, tuple[str, str]] = {}
			file_type_bindings: dict[str, tuple[str, str]] = {}
			# Track first import site per local binding name so conflict diagnostics
			# can point at both the import and the local declaration.
			file_value_binding_span: dict[str, Span] = {}
			file_type_binding_span: dict[str, Span] = {}
			file_module_aliases: dict[str, str] = {}
			# MVP rule: `from m import name` introduces a file-scoped binding in the
			# same namespace as top-level declarations in that file. Conflicts are
			# errors regardless of order (imports are treated as a logical header).
			top_level_fn_by_name: dict[str, object] = {fn.name: fn for fn in getattr(prog, "functions", []) or []}
			top_level_type_by_name: dict[str, object] = {}
			for s in getattr(prog, "structs", []) or []:
				top_level_type_by_name[s.name] = s
			for v in getattr(prog, "variants", []) or []:
				top_level_type_by_name[v.name] = v
			for e in getattr(prog, "exceptions", []) or []:
				top_level_type_by_name[e.name] = e

			for imp in getattr(prog, "imports", []) or []:
				mod = ".".join(getattr(imp, "path", []) or [])
				if not mod:
					continue
				span = _span_in_file(path, getattr(imp, "loc", None))
				dep_edges[mid].append((mod, span))
				if mod not in merged_programs and (external_module_exports is None or mod not in external_module_exports):
					diagnostics.append(Diagnostic(message=f"imported module '{mod}' not found", severity="error", span=span))
					continue
				alias = getattr(imp, "alias", None) or (getattr(imp, "path", []) or [mod])[-1]
				prev = file_module_aliases.get(alias)
				if prev is None:
					file_module_aliases[alias] = mod
				elif prev != mod:
					diagnostics.append(
						Diagnostic(
							message=f"import alias '{alias}' conflicts: cannot import both '{prev}' and '{mod}' as '{alias}'",
							severity="error",
							span=span,
						)
					)

			# Record module aliases for later module-qualified access resolution.
			# This is per-file by design (imports are file-scoped in MVP).
			module_aliases_by_file[path] = dict(file_module_aliases)

			for fi in getattr(prog, "from_imports", []) or []:
				mod = ".".join(getattr(fi, "module_path", []) or [])
				sym = getattr(fi, "symbol", "")
				if not mod or not sym:
					continue
				span = _span_in_file(path, getattr(fi, "loc", None))
				dep_edges[mid].append((mod, span))
				if mod not in merged_programs and (external_module_exports is None or mod not in external_module_exports):
					diagnostics.append(Diagnostic(message=f"imported module '{mod}' not found", severity="error", span=span))
					continue

				exported_values_map = exports_values_by_module.get(mod, {})
				exported_types_set = exports_types_by_module.get(mod, set())
				if mod not in merged_programs and external_module_exports is not None and mod in external_module_exports:
					ext = external_module_exports.get(mod) or {}
					exported_values_map = {n: (mod, n) for n in sorted(ext.get("values") or set())}
					exported_types_set = set(ext.get("types") or set())
				available_exports = sorted(set(exported_values_map.keys()) | exported_types_set)
				is_glob = bool(getattr(fi, "is_glob", False))
				if is_glob:
					if getattr(fi, "alias", None) is not None:
						diagnostics.append(
							Diagnostic(
								message="from-import glob does not support aliasing",
								severity="error",
								span=span,
							)
						)
						continue
					# Expand deterministically: values then types, each sorted.
					glob_values = sorted(exported_values_map.keys())
					glob_types = sorted(exported_types_set)
					for v in glob_values:
						local_name = v
						target = exported_values_map.get(v, (mod, v))
						prev = file_seen_values.get(local_name)
						if prev is None:
							file_seen_values[local_name] = target
							file_value_binding_span[local_name] = span
							file_value_bindings[local_name] = target
						elif prev != target:
							prev_span = file_value_binding_span.get(local_name, Span())
							diagnostics.append(
								Diagnostic(
									message=f"ambiguous imported name '{local_name}': both '{prev[0]}::{prev[1]}' and '{target[0]}::{target[1]}' are in scope",
									severity="error",
									span=span,
									notes=[f"previous import was here: {_format_span_short(prev_span)}"],
								)
							)
					for t in glob_types:
						local_name = t
						target = (mod, t)
						prev = file_seen_types.get(local_name)
						if prev is None:
							file_seen_types[local_name] = target
							file_type_binding_span[local_name] = span
							file_type_bindings[local_name] = target
						elif prev != target:
							prev_span = file_type_binding_span.get(local_name, Span())
							diagnostics.append(
								Diagnostic(
									message=f"ambiguous imported type name '{local_name}': both '{prev[0]}::{prev[1]}' and '{target[0]}::{target[1]}' are in scope",
									severity="error",
									span=span,
									notes=[f"previous import was here: {_format_span_short(prev_span)}"],
								)
							)
					continue

				is_value = sym in exported_values_map
				is_type = sym in exported_types_set
				if not is_value and not is_type:
					available = ", ".join(available_exports)
					notes = (
						[f"available exports: {available}"]
						if available
						else [f"module '{mod}' exports nothing (private by default)"]
					)
					diagnostics.append(
						Diagnostic(
							message=f"module '{mod}' does not export symbol '{sym}'",
							severity="error",
							span=span,
							notes=notes,
						)
					)
					continue

				local_name = getattr(fi, "alias", None) or sym
				if is_value:
					# Re-exported values are exported under the current module id but
					# implemented by another module/symbol. For MVP we materialize
					# re-exports as trampoline functions (`mod::sym`) so imports remain
					# stable and module interfaces are self-contained.
					raw_target = exported_values_map.get(sym, (mod, sym))
					target = (mod, sym) if raw_target != (mod, sym) else raw_target
					prev = file_seen_values.get(local_name)
					if prev is None:
						file_seen_values[local_name] = target
						file_value_binding_span[local_name] = span
					elif prev != target:
						diagnostics.append(
							Diagnostic(
								message=f"ambiguous imported name '{local_name}': both '{prev[0]}::{prev[1]}' and '{target[0]}::{target[1]}' are in scope",
								severity="error",
								span=span,
								notes=[f"previous import was here: {_format_span_short(file_value_binding_span.get(local_name, Span()))}"],
							)
						)
						continue
					file_value_bindings[local_name] = target

				if is_type:
					target = (mod, sym)
					prev = file_seen_types.get(local_name)
					if prev is None:
						file_seen_types[local_name] = target
						file_type_binding_span[local_name] = span
					elif prev != target:
						diagnostics.append(
							Diagnostic(
								message=f"ambiguous imported type name '{local_name}': both '{prev[0]}::{prev[1]}' and '{target[0]}::{target[1]}' are in scope",
								severity="error",
								span=span,
								notes=[f"previous import was here: {_format_span_short(file_type_binding_span.get(local_name, Span()))}"],
							)
						)
						continue
					file_type_bindings[local_name] = target

			from_value_bindings_by_file[path] = file_value_bindings
			from_type_bindings_by_file[path] = file_type_bindings

			# After collecting the effective import bindings, enforce the pinned
			# conflict rule with local top-level declarations.
			for local_name, (imp_mod, imp_sym) in file_value_bindings.items():
				local_decl = top_level_fn_by_name.get(local_name)
				if local_decl is None:
					continue
				decl_span = _span_in_file(path, getattr(local_decl, "loc", None))
				imp_span = file_value_binding_span.get(local_name, Span())
				diagnostics.append(
					Diagnostic(
						message=f"name '{local_name}' conflicts with imported binding from module '{imp_mod}'",
						severity="error",
						span=decl_span,
						notes=[
							f"imported as '{local_name}' from '{imp_mod}::{imp_sym}' at {getattr(imp_span, 'file', str(path))}:{getattr(imp_span, 'line', '?')}:{getattr(imp_span, 'column', '?')}",
						],
					)
				)
			for local_name, (imp_mod, imp_sym) in file_type_bindings.items():
				local_decl = top_level_type_by_name.get(local_name)
				if local_decl is None:
					continue
				decl_span = _span_in_file(path, getattr(local_decl, "loc", None))
				imp_span = file_type_binding_span.get(local_name, Span())
				diagnostics.append(
					Diagnostic(
						message=f"type name '{local_name}' conflicts with imported type from module '{imp_mod}'",
						severity="error",
						span=decl_span,
						notes=[
							f"imported as '{local_name}' from '{imp_mod}::{imp_sym}' at {getattr(imp_span, 'file', str(path))}:{getattr(imp_span, 'line', '?')}:{getattr(imp_span, 'column', '?')}",
						],
					)
				)

	# Collapse edge lists into a simple adjacency set for cycle detection.
	deps: dict[str, set[str]] = {mid: {to for (to, _sp) in edges if to in merged_programs} for mid, edges in dep_edges.items()}

	# Resolve module-qualified and imported type references using per-file import
	# bindings and module export interfaces.
	#
	# After successful resolution we record the canonical `module_id` on the type
	# expression (and rewrite imported aliases to their original symbol name). This
	# preserves module-scoped nominal identity end-to-end.
	def _exported_types_for_module(mod: str) -> set[str]:
		if mod in exports_types_by_module:
			return exports_types_by_module.get(mod, set())
		if external_module_exports is not None and mod in external_module_exports:
			ext = external_module_exports.get(mod) or {}
			return set(ext.get("types") or set())
		return set()

	def _resolve_type_expr_in_file(
		path: Path,
		file_aliases: dict[str, str],
		file_type_bindings: dict[str, tuple[str, str]],
		te: parser_ast.TypeExpr | None,
	) -> None:
		if te is None:
			return
		if getattr(te, "module_alias", None):
			alias = te.module_alias
			mod = file_aliases.get(alias or "")
			span = _span_in_file(path, getattr(te, "loc", None))
			if mod is None:
				diagnostics.append(
					Diagnostic(
						message=f"unknown module alias '{alias}' in type reference '{alias}.{te.name}'",
						severity="error",
						span=span,
					)
				)
			else:
				types = _exported_types_for_module(mod)
				if te.name not in types:
					available = ", ".join(sorted(types))
					notes = (
						[f"available exported types: {available}"]
						if available
						else [f"module '{mod}' exports no types (private by default)"]
					)
					diagnostics.append(
						Diagnostic(
							message=f"module '{mod}' does not export type '{te.name}'",
							severity="error",
							span=span,
							notes=notes,
						)
					)
				else:
					# Record the canonical module id for later lowering.
					te.module_id = mod
					te.module_alias = None
		# Unqualified imported type: `from m import Point` makes `Point` resolve to
		# the imported module, even when referenced without an `x.` qualifier.
		if getattr(te, "module_alias", None) is None and getattr(te, "module_id", None) is None:
			target = file_type_bindings.get(te.name)
			if target is not None:
				mod, sym = target
				te.module_id = mod
				te.name = sym
		for a in getattr(te, "args", []) or []:
			_resolve_type_expr_in_file(path, file_aliases, file_type_bindings, a)

	def _resolve_types_in_block(path: Path, file_aliases: dict[str, str], file_type_bindings: dict[str, tuple[str, str]], blk: parser_ast.Block) -> None:
		for st in getattr(blk, "statements", []) or []:
			if isinstance(st, parser_ast.LetStmt) and getattr(st, "type_expr", None) is not None:
				_resolve_type_expr_in_file(path, file_aliases, file_type_bindings, st.type_expr)
			if isinstance(st, parser_ast.IfStmt):
				_resolve_types_in_block(path, file_aliases, file_type_bindings, st.then_block)
				if st.else_block is not None:
					_resolve_types_in_block(path, file_aliases, file_type_bindings, st.else_block)
			if isinstance(st, parser_ast.TryStmt):
				_resolve_types_in_block(path, file_aliases, file_type_bindings, st.body)
				for c in getattr(st, "catches", []) or []:
					_resolve_types_in_block(path, file_aliases, file_type_bindings, c.block)
			if isinstance(st, parser_ast.WhileStmt):
				_resolve_types_in_block(path, file_aliases, file_type_bindings, st.body)
			if isinstance(st, parser_ast.ForStmt):
				_resolve_types_in_block(path, file_aliases, file_type_bindings, st.body)

	for mid, files in by_module.items():
		for path, prog in files:
			file_aliases = module_aliases_by_file.get(path, {})
			file_type_bindings = from_type_bindings_by_file.get(path, {})
			# Top-level declarations.
			for fn in getattr(prog, "functions", []) or []:
				for p in getattr(fn, "params", []) or []:
					_resolve_type_expr_in_file(path, file_aliases, file_type_bindings, p.type_expr)
				_resolve_type_expr_in_file(path, file_aliases, file_type_bindings, getattr(fn, "return_type", None))
				_resolve_types_in_block(path, file_aliases, file_type_bindings, fn.body)
			for impl in getattr(prog, "implements", []) or []:
				_resolve_type_expr_in_file(path, file_aliases, file_type_bindings, impl.target)
				for mfn in getattr(impl, "methods", []) or []:
					for p in getattr(mfn, "params", []) or []:
						_resolve_type_expr_in_file(path, file_aliases, file_type_bindings, p.type_expr)
					_resolve_type_expr_in_file(path, file_aliases, file_type_bindings, getattr(mfn, "return_type", None))
					_resolve_types_in_block(path, file_aliases, file_type_bindings, mfn.body)
			for s in getattr(prog, "structs", []) or []:
				for f in getattr(s, "fields", []) or []:
					_resolve_type_expr_in_file(path, file_aliases, file_type_bindings, f.type_expr)
			for e in getattr(prog, "exceptions", []) or []:
				for a in getattr(e, "args", []) or []:
					_resolve_type_expr_in_file(path, file_aliases, file_type_bindings, a.type_expr)
			for v in getattr(prog, "variants", []) or []:
				for arm in getattr(v, "arms", []) or []:
					for f in getattr(arm, "fields", []) or []:
						_resolve_type_expr_in_file(path, file_aliases, file_type_bindings, f.type_expr)

	# Cycle detection (MVP: reject import cycles).
	def _find_cycle() -> list[str] | None:
		vis: set[str] = set()
		stack: list[str] = []
		onstack: set[str] = set()

		def dfs(n: str) -> list[str] | None:
			vis.add(n)
			stack.append(n)
			onstack.add(n)
			for m in deps.get(n, set()):
				if m not in merged_programs:
					continue
				if m not in vis:
					c = dfs(m)
					if c is not None:
						return c
				elif m in onstack:
					try:
						i = stack.index(m)
					except ValueError:
						i = 0
					return stack[i:] + [m]
			stack.pop()
			onstack.remove(n)
			return None

		for n in merged_programs:
			if n not in vis:
				c = dfs(n)
				if c is not None:
					return c
		return None

	cycle = _find_cycle()
	if cycle is not None:
		# Anchor the diagnostic to one concrete import site in the cycle.
		# (We choose the first edge in the reported cycle.)
		primary_span: Span | None = None
		notes: list[str] = []
		for i in range(len(cycle) - 1):
			a = cycle[i]
			b = cycle[i + 1]
			for to, sp in dep_edges.get(a, []):
				if to == b:
					if i == 0:
						primary_span = sp
					notes.append(f"{a} imports {b}")
					break
		if primary_span is None:
			# Fallback: pick any import edge span from any node in the cycle.
			for node in cycle:
				for _to, sp in dep_edges.get(node, []):
					primary_span = sp
					break
				if primary_span is not None:
					break
		diagnostics.append(
			Diagnostic(
				message=f"import cycle detected: {' -> '.join(cycle)}",
				severity="error",
				span=primary_span or Span(),
				notes=notes,
			)
		)

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, TypeTable(), {}, {}, diagnostics

	# Lower modules using a shared TypeTable so TypeIds remain comparable across the workspace.
	shared_type_table = TypeTable()
	_prime_builtins(shared_type_table)
	# Pre-declare all nominal type names across the workspace before lowering any
	# individual module.
	#
	# This prevents cross-module type references (e.g. `import lib as x; val p: x.Point`)
	# from accidentally minting placeholder scalar TypeIds via `ensure_named` when
	# the defining module hasn't been lowered yet. `declare_struct`/`declare_variant`
	# are idempotent when the kind already matches, so later per-module lowering
	# can safely re-run its local declaration passes.
	for _mid, _prog in merged_programs.items():
		for _s in getattr(_prog, "structs", []) or []:
			try:
				shared_type_table.declare_struct(_mid, _s.name, [f.name for f in getattr(_s, "fields", []) or []])
			except ValueError as err:
				diagnostics.append(Diagnostic(message=str(err), severity="error", span=Span.from_loc(getattr(_s, "loc", None))))
		for _v in getattr(_prog, "variants", []) or []:
			arms: list[VariantArmSchema] = []
			for _arm in getattr(_v, "arms", []) or []:
				fields = [
					VariantFieldSchema(
						name=_f.name,
						type_expr=_generic_type_expr_from_parser(
							_f.type_expr, type_params=list(getattr(_v, "type_params", []) or [])
						),
					)
					for _f in getattr(_arm, "fields", []) or []
				]
				arms.append(VariantArmSchema(name=_arm.name, fields=fields))
			try:
				shared_type_table.declare_variant(
					_mid,
					_v.name,
					list(getattr(_v, "type_params", []) or []),
					arms,
				)
			except ValueError as err:
				diagnostics.append(Diagnostic(message=str(err), severity="error", span=Span.from_loc(getattr(_v, "loc", None))))

		if any(d.severity == "error" for d in diagnostics):
			return {}, {}, TypeTable(), {}, {}, diagnostics

	def _qualify_fn_name(module_id: str, name: str) -> str:
		# Entrypoint is always the unqualified symbol `main` regardless of which
		# module it is defined in. MVP rule: exactly one `main` across the whole
		# build (enforced downstream by duplicate-symbol diagnostics).
		if name == "main":
			return "main"
		return f"{module_id}::{name}"

	def _qualify_symbol(module_id: str, sym: str, *, local_free_fns: set[str]) -> str:
		"""
		Qualify a module-local callable symbol for inclusion in a multi-module build.

		MVP intent: avoid collisions when multiple modules are lowered into a single
		LLVM module by making callable symbols module-scoped:
		- free functions: `foo` → `mod::foo` (except the entry `main`)
		- methods: `Type::method` → `mod::Type::method`

		This does not define a long-term symbol identity model; it's a pragmatic
		naming layer until the resolver carries explicit module/type identities.
		"""
		if sym in local_free_fns:
			return _qualify_fn_name(module_id, sym)
		if "::" in sym and not sym.startswith(f"{module_id}::"):
			return f"{module_id}::{sym}"
		return sym

	all_func_hirs: dict[str, H.HBlock] = {}
	all_sigs: dict[str, FnSignature] = {}
	exc_catalog: dict[str, int] = {}
	fn_owner_module: dict[str, str] = {}

	# Lower each module and qualify its callable symbols.
	for mid, prog in merged_programs.items():
		func_hirs, sigs, _table, excs, diags = _lower_parsed_program_to_hir(
			prog,
			diagnostics=[],
			type_table=shared_type_table,
		)
		diagnostics.extend(diags)
		exc_catalog.update(excs)

		local_free_fns = {fn.name for fn in getattr(prog, "functions", []) or []}
		exported_values = exports_values_by_module.get(mid, {})

		# Qualify and copy function bodies/signatures.
		for local_name, block in func_hirs.items():
			global_name = _qualify_symbol(mid, local_name, local_free_fns=local_free_fns)
			if global_name in all_func_hirs:
				diagnostics.append(
					Diagnostic(
						message=f"duplicate function symbol '{global_name}' across modules",
						severity="error",
						span=Span(),
					)
				)
				continue
			all_func_hirs[global_name] = block
			fn_owner_module[global_name] = mid

		for local_name, sig in sigs.items():
			global_name = _qualify_symbol(mid, local_name, local_free_fns=local_free_fns)
			# Mark module-interface entry points early so downstream phases can
			# enforce visibility and (later) ABI-boundary rules consistently.
			is_exported = (local_name in local_free_fns) and (local_name in exported_values) and (local_name != "main")
			all_sigs[global_name] = replace(sig, name=global_name, is_exported_entrypoint=is_exported)

	# Materialize re-exported functions as trampoline entry points.
	#
	# Even though `export { foo }` can refer to an imported binding (re-export),
	# module interfaces must remain self-contained: importing a symbol from a module
	# binds to `module::symbol`, not to some hidden downstream module.
	#
	# MVP implementation strategy:
	# - if module `a` exports `foo` that maps to an underlying target `(b, foo)`,
	#   synthesize `a::foo` as a trivial trampoline calling `b::foo`.
	# - this ensures exported entrypoints exist in the exporting module and keeps
	#   future package/interface metadata straightforward.
	for mid, exported_values in exports_values_by_module.items():
		for export_name, (target_mod, target_sym) in exported_values.items():
			if export_name == "main":
				continue
			if (target_mod, target_sym) == (mid, export_name):
				continue
			trampoline_name = _qualify_fn_name(mid, export_name)
			if trampoline_name in all_sigs:
				continue
			target_name = _qualify_fn_name(target_mod, target_sym)
			target_sig = all_sigs.get(target_name)
			if target_sig is None:
				diagnostics.append(
					Diagnostic(
						message=f"internal: missing signature for re-export target '{target_mod}::{target_sym}'",
						severity="error",
						span=Span(),
					)
				)
				continue
			all_sigs[trampoline_name] = replace(target_sig, name=trampoline_name, is_exported_entrypoint=True)
			fn_owner_module[trampoline_name] = mid

			# Build a minimal HIR body that forwards to the underlying target.
			arg_exprs: list[H.HExpr] = []
			for p in getattr(target_sig, "param_names", None) or []:
				if p:
					arg_exprs.append(H.HVar(name=p))
			callee = H.HVar(name=target_name)
			call_expr = H.HCall(fn=callee, args=arg_exprs)
			if target_sig.return_type_id is not None and shared_type_table.is_void(target_sig.return_type_id):
				all_func_hirs[trampoline_name] = H.HBlock(
					statements=[
						H.HExprStmt(expr=call_expr),
						H.HReturn(value=None),
					]
				)
			else:
				all_func_hirs[trampoline_name] = H.HBlock(statements=[H.HReturn(value=call_expr)])

		if any(d.severity == "error" for d in diagnostics):
			return {}, {}, TypeTable(), {}, {}, diagnostics

	# Rewrite call sites: HCall(fn=HVar(name="foo")) -> HVar(name="m::foo") for imported/local functions.
	local_maps: dict[str, dict[str, str]] = {
		mid: {fn.name: _qualify_fn_name(mid, fn.name) for fn in getattr(prog, "functions", []) or []}
		for mid, prog in merged_programs.items()
	}

	def _rewrite_calls_in_block(
		block: H.HBlock,
		*,
		module_id: str,
		fn_symbol: str,
		origin_file: Path | None,
	) -> None:
		local_map = local_maps.get(module_id, {})
		file_bindings = from_value_bindings_by_file.get(origin_file or Path(), {})
		import_map: dict[str, str] = {local_name: _qualify_fn_name(mod, sym) for local_name, (mod, sym) in file_bindings.items()}
		file_module_aliases = module_aliases_by_file.get(origin_file or Path(), {})
		# Call-site rewriting must be scope-correct: a local binding shadows only
		# within its lexical block, not across the whole function.
		#
		# This is still a limited MVP resolver (it only rewrites direct calls
		# represented as `HCall(HVar("foo"))`), but it avoids silent miscompiles by:
		# - never rewriting names that are currently bound (params, lets, binders),
		# - applying bindings as statements are traversed (let-binding is visible
		#   only *after* its initializer).
		param_names: list[str] = []
		sig = all_sigs.get(fn_symbol)
		if sig is not None and getattr(sig, "param_names", None):
			param_names = [p for p in sig.param_names if p]

		def rewrite_name(name: str, *, bound: set[str]) -> str:
			if name in bound:
				return name
			if name in local_map:
				return local_map[name]
			if name in import_map:
				return import_map[name]
			return name

		def exported_value_names(mod: str) -> set[str]:
			if mod in exports_values_by_module:
				return set((exports_values_by_module.get(mod) or {}).keys())
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				return set(ext.get("values") or set())
			return set()

		def exported_type_names(mod: str) -> set[str]:
			if mod in exports_types_by_module:
				return set(exports_types_by_module.get(mod) or set())
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				return set(ext.get("types") or set())
			return set()

		def _rewrite_module_qualified_call(
			*,
			receiver: H.HExpr,
			member: str,
			args: list[H.HExpr],
			kwargs: list[H.HKwArg],
		) -> H.HExpr | None:
			"""
			Rewrite a syntactic member call `x.member(...)` when `x` is a module alias.

			MVP surface rule (pinned):
			  import lib as x
			  x.foo(1, 2)   // call exported function foo from module lib
			  x.Point(...)  // call struct constructor Point from module lib

			We do *not* create a runtime module object. Instead, we resolve the
			member at compile time and rewrite the callee to a fully-qualified
			callable symbol (`lib::foo`) or an unqualified struct constructor (`Point`).

			Note on representation: in stage1 HIR, a `.`-call like `x.foo(...)` is
			represented as `HMethodCall(receiver=x, method_name="foo", ...)` (method
			sugar). We reuse that syntactic form for module-qualified access and
			rewrite it here into a plain `HCall` once we confirm `x` is a module alias.
			"""
			if not isinstance(receiver, H.HVar):
				return None
			if receiver.binding_id is not None:
				# Local/param shadowing wins: `x.foo` refers to the local `x`, not a module.
				return None
			alias = receiver.name
			mod = file_module_aliases.get(alias)
			if mod is None:
				return None
			vals = exported_value_names(mod)
			types = exported_type_names(mod)
			if member in vals:
				return H.HCall(fn=H.HVar(name=_qualify_fn_name(mod, member)), args=args, kwargs=kwargs)
			if member in types:
				# Constructor call through a module alias. MVP supports only struct ctors.
				struct_id = shared_type_table.get_nominal(kind=TypeKind.STRUCT, module_id=mod, name=member)
				if struct_id is None:
					diagnostics.append(
						Diagnostic(
							message=f"module-qualified constructor call '{alias}.{member}(...)' is only supported for structs in MVP",
							severity="error",
							span=getattr(receiver, "loc", Span()),
						)
					)
					return None
				# Rewrite to an internal fully-qualified constructor name so later
				# phases can resolve it deterministically even when multiple modules
				# define the same short type name.
				return H.HCall(fn=H.HVar(name=f"{mod}::{member}"), args=args, kwargs=kwargs)
			available = ", ".join(sorted(vals | types))
			notes = [f"available exports: {available}"] if available else [f"module '{mod}' exports nothing (private by default)"]
			diagnostics.append(
				Diagnostic(
					message=f"module '{mod}' does not export symbol '{member}'",
					severity="error",
					span=getattr(receiver, "loc", Span()),
					notes=notes,
				)
			)
			return None

		def walk_block(b: H.HBlock, *, bound: set[str]) -> None:
			scope_bound = set(bound)
			for st in b.statements:
				walk_stmt(st, bound=scope_bound)
				if isinstance(st, H.HLet):
					scope_bound.add(st.name)

		def walk_expr(expr: H.HExpr, *, bound: set[str]) -> H.HExpr:
			# Module-qualified access: the surface syntax is `x.foo(...)`. Stage1
			# initially represents this as `HMethodCall`, so we rewrite that form
			# when `x` resolves to a module alias in the current file.
			if isinstance(expr, H.HMethodCall):
				expr.receiver = walk_expr(expr.receiver, bound=bound)
				expr.args = [walk_expr(a, bound=bound) for a in expr.args]
				for kw in getattr(expr, "kwargs", []) or []:
					if getattr(kw, "value", None) is not None:
						kw.value = walk_expr(kw.value, bound=bound)
				rewritten = _rewrite_module_qualified_call(
					receiver=expr.receiver,
					member=expr.method_name,
					args=expr.args,
					kwargs=getattr(expr, "kwargs", []) or [],
				)
				if rewritten is not None:
					return rewritten
				return expr

			if isinstance(expr, H.HCall):
				expr.fn = walk_expr(expr.fn, bound=bound)
				expr.args = [walk_expr(a, bound=bound) for a in expr.args]
				for kw in getattr(expr, "kwargs", []) or []:
					if getattr(kw, "value", None) is not None:
						kw.value = walk_expr(kw.value, bound=bound)
				# Resolve direct calls that name a local/imported/free function.
				if isinstance(expr.fn, H.HVar):
					expr.fn.name = rewrite_name(expr.fn.name, bound=bound)
				elif isinstance(expr.fn, H.HField) and isinstance(expr.fn.subject, H.HVar):
					# Handle the (rarer) explicit field-call form: `(x.foo)(...)`.
					q = _rewrite_module_qualified_call(
						receiver=expr.fn.subject,
						member=expr.fn.name,
						args=expr.args,
						kwargs=getattr(expr, "kwargs", []) or [],
					)
					if isinstance(q, H.HCall):
						# Preserve the rewritten call and ignore the original callee expression.
						return q
				return expr

			if isinstance(expr, H.HField) and isinstance(expr.subject, H.HVar) and expr.subject.binding_id is None:
				mod = file_module_aliases.get(expr.subject.name)
				if mod is not None:
					if expr.name in exported_value_names(mod):
						return H.HVar(name=_qualify_fn_name(mod, expr.name))
					# Note: module-qualified type names are handled in type positions
					# via TypeExpr.module_id. Expression-position `x.Point` without
					# call is not a supported surface construct in MVP.
				return expr

			# Generic recursion for other expression shapes.
			for k, child in list(getattr(expr, "__dict__", {}).items()):
				if isinstance(child, H.HExpr):
					setattr(expr, k, walk_expr(child, bound=bound))
				elif isinstance(child, H.HBlock):
					walk_block(child, bound=bound)
				elif isinstance(child, list):
					new_list = []
					for it in child:
						if isinstance(it, H.HExpr):
							new_list.append(walk_expr(it, bound=bound))
						elif isinstance(it, H.HBlock):
							walk_block(it, bound=bound)
							new_list.append(it)
						# Expression-form arms (match/try) live under expression nodes and
						# must be handled here so binders introduce lexical scopes.
						elif hasattr(H, "HMatchArm") and isinstance(it, getattr(H, "HMatchArm")):
							arm_bound = set(bound)
							for bname in getattr(it, "binders", []) or []:
								arm_bound.add(bname)
							walk_block(it.block, bound=arm_bound)
							if getattr(it, "result", None) is not None:
								it.result = walk_expr(it.result, bound=arm_bound)
							new_list.append(it)
						elif hasattr(H, "HTryExprArm") and isinstance(it, getattr(H, "HTryExprArm")):
							arm_bound = set(bound)
							if getattr(it, "binder", None):
								arm_bound.add(it.binder)
							walk_block(it.block, bound=arm_bound)
							if getattr(it, "result", None) is not None:
								it.result = walk_expr(it.result, bound=arm_bound)
							new_list.append(it)
						else:
							new_list.append(it)
					setattr(expr, k, new_list)
			return expr

		def walk_stmt(stmt: H.HStmt, *, bound: set[str]) -> None:
			if isinstance(stmt, H.HTry):
				walk_block(stmt.body, bound=bound)
				for arm in stmt.catches:
					arm_bound = set(bound)
					if arm.binder:
						arm_bound.add(arm.binder)
					walk_block(arm.block, bound=arm_bound)
				return
			for k, child in list(getattr(stmt, "__dict__", {}).items()):
				if isinstance(child, H.HExpr):
					setattr(stmt, k, walk_expr(child, bound=bound))
				elif isinstance(child, H.HBlock):
					walk_block(child, bound=bound)
				elif isinstance(child, list):
					new_list = []
					for it in child:
						if isinstance(it, H.HStmt):
							walk_stmt(it, bound=bound)
							new_list.append(it)
						elif isinstance(it, H.HExpr):
							new_list.append(walk_expr(it, bound=bound))
						elif isinstance(it, H.HBlock):
							walk_block(it, bound=bound)
							new_list.append(it)
						elif hasattr(H, "HCatchArm") and isinstance(it, getattr(H, "HCatchArm")):
							arm_bound = set(bound)
							if getattr(it, "binder", None):
								arm_bound.add(it.binder)
							walk_block(it.block, bound=arm_bound)
							new_list.append(it)
						elif hasattr(H, "HMatchArm") and isinstance(it, getattr(H, "HMatchArm")):
							arm_bound = set(bound)
							for bname in getattr(it, "binders", []) or []:
								arm_bound.add(bname)
							walk_block(it.block, bound=arm_bound)
							if getattr(it, "result", None) is not None:
								it.result = walk_expr(it.result, bound=arm_bound)
							new_list.append(it)
						elif hasattr(H, "HTryExprArm") and isinstance(it, getattr(H, "HTryExprArm")):
							arm_bound = set(bound)
							if getattr(it, "binder", None):
								arm_bound.add(it.binder)
							walk_block(it.block, bound=arm_bound)
							if getattr(it, "result", None) is not None:
								it.result = walk_expr(it.result, bound=arm_bound)
							new_list.append(it)
						else:
							new_list.append(it)
					setattr(stmt, k, new_list)

		initial_bound = set(param_names)
		walk_block(block, bound=initial_bound)

	# Apply rewrite to each function body using its origin file’s import environment.
	fn_origin_file: dict[str, Path] = {}
	for mid, origins in origin_by_module.items():
		for local_sym, src_path in origins.items():
			fn_origin_file[_qualify_fn_name(mid, local_sym)] = src_path

	for fn_name, block in all_func_hirs.items():
		_rewrite_calls_in_block(
			block,
			module_id=fn_owner_module.get(fn_name, "main"),
			fn_symbol=fn_name,
			origin_file=fn_origin_file.get(fn_name),
		)

	# Cross-module exception code collision detection: event codes are derived
	# from the canonical event FQN (`module:Event`). Collisions are extremely
	# unlikely, but if they happen we must diagnose them deterministically.
	payload_seen: dict[int, str] = {}
	for fqn, code in exc_catalog.items():
		payload = code & PAYLOAD_MASK
		other = payload_seen.get(payload)
		if other is not None and other != fqn:
			diagnostics.append(Diagnostic(message=f"exception code collision between '{other}' and '{fqn}' (payload {payload})", severity="error", span=Span()))
		else:
			payload_seen[payload] = fqn

	return all_func_hirs, all_sigs, shared_type_table, exc_catalog, module_exports, diagnostics


def _lower_parsed_program_to_hir(
	prog: parser_ast.Program,
	*,
	diagnostics: list[Diagnostic] | None = None,
	type_table: TypeTable | None = None,
) -> Tuple[Dict[str, H.HBlock], Dict[str, FnSignature], "TypeTable", Dict[str, int], List[Diagnostic]]:
	"""
	Lower an already-parsed `Program` to HIR/signatures/type table.

	This is shared by both single-file and multi-file entry points.
	"""
	diagnostics = list(diagnostics or [])
	module_name = getattr(prog, "module", None)
	module_id = module_name or "main"
	func_hirs: Dict[str, H.HBlock] = {}
	decls: list[_FrontendDecl] = []
	signatures: Dict[str, FnSignature] = {}
	lowerer = AstToHIR()
	lowerer._module_name = module_name
	seen: set[str] = set()
	method_keys: set[tuple[str, str]] = set()  # (impl_target_repr, method_name)
	module_function_names: set[str] = {fn.name for fn in getattr(prog, "functions", []) or []}
	exception_schemas: dict[str, tuple[str, list[str]]] = {}
	struct_defs = list(getattr(prog, "structs", []) or [])
	variant_defs = list(getattr(prog, "variants", []) or [])
	exception_catalog: dict[str, int] = _build_exception_catalog(prog.exceptions, module_name, diagnostics)
	for exc in prog.exceptions:
		fqn = f"{module_name}:{exc.name}" if module_name else exc.name
		field_names = [arg.name for arg in getattr(exc, "args", [])]
		exception_schemas[fqn] = (fqn, field_names)
	# Build a TypeTable early so we can register user-defined type names (structs)
	# before resolving function signatures. This prevents `resolve_opaque_type`
	# from minting unrelated placeholder TypeIds for struct names.
	type_table = type_table or TypeTable()
	_prime_builtins(type_table)
	# Prelude: `Optional<T>` is required for iterator-style `for` desugaring and
	# other control-flow sugar. Until modules are supported, the compiler injects
	# a canonical `Optional<T>` variant base into every compilation unit unless
	# user code declares its own `variant Optional<...>`.
	#
	# MVP contract:
	#   variant Optional<T> { Some(value: T), None }
	if not any(getattr(v, "name", None) == "Optional" for v in variant_defs) and type_table.get_variant_base(
		module_id="lang.core", name="Optional"
	) is None:
		type_table.declare_variant(
			"lang.core",
			"Optional",
			["T"],
			[
				VariantArmSchema(
					name="Some",
					fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
				),
				VariantArmSchema(name="None", fields=[]),
			],
		)
	# Declare all struct names first (placeholder field types) to support recursion.
	for s in struct_defs:
		field_names = [f.name for f in getattr(s, "fields", [])]
		try:
			type_table.declare_struct(module_id, s.name, field_names)
		except ValueError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=Span.from_loc(getattr(s, "loc", None))))
	# Declare all variant names/schemas next so type resolution can instantiate
	# variants (e.g., Optional<Int>) while resolving later annotations/fields.
	for v in variant_defs:
		arms: list[VariantArmSchema] = []
		for arm in getattr(v, "arms", []) or []:
			fields = [
				VariantFieldSchema(
					name=f.name,
					type_expr=_generic_type_expr_from_parser(f.type_expr, type_params=list(getattr(v, "type_params", []) or [])),
				)
				for f in getattr(arm, "fields", []) or []
			]
			arms.append(VariantArmSchema(name=arm.name, fields=fields))
		try:
			type_table.declare_variant(
				module_id,
				v.name,
				list(getattr(v, "type_params", []) or []),
				arms,
			)
		except ValueError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=Span.from_loc(getattr(v, "loc", None))))
	# Fill field TypeIds in a second pass now that all names exist.
	for s in struct_defs:
		struct_id = type_table.require_nominal(kind=TypeKind.STRUCT, module_id=module_id, name=s.name)
		field_types = []
		for f in getattr(s, "fields", []):
			ft = resolve_opaque_type(f.type_expr, type_table, module_id=module_id)
			# MVP escape policy: references cannot be stored in long-lived memory.
			# Struct fields are long-lived by construction, so `struct S(r: &T)` is
			# rejected early (before lowering/typecheck) with a source-anchored
			# diagnostic.
			try:
				td = type_table.get(ft)
			except Exception:
				td = None
			if td is not None and td.kind is TypeKind.REF:
				diagnostics.append(
					Diagnostic(
						message=f"struct '{s.name}' field '{f.name}' cannot have a reference type in MVP",
						severity="error",
						span=Span.from_loc(getattr(f.type_expr, "loc", getattr(f, "loc", None))),
					)
				)
			field_types.append(ft)
		type_table.define_struct_fields(struct_id, field_types)
	# After all variant schemas are known and structs are declared, finalize
	# non-generic variants so their concrete arm types are available.
	type_table.finalize_variants()
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
		param_names = [p.name for p in getattr(fn, "params", []) or []]
		hir_block = lowerer.lower_function_block(stmt_block, param_names=param_names)
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
			# Note: receiver shape/name/type are semantic rules enforced by the
			# typecheck phase. The parser adapter stays structural-only here so
			# related errors consistently report as typecheck diagnostics.
			receiver_ty = fn.params[0].type_expr if fn.params else None
			self_mode: str | None = None
			if receiver_ty is not None:
				self_mode = "value"
				if receiver_ty.name == "&":
					self_mode = "ref"
				elif receiver_ty.name == "&mut":
					self_mode = "ref_mut"

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
			# Enable implicit `self` member lookup for method bodies (spec §3.9).
			# Unknown identifiers may resolve to fields/methods on `self` after
			# locals and module-scope items are considered.
			#
			# We only need names here; semantic validation happens in the typed checker.
			# Collect receiver field names for implicit `self` member lookup.
			#
			# IMPORTANT: structs are module-scoped. We must resolve the impl target
			# in the current module context, not by bare name.
			field_names: set[str] = set()
			try:
				origin_mod = getattr(impl.target, "module_id", None) or module_name or "main"
				struct_id = type_table.get_nominal(kind=TypeKind.STRUCT, module_id=origin_mod, name=impl.target.name)
				if struct_id is not None:
					td = type_table.get(struct_id)
					if td.field_names is not None:
						field_names = set(td.field_names)
			except Exception:
				field_names = set()
			method_names: set[str] = {m.name for m in getattr(impl, "methods", []) or []}
			param_names = [p.name for p in getattr(fn, "params", []) or []]
			if fn.params and self_mode is not None:
				lowerer._push_implicit_self(
					self_name=str(getattr(fn.params[0], "name", "self")),
					self_mode=self_mode,
					field_names=field_names,
					method_names=method_names,
					module_function_names=module_function_names,
				)
				try:
					hir_block = lowerer.lower_function_block(stmt_block, param_names=param_names)
				finally:
					lowerer._pop_implicit_self()
			else:
				hir_block = lowerer.lower_function_block(stmt_block, param_names=param_names)
			func_hirs[symbol_name] = hir_block
	# Build signatures with resolved TypeIds from parser decls.
	from lang2.driftc.type_resolver import resolve_program_signatures

	type_table, sigs = resolve_program_signatures(decls, table=type_table)
	signatures.update(sigs)
	# Thread exception schemas through the shared type table for downstream validators.
	#
	# In a multi-module build, this function may be called repeatedly with a
	# shared TypeTable; preserve previously registered schemas and extend them.
	prev_schemas = getattr(type_table, "exception_schemas", None)
	if not isinstance(prev_schemas, dict):
		prev_schemas = {}
	prev_schemas.update(exception_schemas)
	type_table.exception_schemas = prev_schemas
	return func_hirs, signatures, type_table, exception_catalog, diagnostics


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
		return {}, {}, TypeTable(), {}, [Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc))]
	except UnexpectedInput as err:
		span = Span(
			file=str(path),
			line=getattr(err, "line", None),
			column=getattr(err, "column", None),
			raw=err,
		)
		return {}, {}, TypeTable(), {}, [Diagnostic(message=str(err), severity="error", span=span)]
	return _lower_parsed_program_to_hir(prog, diagnostics=[])


__all__ = ["parse_drift_to_hir", "parse_drift_files_to_hir", "parse_drift_workspace_to_hir"]
