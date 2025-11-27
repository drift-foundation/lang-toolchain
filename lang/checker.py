from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from . import ast
from .types import (
    BOOL,
    CONSOLE_OUT,
    DISPLAYABLE,
    ERROR,
    F64,
    FunctionSignature,
    I64,
    STR,
    ReferenceType,
    Type,
    TypeSystemError,
    UNIT,
    array_of,
    array_element_type,
    is_displayable,
    ref_of,
    resolve_type,
)

RESERVED_IDENTIFIERS = frozenset(
    {
        "val",
        "var",
        "fn",
        "returns",
        "if",
        "else",
        "while",
        "break",
        "continue",
        "try",
        "catch",
        "throw",
        "raise",
        "return",
        "exception",
        "import",
        "module",
        "true",
        "false",
        "not",
        "and",
        "or",
        "auto",
        "pragma",
        "bool",
        "int",
        "float",
        "string",
        "void",
        "abstract",
        "assert",
        "boolean",
        "byte",
        "case",
        "char",
        "class",
        "const",
        "default",
        "do",
        "double",
        "enum",
        "extends",
        "final",
        "finally",
        "for",
        "goto",
        "implements",
        "instanceof",
        "interface",
        "long",
        "native",
        "new",
        "package",
        "private",
        "protected",
        "public",
        "short",
        "static",
        "strictfp",
        "super",
        "switch",
        "synchronized",
        "this",
        "throws",
        "transient",
        "volatile",
    }
)


@dataclass
class VarInfo:
    type: Type
    mutable: bool
    capture_key: Optional[str] = None


@dataclass
class FunctionInfo:
    signature: FunctionSignature
    node: Optional[ast.FunctionDef]


@dataclass
class CheckedProgram:
    program: ast.Program
    functions: Dict[str, FunctionInfo]
    globals: Dict[str, VarInfo]
    structs: Dict[str, "StructInfo"]
    exceptions: Dict[str, "ExceptionInfo"]
    module: Optional[str] = None


@dataclass
class StructInfo:
    name: str
    field_order: List[str]
    field_types: Dict[str, Type]


@dataclass
class ExceptionInfo:
    name: str
    arg_order: List[str]
    arg_types: Dict[str, Type]
    domain: str | None = None


class CheckError(Exception):
    pass


class Scope:
    def __init__(self, parent: Optional[Scope] = None) -> None:
        self.parent = parent
        self.vars: Dict[str, VarInfo] = {}
        self.capture_keys: Set[str] = set()

    def define(self, name: str, info: VarInfo, loc: ast.Located, capture_key: Optional[str] = None) -> None:
        if name in self.vars:
            raise CheckError(f"{loc.line}:{loc.column}: '{name}' already defined in this scope")
        if name in RESERVED_IDENTIFIERS:
            raise CheckError(f"{loc.line}:{loc.column}: '{name}' is a reserved keyword")
        if capture_key:
            if capture_key in self.capture_keys:
                raise CheckError(
                    f"{loc.line}:{loc.column}: duplicate context key '{capture_key}' [E3510]"
                )
            self.capture_keys.add(capture_key)
            info.capture_key = capture_key
        self.vars[name] = info

    def lookup(self, name: str, loc: ast.Located) -> VarInfo:
        if name in self.vars:
            return self.vars[name]
        if self.parent:
            return self.parent.lookup(name, loc)
        raise CheckError(f"{loc.line}:{loc.column}: Unknown identifier '{name}'")


@dataclass
class FunctionContext:
    name: str
    signature: FunctionSignature
    scope: Scope
    allow_returns: bool


