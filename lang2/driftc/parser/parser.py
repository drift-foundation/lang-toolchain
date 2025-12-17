from __future__ import annotations

import ast
import codecs
from pathlib import Path
from typing import List, Optional

from lark import Lark, Token, Tree

from .ast import (
    ArrayLiteral,
    AssignStmt,
    AugAssignStmt,
    Attr,
    Binary,
    Block,
    Call,
    CatchClause,
    ExceptionArg,
    ExceptionDef,
    Expr,
    ExprStmt,
    ForStmt,
    FunctionDef,
    IfStmt,
    ImportStmt,
    ImplementDef,
    Index,
    KwArg,
    LetStmt,
    Literal,
    Located,
    Move,
    Name,
    Placeholder,
    Param,
    Program,
    RaiseStmt,
    RethrowStmt,
    ReturnStmt,
    StructDef,
    StructField,
    TypeExpr,
    Ternary,
    TryCatchExpr,
    CatchExprArm,
    ExceptionCtor,
    TryStmt,
    WhileStmt,
    BreakStmt,
    ContinueStmt,
    Unary,
    FString,
    FStringHole,
    VariantDef,
    VariantArm,
    VariantField,
    MatchExpr,
    MatchArm,
)

_GRAMMAR_PATH = Path(__file__).with_name("grammar.lark")
_GRAMMAR_SRC = _GRAMMAR_PATH.read_text()


def _decode_string_token(tok: Token) -> str:
	"""
	Decode STRING tokens, including \\xHH hex byte escapes. We first interpret
	Python-style escapes (unicode_escape), then reinterpret the resulting code
	points as raw bytes (latin-1) and decode as UTF-8 to recover the intended
	byte sequence.
	"""
	content = tok.value[1:-1]  # strip quotes
	unescaped = codecs.decode(content, "unicode_escape")
	raw_bytes = unescaped.encode("latin-1")
	return raw_bytes.decode("utf-8")


def _decode_string_fragment(raw: str) -> str:
	"""
	Decode the contents of a string *fragment* using the same escape rules as STRING.

	This is used by the f-string parser to decode text parts that are split by holes.
	The input must be the raw source substring *inside* the quotes (i.e., it may
	contain backslash escapes like `\\n` or `\\xNN`).
	"""
	unescaped = codecs.decode(raw, "unicode_escape")
	raw_bytes = unescaped.encode("latin-1")
	return raw_bytes.decode("utf-8")


def _unwrap_ident(node: object) -> Token:
	"""
	Extract an identifier token from a grammar `ident` node.

	The grammar defines `ident: NAME | MOVE` so callers may receive:
	- a `Token` (`NAME` or `MOVE`) directly, or
	- a `Tree('ident', [Token(...)])` wrapper.
	"""
	if isinstance(node, Token):
		if node.type in {"NAME", "MOVE"}:
			return node
		raise TypeError(f"Expected identifier token, got {node.type}")
	if isinstance(node, Tree) and _name(node) == "ident":
		tok = next((c for c in node.children if isinstance(c, Token)), None)
		if tok is None:
			raise TypeError("ident node missing token child")
		if tok.type not in {"NAME", "MOVE"}:
			raise TypeError(f"Expected NAME/MOVE token in ident, got {tok.type}")
		return tok
	raise TypeError(f"Expected ident node, got {type(node)}")


_EXPR_PARSER = Lark(
	_GRAMMAR_SRC,
	parser="lalr",
	lexer="basic",
	start="expr",
	propagate_positions=True,
	maybe_placeholders=False,
)


def _parse_expr_fragment(source: str) -> Expr:
	"""
	Parse a Drift expression from a source fragment.

	This helper is used for f-string holes. The grammar is shared with the main
	parser, but the start rule is `expr` and no newline-terminator insertion is
	performed (holes never contain newlines because string literals don't).
	"""
	tree = _EXPR_PARSER.parse(source)
	return _build_expr(tree)


class FStringParseError(ValueError):
	"""
	Error raised while parsing an f-string literal.

	This is intentionally a `ValueError` subclass so existing parser plumbing can
continue to treat it as a parse-time failure, but it carries a best-effort
location (`loc`) so callers can convert it into a structured diagnostic instead
of crashing the compiler.
	"""

	def __init__(self, message: str, *, loc: "Located") -> None:
		super().__init__(message)
		self.loc = loc


def _parse_fstring(loc: Located, raw_string_token: Token) -> FString:
	"""
	Parse a raw STRING token (including braces) into an f-string AST.

	The main grammar tokenizes the interior of the f-string as a normal STRING token
	so we can reuse the existing string escape rules. This function is responsible
	for:
	- splitting text vs `{...}` holes,
	- supporting brace escaping via `{{` / `}}`,
	- extracting the hole expression and optional `:spec` substring, and
	- producing accurate-ish hole locations (line/column within the string).

	MVP limitations:
	- spec strings are opaque and must not contain `{` or `}`.
	- errors are reported as `FStringParseError` and converted into diagnostics.
	"""
	raw = raw_string_token.value[1:-1]  # strip quotes, keep escapes

	parts: list[str] = []
	holes: list[FStringHole] = []
	text_buf: list[str] = []

	base_line = getattr(raw_string_token, "line", loc.line)
	base_col = getattr(raw_string_token, "column", loc.column)

	def _flush_text() -> None:
		fragment_raw = "".join(text_buf)
		text_buf.clear()
		parts.append(_decode_string_fragment(fragment_raw))

	def _hole_loc(offset: int) -> Located:
		return Located(line=base_line, column=base_col + offset)

	def _unescape_hole_source(src: str) -> str:
		out: list[str] = []
		j = 0
		while j < len(src):
			c = src[j]
			if c == "\\" and j + 1 < len(src):
				nxt = src[j + 1]
				if nxt in ("\\", "\""):
					out.append(nxt)
					j += 2
					continue
			out.append(c)
			j += 1
		return "".join(out)

	i = 0
	while i < len(raw):
		ch = raw[i]
		if raw.startswith("{{", i):
			text_buf.append("{")
			i += 2
			continue
		if raw.startswith("}}", i):
			text_buf.append("}")
			i += 2
			continue
		if ch == "}":
			raise FStringParseError("E-FSTR-UNBALANCED-BRACE: unescaped '}' in f-string", loc=_hole_loc(i))
		if ch != "{":
			text_buf.append(ch)
			i += 1
			continue

		hole_start = i
		_flush_text()
		i += 1  # consume '{'
		if i < len(raw) and raw[i] == "}":
			raise FStringParseError("E-FSTR-EMPTY-HOLE: '{}' is not a valid f-string hole", loc=_hole_loc(hole_start))

		paren_depth = 0
		bracket_depth = 0
		brace_depth = 0
		in_string = False
		expr_buf: list[str] = []
		spec_buf: list[str] | None = None

		while i < len(raw):
			c = raw[i]
			if in_string:
				expr_buf.append(c)
				if c == "\\":
					i += 1
					if i < len(raw):
						expr_buf.append(raw[i])
					i += 1
					continue
				if c == "\"":
					in_string = False
				i += 1
				continue

			if (
				spec_buf is None
				and c == ":"
				and paren_depth == 0
				and bracket_depth == 0
				and brace_depth == 0
			):
				candidate_expr = "".join(expr_buf).strip()
				try:
					_parse_expr_fragment(candidate_expr)
				except Exception:
					expr_buf.append(c)
					i += 1
					continue
				spec_buf = []
				i += 1
				continue

			if c == "\"":
				in_string = True
				expr_buf.append(c)
				i += 1
				continue

			if c == "(":
				paren_depth += 1
			elif c == ")" and paren_depth:
				paren_depth -= 1
			elif c == "[":
				bracket_depth += 1
			elif c == "]" and bracket_depth:
				bracket_depth -= 1
			elif c == "{":
				brace_depth += 1
			elif c == "}":
				if brace_depth:
					brace_depth -= 1
				elif paren_depth == 0 and bracket_depth == 0:
					i += 1
					break
				else:
					raise FStringParseError("E-FSTR-UNBALANCED-BRACE: '}' in f-string hole", loc=_hole_loc(i))

			if spec_buf is None:
				expr_buf.append(c)
			else:
				if c in "{}":
					raise FStringParseError("E-FSTR-NESTED: nested braces are not allowed in :spec (MVP)", loc=_hole_loc(i))
				spec_buf.append(c)
			i += 1
		else:
			raise FStringParseError("E-FSTR-UNBALANCED-BRACE: unterminated '{' in f-string", loc=_hole_loc(hole_start))

		expr_src = _unescape_hole_source("".join(expr_buf).strip())
		if not expr_src:
			raise FStringParseError("E-FSTR-EMPTY-HOLE: hole must contain an expression", loc=_hole_loc(hole_start))

		expr_ast = _parse_expr_fragment(expr_src)
		spec = "".join(spec_buf).strip() if spec_buf is not None else ""
		holes.append(FStringHole(loc=_hole_loc(hole_start), expr=expr_ast, spec=spec))

	_flush_text()
	if len(parts) != len(holes) + 1:
		raise AssertionError("f-string parser bug: parts/holes shape mismatch")
	return FString(loc=loc, parts=parts, holes=holes)


