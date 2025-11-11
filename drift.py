#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lark import UnexpectedInput

from lang import parser
from lang.checker import CheckError, Checker
from lang.interp import Interpreter, RaiseSignal
from lang.runtime import BUILTINS, builtin_signatures


def load_source(path: Path | None) -> str:
    if path is None:
        return sys.stdin.read()
    return path.read_text()


def main() -> int:
    argp = argparse.ArgumentParser(description="Drift language prototype runner")
    argp.add_argument("source", nargs="?", help="Path to a .drift file")
    argp.add_argument("--entry", default="main", help="Function to invoke after loading")
    args = argp.parse_args()

    source_path = Path(args.source) if args.source else None
    try:
        source = load_source(source_path)
    except OSError as exc:
        print(f"error: unable to read source: {exc}", file=sys.stderr)
        return 1

    try:
        program = parser.parse_program(source)
    except UnexpectedInput as exc:
        print(f"parse error: {exc}", file=sys.stderr)
        return 1

    checker = Checker(builtin_signatures())
    try:
        checked = checker.check(program)
    except CheckError as exc:
        print(f"type error: {exc}", file=sys.stderr)
        return 1

    try:
        interpreter = Interpreter(checked, builtins=BUILTINS)
        if args.entry:
            result = interpreter.call(args.entry)
            if result is not None:
                print(result)
    except RaiseSignal as exc:
        err = exc.error
        domain = err.domain or "unknown"
        print(f"runtime error[{domain}]: {err.message}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"runtime error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

