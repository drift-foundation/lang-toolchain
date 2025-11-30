#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, List

from lark import UnexpectedInput

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lang import ast, parser  # noqa: E402

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
PASCAL_CASE = re.compile(r"^[A-Z][A-Za-z0-9]*$")


def main() -> int:
    args = _parse_args()
    files = sorted(_collect_files(args.paths))
    if not files:
        print("draft-linter: no .drift files found", file=sys.stderr)
        return 1

    errors: List[str] = []
    for path in files:
        text = path.read_text()
        _check_indentation(path, text, args.indent, errors)
        try:
            program = parser.parse_program(text)
        except UnexpectedInput as exc:
            errors.append(f"{path}: parse error: {exc}")
            continue
        lint_file(path, program, errors)

    for error in errors:
        print(f"[lint] {error}", file=sys.stderr)

    return 1 if errors else 0


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Draft linter for Drift snippets")
    ap.add_argument(
        "paths",
        nargs="*",
        default=["playground", "examples", "tests/programs"],
        help="files or directories to lint",
    )
    ap.add_argument(
        "--indent",
        choices=("tabs", "spaces"),
        default="tabs",
        help="preferred indentation style (default: tabs)",
    )
    return ap.parse_args()


def _collect_files(targets: Iterable[str]) -> Iterable[Path]:
    seen = set()
    for target in targets:
        base = Path(target)
        if base.is_file() and base.suffix == ".drift":
            resolved = base.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield resolved
            continue
        if base.is_dir():
            for path in base.rglob("*.drift"):
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    yield resolved


def _check_indentation(path: Path, text: str, style: str, errors: List[str]) -> None:
    for idx, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip("\t ")
        if not stripped:
            continue
        indent = line[: len(line) - len(stripped)]
        if not indent:
            continue
        if style == "tabs":
            if " " in indent:
                errors.append(f"{path}:{idx}: indentation must use tabs (found spaces)")
        else:
            if "\t" in indent:
                errors.append(f"{path}:{idx}: indentation must use spaces (found tabs)")


def lint_file(path: Path, program: ast.Program, errors: List[str]) -> None:
    for struct in program.structs:
        _expect_pascal(path, struct.loc, struct.name, "struct", errors)
    for exc in getattr(program, "exceptions", []):
        _expect_pascal(path, exc.loc, exc.name, "exception", errors)
    for fn in program.functions:
        _expect_snake(path, fn.loc, fn.name, "function", errors)
        for param in fn.params:
            _expect_snake(path, fn.loc, param.name, "parameter", errors)
        _lint_block(path, fn.body, errors)
    for stmt in program.statements:
        _lint_stmt(path, stmt, errors)


def _lint_block(path: Path, block: ast.Block, errors: List[str]) -> None:
    for stmt in block.statements:
        _lint_stmt(path, stmt, errors)


def _lint_stmt(path: Path, stmt: ast.Stmt, errors: List[str]) -> None:
    if isinstance(stmt, ast.LetStmt):
        _expect_snake(path, stmt.loc, stmt.name, "binding", errors)
        _lint_expr(path, stmt.value, errors)
    elif isinstance(stmt, ast.AssignStmt):
        _lint_expr(path, stmt.value, errors)
    elif isinstance(stmt, ast.IfStmt):
        _lint_block(path, stmt.then_block, errors)
        if stmt.else_block:
            _lint_block(path, stmt.else_block, errors)
    elif isinstance(stmt, ast.ReturnStmt) and stmt.value:
        _lint_expr(path, stmt.value, errors)
    elif isinstance(stmt, ast.RaiseStmt):
        _lint_expr(path, stmt.value, errors)
    elif isinstance(stmt, ast.ExprStmt):
        _lint_expr(path, stmt.value, errors)
    elif isinstance(stmt, ast.TryStmt):
        _lint_block(path, stmt.body, errors)
        for clause in stmt.catches:
            if clause.binder:
                _expect_snake(path, stmt.loc, clause.binder, "catch binding", errors)
            _lint_block(path, clause.block, errors)


def _lint_expr(path: Path, expr: ast.Expr, errors: List[str]) -> None:
    if isinstance(expr, ast.Call):
        for arg in expr.args:
            _lint_expr(path, arg, errors)
        for kw in expr.kwargs:
            _lint_expr(path, kw.value, errors)
    elif isinstance(expr, ast.TryCatchExpr):
        _lint_expr(path, expr.attempt, errors)
        for stmt in expr.catch_arm.block.statements:
            if isinstance(stmt, ast.ExprStmt):
                _lint_expr(path, stmt.value, errors)
    elif isinstance(expr, ast.Ternary):
        _lint_expr(path, expr.condition, errors)
        _lint_expr(path, expr.then_value, errors)
        _lint_expr(path, expr.else_value, errors)
    elif isinstance(expr, ast.ArrayLiteral):
        for element in expr.elements:
            _lint_expr(path, element, errors)


def _expect_snake(path: Path, loc: ast.Located, name: str, kind: str, errors: List[str]) -> None:
    if not SNAKE_CASE.match(name):
        errors.append(
            f"{path}:{loc.line}:{loc.column}: {kind} '{name}' should be snake_case"
        )


def _expect_pascal(path: Path, loc: ast.Located, name: str, kind: str, errors: List[str]) -> None:
    if not PASCAL_CASE.match(name):
        errors.append(
            f"{path}:{loc.line}:{loc.column}: {kind} '{name}' should be PascalCase"
        )


if __name__ == "__main__":
    raise SystemExit(main())
