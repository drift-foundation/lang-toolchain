from __future__ import annotations

import ast
import codecs
from pathlib import Path
from typing import List, Optional

from lark import Lark, Token, Tree

from .ast import (
	ArrayLiteral,
	AssignStmt,
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
	ReturnStmt,
	StructDef,
	StructField,
	TypeExpr,
	Ternary,
	TryCatchExpr,
	CatchExprArm,
	TryStmt,
	WhileStmt,
	BreakStmt,
	ContinueStmt,
	Unary,
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


class TerminatorInserter:
    always_accept = ("NEWLINE", "SEMI")

    TERMINABLE = {
        "NAME",
        "SIGNED_INT",
        "SIGNED_FLOAT",
        "STRING",
        "TRUE",
        "FALSE",
        "RPAR",
        "RSQB",
        "RBRACE",
        "RETURN",
        "THROW",
        "BREAK",
        "CONTINUE",
        "MOVE",
    }

    SUPPRESS = {
        "DOT",
        "PLUS",
        "MINUS",
        "STAR",
        "SLASH",
        "PIPE",
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
        self._reset()
        for token in stream:
            ttype = token.type
            if ttype == "NEWLINE":
                if self._should_emit_terminator():
                    yield Token.new_borrow_pos("TERMINATOR", token.value, token)
                    self.can_terminate = False
                continue
            if ttype == "SEMI":
                yield Token.new_borrow_pos("TERMINATOR", token.value, token)
                self.can_terminate = False
                continue
            yield token
            self._update_depth(ttype)
            self.can_terminate = self._is_terminable(ttype)

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
	name_token = children[idx]
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
		if isinstance(item, Tree) and _name(item) == "implement_item":
			fn_node = next((c for c in item.children if isinstance(c, Tree) and _name(c) == "func_def"), None)
			if fn_node is not None:
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


def _build_param(tree: Tree) -> Param:
    name_token = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
    type_node = next(child for child in tree.children if isinstance(child, Tree) and _name(child) == "type_expr")
    type_expr = _build_type_expr(type_node)
    return Param(name=name_token.value, type_expr=type_expr)


def _build_type_expr(tree: Tree) -> TypeExpr:
    name = _name(tree)
    if name == "ref_type":
        # '&' ['mut'] type_expr
        inner = _build_type_expr(next(child for child in tree.children if isinstance(child, Tree)))
        mut = any(isinstance(child, Token) and child.value == "mut" for child in tree.children)
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
            args = [
                _build_type_expr(arg) for arg in type_args.children if isinstance(arg, Tree)
            ]
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
                if inner_kind == "simple_stmt" or inner_kind == "if_stmt" or inner_kind == "try_stmt":
                    return _build_stmt(child)
        return None
    if kind == "simple_stmt":
        target = tree.children[0]
        stmt_kind = _name(target)
        if stmt_kind == "let_stmt":
            return _build_let_stmt(target)
        if stmt_kind == "for_stmt":
            return _build_for_stmt(target)
        if stmt_kind == "assign_stmt":
            return _build_assign_stmt(target)
        if stmt_kind == "return_stmt":
            return _build_return_stmt(target)
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
    if kind == "return_stmt":
        return _build_return_stmt(tree)
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


def _build_for_stmt(tree: Tree) -> ForStmt:
    loc = _loc(tree)
    name_token = next(child for child in tree.children if isinstance(child, Token) and child.type == "NAME")
    expr_node = next(
        child
        for child in tree.children
        if isinstance(child, Tree) and _name(child) not in {"block", "terminator_opt"}
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
        if isinstance(child, Token):
            if child.type == "NAME":
                name_token = child
        elif isinstance(child, Tree):
            if _name(child) == "capture_marker":
                capture = True
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
    result: List[Tree] = []
    stack = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, Tree):
            continue
        if _name(node) == "struct_field":
            result.append(node)
            continue
        stack.extend(node.children)
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
        if name == "block" and try_block is None:
            try_block = _build_block(child)
        elif name == "catch_clause":
            catches.append(_build_catch_clause(child))
    if try_block is None:
        raise ValueError("try statement missing body")
    if not catches:
        raise ValueError("try statement requires at least one catch clause")
    return TryStmt(loc=loc, body=try_block, catches=catches)


def _build_catch_clause(tree: Tree) -> CatchClause:
    event: str | None = None
    binder: str | None = None
    block_node = None
    for child in tree.children:
        if isinstance(child, Tree):
            name = _name(child)
            if name in {"catch_pattern", "catch_event", "catch_all"}:
                tokens = [tok for tok in child.children if isinstance(tok, Token) and tok.type == "NAME"]
                if len(tokens) == 2:
                    event = tokens[0].value
                    binder = tokens[1].value
                elif len(tokens) == 1:
                    binder = tokens[0].value
                else:
                    raise ValueError("invalid catch pattern")
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

    if name in {"logic_and_tail", "logic_or_tail"}:
        rhs = next((c for c in node.children if isinstance(c, Tree)), None)
        if rhs is None:
            raise TypeError(f"Unexpected tail shape: {node.children!r}")
        return _build_expr(rhs)

    if name == "logic_or":
        return _fold_chain(node, "logic_or_tail")
    if name == "try_catch_expr":
        return _build_try_catch_expr(node)
    if name == "ternary":
        return _build_ternary(node)
    if name == "pipeline":
        return _build_pipeline(node)
    if name == "logic_and":
        return _fold_chain(node, "logic_and_tail")
    if name == "equality":
        return _fold_chain(node, "equality_tail")
    if name == "comparison":
        return _fold_chain(node, "comparison_tail")
    if name == "sum":
        return _fold_chain(node, "sum_tail")
    if name == "term":
        return _fold_chain(node, "term_tail")
    if name == "factor":
        return _build_expr(node.children[0])
    if name == "borrow":
        mut = any(isinstance(child, Token) and child.value == "mut" for child in node.children)
        target = _build_expr(next(child for child in node.children if isinstance(child, Tree)))
        return Unary(loc=_loc(node), op="&mut" if mut else "&", operand=target)
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
        token = node.children[0]
        return Name(loc=_loc(node), ident=token.value)
    if name == "placeholder":
        return Placeholder(loc=_loc(node))
    if name == "int_lit":
        return Literal(loc=_loc(node), value=int(node.children[0].value))
    if name == "float_lit":
        return Literal(loc=_loc(node), value=float(node.children[0].value))
    if name == "str_lit":
        raw_tok = node.children[0]
        return Literal(loc=_loc(node), value=_decode_string_token(raw_tok))
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
    if len(parts) < 2:
        raise ValueError("try_catch_expr expects attempt and at least one catch arm")
    attempt = _build_expr(parts[0])
    arms: List[CatchExprArm] = []
    for arm_node in parts[1:]:
        arm_name = _name(arm_node)
        if arm_name == "catch_expr_event":
            tokens = [t for t in arm_node.children if isinstance(t, Token) and t.type == "NAME"]
            if len(tokens) < 2:
                raise ValueError("event catch arm requires event and binder")
            event_token, binder_token = tokens[0], tokens[1]
            block_node = next(child for child in arm_node.children if isinstance(child, Tree) and _name(child) == "block")
            arms.append(CatchExprArm(event=event_token.value, binder=binder_token.value, block=_build_block(block_node)))
        elif arm_name == "catch_expr_binder":
            binder_token = next(t for t in arm_node.children if isinstance(t, Token) and t.type == "NAME")
            block_node = next(child for child in arm_node.children if isinstance(child, Tree) and _name(child) == "block")
            arms.append(CatchExprArm(event=None, binder=binder_token.value, block=_build_block(block_node)))
        elif arm_name == "catch_expr_block":
            block_node = next(child for child in arm_node.children if isinstance(child, Tree) and _name(child) == "block")
            arms.append(CatchExprArm(event=None, binder=None, block=_build_block(block_node)))
        else:
            raise ValueError(f"unexpected catch expr arm {arm_name}")
    return TryCatchExpr(loc=_loc(tree), attempt=attempt, catch_arms=arms)


def _build_ternary(tree: Tree) -> Ternary:
    parts = [child for child in tree.children if isinstance(child, Tree)]
    if len(parts) != 3:
        raise ValueError("ternary expects condition, then, else expressions")
    cond = _build_expr(parts[0])
    then_expr = _build_expr(parts[1])
    else_expr = _build_expr(parts[2])
    return Ternary(loc=_loc(tree), condition=cond, then_value=then_expr, else_value=else_expr)


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
        elif child_name == "move_suffix":
            expr = Move(loc=_loc(child), value=expr)
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
    return KwArg(name=name_token.value, value=value)


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