class TerminatorInserter:
    always_accept = ("NEWLINE", "SEMI")

    TERMINABLE = {
        "NAME",
        "SIGNED_INT",
        # Float literals in lang2 MVP use the `FLOAT` token (dot required).
        "FLOAT",
        # Legacy/compat token name; keep for safety if the grammar changes.
        "SIGNED_FLOAT",
        "STRING",
        "TRUE",
        "FALSE",
        "RPAR",
        "RSQB",
        "RBRACE",
        "RETURN",
        "THROW",
        "RETHROW",
        "BREAK",
        "CONTINUE",
    }

    SUPPRESS = {
        "DOT",
        "ARROW",
        "PLUS",
        "PLUS_EQ",
        "MINUS",
        "MINUS_EQ",
        "STAR_EQ",
        "SLASH_EQ",
        "PERCENT_EQ",
        "AMP_EQ",
        "BAR_EQ",
        "CARET_EQ",
        "LSHIFT_EQ",
        "SHR_EQ",
        "STAR",
        "SLASH",
        "PERCENT",
        "BAR",
        "CARET",
        "TILDE",
        "LSHIFT",
        "SHR",
        "PIPE_FWD",
        "PIPE_REV",
        "AND",
        "OR",
        "EQEQ",
        "NOTEQ",
        "LTE",
        "GTE",
        "LT",
        "GT",
        "COLON",
        "COMMA",
        "EQUAL",
        "IF",
        "ELSE",
    }

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self.paren_depth = 0
        self.bracket_depth = 0
        self.can_terminate = False

    def process(self, stream):
        """
        Insert `TERMINATOR` tokens for statement boundaries.

        We treat explicit `;` as an unconditional terminator, and we treat
        newline as a terminator *only* when the previous token can terminate a
        statement and we're not inside parentheses/brackets.

        One additional rule keeps `try ... catch ...` readable across lines:

            try foo()
                catch { ... }

        The newline between the attempt and the `catch` keyword must not create
        a statement terminator. To support that without complicating the
        grammar, we suppress newline-terminator insertion when the next
        significant token is `CATCH`.
        """

        self._reset()
        pending_newline: Token | None = None

        for token in stream:
            ttype = token.type

            if ttype == "NEWLINE":
                # Defer the decision until we see the next non-newline token so
                # we can suppress terminators before `catch`.
                pending_newline = token
                continue

            if ttype == "SEMI":
                # An explicit semicolon always terminates the current statement.
                pending_newline = None
                yield Token.new_borrow_pos("TERMINATOR", token.value, token)
                self.can_terminate = False
                continue

            if pending_newline is not None:
                if self._should_emit_terminator() and ttype != "CATCH":
                    yield Token.new_borrow_pos("TERMINATOR", pending_newline.value, pending_newline)
                    self.can_terminate = False
                pending_newline = None

            yield token
            self._update_depth(ttype)
            self.can_terminate = self._is_terminable(ttype)

        if pending_newline is not None:
            if self._should_emit_terminator():
                yield Token.new_borrow_pos("TERMINATOR", pending_newline.value, pending_newline)
                self.can_terminate = False

    def _update_depth(self, ttype: str) -> None:
        if ttype == "LPAR":
            self.paren_depth += 1
        elif ttype == "RPAR" and self.paren_depth:
            self.paren_depth -= 1
        elif ttype == "LSQB":
            self.bracket_depth += 1
        elif ttype == "RSQB" and self.bracket_depth:
            self.bracket_depth -= 1

    def _is_terminable(self, ttype: str) -> bool:
        if ttype in self.SUPPRESS:
            return False
        return ttype in self.TERMINABLE

    def _should_emit_terminator(self) -> bool:
        return (
            self.paren_depth == 0
            and self.bracket_depth == 0
            and self.can_terminate
        )



_PARSER = Lark(
    _GRAMMAR_SRC,
    parser="lalr",
    lexer="basic",
    start="program",
    propagate_positions=True,
    maybe_placeholders=False,
    postlex=TerminatorInserter(),
)


def parse_program(source: str) -> Program:
    tree = _PARSER.parse(source)
    return _build_program(tree)


def _build_program(tree: Tree) -> Program:
	functions: List[FunctionDef] = []
	implements: List[ImplementDef] = []
	statements: List[ExprStmt | LetStmt | ReturnStmt | RaiseStmt | ImportStmt] = []
	structs: List[StructDef] = []
	exceptions: List[ExceptionDef] = []
	variants: List[VariantDef] = []
	module_name: Optional[str] = None
	for child in tree.children:
		if not isinstance(child, Tree):
			continue
		kind = _name(child)
		if kind == "module_decl":
			module_name = _build_module_decl(child)
			continue
		if kind == "func_def":
			functions.append(_build_function(child))
		elif kind == "implement_def":
			implements.append(_build_implement_def(child))
		elif kind == "struct_def":
			structs.append(_build_struct_def(child))
		elif kind == "exception_def":
			exceptions.append(_build_exception_def(child))
		elif kind == "variant_def":
			variants.append(_build_variant_def(child))
		else:
			stmt = _build_stmt(child)
			if stmt is not None:
				statements.append(stmt)
	return Program(
		functions=functions,
		implements=implements,
		statements=statements,
		structs=structs,
		exceptions=exceptions,
		variants=variants,
		module=module_name,
	)


