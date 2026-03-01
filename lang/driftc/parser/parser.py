from __future__ import annotations

import ast
import codecs
from pathlib import Path
from typing import List, Optional

from lark import Lark, Token, Tree

from .ast import (
    ArrayLiteral,
    MapEntry,
    MapLiteral,
    AssignStmt,
    AssertStmt,
    AugAssignStmt,
    Attr,
    QualifiedMember,
    Binary,
    Block,
    BlockStmt,
    Call,
    MacroCall,
    ConstDef,
    TypeAliasDef,
    Copy,
    CatchClause,
    ExceptionArg,
    ExceptionDef,
    Cast,
    Expr,
    ExprStmt,
    ForCountStmt,
    ForStmt,
    FunctionDef,
    IfStmt,
    ImportStmt,
    UseTraitStmt,
    TraitRef,
    ExportItem,
    ExportModuleStar,
    ExportName,
    ExportStmt,
    ImplementDef,
    Index,
    KwArg,
    LetStmt,
    Literal,
    LocalConstStmt,
    Lambda,
    LambdaCapture,
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
    TraitDef,
    TraitMethodSig,
    InterfaceDef,
    InterfaceMethodSig,
    RequireClause,
    TraitExpr,
    TraitSubject,
    SelfRef,
    TypeNameRef,
    TraitIs,
    TraitAnd,
    TraitOr,
    TraitNot,
    TypeExpr,
    Ternary,
    TryCatchExpr,
    CatchExprArm,
    ExceptionCtor,
    TypeApp,
    TryStmt,
    WhileStmt,
    BreakStmt,
    ContinueStmt,
    UnsafeBlockStmt,
    UnsafeExpr,
    Unary,
    UintLiteral,
    FString,
    FStringHole,
    VariantDef,
    VariantArm,
    VariantField,
    MatchExpr,
    MatchArm,
    YieldExpr,
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

	The grammar defines `ident: NAME | MOVE | COPY` so callers may receive:
	- a `Token` (`NAME`, `MOVE`, or `COPY`) directly, or
	- a `Tree('ident', [Token(...)])` wrapper.
	"""
	if isinstance(node, Token):
		if node.type in {"NAME", "MOVE", "COPY"}:
			return node
		raise TypeError(f"Expected identifier token, got {node.type}")
	if isinstance(node, Tree) and _name(node) == "ident":
		tok = next((c for c in node.children if isinstance(c, Token)), None)
		if tok is None:
			raise TypeError("ident node missing token child")
		if tok.type not in {"NAME", "MOVE", "COPY"}:
			raise TypeError(f"Expected NAME/MOVE/COPY token in ident, got {tok.type}")
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
		return Located(line=base_line, column=base_col + offset, file=_CURRENT_FILE)

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
    always_accept = ("SEMI",)

    def process(self, stream):
        for token in stream:
            if token.type == "SEMI":
                yield Token.new_borrow_pos("TERMINATOR", token.value, token)
                continue
            yield token


class QualifiedTypeArgInserter:
    """
    Disambiguate `<...>` as type arguments in expression position for qualified members.

    We want to support:
      - `TypeName<T>::Ctor(...)`
      - `TypeName::Ctor<type T>(...)`

    …without making `<` ambiguous with the `<` comparison operator (e.g. `i < 3`).

    This post-lexer rewrites generic-angle spans for two deterministic cases:
      - split `>>` inside `<type ...>` call spans
      - pre-`::` type args: `Type<T>::Ctor` -> `QUAL_TYPE_LT/QUAL_TYPE_GT`
    """

    def process(self, stream):
        recent: list[Token] = []
        call_angle_depth = 0
        type_mode = False
        type_mode_next = False
        type_angle_depth = 0
        type_square_depth = 0
        impl_header = False
        impl_typeparam_depth = 0
        struct_header = False
        fn_header = False
        fn_sig = False
        lambda_pending = False
        lambda_sig = False
        allowed_in_type_args = {
            # Type refs.
            "NAME",
            "DOT",
            "DCOLON",
            # Type arg lists.
            "COMMA",
            "LT",
            "GT",
            "SHR",
            # Ref types.
            "AMP",
            "MUT",
            # Square type args (e.g., Array[T], if enabled).
            "LSQB",
            "RSQB",
        }
        type_mode_start = {"COLON", "FOR"}
        type_mode_end = {
            "COMMA",
            "LPAR",
            "RPAR",
            "RBRACE",
            "LBRACE",
            "EQUAL",
            "SEMI",
            "NEWLINE",
            "BAR",
            "FOR",
            "REQUIRE",
        }

        def _emit(tok: Token):
            recent.append(tok)
            if len(recent) > 4:
                recent.pop(0)
            return tok

        def _enter_type_mode() -> None:
            nonlocal type_mode, type_mode_next, type_angle_depth, type_square_depth
            if type_mode:
                return
            type_mode = True
            type_mode_next = False
            type_angle_depth = 0
            type_square_depth = 0

        def _is_pre_base_context(_: Token) -> bool:
            # ... NAME < ...
            return len(recent) >= 1 and recent[-1].type == "NAME"

        def _is_type_arg_span(tokens: list[Token]) -> bool:
            # Only accept spans that look like a type-argument list by token shape.
            # This intentionally rejects obvious expression operators.
            allowed = {
                "NAME",
                "DOT",
                "DCOLON",
                "COMMA",
                "LT",
                "GT",
                "SHR",
                "AMP",
                "MUT",
                "LSQB",
                "RSQB",
            }
            for tok in tokens[1:-1]:
                if tok.type not in allowed:
                    return False
            return True

        def _emit_pre_type_args(first_lt: Token) -> bool:
            # Buffer the <...> span and rewrite to QUAL_TYPE_LT/GT if followed by ::.
            buf: list[Token] = [first_lt]
            depth = 1
            while True:
                try:
                    tok = pushback.pop() if pushback else next(it)
                except StopIteration:
                    for t in buf:
                        yield _emit(t)
                    return True

                if tok.type not in allowed_in_type_args:
                    for t in buf:
                        yield _emit(t)
                    pushback.append(tok)
                    return True

                buf.append(tok)
                if tok.type == "LT":
                    depth += 1
                    continue
                if tok.type == "GT":
                    depth -= 1
                elif tok.type == "SHR":
                    depth -= 2

                if depth > 0:
                    continue
                if depth < 0:
                    for t in buf:
                        yield _emit(t)
                    return True

                try:
                    next_tok = pushback.pop() if pushback else next(it)
                except StopIteration:
                    for t in buf:
                        yield _emit(t)
                    return True

                commit = next_tok.type == "DCOLON" and _is_type_arg_span(buf)
                if commit:
                    depth = 0
                    for t in buf:
                        if t.type == "LT":
                            if depth == 0:
                                yield _emit(Token.new_borrow_pos("QUAL_TYPE_LT", t.value, t))
                            else:
                                yield _emit(t)
                            depth += 1
                            continue
                        if t.type == "GT":
                            if depth == 1:
                                yield _emit(Token.new_borrow_pos("QUAL_TYPE_GT", t.value, t))
                            else:
                                yield _emit(t)
                            depth -= 1
                            continue
                        if t.type == "SHR":
                            for _ in range(2):
                                if depth == 1:
                                    yield _emit(Token.new_borrow_pos("QUAL_TYPE_GT", ">", t))
                                else:
                                    yield _emit(Token.new_borrow_pos("GT", ">", t))
                                depth -= 1
                            continue
                        yield _emit(t)
                else:
                    for t in buf:
                        yield _emit(t)
                pushback.append(next_tok)
                return True

        it = iter(stream)
        pushback: list[Token] = []

        while True:
            try:
                token = pushback.pop() if pushback else next(it)
            except StopIteration:
                break

            if type_mode_next:
                _enter_type_mode()

            tt = token.type
            if call_angle_depth:
                if tt in {"LT", "TYPE_LT", "QUAL_TYPE_LT"}:
                    call_angle_depth += 1
                    yield _emit(token)
                    continue
                if tt in {"GT", "TYPE_GT", "QUAL_TYPE_GT"}:
                    call_angle_depth -= 1
                    yield _emit(token)
                    if call_angle_depth <= 0:
                        call_angle_depth = 0
                    continue
                if tt == "SHR":
                    for _ in range(2):
                        yield _emit(Token.new_borrow_pos("GT", ">", token))
                        call_angle_depth -= 1
                        if call_angle_depth <= 0:
                            call_angle_depth = 0
                            break
                    continue
                yield _emit(token)
                continue
            if impl_header:
                if tt in {"LT", "TYPE_LT"}:
                    impl_typeparam_depth += 1
                elif tt in {"GT", "TYPE_GT"} and impl_typeparam_depth:
                    impl_typeparam_depth -= 1
                elif tt == "SHR" and impl_typeparam_depth >= 2:
                    for _ in range(2):
                        impl_typeparam_depth -= 1
                        yield _emit(Token.new_borrow_pos("GT", ">", token))
                    continue
                if impl_typeparam_depth == 0 and tt == "NAME":
                    impl_header = False
                    _enter_type_mode()
                yield _emit(token)
                continue

            if struct_header and tt == "NAME":
                struct_header = False
                _enter_type_mode()
                yield _emit(token)
                continue

            if fn_header and tt == "NAME":
                fn_header = False
                _enter_type_mode()
                yield _emit(token)
                continue

            if tt == "IMPLEMENT":
                impl_header = True
                yield _emit(token)
                continue

            if tt == "CALL_TYPE_LT":
                yield _emit(token)
                call_angle_depth = 1
                continue

            if tt == "STRUCT":
                struct_header = True
                yield _emit(token)
                continue

            if tt == "FN_KW":
                fn_header = True
                fn_sig = True
                yield _emit(token)
                continue

            if tt == "BAR":
                prev_tt = recent[-1].type if recent else None
                lambda_start_prev = {
                    None,
                    "LPAR",
                    "LBRACE",
                    "COMMA",
                    "EQUAL",
                    "COLON",
                    "TERMINATOR",
                    "RETURN",
                    "IF",
                    "ELSE",
                    "WHILE",
                    "FOR",
                    "TRY",
                    "MATCH",
                    "PIPE_FWD",
                    "PIPE_REV",
                }
                if lambda_pending:
                    lambda_pending = False
                    lambda_sig = True
                elif prev_tt in lambda_start_prev:
                    lambda_pending = True

            if type_mode:
                if tt in {"LT", "TYPE_LT", "QUAL_TYPE_LT"}:
                    type_angle_depth += 1
                    yield _emit(token)
                    continue
                if tt in {"GT", "TYPE_GT", "QUAL_TYPE_GT"}:
                    if type_angle_depth > 0:
                        type_angle_depth -= 1
                    yield _emit(token)
                    if type_angle_depth == 0 and type_square_depth == 0 and tt in type_mode_end:
                        type_mode = False
                    continue
                if tt == "LSQB":
                    type_square_depth += 1
                    yield _emit(token)
                    continue
                if tt == "RSQB":
                    if type_square_depth > 0:
                        type_square_depth -= 1
                    yield _emit(token)
                    continue
                if tt == "SHR" and type_angle_depth >= 1:
                    for i in range(2):
                        if type_angle_depth > 0:
                            type_angle_depth -= 1
                        yield _emit(Token.new_borrow_pos("GT", ">", token))
                        if type_angle_depth == 0 and type_square_depth == 0:
                            type_mode = False
                            if i == 0:
                                pushback.append(Token.new_borrow_pos("GT", ">", token))
                            break
                    continue
                yield _emit(token)
                if type_angle_depth == 0 and type_square_depth == 0 and tt in type_mode_end:
                    type_mode = False
                    if tt in type_mode_start:
                        type_mode_next = True
                continue

            if tt == "LT":
                is_pre = _is_pre_base_context(token)
                if is_pre:
                    yield from _emit_pre_type_args(token)
                    continue
            yield _emit(token)
            if tt in type_mode_start or (tt == "ARROW" and (fn_sig or lambda_sig)):
                type_mode_next = True
            if tt == "FATARROW":
                lambda_sig = False
            if tt in {"LBRACE", "TERMINATOR"}:
                fn_sig = False
            continue


class DriftPostLex:
	"""Combined post-lexer: type-arg disambiguation, then terminator insertion."""

	# Lark may drop tokens that aren't referenced in the grammar unless the
	# post-lexer asks to keep them. We need semicolons for terminator insertion.
	always_accept = TerminatorInserter.always_accept

	def __init__(self) -> None:
		self._type_args = QualifiedTypeArgInserter()
		self._terminators = TerminatorInserter()

	def process(self, stream):
		return self._terminators.process(self._type_args.process(stream))



_PARSER = Lark(
    _GRAMMAR_SRC,
    parser="lalr",
    lexer="basic",
    start="program",
    propagate_positions=True,
    maybe_placeholders=False,
    postlex=DriftPostLex(),
)

_CURRENT_FILE: str | None = None
_CURRENT_FILE_ID: int | None = None


def parse_program(source: str, *, filename: str | None = None, file_id: int | None = None) -> Program:
    global _CURRENT_FILE, _CURRENT_FILE_ID
    prev_file = _CURRENT_FILE
    prev_file_id = _CURRENT_FILE_ID
    _CURRENT_FILE = filename
    _CURRENT_FILE_ID = file_id
    try:
        tree = _PARSER.parse(source)
        return _build_program(tree)
    finally:
        _CURRENT_FILE = prev_file
        _CURRENT_FILE_ID = prev_file_id


class ModuleDeclError(ValueError):
	"""
	User-facing error for invalid `module ...` declarations.

	The driver converts this into a pinned parser-phase diagnostic (it is not an
	internal compiler bug).
	"""

	def __init__(self, message: str, *, loc: object | None) -> None:
		super().__init__(message)
		self.loc = loc


class QualifiedMemberParseError(ValueError):
	"""
	User-facing parse error for invalid `TypeRef::member` syntax.

	This is raised from the parser AST builder (not from the grammar) so the
	driver can report a pinned parser diagnostic instead of crashing with a raw
	Python exception.
	"""

	def __init__(self, message: str, *, loc: object | None) -> None:
		super().__init__(message)
		self.loc = loc


def _build_program(tree: Tree) -> Program:
	functions: List[FunctionDef] = []
	consts: List["ConstDef"] = []
	type_aliases: List["TypeAliasDef"] = []
	implements: List[ImplementDef] = []
	traits: List[TraitDef] = []
	interfaces: List[InterfaceDef] = []
	imports: List[ImportStmt] = []
	exports: List[ExportStmt] = []
	used_traits: List[TraitRef] = []
	statements: List[ExprStmt | LetStmt | ReturnStmt | RaiseStmt] = []
	structs: List[StructDef] = []
	exceptions: List[ExceptionDef] = []
	variants: List[VariantDef] = []
	module_name: Optional[str] = None
	module_loc: Optional[Located] = None
	seen_non_module_item = False
	for child in tree.children:
		if not isinstance(child, Tree):
			continue
		kind = _name(child)
		is_test_only = False
		is_intrinsic = False
		if kind == "item":
			is_test_only = _has_test_build_only_marker(child)
			is_intrinsic = _has_intrinsic_marker(child)
			child = _strip_item_markers(child)
			kind = _name(child)
		if kind == "module_decl":
			if seen_non_module_item:
				raise ModuleDeclError(
					"`module ...` must be the first top-level declaration in the file",
					loc=_loc(child),
				)
			if module_name is not None:
				raise ModuleDeclError(
					"duplicate `module ...` declaration in the same file",
					loc=_loc(child),
				)
			module_name = _build_module_decl(child)
			module_loc = _loc(child)
			continue
		seen_non_module_item = True
		if kind == "pub_item":
			pub_child = next((c for c in child.children if isinstance(c, Tree)), None)
			if pub_child is None:
				continue
			kind = _name(pub_child)
			child = pub_child
			is_pub = True
		else:
			is_pub = False
		if kind == "func_def":
			fn = _build_function(child, allow_missing_body=is_intrinsic)
			fn.is_pub = is_pub
			fn.test_build_only = is_test_only
			fn.is_intrinsic = is_intrinsic
			functions.append(fn)
		elif kind == "const_def":
			const_def = _build_const_def(child)
			const_def.is_pub = is_pub
			const_def.test_build_only = is_test_only
			consts.append(const_def)
		elif kind == "type_alias_def":
			alias_def = _build_type_alias_def(child)
			alias_def.is_pub = is_pub
			alias_def.test_build_only = is_test_only
			type_aliases.append(alias_def)
		elif kind == "implement_def":
			impl = _build_implement_def(child)
			impl.is_pub = is_pub
			impl.test_build_only = is_test_only
			implements.append(impl)
		elif kind == "struct_def":
			struct_def = _build_struct_def(child)
			struct_def.is_pub = is_pub
			struct_def.test_build_only = is_test_only
			structs.append(struct_def)
		elif kind == "exception_def":
			exc = _build_exception_def(child)
			exc.is_pub = is_pub
			exc.test_build_only = is_test_only
			exceptions.append(exc)
		elif kind == "variant_def":
			var_def = _build_variant_def(child)
			var_def.is_pub = is_pub
			var_def.test_build_only = is_test_only
			variants.append(var_def)
		elif kind == "trait_def":
			tr_def = _build_trait_def(child)
			tr_def.is_pub = is_pub
			tr_def.test_build_only = is_test_only
			traits.append(tr_def)
		elif kind == "interface_def":
			if_def = _build_interface_def(child)
			if_def.is_pub = is_pub
			if_def.test_build_only = is_test_only
			interfaces.append(if_def)
		else:
			stmt = _build_stmt(child)
			if stmt is None:
				continue
			if isinstance(stmt, ImportStmt):
				imports.append(stmt)
			elif isinstance(stmt, ExportStmt):
				exports.append(stmt)
			elif isinstance(stmt, UseTraitStmt):
				used_traits.append(stmt.trait)
			else:
				statements.append(stmt)
	return Program(
		functions=functions,
		consts=consts,
		type_aliases=type_aliases,
		implements=implements,
		traits=traits,
		interfaces=interfaces,
		imports=imports,
		exports=exports,
		used_traits=used_traits,
		statements=statements,
		structs=structs,
		exceptions=exceptions,
		variants=variants,
		module=module_name,
		module_loc=module_loc,
	)


def _build_const_def(tree: Tree) -> "ConstDef":
	"""
	Build a top-level constant definition.

	Grammar:
	  const_def: CONST NAME COLON type_expr EQUAL expr TERMINATOR
	"""
	loc = _loc(tree)
	name_tok = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
	type_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr")
	# `expr` is a rule alias (`?expr`), so the parse tree usually contains the
	# concrete expression rule node directly (e.g., `postfix`, `sum`, ...), not an
	# intermediate `expr` node.
	expr_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) != "type_expr")
	return ConstDef(
		loc=loc,
		name=name_tok.value,
		type_expr=_build_type_expr(type_node),
		value=_build_expr(expr_node),
	)


def _build_local_const_stmt(tree: Tree) -> "LocalConstStmt":
	"""
	Build a block-scope constant declaration.

	Grammar:
	  local_const_stmt: CONST NAME COLON type_expr EQUAL expr
	"""
	loc = _loc(tree)
	name_tok = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
	type_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr")
	expr_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) != "type_expr")
	return LocalConstStmt(
		loc=loc,
		name=name_tok.value,
		type_expr=_build_type_expr(type_node),
		value=_build_expr(expr_node),
	)


def _build_type_alias_def(tree: Tree) -> "TypeAliasDef":
	"""
	Build a top-level type alias definition.

	Grammar:
	  type_alias_def: TYPE NAME type_params? EQUAL type_expr TERMINATOR
	"""
	loc = _loc(tree)
	name_tok = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
	type_params_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "type_params"), None)
	type_params: list[str] = []
	type_param_locs: list[Located] = []
	if type_params_node is not None:
		for tok in type_params_node.children:
			if isinstance(tok, Token) and tok.type == "NAME":
				type_params.append(tok.value)
				type_param_locs.append(Located(tok.line, tok.column, file=_CURRENT_FILE))
	type_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr")
	return TypeAliasDef(
		loc=loc,
		name=name_tok.value,
		type_params=type_params,
		type_param_locs=type_param_locs,
		target=_build_type_expr(type_node),
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
		tombstone = any(
			isinstance(child, Tree) and _name(child) == "tombstone_marker" for child in arm_node.children
		)
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
		arms.append(VariantArm(name=arm_name_token.value, fields=fields, tombstone=tombstone, loc=_loc(arm_node)))
	return VariantDef(name=name_token.value, type_params=type_params, arms=arms, loc=loc)


def _build_exception_arg(tree: Tree) -> ExceptionArg:
    if _name(tree) != "exception_param":
        raise ValueError("expected exception_param")
    name_token = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
    type_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr")
    return ExceptionArg(name=name_token.value, type_expr=_build_type_expr(type_node))


def _build_trait_expr(node: Tree) -> TraitExpr:
	name = _name(node)
	if name == "trait_expr":
		child = next((c for c in node.children if isinstance(c, Tree)), None)
		if child is None:
			raise ValueError("trait_expr missing child")
		return _build_trait_expr(child)
	if name in {"trait_or", "trait_and"}:
		op_name = "or" if name == "trait_or" else "and"
		current: TraitExpr | None = None
		pending_op: str | None = None
		for child in node.children:
			if isinstance(child, Token) and child.type in {"OR", "AND"}:
				pending_op = child.value
				continue
			if not isinstance(child, Tree):
				continue
			rhs = _build_trait_expr(child)
			if current is None:
				current = rhs
				continue
			if pending_op is None:
				raise ValueError("trait boolean chain missing operator")
			if pending_op == "and":
				current = TraitAnd(loc=_loc(node), left=current, right=rhs)
			elif pending_op == "or":
				current = TraitOr(loc=_loc(node), left=current, right=rhs)
			else:
				raise ValueError(f"unexpected trait boolean op {pending_op}")
			pending_op = None
		if current is None:
			raise ValueError("trait boolean chain missing operands")
		return current
	if name == "trait_not":
		child = next((c for c in node.children if isinstance(c, Tree)), None)
		if child is None:
			raise ValueError("trait_not missing operand")
		inner = _build_trait_expr(child)
		if _name(child) == "trait_not" or any(isinstance(c, Token) and c.value == "not" for c in node.children):
			return TraitNot(loc=_loc(node), expr=inner)
		return inner
	if name == "trait_atom":
		child_expr = next(
			(
				c
				for c in node.children
				if isinstance(c, Tree) and _name(c) in {"trait_expr", "trait_or", "trait_and", "trait_not", "trait_atom"}
			),
			None,
		)
		if child_expr is not None:
			return _build_trait_expr(child_expr)
		subject_tok = next((c for c in node.children if isinstance(c, Token) and c.type == "NAME"), None)
		if subject_tok is None:
			subject_node = next((c for c in node.children if isinstance(c, Tree) and _name(c) == "trait_subject"), None)
			if subject_node is not None:
				subject_tok = next(
					(c for c in subject_node.children if isinstance(c, Token) and c.type == "NAME"),
					None,
				)
		trait_node = next(
			(
				c
				for c in node.children
				if isinstance(c, Tree) and _name(c) in {"trait_name", "base_type", "qualified_base_type"}
			),
			None,
		)
		if subject_tok is None or trait_node is None:
			raise ValueError("trait atom missing subject or trait name")
		subject_loc = _loc_from_token(subject_tok)
		if subject_tok.value == "Self":
			subject_ref: TraitSubject = SelfRef(loc=subject_loc)
		else:
			subject_ref = TypeNameRef(loc=subject_loc, name=subject_tok.value)
		if _name(trait_node) == "trait_name":
			trait_child = next((c for c in trait_node.children if isinstance(c, Tree)), None)
			if trait_child is None:
				raise ValueError("trait name missing base type")
			trait_node = trait_child
		return TraitIs(loc=_loc(node), subject=subject_ref, trait=_build_type_expr(trait_node))
	raise ValueError(f"unsupported trait expr node: {name}")


def _build_require_clause(tree: Tree) -> RequireClause:
	exprs: list[TraitExpr] = []
	for child in tree.children:
		if isinstance(child, Tree) and _name(child).startswith("trait_"):
			exprs.append(_build_trait_expr(child))
	if not exprs:
		raise ValueError("require clause missing trait expressions")
	combined = exprs[0]
	for expr in exprs[1:]:
		combined = TraitAnd(loc=_loc(tree), left=combined, right=expr)
	return RequireClause(expr=combined, loc=_loc(tree))


def _build_trait_method_sig(tree: Tree) -> TraitMethodSig:
	loc = _loc(tree)
	children = list(tree.children)
	idx = 0
	is_unsafe = False
	if idx < len(children) and isinstance(children[idx], Token) and children[idx].type == "UNSAFE":
		is_unsafe = True
		idx += 1
	if idx < len(children) and isinstance(children[idx], Token) and children[idx].type == "FN_KW":
		idx += 1
	name_token = _unwrap_ident(children[idx])
	idx += 1
	type_params: list[str] = []
	type_param_locs: list[Located] = []
	if idx < len(children) and _name(children[idx]) == "type_params":
		for tok in children[idx].children:
			if isinstance(tok, Token) and tok.type == "NAME":
				type_params.append(tok.value)
				type_param_locs.append(_loc_from_token(tok))
		idx += 1
	params: List[Param] = []
	if idx < len(children) and _name(children[idx]) == "params":
		params = [_build_param(p) for p in children[idx].children if isinstance(p, Tree)]
		idx += 1
	declared_nothrow = False
	declared_throws = False
	if idx < len(children) and isinstance(children[idx], Token):
		if children[idx].type == "NOTHROW":
			declared_nothrow = True
			idx += 1
		elif children[idx].type == "THROWS":
			declared_throws = True
			idx += 1
	return_sig = children[idx]
	type_child = next(child for child in return_sig.children if isinstance(child, Tree))
	return_type = _build_type_expr(type_child)
	return TraitMethodSig(
		name=name_token.value,
		params=params,
		return_type=return_type,
		loc=loc,
		type_params=type_params,
		type_param_locs=type_param_locs,
		declared_nothrow=declared_nothrow,
		declared_throws=declared_throws,
		is_unsafe=is_unsafe,
	)


def _build_trait_def(tree: Tree) -> TraitDef:
	loc = _loc(tree)
	name_token = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
	type_params: list[str] = []
	type_param_locs: list[Located] = []
	type_params_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "type_params"), None)
	if type_params_node is not None:
		for tok in type_params_node.children:
			if isinstance(tok, Token) and tok.type == "NAME":
				type_params.append(tok.value)
				type_param_locs.append(_loc_from_token(tok))
	require_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "require_clause"), None)
	body_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "trait_body"), None)
	methods: list[TraitMethodSig] = []
	if body_node is not None:
		for item in body_node.children:
			if not isinstance(item, Tree):
				continue
			if _name(item) == "trait_item":
				sig_node = next((c for c in item.children if isinstance(c, Tree) and _name(c) == "trait_method_sig"), None)
				if sig_node is not None:
					methods.append(_build_trait_method_sig(sig_node))
			elif _name(item) == "trait_method_sig":
				methods.append(_build_trait_method_sig(item))
	require = _build_require_clause(require_node) if require_node is not None else None
	return TraitDef(
		name=name_token.value,
		methods=methods,
		require=require,
		loc=loc,
		type_params=type_params,
		type_param_locs=type_param_locs,
	)


def _build_interface_method_sig(tree: Tree) -> InterfaceMethodSig:
	loc = _loc(tree)
	children = list(tree.children)
	idx = 0
	is_unsafe = False
	if idx < len(children) and isinstance(children[idx], Token) and children[idx].type == "UNSAFE":
		is_unsafe = True
		idx += 1
	if idx < len(children) and isinstance(children[idx], Token) and children[idx].type == "FN_KW":
		idx += 1
	name_token = _unwrap_ident(children[idx])
	idx += 1
	type_params: list[str] = []
	type_param_locs: list[Located] = []
	if idx < len(children) and isinstance(children[idx], Tree) and _name(children[idx]) == "type_params":
		for tok in children[idx].children:
			if isinstance(tok, Token) and tok.type == "NAME":
				type_params.append(tok.value)
				type_param_locs.append(_loc_from_token(tok))
		idx += 1
	params: List[Param] = []
	if idx < len(children) and _name(children[idx]) == "params":
		params = [_build_param(p) for p in children[idx].children if isinstance(p, Tree)]
		idx += 1
	declared_nothrow = False
	declared_throws = False
	if idx < len(children) and isinstance(children[idx], Token):
		if children[idx].type == "NOTHROW":
			declared_nothrow = True
			idx += 1
		elif children[idx].type == "THROWS":
			declared_throws = True
			idx += 1
	return_sig = children[idx]
	type_child = next(child for child in return_sig.children if isinstance(child, Tree))
	return_type = _build_type_expr(type_child)
	return InterfaceMethodSig(
		name=name_token.value,
		params=params,
		return_type=return_type,
		loc=loc,
		type_params=type_params,
		type_param_locs=type_param_locs,
		declared_nothrow=declared_nothrow,
		declared_throws=declared_throws,
		is_unsafe=is_unsafe,
	)


def _build_interface_def(tree: Tree) -> InterfaceDef:
	loc = _loc(tree)
	name_token = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
	type_params: list[str] = []
	type_param_locs: list[Located] = []
	type_params_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "type_params"), None)
	if type_params_node is not None:
		for tok in type_params_node.children:
			if isinstance(tok, Token) and tok.type == "NAME":
				type_params.append(tok.value)
				type_param_locs.append(_loc_from_token(tok))
	parents: list[TypeExpr] = []
	parents_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "interface_parents"), None)
	if parents_node is not None:
		for child in parents_node.children:
			if isinstance(child, Tree) and _name(child) == "type_expr":
				parents.append(_build_type_expr(child))
	body_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "interface_body"), None)
	methods: list[InterfaceMethodSig] = []
	if body_node is not None:
		for item in body_node.children:
			if not isinstance(item, Tree):
				continue
			if _name(item) == "interface_item":
				sig_node = next((c for c in item.children if isinstance(c, Tree) and _name(c) == "interface_method_sig"), None)
				if sig_node is not None:
					methods.append(_build_interface_method_sig(sig_node))
			elif _name(item) == "interface_method_sig":
				methods.append(_build_interface_method_sig(item))
	return InterfaceDef(
		name=name_token.value,
		methods=methods,
		parents=parents,
		loc=loc,
		type_params=type_params,
		type_param_locs=type_param_locs,
	)


def _build_function(tree: Tree, *, allow_missing_body: bool = False) -> FunctionDef:
	loc = _loc(tree)
	children = list(tree.children)
	idx = 0
	is_unsafe = False
	if idx < len(children) and isinstance(children[idx], Token) and children[idx].type == "UNSAFE":
		is_unsafe = True
		idx += 1
	if idx < len(children) and isinstance(children[idx], Token) and children[idx].type == "FN_KW":
		idx += 1
	name_token = _unwrap_ident(children[idx])
	idx += 1
	orig_name = name_token.value
	type_params: list[str] = []
	type_param_locs: list[Located] = []
	if idx < len(children) and _name(children[idx]) == "type_params":
		for tok in children[idx].children:
			if isinstance(tok, Token) and tok.type == "NAME":
				type_params.append(tok.value)
				type_param_locs.append(_loc_from_token(tok))
		idx += 1
	params: List[Param] = []
	if idx < len(children) and _name(children[idx]) == "params":
		params = [_build_param(p) for p in children[idx].children if isinstance(p, Tree)]
		idx += 1
	declared_nothrow = False
	declared_throws = False
	if idx < len(children) and isinstance(children[idx], Token):
		if children[idx].type == "NOTHROW":
			declared_nothrow = True
			idx += 1
		elif children[idx].type == "THROWS":
			declared_throws = True
			idx += 1
	return_sig = children[idx]
	type_child = next(child for child in return_sig.children if isinstance(child, Tree))
	return_type = _build_type_expr(type_child)
	idx += 1
	require = None
	if idx < len(children) and isinstance(children[idx], Tree) and _name(children[idx]) == "require_clause":
		require = _build_require_clause(children[idx])
		idx += 1
	if idx >= len(children) or not isinstance(children[idx], Tree) or _name(children[idx]) != "block":
		if not allow_missing_body:
			raise ModuleDeclError("function declaration missing body", loc=_loc(tree))
		body = Block(statements=[])
	else:
		body = _build_block(children[idx])
	return FunctionDef(
		name=name_token.value,
		orig_name=orig_name,
		type_params=type_params,
		type_param_locs=type_param_locs,
		params=params,
		return_type=return_type,
		declared_nothrow=declared_nothrow,
		declared_throws=declared_throws,
		is_unsafe=is_unsafe,
		body=body,
		loc=loc,
		require=require,
	)


def _build_implement_def(tree: Tree) -> ImplementDef:
	loc = _loc(tree)
	type_params_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "type_params"), None)
	type_params: list[str] = []
	type_param_locs: list[Located] = []
	if type_params_node is not None:
		for tok in type_params_node.children:
			if isinstance(tok, Token) and tok.type == "NAME":
				type_params.append(tok.value)
				type_param_locs.append(_loc_from_token(tok))
	type_nodes = [child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr"]
	if not type_nodes:
		raise ValueError("implement missing target type")
	trait = None
	if len(type_nodes) >= 2:
		trait = _build_type_expr(type_nodes[0])
		target = _build_type_expr(type_nodes[1])
	else:
		target = _build_type_expr(type_nodes[0])
	require_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "require_clause"), None)
	require = _build_require_clause(require_node) if require_node is not None else None
	methods: List[FunctionDef] = []
	body_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "implement_body")
	for item in body_node.children:
		# Grammar:
		#   implement_item: PUB func_def -> implement_func_pub
		#                | func_def      -> implement_func
		#
		# So we accept either:
		# - `implement_func(func_def)` (preferred),
		# - `implement_func_pub(PUB, func_def)`, or
		# - legacy/alternate shapes that may appear during grammar evolution.
		if not isinstance(item, Tree):
			continue
		item_test_only = _has_test_build_only_marker(item)
		is_pub_from_marker = any(isinstance(c, Token) and c.type == "PUB" for c in getattr(item, "children", []) or [])
		if item_test_only:
			item = _strip_test_build_only_marker(item)
		item_kind = _name(item)
		if item_kind not in {"implement_func", "implement_func_pub", "implement_item", "func_def"}:
			continue
		fn_node: Tree | None = None
		is_pub = False
		if item_kind == "func_def":
			fn_node = item
		else:
			fn_node = next((c for c in item.children if isinstance(c, Tree) and _name(c) == "func_def"), None)
			if any(isinstance(c, Token) and c.type == "PUB" for c in item.children):
				is_pub = True
		if fn_node is None:
			continue
		fn = _build_function(fn_node)
		if item_kind == "func_def":
			is_pub = bool(getattr(fn, "is_pub", False)) or is_pub_from_marker
		fn.is_pub = is_pub
		fn.test_build_only = item_test_only
		fn.is_method = True
		fn.impl_target = target
		methods.append(fn)
	return ImplementDef(
		target=target,
		loc=loc,
		type_params=type_params,
		type_param_locs=type_param_locs,
		trait=trait,
		require=require,
		methods=methods,
	)


def _build_block(tree: Tree) -> Block:
    statements: List[ExprStmt | LetStmt | ReturnStmt | AssertStmt | RaiseStmt] = []
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
    that must not have a terminator.

    Grammar:
        value_block: "{" stmt* expr "}"

    We represent the trailing expression as a final `ExprStmt` in the block.
    Downstream stages (AstToHIR / checker) are responsible for enforcing any
    "yields a value" rules (e.g. try/catch expression catch arms).
    """

    statements: List[ExprStmt | LetStmt | ReturnStmt | AssertStmt | RaiseStmt] = []
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
        result_expr_node = child

    if result_expr_node is None:
        raise ValueError("value_block missing trailing expression")

    if _name(result_expr_node) == "yield_expr":
        result_expr = _build_yield_expr(result_expr_node)
    else:
        result_expr = _build_expr(result_expr_node)
    statements.append(ExprStmt(loc=result_expr.loc, value=result_expr))
    return Block(statements=statements)


def _build_yield_expr(tree: Tree) -> YieldExpr:
    """
    Build a yield expression (value production inside a value block).

    Grammar:
        yield_expr: YIELD expr
    """
    expr_node: Tree | None = None
    for child in tree.children:
        if isinstance(child, Tree):
            expr_node = child
            break
    if expr_node is None:
        raise ValueError("yield_expr missing expression")
    value_expr = _build_expr(expr_node)
    return YieldExpr(loc=value_expr.loc, value=value_expr)


def _build_param(tree: Tree) -> Param:
	mutable = False
	for child in tree.children:
		if isinstance(child, Token) and child.type in {"VAR", "VAL"}:
			mutable = child.type == "VAR"
	ident_node = next(
		child
		for child in tree.children
		if (isinstance(child, Token) and child.type in {"NAME", "MOVE"})
		or (isinstance(child, Tree) and _name(child) == "ident")
	)
	name_token = _unwrap_ident(ident_node)
	type_node = next((child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr"), None)
	type_expr = _build_type_expr(type_node) if type_node is not None else None
	return Param(name=name_token.value, type_expr=type_expr, mutable=mutable)


def _build_lambda(tree: Tree) -> Lambda:
	params: list[Param] = []
	body_expr: Expr | None = None
	body_block: Block | None = None
	ret_type: TypeExpr | None = None
	captures: list[LambdaCapture] | None = None
	declared_nothrow = False

	def _build_lambda_capture(node: Tree) -> LambdaCapture:
		kind = "auto"
		seen_kind = False
		name_tok: Token | None = None
		for child in node.children:
			if isinstance(child, Token):
				if child.type == "COPY":
					kind = "copy"
					seen_kind = True
				elif child.type == "MOVE":
					kind = "move"
					seen_kind = True
				elif child.type == "AMP":
					kind = "ref"
					seen_kind = True
				elif child.type == "MUT":
					kind = "ref_mut"
					seen_kind = True
				elif child.type == "NAME":
					name_tok = child
			elif isinstance(child, Tree) and _name(child) == "lambda_capture_item":
				return _build_lambda_capture(child)
		if not seen_kind:
			kind = "auto"
		if name_tok is None:
			raise ValueError("lambda capture item missing name")
		return LambdaCapture(loc=_loc(node), name=name_tok.value, kind=kind)

	for child in tree.children:
		if isinstance(child, Tree) and _name(child) == "lambda_params":
			for param_node in child.children:
				if isinstance(param_node, Tree) and _name(param_node) == "lambda_param":
					mutable = False
					for tok in param_node.children:
						if isinstance(tok, Token) and tok.type in {"VAR", "VAL"}:
							mutable = tok.type == "VAR"
					name_tok = next(tok for tok in param_node.children if isinstance(tok, Token) and tok.type == "NAME")
					type_node = next(
						(c for c in param_node.children if isinstance(c, Tree) and _name(c) == "type_expr"), None
					)
					params.append(
						Param(
							name=name_tok.value,
							type_expr=_build_type_expr(type_node) if type_node is not None else None,
							mutable=mutable,
						)
					)
		elif isinstance(child, Tree) and _name(child) == "lambda_captures":
			captures = []
			for cap_node in child.children:
				if not isinstance(cap_node, Tree):
					continue
				if _name(cap_node) == "lambda_capture_list":
					for item in cap_node.children:
						if isinstance(item, Tree) and _name(item) == "lambda_capture_item":
							captures.append(_build_lambda_capture(item))
				elif _name(cap_node) == "lambda_capture_item":
					captures.append(_build_lambda_capture(cap_node))
		elif isinstance(child, Tree) and _name(child) == "lambda_returns":
			type_node = next((c for c in child.children if isinstance(c, Tree) and _name(c) == "type_expr"), None)
			ret_type = _build_type_expr(type_node) if type_node is not None else None
		elif isinstance(child, Token) and child.type == "NOTHROW":
			declared_nothrow = True
		elif isinstance(child, Tree) and _name(child) == "lambda_nothrow":
			declared_nothrow = True
		elif isinstance(child, Tree):
			# lambda_body or direct expr/block if the parser simplified.
			target = child
			if _name(child) == "lambda_body" and child.children:
				target = next(c for c in child.children if isinstance(c, Tree))
			if _name(target) == "block":
				body_block = _build_block(target)
			elif _name(target) == "value_block":
				body_block = _build_value_block(target)
			else:
				body_expr = _build_expr(target)
	return Lambda(
		loc=_loc(tree),
		params=params,
		ret_type=ret_type,
		captures=captures,
		body_expr=body_expr,
		body_block=body_block,
		declared_nothrow=declared_nothrow,
	)


def _build_type_expr(tree: Tree) -> TypeExpr:
	name = _name(tree)
	if name == "fn_type":
		param_nodes = [child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr"]
		ret_node = next((child for child in tree.children if isinstance(child, Tree) and _name(child) == "fn_return"), None)
		ret_type_node = None
		if ret_node is not None:
			ret_type_node = next((c for c in ret_node.children if isinstance(c, Tree)), None)
		params = [_build_type_expr(t) for t in param_nodes]
		ret = _build_type_expr(ret_type_node) if ret_type_node is not None else TypeExpr(name="<unknown>")
		is_nothrow = False
		if ret_node is not None:
			is_nothrow = any(isinstance(child, Token) and child.type == "NOTHROW" for child in ret_node.children)
		fn_throws = not is_nothrow
		return TypeExpr(name="fn", args=[*params, ret], fn_throws=fn_throws)
	if name == "ref_type":
		# '&' ['mut'] type_expr
		inner = _build_type_expr(next(child for child in tree.children if isinstance(child, Tree)))
		mut = any(isinstance(child, Token) and child.type == "MUT" for child in tree.children)
		ref_name = "&mut" if mut else "&"
		return TypeExpr(name=ref_name, args=[inner])
	if name == "type_expr":
		for child in tree.children:
			if isinstance(child, Tree) and _name(child) in {"base_type", "qualified_base_type", "type_expr", "ref_type", "fn_type"}:
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
		return TypeExpr(name=name_token.value, args=args, loc=Located(line=name_token.line, column=name_token.column, file=_CURRENT_FILE))
	if name == "qualified_base_type":
		# module_path type_args?
		module_path = tree.children[0]
		path_parts: List[str] = []
		path_loc = None
		if isinstance(module_path, Tree):
			for child in module_path.children:
				if isinstance(child, Token) and child.type == "NAME":
					path_parts.append(child.value)
					if path_loc is None:
						path_loc = Located(line=child.line, column=child.column, file=_CURRENT_FILE)
		if len(path_parts) < 2:
			return TypeExpr(name="<unknown>")
		name_tok = path_parts[-1]
		alias_tok = path_parts[0]
		args: List[TypeExpr] = []
		if len(tree.children) > 1:
			type_args = tree.children[1]
			if isinstance(type_args, Tree):
				children = [arg for arg in type_args.children if isinstance(arg, Tree)]
				if len(children) == 1 and _name(children[0]) in {"angle_type_args", "square_type_args"}:
					children = [arg for arg in children[0].children if isinstance(arg, Tree)]
				args = [_build_type_expr(arg) for arg in children]
		if len(path_parts) == 2:
			return TypeExpr(
				name=name_tok,
				args=args,
				module_alias=alias_tok,
				loc=path_loc,
			)
		module_id = ".".join(path_parts[:-1])
		return TypeExpr(
			name=name_tok,
			args=args,
			module_id=module_id,
			loc=path_loc,
		)
	# fallback for other wrappers
	if tree.children:
		last = tree.children[-1]
		if isinstance(last, Tree):
			return _build_type_expr(last)
	return TypeExpr(name="<unknown>")


def _build_stmt(tree: Tree):
	kind = _name(tree)
	if kind == "stmt":
		for child in tree.children:
			if isinstance(child, Tree):
				inner_kind = _name(child)
				if inner_kind in {"simple_stmt", "compound_stmt", "if_stmt", "try_stmt"}:
					return _build_stmt(child)
		return None
	if kind == "compound_stmt":
		for child in tree.children:
			if isinstance(child, Tree):
				return _build_stmt(child)
		return None
	if kind == "simple_stmt":
		target = tree.children[0]
		stmt_kind = _name(target)
		if stmt_kind == "let_stmt":
			return _build_let_stmt(target)
		if stmt_kind == "local_const_stmt":
			return _build_local_const_stmt(target)
		if stmt_kind == "aug_assign_stmt":
			return _build_aug_assign_stmt(target)
		if stmt_kind == "assign_stmt":
			return _build_assign_stmt(target)
		if stmt_kind == "return_stmt":
			return _build_return_stmt(target)
		if stmt_kind == "assert_stmt":
			return _build_assert_stmt(target)
		if stmt_kind == "rethrow_stmt":
			return _build_rethrow_stmt(target)
		if stmt_kind == "raise_stmt":
			return _build_raise_stmt(target)
		if stmt_kind == "expr_stmt":
			return _build_expr_stmt(target)
		if stmt_kind == "import_stmt":
			return _build_import_stmt(target)
		if stmt_kind == "export_stmt":
			return _build_export_stmt(target)
		if stmt_kind == "use_trait_stmt":
			return _build_use_trait_stmt(target)
		if stmt_kind == "break_stmt":
			return _build_break_stmt(target)
		if stmt_kind == "continue_stmt":
			return _build_continue_stmt(target)
		return None
	if kind == "if_stmt":
		return _build_if_stmt(tree)
	if kind == "match_stmt":
		match_expr = _build_match_expr(tree, arm_node_names=("match_stmt_arm",))
		return ExprStmt(loc=_loc(tree), value=match_expr)
	if kind == "while_stmt":
		return _build_while_stmt(tree)
	if kind == "for_stmt":
		return _build_for_stmt(tree)
	if kind == "for_iter_stmt":
		return _build_for_iter_stmt(tree)
	if kind == "for_legacy_stmt":
		return _build_for_stmt(tree)
	if kind == "for_count_stmt":
		return _build_for_count_stmt(tree)
	if kind == "try_stmt":
		return _build_try_stmt(tree)
	if kind == "unsafe_block":
		block_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "block"), None)
		if block_node is None:
			return None
		return UnsafeBlockStmt(loc=_loc(tree), block=_build_block(block_node))
	if kind == "block":
		return BlockStmt(loc=_loc(tree), block=_build_block(tree))
	if kind == "let_stmt":
		return _build_let_stmt(tree)
	if kind == "assign_stmt":
		return _build_assign_stmt(tree)
	if kind == "aug_assign_stmt":
		return _build_aug_assign_stmt(tree)
	if kind == "return_stmt":
		return _build_return_stmt(tree)
	if kind == "assert_stmt":
		return _build_assert_stmt(tree)
	if kind == "rethrow_stmt":
		return _build_rethrow_stmt(tree)
	if kind == "raise_stmt":
		return _build_raise_stmt(tree)
	if kind == "expr_stmt":
		return _build_expr_stmt(tree)
	if kind == "import_stmt":
		return _build_import_stmt(tree)
	if kind == "export_stmt":
		return _build_export_stmt(tree)
	if kind == "use_trait_stmt":
		return _build_use_trait_stmt(tree)
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


def _parse_for_iter_binding(tree: Tree) -> tuple[str, bool, TypeExpr | None]:
	kind = _name(tree)
	if kind == "for_iter_bind_infer":
		mutable = any(isinstance(c, Token) and c.type == "VAR" for c in tree.children)
		ident_node = next(
			c for c in tree.children
			if (isinstance(c, Token) and c.type in {"NAME", "MOVE"}) or (isinstance(c, Tree) and _name(c) == "ident")
		)
		name_tok = _unwrap_ident(ident_node)
		return name_tok.value, mutable, None
	if kind == "for_iter_bind_typed":
		type_node = next(c for c in tree.children if isinstance(c, Tree) and _name(c) == "type_expr")
		ident_node = next(
			c for c in tree.children
			if (isinstance(c, Token) and c.type in {"NAME", "MOVE"}) or (isinstance(c, Tree) and _name(c) == "ident")
		)
		name_tok = _unwrap_ident(ident_node)
		return name_tok.value, False, _build_type_expr(type_node)
	raise ValueError(f"unsupported for_iter_binding node: {kind}")


def _build_for_iter_stmt(tree: Tree) -> ForStmt:
	loc = _loc(tree)
	binding_inner = next(
		c for c in tree.children
		if isinstance(c, Tree) and _name(c) in {"for_iter_bind_infer", "for_iter_bind_typed", "for_iter_binding"}
	)
	if _name(binding_inner) == "for_iter_binding":
		binding_inner = next(
			c for c in binding_inner.children
			if isinstance(c, Tree) and _name(c) in {"for_iter_bind_infer", "for_iter_bind_typed"}
		)
	var_name, var_mutable, var_type_expr = _parse_for_iter_binding(binding_inner)
	expr_node = next(
		c for c in tree.children
		if isinstance(c, Tree) and _name(c) not in {"for_iter_bind_infer", "for_iter_bind_typed", "for_iter_binding", "block"}
	)
	block_node = next(c for c in tree.children if isinstance(c, Tree) and _name(c) == "block")
	iter_expr = _build_expr(expr_node)
	body_stmts = [_build_stmt(child) for child in block_node.children if isinstance(child, Tree) and _name(child) == "stmt"]
	body_stmts = [s for s in body_stmts if s is not None]
	return ForStmt(loc=loc, var=var_name, iter_expr=iter_expr, body=Block(statements=body_stmts), var_mutable=var_mutable, var_type_expr=var_type_expr)


def _build_for_stmt(tree: Tree) -> ForStmt:
	loc = _loc(tree)
	ident_node = next(
		child
		for child in tree.children
		if (isinstance(child, Token) and child.type in {"NAME", "MOVE"})
		or (isinstance(child, Tree) and _name(child) == "ident")
	)
	name_token = _unwrap_ident(ident_node)
	# The parse tree includes both the loop binding `ident` and the iterable
	# `expr` as Tree nodes. We must select the iterable expression here, not
	# the binding identifier; otherwise we end up trying to build an expression
	# from the `ident` node and crash on its raw Token child.
	expr_node = next(
		child
		for child in tree.children
		if isinstance(child, Tree) and _name(child) not in {"ident", "block"}
	)
	block_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "block")
	iter_expr = _build_expr(expr_node)
	body_stmts = [
		_build_stmt(child) for child in block_node.children if isinstance(child, Tree) and _name(child) == "stmt"
	]
	body_stmts = [s for s in body_stmts if s is not None]
	return ForStmt(loc=loc, var=name_token.value, iter_expr=iter_expr, body=Block(statements=body_stmts))


def _parse_for_count_init(tree: Tree) -> tuple[str, Expr, bool, TypeExpr | None]:
	kind = _name(tree)
	if kind == "for_count_init_infer":
		mutable = any(isinstance(c, Token) and c.type == "VAR" for c in tree.children)
		ident_node = next(
			c for c in tree.children
			if (isinstance(c, Token) and c.type in {"NAME", "MOVE"}) or (isinstance(c, Tree) and _name(c) == "ident")
		)
		name_tok = _unwrap_ident(ident_node)
		expr_node = next(c for c in tree.children if isinstance(c, Tree) and _name(c) != "ident")
		return name_tok.value, _build_expr(expr_node), mutable, None
	if kind == "for_count_init_typed":
		type_node = next(c for c in tree.children if isinstance(c, Tree) and _name(c) == "type_expr")
		ident_node = next(
			c for c in tree.children
			if (isinstance(c, Token) and c.type in {"NAME", "MOVE"}) or (isinstance(c, Tree) and _name(c) == "ident")
		)
		name_tok = _unwrap_ident(ident_node)
		expr_node = next(c for c in tree.children if isinstance(c, Tree) and _name(c) not in {"type_expr", "ident"})
		return name_tok.value, _build_expr(expr_node), False, _build_type_expr(type_node)
	raise ValueError(f"unsupported for_count_init node: {kind}")


def _build_for_count_step(tree: Tree):
	child = next((c for c in tree.children if isinstance(c, Tree)), None)
	if child is None:
		raise ValueError("for_count_step missing child")
	kind = _name(child)
	if kind == "assign_stmt":
		return _build_assign_stmt(child)
	if kind == "aug_assign_stmt":
		return _build_aug_assign_stmt(child)
	if kind == "for_count_step_expr":
		expr_node = next(c for c in child.children if isinstance(c, Tree))
		return ExprStmt(loc=_loc(child), value=_build_expr(expr_node))
	if kind == "expr":
		return ExprStmt(loc=_loc(child), value=_build_expr(child))
	raise ValueError(f"unsupported for_count_step kind: {kind}")


def _build_for_count_stmt(tree: Tree) -> ForCountStmt:
	loc = _loc(tree)
	init_node = next(
		c for c in tree.children
		if isinstance(c, Tree) and _name(c) in {"for_count_init_infer", "for_count_init_typed", "for_count_init"}
	)
	if _name(init_node) == "for_count_init":
		init_node = next(c for c in init_node.children if isinstance(c, Tree))
	init_name, init_value, init_mutable, init_type_expr = _parse_for_count_init(init_node)
	condition_node = next(
		c for c in tree.children
		if isinstance(c, Tree) and _name(c) not in {"for_count_init_infer", "for_count_init_typed", "for_count_init", "for_count_step", "block"}
	)
	condition = _build_expr(condition_node)
	step_node = next(c for c in tree.children if isinstance(c, Tree) and _name(c) == "for_count_step")
	step_stmt = _build_for_count_step(step_node)
	block_node = next(c for c in tree.children if isinstance(c, Tree) and _name(c) == "block")
	body_stmts = [_build_stmt(child) for child in block_node.children if isinstance(child, Tree) and _name(child) == "stmt"]
	body_stmts = [s for s in body_stmts if s is not None]
	return ForCountStmt(
		loc=loc,
		init_name=init_name,
		init_value=init_value,
		condition=condition,
		step=step_stmt,
		body=Block(statements=body_stmts),
		init_mutable=init_mutable,
		init_type_expr=init_type_expr,
	)


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


def _build_assert_stmt(tree: Tree) -> AssertStmt:
	loc = _loc(tree)
	children = [child for child in tree.children if isinstance(child, Tree)]
	cond = _build_expr(children[0])
	msg = _build_expr(children[1]) if len(children) > 1 else None
	return AssertStmt(loc=loc, cond=cond, msg=msg)


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
    value_node = children[idx]
    if isinstance(value_node, Tree) and _name(value_node) == "exception_ctor":
        value = _build_exception_ctor(value_node)
    else:
        value = _build_expr(value_node)
    return RaiseStmt(loc=loc, value=value, domain=domain)


def _build_expr_stmt(tree: Tree) -> ExprStmt:
    loc = _loc(tree)
    expr = _build_expr(tree.children[0])
    return ExprStmt(loc=loc, value=expr)


def _build_struct_def(tree: Tree) -> StructDef:
    loc = _loc(tree)
    name_token = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
    type_params_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "type_params"), None)
    type_params: list[str] = []
    type_param_locs: list[Located] = []
    if type_params_node is not None:
        for tok in type_params_node.children:
            if isinstance(tok, Token) and tok.type == "NAME":
                type_params.append(tok.value)
                type_param_locs.append(_loc_from_token(tok))
    require_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "require_clause"), None)
    body = next(
        (c for c in tree.children if isinstance(c, Tree) and _name(c) in {"struct_body", "tuple_struct", "block_struct"}),
        None,
    )
    if body is None:
        raise ValueError("struct definition missing body")
    field_nodes = _collect_struct_fields(body)
    fields = [_build_struct_field(node) for node in field_nodes]
    require = _build_require_clause(require_node) if require_node is not None else None
    return StructDef(
        name=name_token.value,
        fields=fields,
        type_params=type_params,
        type_param_locs=type_param_locs,
        require=require,
        loc=loc,
    )


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
    is_pub = any(isinstance(child, Token) and child.type == "PUB" for child in tree.children)
    return StructField(name=name_token.value, type_expr=_build_type_expr(type_node), is_pub=is_pub)