class Checker:
    def __init__(self, builtin_functions: Dict[str, FunctionSignature]) -> None:
        self.function_infos: Dict[str, FunctionInfo] = {
            name: FunctionInfo(signature=sig, node=None)
            for name, sig in builtin_functions.items()
        }
        self.struct_infos: Dict[str, StructInfo] = {}
        self.exception_infos: Dict[str, ExceptionInfo] = {}

    def check(self, program: ast.Program) -> CheckedProgram:
        if program.module:
            self._validate_module_name(program.module)
        self._register_exceptions(program.exceptions)
        self._register_structs(program.structs)
        self._register_functions(program.functions)
        global_scope = Scope()
        global_scope.define("out", VarInfo(type=CONSOLE_OUT, mutable=False), ast.Located(line=0, column=0))
        module_ctx = FunctionContext(
            name="<module>",
            signature=FunctionSignature(
                name="<module>", params=(), return_type=UNIT, effects=None
            ),
            scope=global_scope,
            allow_returns=False,
        )
        for stmt in program.statements:
            self._check_stmt(stmt, module_ctx, in_loop=False)
        for func in program.functions:
            self._check_function(func, global_scope)
        return CheckedProgram(
            program=program,
            functions=self.function_infos,
            globals=global_scope.vars.copy(),
            structs=self.struct_infos,
            exceptions=self.exception_infos,
            module=program.module,
        )

    def _validate_module_name(self, name: str) -> None:
        import re
        if len(name.encode("utf-8")) > 254:
            raise CheckError(f"0:0: module name too long (max 254 bytes)")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9_]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9_]*[a-z0-9])?)*", name):
            raise CheckError(f"0:0: invalid module name '{name}' (lowercase alnum with dots/underscores; no leading/trailing dots/underscores)")
        reserved_prefixes = ("lang.", "abi.", "std.", "core.", "lib.")
        if name.startswith(reserved_prefixes):
            raise CheckError(f"0:0: module name '{name}' uses a reserved prefix {reserved_prefixes}")

    def _register_exceptions(self, exceptions: List[ast.ExceptionDef]) -> None:
        for exc in exceptions:
            if exc.name in RESERVED_IDENTIFIERS:
                raise CheckError(f"{exc.loc.line}:{exc.loc.column}: '{exc.name}' is a reserved keyword")
            if exc.name in self.exception_infos:
                raise CheckError(f"{exc.loc.line}:{exc.loc.column}: Exception '{exc.name}' already defined")
            arg_order: List[str] = []
            arg_types: Dict[str, Type] = {}
            for arg in exc.args:
                try:
                    ty = resolve_type(arg.type_expr)
                except TypeSystemError as exc_err:
                    raise CheckError(f"{arg.type_expr.name}: {exc_err}") from exc_err
                if arg.name in arg_types:
                    raise CheckError(f"{exc.loc.line}:{exc.loc.column}: duplicate arg '{arg.name}'")
                arg_order.append(arg.name)
                arg_types[arg.name] = ty
            self.exception_infos[exc.name] = ExceptionInfo(
                name=exc.name,
                arg_order=arg_order,
                arg_types=arg_types,
                domain=exc.domain,
            )

    def _register_functions(self, functions: List[ast.FunctionDef]) -> None:
        for fn in functions:
            if fn.name in RESERVED_IDENTIFIERS:
                raise CheckError(f"{fn.loc.line}:{fn.loc.column}: '{fn.name}' is a reserved keyword")
            if fn.name in self.function_infos:
                raise CheckError(f"{fn.loc.line}:{fn.loc.column}: Function '{fn.name}' already defined")
            try:
                param_types = tuple(resolve_type(param.type_expr) for param in fn.params)
                return_type = resolve_type(fn.return_type)
            except TypeSystemError as exc:
                raise CheckError(str(exc)) from exc
            signature = FunctionSignature(
                name=fn.name,
                params=param_types,
                return_type=return_type,
                effects=None,
            )
            self.function_infos[fn.name] = FunctionInfo(signature=signature, node=fn)

    def _register_structs(self, structs: List[ast.StructDef]) -> None:
        for struct in structs:
            if struct.name in RESERVED_IDENTIFIERS:
                raise CheckError(
                    f"{struct.loc.line}:{struct.loc.column}: '{struct.name}' is a reserved keyword"
                )
            if struct.name in self.struct_infos:
                raise CheckError(
                    f"{struct.loc.line}:{struct.loc.column}: Struct '{struct.name}' already defined"
                )
            field_order: List[str] = []
            field_types: Dict[str, Type] = {}
            for field in struct.fields:
                try:
                    ty = resolve_type(field.type_expr)
                except TypeSystemError as exc:
                    raise CheckError(f"{field.type_expr.name}: {exc}") from exc
                if field.name in field_types:
                    raise CheckError(
                        f"{struct.loc.line}:{struct.loc.column}: duplicate field '{field.name}'"
                    )
                field_order.append(field.name)
                field_types[field.name] = ty
            struct_info = StructInfo(name=struct.name, field_order=field_order, field_types=field_types)
            self.struct_infos[struct.name] = struct_info
            signature = FunctionSignature(
                name=struct.name,
                params=tuple(field_types[name] for name in field_order),
                return_type=Type(struct.name),
                effects=None,
            )
            self.function_infos[struct.name] = FunctionInfo(signature=signature, node=None)

    def _check_function(self, fn: ast.FunctionDef, global_scope: Scope) -> None:
        info = self.function_infos[fn.name]
        scope = Scope(parent=global_scope)
        for param, ty in zip(fn.params, info.signature.params):
            scope.define(param.name, VarInfo(type=ty, mutable=False), fn.loc)
        ctx = FunctionContext(
            name=fn.name,
            signature=info.signature,
            scope=scope,
            allow_returns=True,
        )
        for stmt in fn.body.statements:
            self._check_stmt(stmt, ctx, in_loop=False)
        self.function_infos[fn.name] = FunctionInfo(
            signature=info.signature,
            node=fn,
        )

    def _check_stmt(self, stmt: ast.Stmt, ctx: FunctionContext, in_loop: bool = False) -> None:
        if isinstance(stmt, ast.LetStmt):
            decl_type = None
            if stmt.type_expr is not None:
                try:
                    decl_type = resolve_type(stmt.type_expr)
                except TypeSystemError as exc:
                    raise CheckError(f"{stmt.loc.line}:{stmt.loc.column}: {exc}") from exc
            value_type = self._check_expr(stmt.value, ctx)
            if decl_type is None:
                decl_type = value_type
            else:
                self._expect_type(value_type, decl_type, stmt.loc)
            capture_key = None
            if stmt.capture:
                capture_key = stmt.capture_alias or stmt.name
            ctx.scope.define(
                stmt.name,
                VarInfo(type=decl_type, mutable=stmt.mutable),
                stmt.loc,
                capture_key=capture_key,
            )
            return
        if isinstance(stmt, ast.AssignStmt):
            expected_type = self._check_assignment_target(stmt.target, ctx)
            value_type = self._check_expr(stmt.value, ctx)
            self._expect_type(value_type, expected_type, stmt.value.loc)
            return
        if isinstance(stmt, ast.ReturnStmt):
            if not ctx.allow_returns:
                raise CheckError(f"{stmt.loc.line}:{stmt.loc.column}: Return outside function")
            if stmt.value is None:
                self._expect_type(UNIT, ctx.signature.return_type, stmt.loc)
            else:
                value_type = self._check_expr(stmt.value, ctx)
                self._expect_type(value_type, ctx.signature.return_type, stmt.loc)
            return
        if isinstance(stmt, ast.RaiseStmt):
            value_type = self._check_expr(stmt.value, ctx)
            self._expect_type(value_type, ERROR, stmt.loc)
            return
        if isinstance(stmt, ast.ExprStmt):
            self._check_expr(stmt.value, ctx)
            return
        if isinstance(stmt, ast.ImportStmt):
            return
        if isinstance(stmt, ast.IfStmt):
            cond_type = self._check_expr(stmt.condition, ctx)
            self._expect_type(cond_type, BOOL, stmt.loc)
            for inner in stmt.then_block.statements:
                self._check_stmt(inner, ctx)
            if stmt.else_block:
                for inner in stmt.else_block.statements:
                    self._check_stmt(inner, ctx)
            return
        if isinstance(stmt, ast.WhileStmt):
            cond_type = self._check_expr(stmt.condition, ctx)
            self._expect_type(cond_type, BOOL, stmt.loc)
            for inner in stmt.body.statements:
                self._check_stmt(inner, ctx, in_loop=in_loop)
            return
        if isinstance(stmt, ast.TryStmt):
            for inner in stmt.body.statements:
                self._check_stmt(inner, ctx, in_loop=in_loop)
            for clause in stmt.catches:
                catch_scope = Scope(parent=ctx.scope)
                binder_name = clause.binder
                catch_ctx = FunctionContext(
                    name=ctx.name,
                    signature=ctx.signature,
                    scope=catch_scope,
                    allow_returns=ctx.allow_returns,
                )
                if binder_name:
                    catch_scope.define(
                        binder_name,
                        VarInfo(type=ERROR, mutable=False),
                        stmt.loc,
                    )
                for inner in clause.block.statements:
                    self._check_stmt(inner, catch_ctx, in_loop=in_loop)
            return
        if isinstance(stmt, ast.BreakStmt):
            if not in_loop:
                raise CheckError(f"{stmt.loc.line}:{stmt.loc.column}: 'break' outside loop")
            return
        if isinstance(stmt, ast.ContinueStmt):
            if not in_loop:
                raise CheckError(f"{stmt.loc.line}:{stmt.loc.column}: 'continue' outside loop")
            return
        raise CheckError(f"{stmt.loc.line}:{stmt.loc.column}: Unsupported statement {stmt}")

    def _check_expr(self, expr: ast.Expr, ctx: FunctionContext) -> Type:
        if isinstance(expr, ast.Literal):
            value = expr.value
            if isinstance(value, bool):
                return BOOL
            if isinstance(value, int):
                return I64
            if isinstance(value, float):
                return F64
            if isinstance(value, str):
                return STR
            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Unsupported literal {value!r}")
        if isinstance(expr, ast.Name):
            info = ctx.scope.lookup(expr.ident, expr.loc)
            return info.type
        if isinstance(expr, ast.Call):
            return self._check_call(expr, ctx)
        if isinstance(expr, ast.Attr):
            base_type = self._check_expr(expr.value, ctx)
            return self._resolve_attr_type(base_type, expr)
        if isinstance(expr, ast.Move):
            return self._check_expr(expr.value, ctx)
        if isinstance(expr, ast.ArrayLiteral):
            return self._check_array_literal(expr, ctx)
        if isinstance(expr, ast.Index):
            return self._check_index_expr(expr, ctx)
        if isinstance(expr, ast.TryExpr):
            return self._check_try_expr(expr, ctx)
        if isinstance(expr, ast.Ternary):
            return self._check_ternary(expr, ctx)
        if isinstance(expr, ast.Unary):
            operand_type = self._check_expr(expr.operand, ctx)
            if expr.op == "-":
                self._expect_number(operand_type, expr.loc)
                return operand_type
            if expr.op == "not":
                self._expect_type(operand_type, BOOL, expr.loc)
                return BOOL
            if expr.op in ("&", "&mut"):
                if not isinstance(expr.operand, ast.Name):
                    raise CheckError(
                        f"{expr.loc.line}:{expr.loc.column}: cannot borrow from a temporary or non-lvalue"
                    )
                var = ctx.scope.lookup(expr.operand.ident, expr.operand.loc)
                if expr.op == "&mut" and not var.mutable:
                    raise CheckError(
                        f"{expr.loc.line}:{expr.loc.column}: cannot take &mut of immutable binding '{expr.operand.ident}'"
                    )
                return ref_of(operand_type, mutable=(expr.op == "&mut"))
            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Unknown unary operator {expr.op}")
        if isinstance(expr, ast.Binary):
            return self._check_binary(expr, ctx)
        raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Unsupported expression {expr}")

    def _check_try_expr(self, expr: ast.TryExpr, ctx: FunctionContext) -> Type:
        attempt_type = self._check_expr(expr.expr, ctx)
        fallback_type = self._check_expr(expr.fallback, ctx)
        self._expect_type(fallback_type, attempt_type, expr.fallback.loc)
        return attempt_type

    def _check_ternary(self, expr: ast.Ternary, ctx: FunctionContext) -> Type:
        cond_type = self._check_expr(expr.condition, ctx)
        self._expect_type(cond_type, BOOL, expr.condition.loc)
        then_type = self._check_expr(expr.then_value, ctx)
        else_type = self._check_expr(expr.else_value, ctx)
        self._expect_type(else_type, then_type, expr.else_value.loc)
        return then_type

    def _check_array_literal(self, expr: ast.ArrayLiteral, ctx: FunctionContext) -> Type:
        if not expr.elements:
            raise CheckError(
                f"{expr.loc.line}:{expr.loc.column}: Cannot infer type of empty array literal"
            )
        first_type = self._check_expr(expr.elements[0], ctx)
        for element in expr.elements[1:]:
            actual = self._check_expr(element, ctx)
            self._expect_type(actual, first_type, element.loc)
        return array_of(first_type)

    def _check_index_expr(self, expr: ast.Index, ctx: FunctionContext) -> Type:
        container_type = self._check_expr(expr.value, ctx)
        element_type = array_element_type(container_type)
        if element_type is None:
            raise CheckError(
                f"{expr.loc.line}:{expr.loc.column}: Type {container_type} is not indexable"
            )
        index_type = self._check_expr(expr.index, ctx)
        self._expect_type(index_type, I64, expr.index.loc)
        return element_type

    def _check_call(self, expr: ast.Call, ctx: FunctionContext) -> Type:
        callee_name = self._resolve_callee(expr.func, ctx)
        exc_info = self.exception_infos.get(callee_name)
        if exc_info:
            self._check_exception_constructor(expr, exc_info, ctx)
            return ERROR
        struct_info = self.struct_infos.get(callee_name)
        if struct_info:
            self._check_struct_constructor(expr, struct_info, ctx)
            return Type(callee_name)
        if callee_name not in self.function_infos:
            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Unknown function '{callee_name}'")
        info = self.function_infos[callee_name]
        sig = info.signature
        if len(expr.args) != len(sig.params):
            raise CheckError(
                f"{expr.loc.line}:{expr.loc.column}: '{callee_name}' expects {len(sig.params)} args, got {len(expr.args)}"
            )
        for kw in expr.kwargs:
            if kw.name not in sig.allowed_kwargs:
                raise CheckError(
                    f"{kw.value.loc.line}:{kw.value.loc.column}: '{callee_name}' does not accept keyword '{kw.name}'"
                )
            self._check_expr(kw.value, ctx)
        for arg_expr, expected in zip(expr.args, sig.params):
            if isinstance(expected, ReferenceType):
                # Auto-borrow from lvalues only.
                if isinstance(arg_expr, ast.Unary) and arg_expr.op in ("&", "&mut"):
                    inner_type = self._check_expr(arg_expr.operand, ctx)
                    if expected.mutable and arg_expr.op != "&mut":
                        raise CheckError(
                            f"{arg_expr.loc.line}:{arg_expr.loc.column}: expected &mut but found shared borrow"
                        )
                    if not expected.mutable and arg_expr.op == "&mut":
                        raise CheckError(
                            f"{arg_expr.loc.line}:{arg_expr.loc.column}: expected shared borrow but found &mut"
                        )
                    if arg_expr.op == "&mut" and isinstance(arg_expr.operand, ast.Name):
                        var = ctx.scope.lookup(arg_expr.operand.ident, arg_expr.operand.loc)
                        if not var.mutable:
                            raise CheckError(
                                f"{arg_expr.loc.line}:{arg_expr.loc.column}: cannot take &mut of immutable binding '{arg_expr.operand.ident}'"
                            )
                    self._expect_type(inner_type, expected.args[0], arg_expr.loc)
                elif isinstance(arg_expr, ast.Name):
                    var = ctx.scope.lookup(arg_expr.ident, arg_expr.loc)
                    if expected.mutable and not var.mutable:
                        raise CheckError(
                            f"{arg_expr.loc.line}:{arg_expr.loc.column}: expected mutable binding for &mut parameter"
                        )
                    self._expect_type(var.type, expected.args[0], arg_expr.loc)
                else:
                    raise CheckError(
                        f"{arg_expr.loc.line}:{arg_expr.loc.column}: cannot borrow from a temporary or moved value"
                    )
            else:
                actual = self._check_expr(arg_expr, ctx)
                self._expect_type(actual, expected, arg_expr.loc)
        return sig.return_type

    def _check_struct_constructor(self, expr: ast.Call, info: StructInfo, ctx: FunctionContext) -> None:
        if len(expr.args) > len(info.field_order):
            raise CheckError(
                f"{expr.loc.line}:{expr.loc.column}: '{info.name}' expects {len(info.field_order)} args, got {len(expr.args)}"
            )
        used = []
        for arg_expr, field_name in zip(expr.args, info.field_order):
            actual = self._check_expr(arg_expr, ctx)
            self._expect_type(actual, info.field_types[field_name], arg_expr.loc)
            used.append(field_name)
        for kw in expr.kwargs:
            if kw.name not in info.field_types:
                raise CheckError(
                    f"{kw.value.loc.line}:{kw.value.loc.column}: Struct '{info.name}' has no field '{kw.name}'"
                )
            if kw.name in used:
                raise CheckError(
                    f"{kw.value.loc.line}:{kw.value.loc.column}: Field '{kw.name}' initialized twice"
                )
            actual = self._check_expr(kw.value, ctx)
            self._expect_type(actual, info.field_types[kw.name], kw.value.loc)
            used.append(kw.name)
        missing = [name for name in info.field_order if name not in used]
        if missing:
            raise CheckError(
                f"{expr.loc.line}:{expr.loc.column}: Missing fields for '{info.name}': {', '.join(missing)}"
            )

    def _check_exception_constructor(
        self, expr: ast.Call, info: ExceptionInfo, ctx: FunctionContext
    ) -> None:
        used: List[str] = []
        if len(expr.args) > len(info.arg_order):
            raise CheckError(
                f"{expr.loc.line}:{expr.loc.column}: '{info.name}' expects {len(info.arg_order)} args, got {len(expr.args)}"
            )
        for arg_expr, arg_name in zip(expr.args, info.arg_order):
            actual = self._check_expr(arg_expr, ctx)
            self._expect_type(actual, info.arg_types[arg_name], arg_expr.loc)
            used.append(arg_name)
        for kw in expr.kwargs:
            if kw.name == "domain":
                # Allow domain override even if not a declared field.
                actual = self._check_expr(kw.value, ctx)
                self._expect_type(actual, STR, kw.value.loc)
                continue
            if kw.name not in info.arg_types:
                raise CheckError(
                    f"{kw.value.loc.line}:{kw.value.loc.column}: Exception '{info.name}' has no field '{kw.name}'"
                )
            if kw.name in used:
                raise CheckError(
                    f"{kw.value.loc.line}:{kw.value.loc.column}: Argument '{kw.name}' provided multiple times"
                )
            actual = self._check_expr(kw.value, ctx)
            self._expect_type(actual, info.arg_types[kw.name], kw.value.loc)
            used.append(kw.name)
        missing = [name for name in info.arg_order if name not in used]
        if missing:
            raise CheckError(
                f"{expr.loc.line}:{expr.loc.column}: Missing arguments for '{info.name}': {', '.join(missing)}"
            )

    def _check_binary(self, expr: ast.Binary, ctx: FunctionContext) -> Type:
        left = self._check_expr(expr.left, ctx)
        right = self._check_expr(expr.right, ctx)
        op = expr.op
        if op in {"+", "-", "*", "/"}:
            if left == STR and op == "+":
                self._expect_type(right, STR, expr.loc)
                return STR
            self._expect_number(left, expr.loc)
            self._expect_type(right, left, expr.loc)
            return left
        if op in {"==", "!="}:
            self._expect_type(right, left, expr.loc)
            return BOOL
        if op in {"<", "<=", ">", ">="}:
            self._expect_number(left, expr.loc)
            self._expect_type(right, left, expr.loc)
            return BOOL
        if op in {"and", "or"}:
            self._expect_type(left, BOOL, expr.loc)
            self._expect_type(right, BOOL, expr.loc)
            return BOOL
        if op == ">>":
            raise CheckError(
                f"{expr.loc.line}:{expr.loc.column}: pipeline operator is not supported yet"
            )
        raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Unsupported operator '{op}'")

    def _resolve_callee(self, func_expr: ast.Expr, ctx: FunctionContext) -> str:
        if isinstance(func_expr, ast.Name):
            return func_expr.ident
        if isinstance(func_expr, ast.Attr):
            base_type = self._check_expr(func_expr.value, ctx)
            if base_type == CONSOLE_OUT and func_expr.attr == "writeln":
                return "out.writeln"
            raise CheckError(
                f"{func_expr.loc.line}:{func_expr.loc.column}: Unknown attribute '{func_expr.attr}'"
            )
        raise CheckError(
            f"{func_expr.loc.line}:{func_expr.loc.column}: Unsupported callee expression"
        )

    def _resolve_attr_type(self, base_type: Type, attr: ast.Attr) -> Type:
        struct_info = self.struct_infos.get(base_type.name)
        if struct_info:
            if attr.attr not in struct_info.field_types:
                raise CheckError(
                    f"{attr.loc.line}:{attr.loc.column}: Struct '{struct_info.name}' has no field '{attr.attr}'"
                )
            return struct_info.field_types[attr.attr]
        if base_type == CONSOLE_OUT and attr.attr == "writeln":
            return UNIT
        raise CheckError(
            f"{attr.loc.line}:{attr.loc.column}: Unknown attribute '{attr.attr}' on type '{base_type}'"
        )

    def _expect_number(self, actual: Type, loc: ast.Located) -> None:
        if actual not in (I64, F64):
            raise CheckError(f"{loc.line}:{loc.column}: Expected numeric type, got {actual}")

    def _expect_type(self, actual: Type, expected: Type, loc: ast.Located) -> None:
        if expected == DISPLAYABLE:
            if not is_displayable(actual):
                raise CheckError(
                    f"{loc.line}:{loc.column}: Expected type implementing Display, got {actual}"
                )
            return
        if actual != expected:
            raise CheckError(
                f"{loc.line}:{loc.column}: Expected type {expected}, got {actual}"
            )

    def _check_assignment_target(self, target: ast.Expr, ctx: FunctionContext) -> Type:
        if isinstance(target, ast.Name):
            info = ctx.scope.lookup(target.ident, target.loc)
            if not info.mutable:
                raise CheckError(
                    f"{target.loc.line}:{target.loc.column}: '{target.ident}' is immutable"
                )
            return info.type
        if isinstance(target, ast.Index):
            self._ensure_mutable_root(target.value, ctx)
            container_type = self._check_expr(target.value, ctx)
            element_type = array_element_type(container_type)
            if element_type is None:
                raise CheckError(
                    f"{target.loc.line}:{target.loc.column}: Type {container_type} is not indexable"
                )
            index_type = self._check_expr(target.index, ctx)
            self._expect_type(index_type, I64, target.index.loc)
            return element_type
        raise CheckError(
            f"{target.loc.line}:{target.loc.column}: Unsupported assignment target"
        )

    def _ensure_mutable_root(self, expr: ast.Expr, ctx: FunctionContext) -> None:
        if isinstance(expr, ast.Name):
            info = ctx.scope.lookup(expr.ident, expr.loc)
            if not info.mutable:
                raise CheckError(
                    f"{expr.loc.line}:{expr.loc.column}: '{expr.ident}' is immutable"
                )
            return
        if isinstance(expr, ast.Index):
            self._ensure_mutable_root(expr.value, ctx)
            return
        raise CheckError(
            f"{expr.loc.line}:{expr.loc.column}: Assignment target must be a variable or array element"
        )