def _build_module_decl(tree: Tree) -> str:
    from lark import Token as LarkToken

    path_parts = [tok.value for tok in tree.scan_values(lambda v: isinstance(v, LarkToken) and v.type == "NAME")]
    return ".".join(path_parts) if path_parts else "main"


def _build_exception_def(tree: Tree) -> ExceptionDef:
    loc = _loc(tree)
    name_token = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
    args: List[ExceptionArg] = []
    params_node = next(
        (child for child in tree.children if isinstance(child, Tree) and _name(child) == "exception_params"),
        None,
    )
    domain_node = next(
        (child for child in tree.children if isinstance(child, Tree) and _name(child) == "exception_domain_param"),
        None,
    )
    domain_val = None
    if domain_node:
        str_node = next((c for c in domain_node.children if isinstance(c, Token) and c.type == "STRING"), None)
        if str_node:
            domain_val = _decode_string_token(str_node)
    if params_node:
        args = []
        for child in params_node.children:
            if isinstance(child, Tree) and _name(child) == "exception_param":
                args.append(_build_exception_arg(child))
            if isinstance(child, Tree) and _name(child) == "exception_domain_param":
                str_node = next((c for c in child.children if isinstance(c, Token) and c.type == "STRING"), None)
                if str_node:
                    domain_val = _decode_string_token(str_node)
    return ExceptionDef(name=name_token.value, args=args, loc=loc, domain=domain_val)


def _build_variant_def(tree: Tree) -> VariantDef:
	"""
	Build a variant definition.

	Grammar:
	  variant_def: VARIANT NAME type_params? variant_body
	"""
	loc = _loc(tree)
	name_token = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
	type_params_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "type_params"), None)
	type_params: list[str] = []
	if type_params_node is not None:
		type_params = [tok.value for tok in type_params_node.children if isinstance(tok, Token) and tok.type == "NAME"]
	body_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "variant_body")
	arms: list[VariantArm] = []
	for arm_node in (c for c in body_node.children if isinstance(c, Tree) and _name(c) == "variant_arm"):
		arm_name_token = next(child for child in arm_node.children if isinstance(child, Token) and child.type == "NAME")
		fields_node = next((c for c in arm_node.children if isinstance(c, Tree) and _name(c) == "variant_fields"), None)
		fields: list[VariantField] = []
		if fields_node is not None:
			field_list = next((c for c in fields_node.children if isinstance(c, Tree) and _name(c) == "variant_field_list"), None)
			if field_list is not None:
				for field_node in (c for c in field_list.children if isinstance(c, Tree) and _name(c) == "variant_field"):
					fname_tok = next(child for child in field_node.children if isinstance(child, Token) and child.type == "NAME")
					ftype_node = next(child for child in field_node.children if isinstance(child, Tree) and _name(child) == "type_expr")
					fields.append(VariantField(name=fname_tok.value, type_expr=_build_type_expr(ftype_node)))
		arms.append(VariantArm(name=arm_name_token.value, fields=fields, loc=_loc(arm_node)))
	return VariantDef(name=name_token.value, type_params=type_params, arms=arms, loc=loc)


def _build_exception_arg(tree: Tree) -> ExceptionArg:
    if _name(tree) != "exception_param":
        raise ValueError("expected exception_param")
    name_token = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
    type_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr")
    return ExceptionArg(name=name_token.value, type_expr=_build_type_expr(type_node))


def _build_function(tree: Tree) -> FunctionDef:
	loc = _loc(tree)
	children = list(tree.children)
	idx = 0
	name_token = _unwrap_ident(children[idx])
	idx += 1
	orig_name = name_token.value
	params: List[Param] = []
	if idx < len(children) and _name(children[idx]) == "params":
		params = [_build_param(p) for p in children[idx].children if isinstance(p, Tree)]
		idx += 1
	return_sig = children[idx]
	type_child = next(child for child in return_sig.children if isinstance(child, Tree))
	return_type = _build_type_expr(type_child)
	idx += 1
	body = _build_block(children[idx])
	return FunctionDef(
		name=name_token.value,
		orig_name=orig_name,
		params=params,
		return_type=return_type,
		body=body,
		loc=loc,
	)


def _build_implement_def(tree: Tree) -> ImplementDef:
	loc = _loc(tree)
	target_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr")
	target = _build_type_expr(target_node)
	methods: List[FunctionDef] = []
	body_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "implement_body")
	for item in body_node.children:
		# Grammar: implement_item: func_def -> implement_func
		#
		# So we accept either:
		# - `implement_func(func_def)` (preferred), or
		# - legacy/alternate shapes that may appear during grammar evolution.
		if not isinstance(item, Tree):
			continue
		item_kind = _name(item)
		if item_kind not in {"implement_func", "implement_item", "func_def"}:
			continue
		fn_node: Tree | None = None
		if item_kind == "func_def":
			fn_node = item
		else:
			fn_node = next((c for c in item.children if isinstance(c, Tree) and _name(c) == "func_def"), None)
		if fn_node is None:
			continue
		fn = _build_function(fn_node)
		fn.is_method = True
		fn.impl_target = target
		methods.append(fn)
	return ImplementDef(target=target, methods=methods, loc=loc)


def _build_block(tree: Tree) -> Block:
    statements: List[ExprStmt | LetStmt | ReturnStmt | RaiseStmt] = []
    for child in tree.children:
        if not isinstance(child, Tree):
            continue
        if _name(child) == "stmt":
            stmt = _build_stmt(child)
            if stmt is not None:
                statements.append(stmt)
    return Block(statements=statements)


def _build_value_block(tree: Tree) -> Block:
    """
    Build a "value block": a braced block that ends with a trailing expression
    that does not require a terminator (though one may be present).

    Grammar:
        value_block: "{" (stmt | TERMINATOR)* expr terminator_opt "}"

    We represent the trailing expression as a final `ExprStmt` in the block.
    Downstream stages (AstToHIR / checker) are responsible for enforcing any
    "yields a value" rules (e.g. try/catch expression catch arms).
    """

    statements: List[ExprStmt | LetStmt | ReturnStmt | RaiseStmt] = []
    result_expr_node: Tree | None = None

    for child in tree.children:
        if not isinstance(child, Tree):
            continue
        name = _name(child)
        if name == "stmt":
            stmt = _build_stmt(child)
            if stmt is not None:
                statements.append(stmt)
            continue
        if name == "terminator_opt":
            continue
        result_expr_node = child

    if result_expr_node is None:
        raise ValueError("value_block missing trailing expression")

    result_expr = _build_expr(result_expr_node)
    statements.append(ExprStmt(loc=result_expr.loc, value=result_expr))
    return Block(statements=statements)