def _build_import_stmt(tree: Tree) -> ImportStmt:
    loc = _loc(tree)
    path_node = tree.children[0]
    parts = [child.value for child in path_node.children if isinstance(child, Token) and child.type == "NAME"]
    alias = None
    if len(tree.children) > 1 and isinstance(tree.children[1], Tree) and _name(tree.children[1]) == "import_alias":
        alias_tok = next((c for c in tree.children[1].children if isinstance(c, Token) and c.type == "NAME"), None)
        if alias_tok:
            alias = alias_tok.value
    return ImportStmt(loc=loc, path=parts, alias=alias)


def _build_export_stmt(tree: Tree) -> ExportStmt:
	loc = _loc(tree)
	items_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "export_items"), None)
	items: List[ExportItem] = []
	if items_node is not None:
		for child in items_node.children:
			if isinstance(child, Tree) and _name(child) == "export_item":
				items.append(_build_export_item(child))
	return ExportStmt(loc=loc, items=items)


def _build_export_item(tree: Tree) -> ExportItem:
	loc = _loc(tree)
	module_node = next(
		(c for c in tree.children if isinstance(c, Tree) and _name(c) in {"module_path", "module_path_star"}),
		None,
	)
	if module_node is not None:
		parts = [tok.value for tok in module_node.children if isinstance(tok, Token) and tok.type == "NAME"]
		return ExportModuleStar(loc=loc, module_path=parts)
	name_tok = next((c for c in tree.children if isinstance(c, Token) and c.type == "NAME"), None)
	if name_tok is not None:
		return ExportName(loc=loc, name=name_tok.value)
	raise ValueError("export item missing name or module path")


