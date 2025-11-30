from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence

from . import ast
from .checker import CheckedProgram, StructInfo, ExceptionInfo
from .runtime import BUILTINS, BuiltinFunction, ConsoleOut, ErrorValue, RuntimeContext


class ReturnSignal(Exception):
    def __init__(self, value: object) -> None:
        self.value = value


class RaiseSignal(Exception):
    def __init__(self, error: ErrorValue) -> None:
        self.error = error


class Environment:
    def __init__(self, parent: Environment | None = None) -> None:
        self.parent = parent
        self.values: Dict[str, object] = {}

    def define(self, name: str, value: object) -> None:
        if name in self.values:
            raise RuntimeError(f"'{name}' already defined in this scope")
        self.values[name] = value

    def set(self, name: str, value: object) -> None:
        if name in self.values:
            self.values[name] = value
            return
        if self.parent:
            self.parent.set(name, value)
            return
        raise RuntimeError(f"Unknown variable '{name}'")

    def get(self, name: str) -> object:
        if name in self.values:
            return self.values[name]
        if self.parent:
            return self.parent.get(name)
        raise RuntimeError(f"Unknown identifier '{name}'")


@dataclass
class UserFunction:
    definition: ast.FunctionDef
    interpreter: Interpreter  # type: ignore  # forward reference

    def __call__(self, args: Sequence[object], kwargs: Dict[str, object]) -> object:
        if kwargs:
            raise RuntimeError("Keyword arguments not supported for user functions")
        if len(args) != len(self.definition.params):
            raise RuntimeError(
                f"{self.definition.name} expects {len(self.definition.params)} args, got {len(args)}"
            )
        env = Environment(parent=self.interpreter.global_env)
        for param, value in zip(self.definition.params, args):
            env.define(param.name, value)
        try:
            self.interpreter._execute_block(self.definition.body.statements, env)
        except ReturnSignal as signal:
            return signal.value
        return None