def _build_param(tree: Tree) -> Param:
    ident_node = next(
        child
        for child in tree.children
        if (isinstance(child, Token) and child.type in {"NAME", "MOVE"})
        or (isinstance(child, Tree) and _name(child) == "ident")
    )
    name_token = _unwrap_ident(ident_node)
    type_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr")
    type_expr = _build_type_expr(type_node)
    return Param(name=name_token.value, type_expr=type_expr)


def _build_type_expr(tree: Tree) -> TypeExpr:
	name = _name(tree)
	if name == "ref_type":
		# '&' ['mut'] type_expr
		inner = _build_type_expr(next(child for child in tree.children if isinstance(child, Tree)))
		mut = any(isinstance(child, Token) and child.type == "MUT" for child in tree.children)
		ref_name = "&mut" if mut else "&"
		return TypeExpr(name=ref_name, args=[inner])
	if name == "type_expr":
		for child in tree.children:
			if isinstance(child, Tree) and _name(child) in {"base_type", "type_expr", "ref_type"}:
				return _build_type_expr(child)
		return TypeExpr(name="<unknown>")
	if name == "base_type":
		name_token = tree.children[0]
		args: List[TypeExpr] = []
		if len(tree.children) > 1:
			type_args = tree.children[1]
			if isinstance(type_args, Tree):
				children = [arg for arg in type_args.children if isinstance(arg, Tree)]
				# Unwrap type_args wrappers that nest the real angle/square args.
				if len(children) == 1 and _name(children[0]) in {"angle_type_args", "square_type_args"}:
					children = [arg for arg in children[0].children if isinstance(arg, Tree)]
				args = [_build_type_expr(arg) for arg in children]
		return TypeExpr(name=name_token.value, args=args)
	# fallback for other wrappers
	if tree.children:
		return _build_type_expr(tree.children[-1])
	return TypeExpr(name="<unknown>")
def _build_stmt(tree: Tree):
	kind = _name(tree)
	if kind == "stmt":
		for child in tree.children:
			if isinstance(child, Tree):
				inner_kind = _name(child)
				if inner_kind in {"simple_stmt", "if_stmt", "try_stmt"}:
					return _build_stmt(child)
		return None
	if kind == "simple_stmt":
		target = tree.children[0]
		stmt_kind = _name(target)
		if stmt_kind == "let_stmt":
			return _build_let_stmt(target)
		if stmt_kind == "for_stmt":
			return _build_for_stmt(target)
		if stmt_kind == "aug_assign_stmt":
			return _build_aug_assign_stmt(target)
		if stmt_kind == "assign_stmt":
			return _build_assign_stmt(target)
		if stmt_kind == "return_stmt":
			return _build_return_stmt(target)
		if stmt_kind == "rethrow_stmt":
			return _build_rethrow_stmt(target)
		if stmt_kind == "raise_stmt":
			return _build_raise_stmt(target)
		if stmt_kind == "expr_stmt":
			return _build_expr_stmt(target)
		if stmt_kind == "import_stmt":
			return _build_import_stmt(target)
		if stmt_kind == "while_stmt":
			return _build_while_stmt(target)
		if stmt_kind == "break_stmt":
			return _build_break_stmt(target)
		if stmt_kind == "continue_stmt":
			return _build_continue_stmt(target)
		return None
	if kind == "if_stmt":
		return _build_if_stmt(tree)
	if kind == "while_stmt":
		return _build_while_stmt(tree)
	if kind == "try_stmt":
		return _build_try_stmt(tree)
	if kind == "let_stmt":
		return _build_let_stmt(tree)
	if kind == "assign_stmt":
		return _build_assign_stmt(tree)
	if kind == "aug_assign_stmt":
		return _build_aug_assign_stmt(tree)
	if kind == "return_stmt":
		return _build_return_stmt(tree)
	if kind == "rethrow_stmt":
		return _build_rethrow_stmt(tree)
	if kind == "raise_stmt":
		return _build_raise_stmt(tree)
	if kind == "expr_stmt":
		return _build_expr_stmt(tree)
	return None


def _build_let_stmt(tree: Tree) -> LetStmt:
    loc = _loc(tree)
    child_nodes = [child for child in tree.children if isinstance(child, Tree)]
    idx = 0
    binder_node = child_nodes[idx]
    idx += 1
    mutable = _binder_is_mutable(binder_node)
    binding_node = child_nodes[idx]
    idx += 1
    name_token, capture = _parse_binding_name(binding_node)
    type_expr = None
    if idx < len(child_nodes) and _name(child_nodes[idx]) == "type_spec":
        type_spec_node = child_nodes[idx]
        idx += 1
        type_expr_node = next(
            (
                child
                for child in type_spec_node.children
                if isinstance(child, Tree) and _name(child) == "type_expr"
            ),
            None,
        )
        if type_expr_node is None:
            raise ValueError("type_spec missing type expression")
        type_expr = _build_type_expr(type_expr_node)
    capture_alias = None
    if idx < len(child_nodes) and _name(child_nodes[idx]) == "alias_clause":
        capture_alias = _parse_alias(child_nodes[idx])
        idx += 1
    value_expr = _build_expr(child_nodes[idx])
    return LetStmt(
        loc=loc,
        name=name_token.value,
        type_expr=type_expr,
        value=value_expr,
        mutable=mutable,
        capture=capture,
        capture_alias=capture_alias,
    )


def _build_assign_stmt(tree: Tree) -> AssignStmt:
    loc = _loc(tree)
    tree_children = [child for child in tree.children if isinstance(child, Tree)]
    target_node = tree_children[0]
    value_node = tree_children[1]
    target = _build_expr(target_node)
    value = _build_expr(value_node)
    return AssignStmt(loc=loc, target=target, value=value)


def _build_aug_assign_stmt(tree: Tree) -> "AugAssignStmt":
    """
    Build an augmented-assignment statement.

    Surface syntax (MVP):
      <lvalue> += <expr>   <lvalue> -= <expr>
      <lvalue> *= <expr>   <lvalue> /= <expr>
      <lvalue> %= <expr>
      <lvalue> &= <expr>   <lvalue> |= <expr>   <lvalue> ^= <expr>
      <lvalue> <<= <expr>  <lvalue> >>= <expr>

    We parse `+=` as its own statement form rather than desugaring to
    `x = x + y` / `x = x - y` in the parser. This keeps later lowering correct for complex
    lvalues (e.g., `arr[i] += 1`) because the target is evaluated once at the
    MIR level (address-of + load + add + store) instead of being duplicated.
    """
    loc = _loc(tree)
    # Children include: <assign_target> <op token> <expr>
    target_node = next(child for child in tree.children if isinstance(child, Tree))
    op_tok = next(
        child
        for child in tree.children
        if isinstance(child, Token)
        and child.type
        in {
            "PLUS_EQ",
            "MINUS_EQ",
            "STAR_EQ",
            "SLASH_EQ",
            "PERCENT_EQ",
            "AMP_EQ",
            "BAR_EQ",
            "CARET_EQ",
            "LSHIFT_EQ",
            "SHR_EQ",
        }
    )
    value_node = next(
        child
        for child in tree.children
        if isinstance(child, Tree) and child is not target_node
    )
    target = _build_expr(target_node)
    value = _build_expr(value_node)
    return AugAssignStmt(loc=loc, target=target, op=op_tok.value, value=value)