def _build_use_trait_stmt(tree: Tree) -> UseTraitStmt:
	loc = _loc(tree)
	ref_node = next(
		(
			c
			for c in tree.children
			if isinstance(c, Tree) and _name(c) in {"trait_ref", "module_path"}
		),
		None,
	)
	if ref_node is None:
		raise ValueError("use trait missing trait reference")
	if _name(ref_node) == "trait_ref":
		child = next((c for c in ref_node.children if isinstance(c, Tree) and _name(c) == "module_path"), None)
		if child is not None:
			ref_node = child
	parts = [tok.value for tok in ref_node.children if isinstance(tok, Token) and tok.type == "NAME"]
	if len(parts) < 1:
		raise ValueError("trait reference missing module path or trait name")
	module_path = parts[:-1]
	trait_name = parts[-1]
	return UseTraitStmt(
		loc=loc,
		trait=TraitRef(loc=_loc(ref_node), module_path=module_path, name=trait_name),
	)


def _build_if_stmt(tree: Tree) -> IfStmt:
    loc = _loc(tree)
    condition_node = None
    then_block_node = None
    else_block_node = None
    for child in tree.children:
        if not isinstance(child, Tree):
            continue
        name = _name(child)
        if condition_node is None:
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
    cond_node = condition_node
    if _name(cond_node) == "if_cond":
        cond_node = next((c for c in cond_node.children if isinstance(c, Tree)), cond_node)
    if isinstance(cond_node, Tree) and _name(cond_node).startswith("trait_"):
        condition = _build_trait_expr(cond_node)
    else:
        condition = _build_expr(cond_node)
    then_block = _build_block(then_block_node)
    else_block = _build_block(else_block_node) if else_block_node else None
    return IfStmt(loc=loc, condition=condition, then_block=then_block, else_block=else_block)