class Interpreter:
    def __init__(
        self,
        checked: CheckedProgram,
        builtins: Mapping[str, BuiltinFunction] | None = None,
        stdout=None,
        source_label: str = "<unknown>",
    ) -> None:
        self.program = checked.program
        self.function_infos = checked.functions
        self.struct_infos = checked.structs
        self.exception_infos = checked.exceptions
        self.stdout = stdout or sys.stdout
        self.runtime_ctx = RuntimeContext(self.stdout)
        self.builtins = builtins or BUILTINS
        self.global_env = Environment()
        self._current_source = source_label
        self._call_stack: list[tuple[str, ast.Located, str]] = []
        self._register_builtins()
        self._register_struct_constructors()
        self._register_exception_constructors()
        self._install_console()
        self._register_functions()
        self._push_frame("<module>", ast.Located(line=0, column=0), "<module>")
        self._execute_block(self.program.statements, self.global_env)
        self._pop_frame()

    def _register_builtins(self) -> None:
        for name, builtin in self.builtins.items():
            self.global_env.define(name, builtin)

    def _install_console(self) -> None:
        console = ConsoleOut(self.runtime_ctx)
        self.global_env.define("out", console)
        self.console_out = console

    def _register_struct_constructors(self) -> None:
        for name, info in self.struct_infos.items():
            self.global_env.define(name, StructConstructor(info))

    def _register_exception_constructors(self) -> None:
        for name, info in self.exception_infos.items():
            self.global_env.define(name, ExceptionConstructor(info))

    def _register_functions(self) -> None:
        for name, info in self.function_infos.items():
            if info.node is None:
                continue
            self.global_env.define(name, UserFunction(info.node, self))

    def call(self, name: str, *args: object) -> object:
        func = self.global_env.get(name)
        call_loc = ast.Located(line=0, column=0)
        if isinstance(func, UserFunction):
            call_loc = func.definition.loc
        return self._invoke(func, list(args), {}, call_loc)

    def _execute_block(self, statements: List[ast.Stmt], env: Environment) -> None:
        for stmt in statements:
            self._exec_stmt(stmt, env)

    def _exec_stmt(self, stmt: ast.Stmt, env: Environment) -> None:
        if isinstance(stmt, ast.LetStmt):
            value = self._eval_expr(stmt.value, env)
            env.define(stmt.name, value)
            return
        if isinstance(stmt, ast.AssignStmt):
            value = self._eval_expr(stmt.value, env)
            self._assign(stmt.target, value, env)
            return
        if isinstance(stmt, ast.ReturnStmt):
            value = self._eval_expr(stmt.value, env) if stmt.value else None
            raise ReturnSignal(value)
        if isinstance(stmt, ast.RaiseStmt):
            value = self._eval_expr(stmt.value, env)
            if not isinstance(value, ErrorValue):
                raise RuntimeError("raise expects an error value")
            raise RaiseSignal(self._attach_stack(value, stmt.loc))
        if isinstance(stmt, ast.ExprStmt):
            self._eval_expr(stmt.value, env)
            return
        if isinstance(stmt, ast.ImportStmt):
            return
        if isinstance(stmt, ast.IfStmt):
            condition = bool(self._eval_expr(stmt.condition, env))
            branch = stmt.then_block if condition else stmt.else_block
            if branch:
                self._execute_block(branch.statements, env)
            return
        if isinstance(stmt, ast.WhileStmt):
            while bool(self._eval_expr(stmt.condition, env)):
                body_env = Environment(parent=env)
                self._execute_block(stmt.body.statements, body_env)
            return
        if isinstance(stmt, ast.TryStmt):
            try:
                self._execute_block(stmt.body.statements, env)
            except RaiseSignal as exc:
                handled = False
                for clause in stmt.catches:
                    if clause.event is not None and clause.event != exc.error.message:
                        continue
                    catch_env = Environment(parent=env)
                    if clause.binder:
                        catch_env.define(clause.binder, exc.error)
                    self._execute_block(clause.block.statements, catch_env)
                    handled = True
                    break
                if not handled:
                    raise
            return
        raise RuntimeError(f"Unsupported statement {stmt}")

    def _eval_expr(self, expr: ast.Expr, env: Environment) -> object:
        if isinstance(expr, ast.Literal):
            return expr.value
        if isinstance(expr, ast.Name):
            return env.get(expr.ident)
        if isinstance(expr, ast.Call):
            func = self._eval_expr(expr.func, env)
            args = [self._eval_expr(arg, env) for arg in expr.args]
            kwargs = {kw.name: self._eval_expr(kw.value, env) for kw in expr.kwargs}
            return self._invoke(func, args, kwargs, expr.loc)
        if isinstance(expr, ast.Attr):
            base = self._eval_expr(expr.value, env)
            return self._resolve_attr(base, expr.attr)
        if isinstance(expr, ast.Move):
            return self._eval_expr(expr.value, env)
        if isinstance(expr, ast.ArrayLiteral):
            return [self._eval_expr(elem, env) for elem in expr.elements]
        if isinstance(expr, ast.Index):
            base = self._eval_expr(expr.value, env)
            index = self._eval_expr(expr.index, env)
            label = self._describe_container(expr.value)
            self._check_bounds(base, index, label, expr.loc)
            return base[index]
        if isinstance(expr, ast.TryCatchExpr):
            try:
                return self._eval_expr(expr.attempt, env)
            except RaiseSignal as exc:
                arm = expr.catch_arm
                if arm.event is not None and arm.event != exc.error.message:
                    raise
                catch_env = Environment(parent=env)
                if arm.binder:
                    catch_env.define(arm.binder, exc.error)
                return self._eval_block_result(arm.block, catch_env)
        if isinstance(expr, ast.Ternary):
            cond = bool(self._eval_expr(expr.condition, env))
            branch = expr.then_value if cond else expr.else_value
            return self._eval_expr(branch, env)
        if isinstance(expr, ast.Unary):
            value = self._eval_expr(expr.operand, env)
            if expr.op == "-":
                return -value
            if expr.op == "not":
                return not bool(value)
            raise RuntimeError(f"Unknown unary operator {expr.op}")
        if isinstance(expr, ast.Binary):
            return self._eval_binary(expr, env)
        raise RuntimeError(f"Unsupported expression {expr}")

    def _eval_block_result(self, block: ast.Block, env: Environment) -> object:
        if not block.statements:
            return None
        for stmt in block.statements[:-1]:
            self._execute_block([stmt], env)
        last = block.statements[-1]
        if isinstance(last, ast.ExprStmt):
            return self._eval_expr(last.value, env)
        if isinstance(last, ast.ReturnStmt):
            if last.value is None:
                return None
            return self._eval_expr(last.value, env)
        self._execute_block([last], env)
        return None

    def _eval_binary(self, expr: ast.Binary, env: Environment) -> object:
        op = expr.op
        if op == "and":
            left = bool(self._eval_expr(expr.left, env))
            if not left:
                return False
            return bool(self._eval_expr(expr.right, env))
        if op == "or":
            left = bool(self._eval_expr(expr.left, env))
            if left:
                return True
            return bool(self._eval_expr(expr.right, env))
        left = self._eval_expr(expr.left, env)
        right = self._eval_expr(expr.right, env)
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            return left / right
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == ">>":
            raise RuntimeError("pipeline operator is not supported yet")
        raise RuntimeError(f"Unsupported operator {op}")

    def _resolve_attr(self, base: object, attr: str) -> object:
        if isinstance(base, StructInstance):
            if attr not in base.fields:
                raise RuntimeError(f"Struct '{base.type_name}' has no field '{attr}'")
            return base.fields[attr]
        if hasattr(base, "get_attr"):
            return base.get_attr(attr)
        raise RuntimeError(f"Object {base!r} has no attribute '{attr}'")

    def _invoke(self, func: object, args: Sequence[object], kwargs: Dict[str, object], call_loc: ast.Located) -> object:
        if isinstance(func, BuiltinFunction):
            self._push_frame(func.signature.name, call_loc, self._current_source)
            try:
                return func.impl(self.runtime_ctx, args, kwargs)
            finally:
                self._pop_frame()
        if isinstance(func, UserFunction):
            self._push_frame(func.definition.name, call_loc, self._current_source)
            try:
                return func(args, kwargs)
            finally:
                self._pop_frame()
        if isinstance(func, StructConstructor):
            return func(args, kwargs)
        if isinstance(func, ExceptionConstructor):
            return func(args, kwargs)
        raise RuntimeError(f"Object {func} is not callable")

    def _assign(self, target: ast.Expr, value: object, env: Environment) -> None:
        if isinstance(target, ast.Name):
            env.set(target.ident, value)
            return
        if isinstance(target, ast.Index):
            base = self._eval_expr(target.value, env)
            index = self._eval_expr(target.index, env)
            if not hasattr(base, "__setitem__"):
                raise RuntimeError("Object is not indexable")
            label = self._describe_container(target.value)
            self._check_bounds(base, index, label, target.loc)
            base[index] = value
            return
        raise RuntimeError("Unsupported assignment target")

    def _describe_container(self, expr: ast.Expr) -> str:
        if isinstance(expr, ast.Name):
            return expr.ident
        if isinstance(expr, ast.Attr):
            return expr.attr
        return "Array"

    def _check_bounds(self, base: object, index: object, label: str, loc: ast.Located) -> None:
        if not hasattr(base, "__len__"):
            return
        if not isinstance(index, int):
            raise RuntimeError("Index must be an integer")
        length = len(base)
        if index < 0 or index >= length:
            raise RaiseSignal(
                self._attach_stack(
                    ErrorValue(message="IndexError", attrs={"container": label, "index": index}),
                    loc,
                )
            )

    def _attach_stack(self, err: ErrorValue, loc: ast.Located | None = None) -> ErrorValue:
        if err.stack is None:
            frames = []
            for name, frame_loc, source in self._call_stack:
                frames.append(f"{Path(source).name}:{name}:{frame_loc.line}")
            if loc is not None and self._call_stack:
                caller_name, _, source = self._call_stack[-1]
                frames[-1] = f"{Path(source).name}:{caller_name}:{loc.line}"
            err.stack = frames
        return err

    def _push_frame(self, name: str, loc: ast.Located, source: str) -> None:
        self._call_stack.append((name, loc, source))

    def _pop_frame(self) -> None:
        if self._call_stack:
            self._call_stack.pop()