def _build_for_stmt(tree: Tree) -> ForStmt:
    loc = _loc(tree)
    ident_node = next(
        child
        for child in tree.children
        if (isinstance(child, Token) and child.type in {"NAME", "MOVE"})
        or (isinstance(child, Tree) and _name(child) == "ident")
    )
    name_token = _unwrap_ident(ident_node)
    # `for` statement shape:
    #   for <ident> in <expr> terminator_opt <block>
    #
    # The parse tree includes both the loop binding `ident` and the iterable
    # `expr` as Tree nodes. We must select the iterable expression here, not
    # the binding identifier; otherwise we end up trying to build an expression
    # from the `ident` node and crash on its raw Token child.
    expr_node = next(
        child
        for child in tree.children
        if isinstance(child, Tree) and _name(child) not in {"ident", "block", "terminator_opt"}
    )
    block_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "block")
    iter_expr = _build_expr(expr_node)
    body_stmts = [
        _build_stmt(child) for child in block_node.children if isinstance(child, Tree) and _name(child) == "stmt"
    ]
    body_stmts = [s for s in body_stmts if s is not None]
    return ForStmt(loc=loc, var=name_token.value, iter_expr=iter_expr, body=Block(statements=body_stmts))


def _parse_binding_name(tree: Tree) -> tuple[Token, bool]:
    capture = False
    name_token: Optional[Token] = None
    for child in tree.children:
        if isinstance(child, Token) and child.type in {"NAME", "MOVE"}:
            name_token = child
        elif isinstance(child, Tree):
            if _name(child) == "capture_marker":
                capture = True
            elif _name(child) == "ident":
                name_token = _unwrap_ident(child)
            else:
                # capture_marker child holds the caret token; ignore
                for grand in child.children:
                    if isinstance(grand, Token) and grand.type == "CARET":
                        capture = True
    if name_token is None:
        raise ValueError("binding name missing identifier")
    return name_token, capture


def _parse_alias(tree: Tree) -> str:
	string_token = next(
		child for child in tree.children if isinstance(child, Token) and child.type == "STRING"
	)
	return _decode_string_token(string_token)


def _binder_is_mutable(node: Tree) -> bool:
    for child in node.children:
        if isinstance(child, Token):
            if child.type == "VAR":
                return True
    return False


def _build_return_stmt(tree: Tree) -> ReturnStmt:
	loc = _loc(tree)
	children = [child for child in tree.children if not isinstance(child, Token) or child.type != "RETURN"]
	value = _build_expr(children[0]) if children else None
	return ReturnStmt(loc=loc, value=value)


def _build_rethrow_stmt(tree: Tree) -> RethrowStmt:
	loc = _loc(tree)
	return RethrowStmt(loc=loc)


def _build_raise_stmt(tree: Tree) -> RaiseStmt:
    loc = _loc(tree)
    children = [
        child
        for child in tree.children
        if not isinstance(child, Token) or child.type not in {"RAISE", "THROW"}
    ]
    idx = 0
    domain: Optional[str] = None
    if idx < len(children) and _name(children[idx]) == "domain_clause":
        domain = children[idx].children[0].value
        idx += 1
    value = _build_expr(children[idx])
    return RaiseStmt(loc=loc, value=value, domain=domain)


def _build_expr_stmt(tree: Tree) -> ExprStmt:
    loc = _loc(tree)
    expr = _build_expr(tree.children[0])
    return ExprStmt(loc=loc, value=expr)


def _build_struct_def(tree: Tree) -> StructDef:
    loc = _loc(tree)
    name_token = tree.children[0]
    body = tree.children[1]
    field_nodes = _collect_struct_fields(body)
    fields = [_build_struct_field(node) for node in field_nodes]
    return StructDef(name=name_token.value, fields=fields, loc=loc)


def _collect_struct_fields(tree: Tree) -> List[Tree]:
    """
    Collect `struct_field` nodes from a struct body, preserving source order.

    Struct declarations have two surface forms:
      - tuple form: `struct S(x: Int, y: Int)`
      - block form: `struct S { x: Int, y: Int }`

    The parser needs the declared field order to be stable and predictable:
      - it defines the positional constructor argument order (`S(1, 2)`),
      - it defines the layout order in the TypeTable (field indices),
      - it defines the LLVM struct layout order (GEP indices).

    This walk is intentionally order-preserving (left-to-right pre-order). Avoid
    stack-based DFS here: it reverses field ordering and silently miscompiles
    field access/borrows.
    """
    result: List[Tree] = []

    def walk(node: object) -> None:
        if not isinstance(node, Tree):
            return
        if _name(node) == "struct_field":
            result.append(node)
            return
        for child in node.children:
            walk(child)

    walk(tree)
    return result


def _build_struct_field(tree: Tree) -> StructField:
    name_token = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
    type_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr")
    return StructField(name=name_token.value, type_expr=_build_type_expr(type_node))


def _build_import_stmt(tree: Tree) -> ImportStmt:
    loc = _loc(tree)
    path_node = tree.children[0]
    parts = [child.value for child in path_node.children if isinstance(child, Token) and child.type == "NAME"]
    alias = None
    if len(tree.children) > 1 and isinstance(tree.children[1], Tree) and _name(tree.children[1]) == "alias_clause":
        alias_tok = next((c for c in tree.children[1].children if isinstance(c, Token) and c.type == "NAME"), None)
        if alias_tok:
            alias = alias_tok.value
    return ImportStmt(loc=loc, path=parts, alias=alias)


def _build_if_stmt(tree: Tree) -> IfStmt:
    loc = _loc(tree)
    condition_node = None
    then_block_node = None
    else_block_node = None
    for child in tree.children:
        if not isinstance(child, Tree):
            continue
        name = _name(child)
        if condition_node is None and name != "terminator_opt":
            condition_node = child
            continue
        if then_block_node is None and name == "block":
            then_block_node = child
            continue
        if name == "else_clause":
            for grand in child.children:
                if isinstance(grand, Tree) and _name(grand) == "block":
                    else_block_node = grand
                    break
            continue
        if name == "block":
            else_block_node = child
            break
    if condition_node is None or then_block_node is None:
        raise ValueError("malformed if statement")
    condition = _build_expr(condition_node)
    then_block = _build_block(then_block_node)
    else_block = _build_block(else_block_node) if else_block_node else None
    return IfStmt(loc=loc, condition=condition, then_block=then_block, else_block=else_block)


