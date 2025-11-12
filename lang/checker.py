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
    Type,
    TypeSystemError,
    UNIT,
    is_displayable,
    resolve_type,
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
    effects: frozenset[str]


@dataclass
class CheckedProgram:
    program: ast.Program
    functions: Dict[str, FunctionInfo]
    globals: Dict[str, VarInfo]
    structs: Dict[str, "StructInfo"]


@dataclass
class StructInfo:
    name: str
    field_order: List[str]
    field_types: Dict[str, Type]


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
    effects: Set[str]
    allow_returns: bool

    @property
    def allowed_effects(self) -> Optional[frozenset[str]]:
        return self.signature.effects


class Checker:
    def __init__(self, builtin_functions: Dict[str, FunctionSignature]) -> None:
        self.function_infos: Dict[str, FunctionInfo] = {
            name: FunctionInfo(signature=sig, node=None, effects=sig.effects or frozenset())
            for name, sig in builtin_functions.items()
        }
        self.struct_infos: Dict[str, StructInfo] = {}

    def check(self, program: ast.Program) -> CheckedProgram:
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
            effects=set(),
            allow_returns=False,
        )
        for stmt in program.statements:
            self._check_stmt(stmt, module_ctx)
        for func in program.functions:
            self._check_function(func, global_scope)
        return CheckedProgram(
            program=program,
            functions=self.function_infos,
            globals=global_scope.vars.copy(),
            structs=self.struct_infos,
        )

    def _register_functions(self, functions: List[ast.FunctionDef]) -> None:
        for fn in functions:
            if fn.name in self.function_infos:
                raise CheckError(f"{fn.loc.line}:{fn.loc.column}: Function '{fn.name}' already defined")
            try:
                param_types = tuple(resolve_type(param.type_expr) for param in fn.params)
                return_type = resolve_type(fn.return_type)
            except TypeSystemError as exc:
                raise CheckError(str(exc)) from exc
            effects = None
            if fn.thrown_domains is not None:
                effects = frozenset(fn.thrown_domains)
            signature = FunctionSignature(
                name=fn.name,
                params=param_types,
                return_type=return_type,
                effects=effects,
            )
            self.function_infos[fn.name] = FunctionInfo(signature=signature, node=fn, effects=frozenset())

    def _register_structs(self, structs: List[ast.StructDef]) -> None:
        for struct in structs:
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
            self.function_infos[struct.name] = FunctionInfo(signature=signature, node=None, effects=frozenset())

    def _check_function(self, fn: ast.FunctionDef, global_scope: Scope) -> None:
        info = self.function_infos[fn.name]
        scope = Scope(parent=global_scope)
        for param, ty in zip(fn.params, info.signature.params):
            scope.define(param.name, VarInfo(type=ty, mutable=False), fn.loc)
        ctx = FunctionContext(
            name=fn.name,
            signature=info.signature,
            scope=scope,
            effects=set(),
            allow_returns=True,
        )
        for stmt in fn.body.statements:
            self._check_stmt(stmt, ctx)
        if ctx.allowed_effects is not None and not ctx.effects.issubset(ctx.allowed_effects):
            diff = ", ".join(sorted(ctx.effects - ctx.allowed_effects))
            raise CheckError(
                f"{fn.loc.line}:{fn.loc.column}: function '{fn.name}' may throw {{ {diff} }} but only declares {sorted(ctx.allowed_effects)}"
            )
        self.function_infos[fn.name] = FunctionInfo(
            signature=info.signature,
            node=fn,
            effects=frozenset(ctx.effects),
        )

    def _check_stmt(self, stmt: ast.Stmt, ctx: FunctionContext) -> None:
        if isinstance(stmt, ast.LetStmt):
            if stmt.type_expr is None:
                raise CheckError(f"{stmt.loc.line}:{stmt.loc.column}: Type annotation required")
            try:
                decl_type = resolve_type(stmt.type_expr)
            except TypeSystemError as exc:
                raise CheckError(f"{stmt.loc.line}:{stmt.loc.column}: {exc}") from exc
            value_type = self._check_expr(stmt.value, ctx)
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
            if stmt.domain is not None:
                ctx.effects.add(stmt.domain)
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
        if isinstance(expr, ast.Unary):
            operand_type = self._check_expr(expr.operand, ctx)
            if expr.op == "-":
                self._expect_number(operand_type, expr.loc)
                return operand_type
            if expr.op == "not":
                self._expect_type(operand_type, BOOL, expr.loc)
                return BOOL
            raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Unknown unary operator {expr.op}")
        if isinstance(expr, ast.Binary):
            return self._check_binary(expr, ctx)
        raise CheckError(f"{expr.loc.line}:{expr.loc.column}: Unsupported expression {expr}")

    def _check_call(self, expr: ast.Call, ctx: FunctionContext) -> Type:
        callee_name = self._resolve_callee(expr.func, ctx)
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
            actual = self._check_expr(arg_expr, ctx)
            self._expect_type(actual, expected, arg_expr.loc)
        if sig.effects is not None:
            ctx.effects.update(sig.effects)
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