def _build_while_stmt(tree: Tree) -> WhileStmt:
    loc = _loc(tree)
    condition_node = None
    body_node = None
    for child in tree.children:
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


def _build_break_stmt(tree: Tree) -> BreakStmt:
    return BreakStmt(loc=_loc(tree))


def _build_continue_stmt(tree: Tree) -> ContinueStmt:
    return ContinueStmt(loc=_loc(tree))


def _build_try_stmt(tree: Tree) -> TryStmt:
    loc = _loc(tree)
    try_block = None
    body_from_expr = False
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
                body_from_expr = True
        elif name == "catch_clause":
            catches.append(_build_catch_clause(child))
    if try_block is None:
        raise ValueError("try statement missing body")
    if not catches and body_from_expr:
        raise ValueError("try statement with expression body requires at least one catch clause")
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
			if name in {"catch_pattern", "catch_event", "catch_event_unqualified", "catch_all", "catch_all_empty"}:
				if name in {"catch_event", "catch_event_unqualified"}:
					if name == "catch_event":
						event_node = next(
							(c for c in child.children if isinstance(c, Tree) and _name(c) == "event_fqn"),
							None,
						)
						if event_node is not None:
							event = _fqn_from_tree(event_node)
						ident_nodes = [
							c
							for c in child.children
							if (isinstance(c, Token) and c.type in {"NAME", "MOVE"})
							or (isinstance(c, Tree) and _name(c) == "ident")
						]
						if ident_nodes:
							binder = _unwrap_ident(ident_nodes[0]).value
					else:
						ident_nodes = [
							c
							for c in child.children
							if (isinstance(c, Token) and c.type in {"NAME", "MOVE"})
							or (isinstance(c, Tree) and _name(c) == "ident")
						]
						if len(ident_nodes) >= 2:
							event = _unwrap_ident(ident_nodes[0]).value
							binder = _unwrap_ident(ident_nodes[1]).value
				elif name == "catch_all":
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
    if name == "unsafe_expr":
        vb_node = next((c for c in node.children if isinstance(c, Tree) and _name(c) == "value_block"), None)
        if vb_node is None:
            raise ValueError("unsafe_expr missing value_block")
        return UnsafeExpr(loc=_loc(node), block=_build_value_block(vb_node))
    if name == "match_expr":
        return _build_match_expr(node, arm_node_names=("match_expr_arm",))
    if name == "ternary":
        return _build_ternary(node)
    if name == "pipeline":
        # Pipeline operators are a left-associative chain of `pipeline_tail`
        # nodes (token + rhs).
        return _fold_chain(node, "pipeline_tail")
    if name == "lambda_expr":
        return _build_lambda(node)
    if name == "cast_expr":
        type_node = next(child for child in node.children if isinstance(child, Tree) and _name(child) == "type_expr")
        expr_node = next(child for child in node.children if isinstance(child, Tree) and _name(child) != "type_expr")
        return Cast(loc=_loc(node), target_type=_build_type_expr(type_node), expr=_build_expr(expr_node))
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
    if name == "copy_op":
        # Explicit copy marker: `copy <expr>`.
        target = next((c for c in node.children if isinstance(c, Tree)), None)
        if target is None:
            raise TypeError(f"copy_op expects an operand, got {node.children!r}")
        expr = _build_expr(target)
        return Copy(loc=_loc(node), value=expr)
    if name == "postfix":
        return _build_postfix(node)
    if name == "qualified_member":
        name_toks = [c for c in node.children if isinstance(c, Token) and c.type == "NAME"]
        if len(name_toks) not in {2, 3}:
            raise QualifiedMemberParseError(
                "E-PARSE-QMEM-SHAPE: qualified member must have a base name (optionally module-qualified) and a member name",
                loc=_loc(node),
            )

        type_arg_nodes = [
            c
            for c in node.children
            if isinstance(c, Tree)
            and _name(c) in {"qualified_pre_type_args", "qualified_post_type_args"}
        ]
        if len(type_arg_nodes) > 1:
            # MVP: accept at most one explicit type-argument list. Supporting both
            # `Optional<T>::Ctor(...)` and `Optional::Ctor<T>(...)` is enough; mixing
            # both is ambiguous and not needed yet.
            raise QualifiedMemberParseError(
                "E-PARSE-QMEM-DUP-TYPEARGS: qualified member may specify type arguments only once",
                loc=_loc(type_arg_nodes[1]),
            )

        type_args: list[TypeExpr] = []
        if type_arg_nodes:
            type_args = [
                _build_type_expr(t)
                for t in type_arg_nodes[0].children
                if isinstance(t, Tree) and _name(t) == "type_expr"
            ]

        if len(name_toks) == 3:
            base_alias = name_toks[0].value
            base_name = name_toks[1].value
            member_tok = name_toks[2]
            base_loc = Located(line=name_toks[0].line, column=name_toks[0].column, file=_CURRENT_FILE)
        else:
            base_alias = None
            base_name = name_toks[0].value
            member_tok = name_toks[1]
            base_loc = Located(line=name_toks[0].line, column=name_toks[0].column, file=_CURRENT_FILE)

        base_type = TypeExpr(
            name=base_name,
            args=type_args,
            module_alias=base_alias,
            loc=base_loc,
        )
        return QualifiedMember(loc=_loc(node), base_type=base_type, member=member_tok.value)
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
    if name == "pos":
        # Unary plus: `+<expr>`.
        #
        # This is a no-op semantically, but we keep it as an AST node so later
        # phases can diagnose/handle it consistently (e.g., const evaluation).
        target = next((c for c in node.children if isinstance(c, Tree)), None)
        if target is None:
            raise TypeError(f"pos expects an operand, got {node.children!r}")
        expr = _build_expr(target)
        return Unary(loc=_loc(node), op="+", operand=expr)
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
    if name == "uint_lit":
        raw = node.children[0].value
        return UintLiteral(loc=_loc(node), value=int(raw[:-1]))
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
    if name == "map_literal":
        return _build_map_literal(node)
    if node.children:
        return _build_expr(node.children[0])
    raise ValueError(f"Unsupported expression node: {name}")