def _build_while_stmt(tree: Tree) -> ast.WhileStmt:
    loc = _loc(tree)
    condition_node = None
    body_node = None
    for child in tree.children:
        if isinstance(child, Tree) and _name(child) == "terminator_opt":
            continue
        if condition_node is None and isinstance(child, Tree):
            condition_node = child
            continue
        if isinstance(child, Tree) and _name(child) == "block":
            body_node = child
            break
    if condition_node is None or body_node is None:
        raise ValueError("malformed while statement")
    condition = _build_expr(condition_node)
    body = _build_block(body_node)
    return WhileStmt(loc=loc, condition=condition, body=body)


def _build_break_stmt(tree: Tree) -> ast.BreakStmt:
    return BreakStmt(loc=_loc(tree))


def _build_continue_stmt(tree: Tree) -> ast.ContinueStmt:
    return ContinueStmt(loc=_loc(tree))


def _build_try_stmt(tree: Tree) -> TryStmt:
    loc = _loc(tree)
    try_block = None
    catches: list[CatchClause] = []
    for child in tree.children:
        if not isinstance(child, Tree):
            continue
        name = _name(child)
        if try_block is None and name != "catch_clause":
            # The try statement supports both block form:
            #   try { ... } catch ...
            # and call/expression shorthand:
            #   try foo() catch ...
            #
            # For the shorthand we wrap the expression into a single ExprStmt
            # so the rest of the pipeline continues to treat TryStmt.body as a block.
            if name == "block":
                try_block = _build_block(child)
            else:
                expr = _build_expr(child)
                try_block = Block(statements=[ExprStmt(loc=_loc(child), value=expr)])
        elif name == "catch_clause":
            catches.append(_build_catch_clause(child))
    if try_block is None:
        raise ValueError("try statement missing body")
    if not catches:
        raise ValueError("try statement requires at least one catch clause")
    return TryStmt(loc=loc, body=try_block, catches=catches)


def _fqn_from_tree(tree: Tree) -> str:
    parts: list[str] = []
    for child in tree.children:
        if isinstance(child, Token):
            parts.append(child.value)
        elif isinstance(child, Tree):
            parts.append(_fqn_from_tree(child))
    return "".join(parts)


def _build_catch_clause(tree: Tree) -> CatchClause:
	event: str | None = None
	binder: str | None = None
	block_node = None
	for child in tree.children:
		if isinstance(child, Tree):
			name = _name(child)
			if name in {"catch_pattern", "catch_event", "catch_all", "catch_all_empty"}:
				event_node = next((c for c in child.children if isinstance(c, Tree) and _name(c) == "event_fqn"), None)
				if event_node is not None:
					event = _fqn_from_tree(event_node)
				ident_node = next(
					(
						c
						for c in child.children
						if (isinstance(c, Token) and c.type in {"NAME", "MOVE"})
						or (isinstance(c, Tree) and _name(c) == "ident")
					),
					None,
				)
				if ident_node is not None:
					binder = _unwrap_ident(ident_node).value
			elif name == "block":
				block_node = child
	if block_node is None:
		raise ValueError("catch clause missing block")
	block = _build_block(block_node)
	return CatchClause(event=event, binder=binder, block=block)


def _build_expr(node) -> Expr:
    if isinstance(node, Tree):
        # Some grammar nodes carry a Token as .data (e.g., *_tail); unwrap selected
        # cases, otherwise treat the token value as the rule name.
        if isinstance(node.data, Token):
            name = node.data.value
        else:
            name = _name(node)
    else:
        raise TypeError(f"Unexpected node type: {type(node)}")

    if name in {
        "logic_and_tail",
        "logic_or_tail",
        # These tail nodes are part of left-associative binary chains. They
        # should never be built as standalone expressions, but the parser may
        # surface them as intermediate nodes during error recovery. Treat them
        # as “yield the RHS expression” so callers don’t silently drop terms.
        "pipeline_tail",
        "shift_tail",
        "bit_or_tail",
        "bit_xor_tail",
        "bit_and_tail",
    }:
        rhs = next((c for c in node.children if isinstance(c, Tree)), None)
        if rhs is None:
            raise TypeError(f"Unexpected tail shape: {node.children!r}")
        return _build_expr(rhs)

    if name == "logic_or":
        return _fold_chain(node, "logic_or_tail")
    if name == "try_catch_expr":
        return _build_try_catch_expr(node)
    if name == "match_expr":
        return _build_match_expr(node)
    if name == "ternary":
        return _build_ternary(node)
    if name == "pipeline":
        # Pipeline operators are a left-associative chain of `pipeline_tail`
        # nodes (token + rhs).
        return _fold_chain(node, "pipeline_tail")
    if name == "exception_ctor":
        return _build_exception_ctor(node)
    if name == "logic_and":
        return _fold_chain(node, "logic_and_tail")
    if name == "bit_or":
        return _fold_chain(node, "bit_or_tail")
    if name == "bit_xor":
        return _fold_chain(node, "bit_xor_tail")
    if name == "bit_and":
        return _fold_chain(node, "bit_and_tail")
    if name == "equality":
        return _fold_chain(node, "equality_tail")
    if name == "comparison":
        return _fold_chain(node, "comparison_tail")
    if name == "shift":
        return _fold_chain(node, "shift_tail")
    if name == "sum":
        return _fold_chain(node, "sum_tail")
    if name == "term":
        return _fold_chain(node, "term_tail")
    if name == "factor":
        return _build_expr(node.children[0])
    if name == "borrow":
        mut = any(isinstance(child, Token) and child.type == "MUT" for child in node.children)
        target = _build_expr(next(child for child in node.children if isinstance(child, Tree)))
        return Unary(loc=_loc(node), op="&mut" if mut else "&", operand=target)
    if name == "bit_not":
        target = next((c for c in node.children if isinstance(c, Tree)), None)
        if target is None:
            raise TypeError(f"bit_not expects an operand, got {node.children!r}")
        expr = _build_expr(target)
        return Unary(loc=_loc(node), op="~", operand=expr)
    if name == "deref":
        # Pointer dereference: `*expr`. The type checker restricts this to
        # references, and assignment restricts it to `*place = ...` for `&mut`.
        target = next((c for c in node.children if isinstance(c, Tree)), None)
        if target is None:
            raise TypeError(f"deref expects an operand, got {node.children!r}")
        expr = _build_expr(target)
        return Unary(loc=_loc(node), op="*", operand=expr)
    if name == "move_op":
        # Ownership transfer marker: `move <expr>`.
        #
        # Note: this is syntax-only today; semantic enforcement (no move from
        # borrows/vals, moved-from state) is handled by later phases once
        # move-only types are fully implemented.
        target = next((c for c in node.children if isinstance(c, Tree)), None)
        if target is None:
            raise TypeError(f"move_op expects an operand, got {node.children!r}")
        expr = _build_expr(target)
        return Move(loc=_loc(node), value=expr)
    if name == "postfix":
        return _build_postfix(node)
    if name == "leading_dot":
        return _build_leading_dot(node)
    if name == "primary":
        return _build_expr(node.children[0])
    if name == "neg":
        # Unary minus: children include the '-' token and the operand expression.
        target = next((c for c in node.children if isinstance(c, Tree)), None)
        if target is None:
            raise TypeError(f"neg expects an operand, got {node.children!r}")
        expr = _build_expr(target)
        return Unary(loc=_loc(node), op="-", operand=expr)
    if name == "not_op":
        # Logical negation: children include the '!' token (or 'not') and the operand.
        target = next((c for c in node.children if isinstance(c, Tree)), None)
        if target is None:
            raise TypeError(f"not_op expects an operand, got {node.children!r}")
        expr = _build_expr(target)
        return Unary(loc=_loc(node), op="not", operand=expr)
    if name == "var":
        ident_token = _unwrap_ident(node.children[0])
        return Name(loc=_loc(node), ident=ident_token.value)
    if name == "placeholder":
        return Placeholder(loc=_loc(node))
    if name == "int_lit":
        return Literal(loc=_loc(node), value=int(node.children[0].value))
    if name == "float_lit":
        return Literal(loc=_loc(node), value=float(node.children[0].value))
    if name == "str_lit":
        raw_tok = node.children[0]
        return Literal(loc=_loc(node), value=_decode_string_token(raw_tok))
    if name == "fstr_lit":
        # Children: FSTRING_PREFIX token and STRING token.
        string_tok = next((c for c in node.children if isinstance(c, Token) and c.type == "STRING"), None)
        if string_tok is None:
            raise ValueError("f-string missing STRING token")
        return _parse_fstring(_loc(node), string_tok)
    if name == "true_lit":
        return Literal(loc=_loc(node), value=True)
    if name == "false_lit":
        return Literal(loc=_loc(node), value=False)
    if name == "array_literal":
        return _build_array_literal(node)
    if node.children:
        return _build_expr(node.children[0])
    raise ValueError(f"Unsupported expression node: {name}")


