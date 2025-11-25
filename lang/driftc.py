#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
# Ensure project root is ahead of the script directory to avoid shadowing stdlib modules (e.g., types)
sys.path = [str(ROOT)] + [p for p in sys.path if p != str(SCRIPT_DIR)]

from lang import parser, checker  # type: ignore
from lang.lower_to_mir import lower_straightline
from lang.mir_to_llvm import lower_function
from lang.runtime import builtin_signatures
from lang.mir_verifier import verify_program


def compile_file(source_path: Path, output_path: Path, emit_ir: bool) -> int:
    source = source_path.read_text()
    prog = parser.parse_program(source)
    checked = checker.Checker(builtin_signatures()).check(prog)
    mir_prog = lower_straightline(checked, source_name=str(source_path))
    verify_program(mir_prog)
    # TODO: handle multiple functions; currently only the first is emitted.
    fn = next(iter(mir_prog.functions.values()))
    llvm_ir, obj_bytes = lower_function(fn)
    if emit_ir:
        print(llvm_ir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(obj_bytes)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="driftc: minimal Drift -> MIR -> LLVM compiler (straight-line subset)")
    ap.add_argument("source", type=Path, help="Drift source file")
    ap.add_argument("-o", "--output", type=Path, help="Output object file (.o)", required=True)
    ap.add_argument("--emit-ir", action="store_true", help="Print the generated LLVM IR to stdout")
    args = ap.parse_args(argv)

    return compile_file(args.source, args.output, args.emit_ir)


if __name__ == "__main__":
    raise SystemExit(main())