def _build_array_literal(tree: Tree) -> ArrayLiteral:
    elements = [_build_expr(child) for child in tree.children if isinstance(child, Tree)]
    return ArrayLiteral(loc=_loc(tree), elements=elements)


def _build_map_literal(tree: Tree) -> MapLiteral:
    entries: list[MapEntry] = []
    map_entries = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "map_entries"), None)
    if map_entries is None:
        return MapLiteral(loc=_loc(tree), entries=entries)
    for child in map_entries.children:
        if not isinstance(child, Tree) or _name(child) != "map_entry":
            continue
        expr_nodes = [c for c in child.children if isinstance(c, Tree)]
        if len(expr_nodes) < 2:
            continue
        key_node = expr_nodes[0]
        value_node = expr_nodes[1]
        entries.append(MapEntry(key=_build_expr(key_node), value=_build_expr(value_node), loc=_loc(child)))
    return MapLiteral(loc=_loc(tree), entries=entries)


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
    fallback = _loc_from_token(op_token)
    loc = _loc_from_bounds(left, right, fallback)
    return Binary(loc=loc, op=op_token.value, left=left, right=right)


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
        if arm_name in {"catch_expr_event", "catch_expr_event_unqualified"}:
            if arm_name == "catch_expr_event":
                event_node = next(
                    (c for c in arm_node.children if isinstance(c, Tree) and _name(c) == "event_fqn"),
                    None,
                )
                if event_node is None:
                    raise ValueError("event catch arm requires event")
                ident_nodes = [
                    c
                    for c in arm_node.children
                    if (isinstance(c, Token) and c.type in {"NAME", "MOVE"})
                    or (isinstance(c, Tree) and _name(c) == "ident")
                ]
                if not ident_nodes:
                    raise ValueError("event catch arm requires binder")
                event = _fqn_from_tree(event_node)
                binder_token = _unwrap_ident(ident_nodes[0])
            else:
                ident_nodes = [
                    c
                    for c in arm_node.children
                    if (isinstance(c, Token) and c.type in {"NAME", "MOVE"})
                    or (isinstance(c, Tree) and _name(c) == "ident")
                ]
                if len(ident_nodes) < 2:
                    raise ValueError("event catch arm requires event and binder")
                event = _unwrap_ident(ident_nodes[0]).value
                binder_token = _unwrap_ident(ident_nodes[1])
            block_node = next(
                child for child in arm_node.children if isinstance(child, Tree) and _name(child) == "value_block"
            )
            arms.append(
                CatchExprArm(
                    event=event,
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


def _build_match_expr(tree: Tree, *, arm_node_names: tuple[str, ...] = ("match_expr_arm",)) -> MatchExpr:
	"""
	Build a match expression.

	Grammar:
	  match_expr: MATCH expr "{" match_expr_arms? "}"
	"""
	# The scrutinee is the first expression subtree directly under `match_expr`.
	#
	# Note: because `expr` is an inlined rule (`?expr`), Lark does not always
	# materialize it as a distinct `Tree("expr")`. The first child that is a
	# `Tree` and is *not* a match arm is the scrutinee expression.
	excluded = set(arm_node_names) | {"match_expr_arms", "match_stmt_arms"}
	scrutinee_node = next(
		(c for c in tree.children if isinstance(c, Tree) and _name(c) not in excluded),
		None,
	)
	if scrutinee_node is None:
		raise ValueError("match_expr missing scrutinee expression")
	scrutinee = _build_expr(scrutinee_node)

	arms: list[MatchArm] = []
	arm_nodes: list[Tree] = []
	for child in (c for c in tree.children if isinstance(c, Tree)):
		name = _name(child)
		if name in arm_node_names:
			arm_nodes.append(child)
			continue
		if name in {"match_expr_arms", "match_stmt_arms"}:
			arm_nodes.extend(
				c for c in child.children if isinstance(c, Tree) and _name(c) in arm_node_names
			)
	for arm_node in arm_nodes:
		pat: Optional[Tree] = None
		for child in (c for c in arm_node.children if isinstance(c, Tree)):
			child_name = _name(child)
			if child_name == "match_pat":
				pat = next((c for c in child.children if isinstance(c, Tree)), None)
				break
			# `match_pat` is not inlined, but because each alternative uses `->`,
			# Lark typically produces the alternative node directly (e.g.
			# `match_ctor_paren`) rather than a `match_pat` wrapper.
			if child_name in (
				"match_default",
				"match_ctor",
				"match_ctor0",
				"match_ctor_named",
				"match_ctor_paren",
				"match_ctor_qualified",
				"match_ctor0_qualified",
				"match_ctor_named_qualified",
				"match_ctor_paren_qualified",
			):
				pat = child
				break
		if pat is None:
			raise ValueError("match_arm missing pattern")

		def _parse_match_binder(node: object) -> tuple[str, bool]:
			if isinstance(node, Token) and node.type == "NAME":
				return (node.value, False)
			if isinstance(node, Tree) and _name(node) == "match_binder":
				name_tok = next((c for c in node.children if isinstance(c, Token) and c.type == "NAME"), None)
				if name_tok is None:
					raise ValueError("match_binder missing NAME token")
				is_mut = False
				for c in node.children:
					if isinstance(c, Token) and c.type == "VAR":
						is_mut = True
						break
					if isinstance(c, Tree) and _name(c) == "binder":
						is_mut = _binder_is_mutable(c)
						if is_mut:
							break
				return (name_tok.value, is_mut)
			raise ValueError("unexpected match_binder node")

		pat_kind = _name(pat)
		ctor: Optional[str] = None
		ctor_base: Optional[TypeExpr] = None
		binders: list[str] = []
		binder_is_mutable: list[bool] = []
		binder_fields: list[str] | None = None
		pattern_arg_form = "positional"
		def _parse_qualified_ctor(pat_node: Tree) -> tuple[str, TypeExpr]:
			qnode = next((c for c in pat_node.children if isinstance(c, Tree) and _name(c) == "qualified_member"), None)
			if qnode is None:
				raise ValueError("match qualified ctor missing qualified_member")
			qm = _build_expr(qnode)
			if not isinstance(qm, QualifiedMember):
				raise ValueError("match qualified ctor did not parse as QualifiedMember")
			return (qm.member, qm.base_type)
		if pat_kind == "match_default":
			ctor = None
			pattern_arg_form = "bare"
		elif pat_kind == "match_ctor_qualified":
			ctor, ctor_base = _parse_qualified_ctor(pat)
			binders_node = next((c for c in pat.children if isinstance(c, Tree) and _name(c) == "match_binders"), None)
			if binders_node is not None:
				binders = []
				binder_is_mutable = []
				for c in binders_node.children:
					if isinstance(c, Tree) and _name(c) == "match_binder":
						bname, is_mut = _parse_match_binder(c)
						binders.append(bname)
						binder_is_mutable.append(is_mut)
					elif isinstance(c, Token) and c.type == "NAME":
						binders.append(c.value)
						binder_is_mutable.append(False)
			pattern_arg_form = "positional"
		elif pat_kind == "match_ctor_named_qualified":
			ctor, ctor_base = _parse_qualified_ctor(pat)
			fields_node = next((c for c in pat.children if isinstance(c, Tree) and _name(c) == "match_named_binders"), None)
			if fields_node is None:
				raise ValueError("match_ctor_named_qualified missing match_named_binders")
			binders = []
			binder_is_mutable = []
			binder_fields = []
			for nb in (c for c in fields_node.children if isinstance(c, Tree) and _name(c) == "match_named_binder"):
				parts = [c for c in nb.children if isinstance(c, Token) and c.type == "NAME"]
				if len(parts) < 1:
					raise ValueError("match_named_binder expects field NAME token")
				field_name = parts[0].value
				binder_node = next((c for c in nb.children if isinstance(c, Tree) and _name(c) == "match_binder"), None)
				if binder_node is None:
					if len(parts) != 2:
						raise ValueError("match_named_binder expects field NAME and binder NAME tokens")
					binder_name = parts[1].value
					is_mut = False
				else:
					binder_name, is_mut = _parse_match_binder(binder_node)
				binder_fields.append(field_name)
				binders.append(binder_name)
				binder_is_mutable.append(is_mut)
			pattern_arg_form = "named"
		elif pat_kind == "match_ctor_paren_qualified":
			ctor, ctor_base = _parse_qualified_ctor(pat)
			binders = []
			binder_fields = None
			pattern_arg_form = "paren"
		elif pat_kind == "match_ctor0_qualified":
			ctor, ctor_base = _parse_qualified_ctor(pat)
			pattern_arg_form = "bare"
		elif pat_kind == "match_ctor":
			name_tok = next(c for c in pat.children if isinstance(c, Token) and c.type == "NAME")
			ctor = name_tok.value
			binders_node = next((c for c in pat.children if isinstance(c, Tree) and _name(c) == "match_binders"), None)
			if binders_node is not None:
				binders = []
				binder_is_mutable = []
				for c in binders_node.children:
					if isinstance(c, Tree) and _name(c) == "match_binder":
						bname, is_mut = _parse_match_binder(c)
						binders.append(bname)
						binder_is_mutable.append(is_mut)
					elif isinstance(c, Token) and c.type == "NAME":
						binders.append(c.value)
						binder_is_mutable.append(False)
			pattern_arg_form = "positional"
		elif pat_kind == "match_ctor_named":
			name_tok = next(c for c in pat.children if isinstance(c, Token) and c.type == "NAME")
			ctor = name_tok.value
			fields_node = next((c for c in pat.children if isinstance(c, Tree) and _name(c) == "match_named_binders"), None)
			if fields_node is None:
				raise ValueError("match_ctor_named missing match_named_binders")
			binders = []
			binder_is_mutable = []
			binder_fields = []
			for nb in (c for c in fields_node.children if isinstance(c, Tree) and _name(c) == "match_named_binder"):
				parts = [c for c in nb.children if isinstance(c, Token) and c.type == "NAME"]
				if len(parts) < 1:
					raise ValueError("match_named_binder expects field NAME token")
				field_name = parts[0].value
				binder_node = next((c for c in nb.children if isinstance(c, Tree) and _name(c) == "match_binder"), None)
				if binder_node is None:
					if len(parts) != 2:
						raise ValueError("match_named_binder expects field NAME and binder NAME tokens")
					binder_name = parts[1].value
					is_mut = False
				else:
					binder_name, is_mut = _parse_match_binder(binder_node)
				binder_fields.append(field_name)
				binders.append(binder_name)
				binder_is_mutable.append(is_mut)
			pattern_arg_form = "named"
		elif pat_kind == "match_ctor_paren":
			name_tok = next(c for c in pat.children if isinstance(c, Token) and c.type == "NAME")
			ctor = name_tok.value
			binders = []
			binder_fields = None
			pattern_arg_form = "paren"
		elif pat_kind == "match_ctor0":
			name_tok = next(c for c in pat.children if isinstance(c, Token) and c.type == "NAME")
			ctor = name_tok.value
			pattern_arg_form = "bare"
		else:
			raise ValueError(f"Unsupported match_pat shape: {pat_kind}")

		body_node = next(
			(
				c
				for c in arm_node.children
				if isinstance(c, Tree)
				and _name(c) in ("match_expr_arm_body", "match_stmt_arm_body", "match_arm_body")
			),
			None,
		)
		if body_node is None:
			raise ValueError("match_arm missing body")
		inner = next((c for c in body_node.children if isinstance(c, Tree)), None)
		if inner is None:
			raise ValueError("match_arm_body missing block")
		block = _build_value_block(inner) if _name(inner) == "value_block" else _build_block(inner)
		arms.append(
			MatchArm(
				loc=_loc(arm_node),
				ctor=ctor,
				ctor_base=ctor_base,
				pattern_arg_form=pattern_arg_form,
				binders=binders,
				binder_is_mutable=binder_is_mutable if binder_is_mutable else None,
				binder_fields=binder_fields,
				block=block,
			)
		)

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
	event_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "event_fqn"), None)
	if name_tok is None and event_node is None:
		raise ValueError("exception_ctor missing name")
	if event_node is not None:
		name = _fqn_from_tree(event_node)
	else:
		name = name_tok.value

	# Grammar shape: NAME "(" [call_args] ")"
	args_node = next((c for c in tree.children if isinstance(c, Tree) and _name(c) == "call_args"), None)
	args, kwargs = _build_call_args(args_node)
	return ExceptionCtor(name=name, args=args, kwargs=kwargs, loc=_loc(tree))