def _build_array_literal(tree: Tree) -> ArrayLiteral:
    elements = [_build_expr(child) for child in tree.children if isinstance(child, Tree)]
    return ArrayLiteral(loc=_loc(tree), elements=elements)


def _apply_index_suffix(base: Expr, suffix_node: Tree) -> Index:
    idx_child = next((c for c in suffix_node.children if isinstance(c, Tree)), None)
    if idx_child is None:
        raise ValueError("index suffix missing expression")
    index_expr = _build_expr(idx_child)
    return Index(loc=_loc(suffix_node), value=base, index=index_expr)


def _fold_chain(tree: Tree, tail_name: str) -> Expr:
    child_nodes = [child for child in tree.children if isinstance(child, Tree)]
    head = _build_expr(child_nodes[0])
    result = head
    for child in child_nodes[1:]:
        if _name(child) != tail_name:
            continue
        result = _binary_tail(result, child)
    return result


def _binary_tail(left: Expr, tail: Tree) -> Expr:
    op_token = tail.children[0]
    right = _build_expr(tail.children[1])
    return Binary(loc=_loc_from_token(op_token), op=op_token.value, left=left, right=right)


def _build_pipeline(tree: Tree) -> Expr:
    children = list(tree.children)
    if not children:
        raise ValueError("pipeline requires at least one child")
    idx = 0
    result = _build_expr(children[idx])
    idx += 1
    while idx < len(children):
        token = children[idx]
        idx += 1
        if not isinstance(token, Token):
            raise ValueError("Expected pipeline operator token")
        right = _build_expr(children[idx])
        idx += 1
        result = Binary(loc=_loc_from_token(token), op=token.value, left=result, right=right)
    return result


def _build_try_catch_expr(tree: Tree) -> TryCatchExpr:
    parts = [child for child in tree.children if isinstance(child, Tree)]
    if not parts:
        raise ValueError("try_catch_expr expects attempt and at least one catch arm")

    attempt_node: Tree | None = None
    arm_nodes: list[Tree] = []
    for node in parts:
        name = _name(node)
        if name == "terminator_opt":
            continue
        if name.startswith("catch_expr_"):
            arm_nodes.append(node)
            continue
        if attempt_node is None:
            attempt_node = node
            continue
        raise ValueError("try_catch_expr has unexpected extra expression nodes")

    if attempt_node is None or not arm_nodes:
        raise ValueError("try_catch_expr expects attempt and at least one catch arm")

    attempt = _build_expr(attempt_node)
    arms: List[CatchExprArm] = []
    for arm_node in arm_nodes:
        arm_name = _name(arm_node)
        if arm_name == "catch_expr_event":
            event_node = next((c for c in arm_node.children if isinstance(c, Tree) and _name(c) == "event_fqn"), None)
            if event_node is None:
                raise ValueError("event catch arm requires event")
            binder_node = next(
                (
                    c
                    for c in arm_node.children
                    if (isinstance(c, Token) and c.type in {"NAME", "MOVE"})
                    or (isinstance(c, Tree) and _name(c) == "ident")
                ),
                None,
            )
            if binder_node is None:
                raise ValueError("event catch arm requires binder")
            binder_token = _unwrap_ident(binder_node)
            block_node = next(
                child for child in arm_node.children if isinstance(child, Tree) and _name(child) == "value_block"
            )
            arms.append(
                CatchExprArm(
                    event=_fqn_from_tree(event_node),
                    binder=binder_token.value,
                    block=_build_value_block(block_node),
                )
            )
        elif arm_name == "catch_expr_binder":
            binder_node = next(
                c
                for c in arm_node.children
                if (isinstance(c, Token) and c.type in {"NAME", "MOVE"})
                or (isinstance(c, Tree) and _name(c) == "ident")
            )
            binder_token = _unwrap_ident(binder_node)
            block_node = next(
                child for child in arm_node.children if isinstance(child, Tree) and _name(child) == "value_block"
            )
            arms.append(CatchExprArm(event=None, binder=binder_token.value, block=_build_value_block(block_node)))
        elif arm_name == "catch_expr_block":
            block_node = next(
                child for child in arm_node.children if isinstance(child, Tree) and _name(child) == "value_block"
            )
            arms.append(CatchExprArm(event=None, binder=None, block=_build_value_block(block_node)))
        else:
            raise ValueError(f"unexpected catch expr arm {arm_name}")
    return TryCatchExpr(loc=_loc(tree), attempt=attempt, catch_arms=arms)


