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
    INT,
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
from .xxhash64 import hash64

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
    exception_metadata: List["ExceptionMeta"] = None


@dataclass
class StructInfo:
    name: str
    field_order: List[str]
    field_types: Dict[str, Type]
    is_synthetic: bool = False
    methods: Dict[str, FunctionSignature] = None


@dataclass
class ExceptionInfo:
    name: str
    arg_order: List[str]
    arg_types: Dict[str, Type]
    domain: str | None = None
    event_code: int = 0
    arg_key_type: Optional[str] = None
    args_view_type: Optional[str] = None
    # placeholder for future synthetic struct defs
    arg_key_struct: Optional["StructInfo"] = None
    args_view_struct: Optional["StructInfo"] = None


@dataclass
class ExceptionMeta:
    fqn: str
    kind: int
    payload60: int
    event_code: int
    arg_order: Optional[List[str]] = None


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
    receiver_placeholder: Optional[Type]


class Checker:
    def __init__(self, builtin_functions: Dict[str, FunctionSignature]) -> None:
        self.function_infos: Dict[str, FunctionInfo] = {
            name: FunctionInfo(signature=sig, node=None)
            for name, sig in builtin_functions.items()
        }
        self.struct_infos: Dict[str, StructInfo] = {}
        self.exception_infos: Dict[str, ExceptionInfo] = {}
        self.module_name: str = ""
        self.exception_metadata: List[ExceptionMeta] = []

    def check(self, program: ast.Program) -> CheckedProgram:
        if program.module:
            self._validate_module_name(program.module)
        self.module_name = program.module or ""
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
            receiver_placeholder=None,
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
            exception_metadata=self.exception_metadata,
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
        payload_seen: Dict[int, str] = {}
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
            fqn = f"{self.module_name}:{exc.name}"
            payload60 = hash64(fqn.encode("utf-8")) & ((1 << 60) - 1)
            if payload60 in payload_seen and payload_seen[payload60] != fqn:
                raise CheckError(
                    f"{exc.loc.line}:{exc.loc.column}: exception code collision in module '{self.module_name}' "
                    f"between '{payload_seen[payload60]}' and '{fqn}'"
                )
            payload_seen[payload60] = fqn
            event_code = (0b0001 << 60) | payload60
            # Synthesize arg-key and args-view struct fields.
            key_struct_fields = [
                ast.StructField(name="name", type_expr=ast.TypeExpr(name="String")),
            ]
            view_struct_fields = [
                ast.StructField(name="error", type_expr=ast.TypeExpr(name="Error")),
            ]
            exc_info = ExceptionInfo(
                name=exc.name,
                arg_order=arg_order,
                arg_types=arg_types,
                domain=exc.domain,
                event_code=event_code,
                arg_key_type=f"{exc.name}ArgKey",
                args_view_type=f"{exc.name}ArgsView",
            )
            # Register synthetic structs for arg key / args view.
            key_struct = self._define_struct(f"{exc.name}ArgKey", key_struct_fields, exc.loc, is_synthetic=True)
            view_struct = self._define_struct(f"{exc.name}ArgsView", view_struct_fields, exc.loc, is_synthetic=True)
            # Generate per-field key helpers on the args-view (methods returning ArgKey).
            view_methods: Dict[str, FunctionSignature] = {}
            for arg_name in arg_order:
                view_methods[arg_name] = FunctionSignature(
                    name=f"{exc.name}ArgsView.{arg_name}",
                    params=(),
                    return_type=Type(exc_info.arg_key_type),
                    effects=None,
                )
            view_struct.methods.update(view_methods)
            for sig in view_methods.values():
                self.function_infos[sig.name] = FunctionInfo(signature=sig, node=None)
            exc_info.arg_key_struct = key_struct
            exc_info.args_view_struct = view_struct
            self.exception_infos[exc.name] = exc_info
            self.exception_metadata.append(
                ExceptionMeta(fqn=fqn, kind=0b0001, payload60=payload60, event_code=event_code, arg_order=arg_order.copy())
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
            if return_type == ERROR:
                raise CheckError(f"{fn.loc.line}:{fn.loc.column}: Functions may not return Error")
            signature = FunctionSignature(
                name=fn.name,
                params=param_types,
                return_type=return_type,
                effects=None,
            )
            self.function_infos[fn.name] = FunctionInfo(signature=signature, node=fn)
            # If the first parameter is a struct type, also register a method-style callee.
            if param_types:
                recv_ty = param_types[0]
                recv_struct = recv_ty.args[0] if isinstance(recv_ty, ReferenceType) else recv_ty
                struct_info = self.struct_infos.get(recv_struct.name)
                if struct_info:
                    method_sig = FunctionSignature(
                        name=fn.name,
                        params=param_types,
                        return_type=return_type,
                        effects=None,
                    )
                    struct_info.methods[fn.name] = method_sig

    def _register_structs(self, structs: List[ast.StructDef]) -> None:
        for struct in structs:
            self._define_struct(struct.name, struct.fields, struct.loc, is_synthetic=False)

    def _define_struct(self, name: str, fields: List[ast.StructField], loc: ast.Located, is_synthetic: bool = False) -> StructInfo:
        """Define a struct (real or synthetic) and its constructor signature."""
        if name in RESERVED_IDENTIFIERS:
            raise CheckError(f"{loc.line}:{loc.column}: '{name}' is a reserved keyword")
        if name in self.struct_infos:
            # allow synthetic re-definition if it is identical
            if not is_synthetic:
                raise CheckError(f"{loc.line}:{loc.column}: Struct '{name}' already defined")
            return self.struct_infos[name]
        field_order: List[str] = []
        field_types: Dict[str, Type] = {}
        for field in fields:
            try:
                ty = resolve_type(field.type_expr)
            except TypeSystemError as exc:
                raise CheckError(f"{field.type_expr.name}: {exc}") from exc
            if field.name in field_types:
                raise CheckError(f"{loc.line}:{loc.column}: duplicate field '{field.name}'")
            field_order.append(field.name)
            field_types[field.name] = ty
        struct_info = StructInfo(
            name=name, field_order=field_order, field_types=field_types, is_synthetic=is_synthetic, methods={}
        )
        self.struct_infos[name] = struct_info
        signature = FunctionSignature(
            name=name,
            params=tuple(field_types[n] for n in field_order),
            return_type=Type(name),
            effects=None,
        )
        self.function_infos[name] = FunctionInfo(signature=signature, node=None)
        return struct_info

    def _check_function(self, fn: ast.FunctionDef, global_scope: Scope) -> None:
        info = self.function_infos[fn.name]
        scope = Scope(parent=global_scope)
        for param, ty in zip(fn.params, info.signature.params):
            is_mut = ty.name in {"&", "&mut"}
            scope.define(param.name, VarInfo(type=ty, mutable=is_mut), fn.loc)
        ctx = FunctionContext(
            name=fn.name,
            signature=info.signature,
            scope=scope,
            allow_returns=True,
            receiver_placeholder=None,
        )
        for stmt in fn.body.statements:
            self._check_stmt(stmt, ctx, in_loop=False)
        self.function_infos[fn.name] = FunctionInfo(
            signature=info.signature,
            node=fn,
        )

    def _check_stmt(self, stmt: ast.Stmt, ctx: FunctionContext, in_loop: bool = False) -> None:
        if isinstance(stmt, ast.LetStmt):
            if stmt.name.startswith("_t"):
                raise CheckError(f"{stmt.loc.line}:{stmt.loc.column}: identifiers starting with '_t' are reserved")
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
            # Special-case exception constructors in throw/raise.
            if isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, (ast.Name, ast.Attr)):
                callee_name = self._resolve_callee(stmt.value.func, ctx)
                exc_info = self.exception_infos.get(callee_name)
                if exc_info:
                    fields = self._check_exception_constructor(stmt.value, exc_info, ctx, collect=True)
                    stmt.value = ast.ExceptionCtor(
                        loc=stmt.loc,
                        name=exc_info.name,
                        event_code=exc_info.event_code,
                        fields=fields,
                        arg_order=exc_info.arg_order.copy(),
                    )
                    return
                # If it isn't an exception and also not a known callable, report as unknown exception.
                is_known_callable = callee_name in self.function_infos or callee_name in self.struct_infos
                if not is_known_callable and isinstance(stmt.value.func, ast.Name):
                    raise CheckError(f"{stmt.loc.line}:{stmt.loc.column}: Unknown exception '{callee_name}'")
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
        if isinstance(stmt, ast.ForStmt):
            iter_ty = self._check_expr(stmt.iter_expr, ctx)
            elem_ty = array_element_type(iter_ty)
            if elem_ty is None:
                raise CheckError(f"{stmt.loc.line}:{stmt.loc.column}: for-loop expects an Array, got {iter_ty}")
            ctx.scope.define(stmt.var, VarInfo(type=elem_ty, mutable=False), stmt.loc)
            for inner in stmt.body.statements:
                self._check_stmt(inner, ctx, in_loop=in_loop)
            return
        if isinstance(stmt, ast.TryStmt):
            for inner in stmt.body.statements:
                self._check_stmt(inner, ctx, in_loop=in_loop)
            for clause in stmt.catches:
                catch_scope = Scope(parent=ctx.scope)
                binder_name = clause.binder
                binder_type = ERROR
                if clause.event:
                    exc_info = self.exception_infos.get(clause.event)
                    if exc_info:
                        binder_type = Type(exc_info.name)
                catch_ctx = FunctionContext(
                    name=ctx.name,
                    signature=ctx.signature,
                    scope=catch_scope,
                    allow_returns=ctx.allow_returns,
                    receiver_placeholder=None,
                )
                if binder_name:
                    catch_scope.define(
                        binder_name,
                        VarInfo(type=binder_type, mutable=False),
                        stmt.loc,
                    )
                if clause.event:
                    exc_info = self.exception_infos.get(clause.event)
                    if exc_info:
                        clause.event_code = exc_info.event_code
                        clause.arg_order = exc_info.arg_order
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
                return INT
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
        if isinstance(expr, ast.ExceptionCtor):
            # Already type-checked; constructors yield Error.
            return ERROR
        if isinstance(expr, ast.Attr):
            base_type = self._check_expr(expr.value, ctx)
            return self._resolve_attr_type(base_type, expr)
        if isinstance(expr, ast.Move):
            return self._check_expr(expr.value, ctx)
        if isinstance(expr, ast.ArrayLiteral):
            return self._check_array_literal(expr, ctx)
        if isinstance(expr, ast.Index):
            return self._check_index_expr(expr, ctx)
        if isinstance(expr, ast.TryCatchExpr):
            return self._check_try_catch_expr(expr, ctx)
        if isinstance(expr, ast.Ternary):
            return self._check_ternary(expr, ctx)
        if isinstance(expr, ast.Placeholder):
            if ctx.receiver_placeholder is None:
                raise CheckError(f"{expr.loc.line}:{expr.loc.column}: '.' placeholder used outside of receiver context")
            return ctx.receiver_placeholder
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

    def _check_try_catch_expr(self, expr: ast.TryCatchExpr, ctx: FunctionContext) -> Type:
        attempt_type = self._check_expr(expr.attempt, ctx)
        if not expr.catch_arms:
            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: try/catch expression needs at least one catch arm")
        seen_catch_all = False
        for arm in expr.catch_arms:
            if arm.event:
                if arm.event not in self.exception_infos:
                    raise CheckError(f"{expr.loc.line}:{expr.loc.column}: unknown event '{arm.event}' in catch")
            else:
                if seen_catch_all:
                    raise CheckError(f"{expr.loc.line}:{expr.loc.column}: multiple catch-all arms in try expression")
                seen_catch_all = True
            catch_scope = Scope(parent=ctx.scope)
            catch_ctx = FunctionContext(
                name=ctx.name,
                signature=ctx.signature,
                scope=catch_scope,
                allow_returns=ctx.allow_returns,
                receiver_placeholder=None,
            )
            if arm.binder:
                catch_scope.define(arm.binder, VarInfo(type=ERROR, mutable=False), expr.loc)
            if arm.event:
                exc_info = self.exception_infos.get(arm.event)
                if exc_info:
                    arm.event_code = exc_info.event_code
            catch_type = self._check_block_result(arm.block, catch_ctx)
            self._expect_type(catch_type, attempt_type, expr.loc)
        return attempt_type

    def _check_block_result(self, block: ast.Block, ctx: FunctionContext) -> Type:
        if not block.statements:
            raise CheckError("empty catch block")
        for stmt in block.statements[:-1]:
            self._check_stmt(stmt, ctx)
        last = block.statements[-1]
        if isinstance(last, ast.ExprStmt):
            return self._check_expr(last.value, ctx)
        if isinstance(last, ast.ReturnStmt):
            if last.value is None:
                return UNIT
            return self._check_expr(last.value, ctx)
        # If the last statement is not an expression/return, still check it and default to Void.
        self._check_stmt(last, ctx)
        return UNIT

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
            if container_type.name == "ErrorAttrs":
                index_type = self._check_expr(expr.index, ctx)
                self._expect_type(index_type, STR, expr.index.loc)
                return Type("DiagnosticValue")
            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Type {container_type} is not indexable")
        index_type = self._check_expr(expr.index, ctx)
        self._expect_type(index_type, INT, expr.index.loc)
        return element_type

    def _check_call(self, expr: ast.Call, ctx: FunctionContext) -> Type:
        placeholder_prev = ctx.receiver_placeholder
        base_type_for_placeholder: Optional[Type] = None
        try:
            if isinstance(expr.func, ast.Attr):
                base_type_for_placeholder = self._check_expr(expr.func.value, ctx)
                ctx.receiver_placeholder = base_type_for_placeholder
            # Optional<T> builtin methods are handled specially.
            method_sig: Optional[FunctionSignature] = None
            if isinstance(expr.func, ast.Attr):
                base_type = base_type_for_placeholder if base_type_for_placeholder is not None else self._check_expr(expr.func.value, ctx)
                if base_type.name == "Optional":
                    return self._check_optional_call(expr, base_type, ctx)
                if base_type.name == "DiagnosticValue":
                    method = expr.func.attr
                    if expr.kwargs:
                        raise CheckError(f"{expr.loc.line}:{expr.loc.column}: DiagnosticValue.{method} does not accept keyword args")
                    if method == "get":
                        if len(expr.args) != 1:
                            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: DiagnosticValue.get expects 1 argument")
                        arg_ty = self._check_expr(expr.args[0], ctx)
                        self._expect_type(arg_ty, STR, expr.args[0].loc)
                        return Type("DiagnosticValue")
                    if method == "index":
                        if len(expr.args) != 1:
                            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: DiagnosticValue.index expects 1 argument")
                        arg_ty = self._check_expr(expr.args[0], ctx)
                        self._expect_type(arg_ty, INT, expr.args[0].loc)
                        return Type("DiagnosticValue")
                    if method == "as_int":
                        if expr.args:
                            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: DiagnosticValue.as_int takes no arguments")
                        return Type("Optional", (INT,))
                    if method == "as_bool":
                        if expr.args:
                            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: DiagnosticValue.as_bool takes no arguments")
                        return Type("Optional", (BOOL,))
                    if method == "as_float":
                        if expr.args:
                            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: DiagnosticValue.as_float takes no arguments")
                        return Type("Optional", (Type("Float64"),))
                    if method == "as_string":
                        if expr.args:
                            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: DiagnosticValue.as_string takes no arguments")
                        return Type("Optional", (STR,))
                    raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Unknown DiagnosticValue method '{method}'")
                struct_info = self.struct_infos.get(base_type.name)
                if struct_info and struct_info.methods and expr.func.attr in struct_info.methods:
                    method_sig = struct_info.methods[expr.func.attr]
            callee_name = self._resolve_callee(expr.func, ctx)
            exc_info = self.exception_infos.get(callee_name)
            if exc_info:
                self._check_exception_constructor(expr, exc_info, ctx)
                return ERROR
            struct_info = self.struct_infos.get(callee_name)
            if struct_info:
                self._check_struct_constructor(expr, struct_info, ctx)
                return Type(callee_name)
            if method_sig is None and callee_name not in self.function_infos:
                raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Unknown function '{callee_name}'")
            sig = method_sig or self.function_infos[callee_name].signature
            expected_params = sig.params[1:] if method_sig else sig.params
            if len(expr.args) != len(expected_params):
                raise CheckError(
                    f"{expr.loc.line}:{expr.loc.column}: '{callee_name}' expects {len(expected_params)} args, got {len(expr.args)}"
                )
            for kw in expr.kwargs:
                if kw.name not in sig.allowed_kwargs:
                    raise CheckError(
                        f"{kw.value.loc.line}:{kw.value.loc.column}: '{callee_name}' does not accept keyword '{kw.name}'"
                    )
                self._check_expr(kw.value, ctx)
            for arg_expr, expected in zip(expr.args, expected_params):
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
        finally:
            ctx.receiver_placeholder = placeholder_prev

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
        self, expr: ast.Call, info: ExceptionInfo, ctx: FunctionContext, collect: bool = False
    ) -> Dict[str, ast.Expr] | None:
        used: List[str] = []
        fields: Dict[str, ast.Expr] = {}
        if len(expr.args) > len(info.arg_order):
            raise CheckError(
                f"{expr.loc.line}:{expr.loc.column}: '{info.name}' expects {len(info.arg_order)} args, got {len(expr.args)}"
            )
        for arg_expr, arg_name in zip(expr.args, info.arg_order):
            actual = self._check_expr(arg_expr, ctx)
            self._expect_type(actual, info.arg_types[arg_name], arg_expr.loc)
            used.append(arg_name)
            fields[arg_name] = arg_expr
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
            fields[kw.name] = kw.value
        missing = [name for name in info.arg_order if name not in used]
        if missing:
            raise CheckError(
                f"{expr.loc.line}:{expr.loc.column}: Missing arguments for '{info.name}': {', '.join(missing)}"
            )
        return fields if collect else None

    def _check_binary(self, expr: ast.Binary, ctx: FunctionContext) -> Type:
        left = self._check_expr(expr.left, ctx)
        right = self._check_expr(expr.right, ctx)
        def _base_numeric(t: Type) -> Type:
            if isinstance(t, ReferenceType):
                return t.args[0]
            return t
        lnum = _base_numeric(left)
        rnum = _base_numeric(right)
        op = expr.op
        if op in {"+", "-", "*", "/"}:
            if lnum == STR and op == "+":
                self._expect_type(rnum, STR, expr.loc)
                return STR
            self._expect_number(lnum, expr.loc)
            self._expect_type(rnum, lnum, expr.loc)
            return lnum
        if op in {"==", "!="}:
            self._expect_type(rnum, lnum, expr.loc)
            return BOOL
        if op in {"<", "<=", ">", ">="}:
            self._expect_number(lnum, expr.loc)
            self._expect_type(rnum, lnum, expr.loc)
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
            struct_info = self.struct_infos.get(base_type.name)
            if struct_info:
                if struct_info.methods and func_expr.attr in struct_info.methods:
                    return struct_info.methods[func_expr.attr].name
                # Fallback for synthetic args-view helpers: allow method name even if not pre-registered.
                if base_type.name.endswith("ArgsView"):
                    return f"{base_type.name}.{func_expr.attr}"
            raise CheckError(
                f"{func_expr.loc.line}:{func_expr.loc.column}: Unknown attribute '{func_expr.attr}'"
            )
        raise CheckError(
            f"{func_expr.loc.line}:{func_expr.loc.column}: Unsupported callee expression"
        )

    def _resolve_attr_type(self, base_type: Type, attr: ast.Attr) -> Type:
        if base_type.name in {"&", "&mut"} and base_type.args:
            base_type = base_type.args[0]
        struct_info = self.struct_infos.get(base_type.name)
        if struct_info:
            # Synthetic method lookup for args-view key helpers.
            if attr.attr in struct_info.methods:
                return struct_info.methods[attr.attr].return_type
            if attr.attr not in struct_info.field_types:
                raise CheckError(
                    f"{attr.loc.line}:{attr.loc.column}: Struct '{struct_info.name}' has no field '{attr.attr}'"
                )
            return struct_info.field_types[attr.attr]
        if base_type == ERROR:
            if attr.attr == "code":
                return I64
            if attr.attr == "attrs":
                return Type("ErrorAttrs")
            raise CheckError(
                f"{attr.loc.line}:{attr.loc.column}: Error has no field '{attr.attr}'"
            )
        exc_info = self.exception_infos.get(base_type.name)
        if exc_info:
            if attr.attr == "attrs":
                return Type("ErrorAttrs")
            if attr.attr not in exc_info.arg_types:
                raise CheckError(
                    f"{attr.loc.line}:{attr.loc.column}: Exception '{exc_info.name}' has no field '{attr.attr}'"
                )
            return exc_info.arg_types[attr.attr]
        if base_type == CONSOLE_OUT and attr.attr == "writeln":
            return UNIT
        raise CheckError(
            f"{attr.loc.line}:{attr.loc.column}: Unknown attribute '{attr.attr}' on type '{base_type}'"
        )

    def _check_optional_call(self, expr: ast.Call, base_type: Type, ctx: FunctionContext) -> Type:
        if not base_type.args:
            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Optional without type argument")
        inner_ty = base_type.args[0]
        method = expr.func.attr
        if expr.kwargs:
            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Optional.{method} does not accept keyword args")
        if method in {"is_some", "is_none"}:
            if expr.args:
                raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Optional.{method} takes no arguments")
            return BOOL
        if method == "unwrap_or":
            if len(expr.args) != 1:
                raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Optional.unwrap_or expects exactly 1 argument")
            arg_ty = self._check_expr(expr.args[0], ctx)
            self._expect_type(arg_ty, inner_ty, expr.args[0].loc)
            return inner_ty
        raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Unknown Optional method '{method}'")

    def _expect_number(self, actual: Type, loc: ast.Located) -> None:
        if actual not in (INT, I64, F64):
            raise CheckError(f"{loc.line}:{loc.column}: Expected numeric type, got {actual}")

    def _expect_type(self, actual: Type, expected: Type, loc: ast.Located) -> None:
        if expected == DISPLAYABLE:
            if not is_displayable(actual):
                raise CheckError(
                    f"{loc.line}:{loc.column}: Expected type implementing Display, got {actual}"
                )
            return
        # Allow auto-deref of references when the inner type matches.
        if isinstance(actual, ReferenceType) and actual.args and actual.args[0] == expected:
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
            if isinstance(info.type, ReferenceType):
                return info.type.args[0]
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
            self._expect_type(index_type, INT, target.index.loc)
            return element_type
        if isinstance(target, ast.Attr):
            self._ensure_mutable_root(target.value, ctx)
            base_type = self._check_expr(target.value, ctx)
            return self._resolve_attr_type(base_type, target)
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
        if isinstance(expr, ast.Attr):
            self._ensure_mutable_root(expr.value, ctx)
            return
        raise CheckError(
            f"{expr.loc.line}:{expr.loc.column}: Assignment target must be a variable, field, or array element"
        )