def run_program(checked: CheckedProgram, stdout=None) -> Interpreter:
    return Interpreter(checked, BUILTINS, stdout=stdout)
@dataclass
class StructInstance:
    type_name: str
    fields: Dict[str, object]


class StructConstructor:
    def __init__(self, info: StructInfo) -> None:
        self.info = info

    def __call__(self, args: Sequence[object], kwargs: Dict[str, object]) -> StructInstance:
        if len(args) > len(self.info.field_order):
            raise RuntimeError(
                f"{self.info.name} expects {len(self.info.field_order)} args, got {len(args)}"
            )
        values: Dict[str, object] = {}
        for field_name, value in zip(self.info.field_order, args):
            values[field_name] = value
        for key, value in kwargs.items():
            if key not in self.info.field_order:
                raise RuntimeError(f"{self.info.name} has no field '{key}'")
            if key in values:
                raise RuntimeError(f"Field '{key}' initialized twice in {self.info.name}")
            values[key] = value
        missing = [name for name in self.info.field_order if name not in values]
        if missing:
            raise RuntimeError(
                f"Missing fields for {self.info.name}: {', '.join(missing)}"
            )
        return StructInstance(type_name=self.info.name, fields=values)


class ExceptionConstructor:
    def __init__(self, info: ExceptionInfo) -> None:
        self.info = info

    def __call__(self, args: Sequence[object], kwargs: Dict[str, object]) -> ErrorValue:
        if len(args) > len(self.info.arg_order):
            raise RuntimeError(
                f"{self.info.name} expects {len(self.info.arg_order)} args, got {len(args)}"
            )
        values: Dict[str, object] = {}
        for arg_name, value in zip(self.info.arg_order, args):
            values[arg_name] = value
        for key, value in kwargs.items():
            if key not in self.info.arg_types:
                raise RuntimeError(f"{self.info.name} has no field '{key}'")
            if key in values:
                raise RuntimeError(f"Argument '{key}' provided multiple times")
            values[key] = value
        missing = [name for name in self.info.arg_order if name not in values]
        if missing:
            raise RuntimeError(
                f"Missing arguments for {self.info.name}: {', '.join(missing)}"
            )
        return ErrorValue(message=self.info.name, attrs=values)