def _build_postfix(tree: Tree) -> Expr:
    if not tree.children:
        raise ValueError("postfix node missing children")
    expr = _build_expr(tree.children[0])
    suffix_nodes = tree.children[1:]
    return _apply_postfix_suffixes(expr, suffix_nodes)


def _build_leading_dot(tree: Tree) -> Expr:
    # Base receiver placeholder with an initial attribute name from the DOT NAME.
    name_tok = next(
        tok for tok in tree.children if isinstance(tok, Token) and tok.type in {"NAME", "CAPTURES"}
    )
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
            type_args_node = next(
                (
                    c
                    for c in child.children
                    if isinstance(c, Tree) and _name(c) == "call_type_args"
                ),
                None,
            )
            args_node = next(
                (
                    c
                    for c in child.children
                    if isinstance(c, Tree) and _name(c) == "call_args"
                ),
                None,
            )
            type_args = _build_call_type_args(type_args_node)
            if isinstance(expr, TypeApp):
                if type_args is not None:
                    raise QualifiedMemberParseError(
                        "E-PARSE-CALL-DUP-TYPEARGS: call may specify type arguments only once",
                        loc=_loc(child),
                    )
                type_args = list(expr.type_args)
                expr = expr.func
            args, kwargs = _build_call_args(args_node)
            expr = Call(loc=_loc(child), func=expr, args=args, kwargs=kwargs, type_args=type_args)
        elif child_name == "macro_call_suffix":
            args_node = next(
                (
                    c
                    for c in child.children
                    if isinstance(c, Tree) and _name(c) == "call_args"
                ),
                None,
            )
            args, kwargs = _build_call_args(args_node)
            expr = MacroCall(loc=_loc(child), func=expr, args=args, kwargs=kwargs)
        elif child_name == "attr_suffix":
            attr_token = next(
                token
                for token in child.children
                if isinstance(token, Token) and token.type in {"NAME", "CAPTURES"}
            )
            expr = Attr(loc=_loc(child), value=expr, attr=attr_token.value)
        elif child_name == "arrow_suffix":
            attr_token = next(
                token for token in child.children if isinstance(token, Token) and token.type == "NAME"
            )
            expr = Attr(loc=_loc(child), value=expr, attr=attr_token.value, op="->")
        elif child_name == "index_suffix":
            expr = _apply_index_suffix(expr, child)
        elif child_name == "type_app_suffix":
            type_args_node = next((c for c in child.children if isinstance(c, Tree)), None)
            type_args = _build_call_type_args(type_args_node)
            if type_args is None:
                raise ValueError("type application suffix missing type arguments")
            if isinstance(expr, TypeApp):
                raise QualifiedMemberParseError(
                    "E-PARSE-TYPEAPP-DUP-TYPEARGS: type application may specify type arguments only once",
                    loc=_loc(child),
                )
            expr = TypeApp(loc=_loc(child), func=expr, type_args=type_args)
        elif child_name == "qualified_suffix":
            type_arg_node = next(
                (c for c in child.children if isinstance(c, Tree) and _name(c) == "qualified_pre_type_args"),
                None,
            )
            type_args: list[TypeExpr] = []
            if type_arg_node is not None:
                type_args = [
                    _build_type_expr(t)
                    for t in type_arg_node.children
                    if isinstance(t, Tree) and _name(t) == "type_expr"
                ]
            if isinstance(expr, TypeApp):
                if type_arg_node is not None:
                    raise QualifiedMemberParseError(
                        "E-PARSE-QMEM-DUP-TYPEARGS: qualified member may specify type arguments only once",
                        loc=_loc(child),
                    )
                type_args = list(expr.type_args)
                expr = expr.func
            def _attr_chain_parts(node: Expr) -> list[str] | None:
                if isinstance(node, Name):
                    return [node.ident]
                if isinstance(node, Attr):
                    parts = _attr_chain_parts(node.value)
                    if parts is None:
                        return None
                    parts.append(node.attr)
                    return parts
                return None

            base_alias: str | None = None
            base_name: str | None = None
            base_loc = getattr(expr, "loc", _loc(child))
            parts = _attr_chain_parts(expr)
            if parts is not None:
                if len(parts) == 1:
                    base_name = parts[0]
                else:
                    base_alias = ".".join(parts[:-1])
                    base_name = parts[-1]
            else:
                raise QualifiedMemberParseError(
                    "E-PARSE-QMEM-SHAPE: qualified member base must be a name or module-qualified name",
                    loc=_loc(child),
                )
            base_type = TypeExpr(
                name=base_name or "<unknown>",
                args=type_args,
                module_alias=base_alias,
                loc=base_loc,
            )
            member_tok = next(
                (tok for tok in child.children if isinstance(tok, Token) and tok.type == "NAME"),
                None,
            )
            if member_tok is None:
                raise QualifiedMemberParseError(
                    "E-PARSE-QMEM-SHAPE: qualified member missing member name",
                    loc=_loc(child),
                )
            expr = QualifiedMember(loc=_loc(child), base_type=base_type, member=member_tok.value)
        else:
            raise ValueError(f"Unexpected postfix child: {child_name}")
    return expr