def _build_match_expr(tree: Tree) -> MatchExpr:
	"""
	Build a match expression.

	Grammar:
	  match_expr: MATCH expr "{" (match_arm (TERMINATOR | COMMA)*)+ "}"
	"""
	# The scrutinee is the first expression subtree directly under `match_expr`.
	#
	# Note: because `expr` is an inlined rule (`?expr`), Lark does not always
	# materialize it as a distinct `Tree("expr")`. The first child that is a
	# `Tree` and is *not* a `match_arm` is the scrutinee expression.
	scrutinee_node = next(
		(c for c in tree.children if isinstance(c, Tree) and _name(c) != "match_arm"),
		None,
	)
	if scrutinee_node is None:
		raise ValueError("match_expr missing scrutinee expression")
	scrutinee = _build_expr(scrutinee_node)

	arms: list[MatchArm] = []
	for arm_node in (c for c in tree.children if isinstance(c, Tree) and _name(c) == "match_arm"):
		pat: Optional[Tree] = None
		for child in (c for c in arm_node.children if isinstance(c, Tree)):
			child_name = _name(child)
			if child_name == "match_pat":
				pat = next((c for c in child.children if isinstance(c, Tree)), None)
				break
			if child_name in ("match_default", "match_ctor", "match_ctor0"):
				pat = child
				break
		if pat is None:
			raise ValueError("match_arm missing pattern")

		pat_kind = _name(pat)
		ctor: Optional[str] = None
		binders: list[str] = []
		if pat_kind == "match_default":
			ctor = None
		elif pat_kind == "match_ctor":
			name_tok = next(c for c in pat.children if isinstance(c, Token) and c.type == "NAME")
			ctor = name_tok.value
			binders_node = next((c for c in pat.children if isinstance(c, Tree) and _name(c) == "match_binders"), None)
			if binders_node is not None:
				binders = [c.value for c in binders_node.children if isinstance(c, Token) and c.type == "NAME"]
		elif pat_kind == "match_ctor0":
			name_tok = next(c for c in pat.children if isinstance(c, Token) and c.type == "NAME")
			ctor = name_tok.value
		else:
			raise ValueError(f"Unsupported match_pat shape: {pat_kind}")

		body_node = next((c for c in arm_node.children if isinstance(c, Tree) and _name(c) == "match_arm_body"), None)
		if body_node is None:
			raise ValueError("match_arm missing body")
		inner = next((c for c in body_node.children if isinstance(c, Tree)), None)
		if inner is None:
			raise ValueError("match_arm_body missing block")
		block = _build_value_block(inner) if _name(inner) == "value_block" else _build_block(inner)
		arms.append(MatchArm(loc=_loc(arm_node), ctor=ctor, binders=binders, block=block))

	if not arms:
		raise ValueError("match_expr requires at least one arm")
	return MatchExpr(loc=_loc(tree), scrutinee=scrutinee, arms=arms)


def _build_ternary(tree: Tree) -> Ternary:
    parts = [child for child in tree.children if isinstance(child, Tree)]
    if len(parts) != 3:
        raise ValueError("ternary expects condition, then, else expressions")
    cond = _build_expr(parts[0])
    then_expr = _build_expr(parts[1])
    else_expr = _build_expr(parts[2])
    return Ternary(loc=_loc(tree), condition=cond, then_value=then_expr, else_value=else_expr)


def _build_exception_ctor(tree: Tree) -> ExceptionCtor:
	"""
	Exception constructor expression (throw-only):
	  Name(arg0, field = expr, ...)

	This mirrors call argument parsing rules:
	  - Positional arguments must precede keyword arguments.
	"""
	name_tok = next((c for c in tree.children if isinstance(c, Token) and c.type == "NAME"), None)
	if name_tok is None:
		raise ValueError("exception_ctor missing name")

	# Grammar shape: NAME "(" [call_args] ")"
	args_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "call_args"), None)
	args, kwargs = _build_call_args(args_node)
	return ExceptionCtor(name=name_tok.value, args=args, kwargs=kwargs, loc=_loc(tree))


def _build_postfix(tree: Tree) -> Expr:
    if not tree.children:
        raise ValueError("postfix node missing children")
    expr = _build_expr(tree.children[0])
    suffix_nodes = tree.children[1:]
    return _apply_postfix_suffixes(expr, suffix_nodes)


def _build_leading_dot(tree: Tree) -> Expr:
    # Base receiver placeholder with an initial attribute name from the DOT NAME.
    name_tok = next(tok for tok in tree.children if isinstance(tok, Token) and tok.type == "NAME")
    base_loc = _loc(tree)
    expr: Expr = Attr(loc=_loc_from_token(name_tok), value=Placeholder(loc=base_loc), attr=name_tok.value)
    suffix_nodes = [child for child in tree.children if isinstance(child, Tree)]
    return _apply_postfix_suffixes(expr, suffix_nodes)


def _apply_postfix_suffixes(expr: Expr, suffix_nodes: List[Tree]) -> Expr:
    for child in suffix_nodes:
        if not isinstance(child, Tree):
            raise ValueError("Unexpected postfix child token")
        suffix_node = child
        suffix_name = _name(suffix_node)
        if suffix_name == "postfix_suffix" and suffix_node.children:
            suffix_node = suffix_node.children[0]
            suffix_name = _name(suffix_node)
        child = suffix_node
        child_name = suffix_name
        if child_name == "call_suffix":
            args_node = child.children[0] if child.children else None
            args, kwargs = _build_call_args(args_node)
            expr = Call(loc=_loc(child), func=expr, args=args, kwargs=kwargs)
        elif child_name == "attr_suffix":
            attr_token = next(
                token for token in child.children if isinstance(token, Token) and token.type == "NAME"
            )
            expr = Attr(loc=_loc(child), value=expr, attr=attr_token.value)
        elif child_name == "arrow_suffix":
            attr_token = next(
                token for token in child.children if isinstance(token, Token) and token.type == "NAME"
            )
            expr = Attr(loc=_loc(child), value=expr, attr=attr_token.value, op="->")
        elif child_name == "index_suffix":
            expr = _apply_index_suffix(expr, child)
        else:
            raise ValueError(f"Unexpected postfix child: {child_name}")
    return expr


def _build_call_args(node: Tree | None) -> tuple[List[Expr], List[KwArg]]:
    args: List[Expr] = []
    kwargs: List[KwArg] = []
    if node is None:
        return args, kwargs
    positional_done = False
    for arg in node.children:
        if not isinstance(arg, Tree):
            continue
        kind = _name(arg)
        if kind == "kwarg":
            positional_done = True
            kwargs.append(_build_kwarg(arg))
        else:
            if positional_done:
                raise SyntaxError("Positional arguments cannot follow keyword arguments")
            args.append(_build_expr(arg))
    return args, kwargs


def _build_kwarg(tree: Tree) -> KwArg:
    name_token = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
    value_node = next(child for child in tree.children if isinstance(child, Tree))
    value = _build_expr(value_node)
    return KwArg(name=name_token.value, value=value, loc=_loc(tree))


def _loc(tree: Tree) -> Located:
    meta = tree.meta
    return Located(line=meta.line, column=meta.column)


def _loc_from_token(token: Token) -> Located:
    return Located(line=token.line, column=token.column)


def _name(node: Tree | Token) -> str:
    if isinstance(node, Tree):
        data = node.data
        if isinstance(data, Token):
            return data.value
        return data
    if isinstance(node, Token):
        return node.type
    return str(node)