def _build_call_type_args(node: Tree | None) -> list[TypeExpr] | None:
    if node is None:
        return None
    if _name(node) != "call_type_args":
        return None
    args: list[TypeExpr] = []
    for child in node.children:
        if not isinstance(child, Tree):
            continue
        args.append(_build_type_expr(child))
    return args


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
    return Located(
        line=meta.line,
        column=meta.column,
        file=_CURRENT_FILE,
        end_line=getattr(meta, "end_line", None),
        end_column=getattr(meta, "end_column", None),
        start_pos=getattr(meta, "start_pos", None),
        end_pos=getattr(meta, "end_pos", None),
        file_id=_CURRENT_FILE_ID,
    )


def _loc_from_token(token: Token) -> Located:
    return Located(
        line=token.line,
        column=token.column,
        file=_CURRENT_FILE,
        end_line=getattr(token, "end_line", None),
        end_column=getattr(token, "end_column", None),
        start_pos=getattr(token, "start_pos", None),
        end_pos=getattr(token, "end_pos", None),
        file_id=_CURRENT_FILE_ID,
    )


def _loc_from_bounds(left: Expr, right: Expr, fallback: Located) -> Located:
    left_loc = getattr(left, "loc", None)
    right_loc = getattr(right, "loc", None)
    if left_loc is None or right_loc is None:
        return fallback
    start_pos = getattr(left_loc, "start_pos", None)
    end_pos = getattr(right_loc, "end_pos", None)
    if start_pos is None or end_pos is None:
        return fallback
    return Located(
        line=getattr(left_loc, "line", None) or fallback.line,
        column=getattr(left_loc, "column", None) or fallback.column,
        file=getattr(left_loc, "file", None) or getattr(right_loc, "file", None) or fallback.file,
        end_line=getattr(right_loc, "end_line", None) or fallback.end_line,
        end_column=getattr(right_loc, "end_column", None) or fallback.end_column,
        start_pos=start_pos,
        end_pos=end_pos,
        file_id=getattr(left_loc, "file_id", None) or getattr(right_loc, "file_id", None) or fallback.file_id,
    )


def _name(node: Tree | Token) -> str:
    if isinstance(node, Tree):
        data = node.data
        if isinstance(data, Token):
            return data.value
        return data
    if isinstance(node, Token):
        return node.type
    return str(node)


def _has_test_build_only_marker(node: Tree) -> bool:
    return any(isinstance(child, Tree) and _name(child) == "test_build_only_marker" for child in node.children)


def _strip_test_build_only_marker(node: Tree) -> Tree:
    for child in node.children:
        if isinstance(child, Tree) and _name(child) != "test_build_only_marker":
            return child
    return node


def _has_intrinsic_marker(node: Tree) -> bool:
    return any(isinstance(child, Tree) and _name(child) == "intrinsic_marker" for child in node.children)


def _strip_item_markers(node: Tree) -> Tree:
    for child in node.children:
        if isinstance(child, Tree) and _name(child) not in {"test_build_only_marker", "intrinsic_marker"}:
            return child
    return node
