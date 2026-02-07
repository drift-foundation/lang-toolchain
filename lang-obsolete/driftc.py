#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import os

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
# Ensure project root is ahead of the script directory to avoid shadowing stdlib modules (e.g., types)
sys.path = [str(ROOT)] + [p for p in sys.path if p != str(SCRIPT_DIR)]

from lang import parser, checker, mir  # type: ignore
from lang.lower_to_mir_ssa import lower_function_ssa, LoweringError
from lang.runtime import builtin_signatures
from lang.mir_verifier_ssa_v2 import SSAVerifierV2
from lang.mir_simplify_ssa import simplify_function
from lang.ssa_codegen import emit_dummy_main_object, emit_module_object
from lang.ir_layout import StructLayout


def _annotate_can_error(funcs: list[mir.Function]) -> None:
    """Mark functions that can produce errors and enforce basic invariants."""
    name_to_fn = {f.name: f for f in funcs}
    # Seed from function bodies.
    for f in funcs:
        for block in f.blocks.values():
            term = block.terminator
            if isinstance(term, mir.Throw):
                f.can_error = True
            if isinstance(term, mir.Return) and term.error is not None:
                f.can_error = True
    # Enforce: Throw only in can-error functions.
    for f in funcs:
        for block in f.blocks.values():
            if isinstance(block.terminator, mir.Throw) and not f.can_error:
                raise RuntimeError(f"throw in non-can-error function {f.name}")
    # Enforce: call-with-edges only to can-error functions.
    for f in funcs:
        for block in f.blocks.values():
            term = block.terminator
            if isinstance(term, mir.Call) and term.normal and term.error:
                callee = name_to_fn.get(term.callee)
                if callee and not callee.can_error:
                    raise RuntimeError(f"call with error edges to non-can-error function {term.callee}")
            # Plain calls to can-error functions must not drop the error channel.
            for instr in block.instructions:
                if isinstance(instr, mir.Call) and not (instr.normal or instr.error):
                    callee = name_to_fn.get(instr.callee)
                    if callee and callee.can_error:
                        raise RuntimeError(f"call to can-error function {instr.callee} without error edges")


def _dump_ssa(fn_name: str, blocks: dict[str, mir.BasicBlock], file=None) -> None:
    if file is None:
        file = sys.stderr
    print(f"== SSA for {fn_name} ==", file=file)
    for name, block in blocks.items():
        params = ", ".join(f"{p.name}:{p.type}" for p in block.params)
        print(f"block {name}({params})", file=file)
        for instr in block.instructions:
            print(f"  {instr}", file=file)
        print(f"  term {block.terminator}", file=file)


def _run_ssa_check(checked: checker.CheckedProgram, simplify: bool, dump: bool) -> None:
    """Lower all user-defined functions through the SSA scaffold and verify structure."""
    for fn_def in checked.program.functions:
        if fn_def.name not in checked.functions:
            continue
        try:
            lowered = lower_function_ssa(fn_def, checked)
        except LoweringError as e:
            raise SystemExit(f"SSA lowering failed for {fn_def.name}: {e}")
        fn_blocks = lowered.blocks
        fn_info = checked.functions.get(lowered.name, checked.functions[fn_def.name])
        if simplify:
            fn_blocks = simplify_function(
                mir.Function(
                    name=lowered.name,
                    params=[
                        mir.Param(name=p.name, type=fn_info.signature.params[idx])
                        for idx, p in enumerate(fn_def.params)
                    ],
                    return_type=fn_info.signature.return_type,
                    entry=lowered.entry,
                    module=checked.module or "<module>",
                    source=None,
                    blocks=fn_blocks,
                )
            ).blocks
        if dump:
            _dump_ssa(fn_def.name, fn_blocks)
        verifier_fn = type("F", (), {"blocks": fn_blocks, "entry": lowered.entry})
        SSAVerifierV2(verifier_fn).verify()


def compile_file(
    source_path: Path,
    output_path: Path,
    emit_ir: bool,
    ssa_check: bool,
    ssa_mode: str,
    ssa_simplify: bool,
    dump_ssa: bool,
) -> int:
    source = source_path.read_text()
    prog = parser.parse_program(source)
    checked = checker.Checker(builtin_signatures()).check(prog)
    if ssa_check:
        try:
            _run_ssa_check(checked, simplify=ssa_simplify, dump=dump_ssa)
        except Exception as e:
            if ssa_mode == "warn":
                print(f"[ssa-check] warning: {e}", file=sys.stderr)
            else:
                raise
    ssa_fns: list[mir.Function] = []
    struct_layouts: dict[str, StructLayout] = {}
    for sname, sinfo in checked.structs.items():
        field_names = list(sinfo.field_order)
        field_types = [sinfo.field_types[name] for name in field_names]
        struct_layouts[sname] = StructLayout(name=sname, field_names=field_names, field_types=field_types)
    for fn_def in checked.program.functions:
        if fn_def.name not in checked.functions:
            continue
        lowered = lower_function_ssa(fn_def, checked)
        fn_info = checked.functions.get(lowered.name, checked.functions[fn_def.name])
        ssa_fn = mir.Function(
            name=lowered.name,
            params=[mir.Param(name=p.name, type=fn_info.signature.params[idx]) for idx, p in enumerate(fn_def.params)],
            return_type=fn_info.signature.return_type,
            entry=lowered.entry,
            module=checked.module or prog.module or "<module>",
            source=str(source_path),
            blocks=lowered.blocks,
        )
        simplified = simplify_function(ssa_fn)
        ssa_fns.append(simplified)
    _annotate_can_error(ssa_fns)
    emit_module_object(ssa_fns, struct_layouts, entry="main", out_path=output_path, exception_names=set(checked.exceptions.keys()))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="driftc: minimal Drift -> MIR -> LLVM compiler (straight-line subset)")
    ap.add_argument("source", type=Path, help="Drift source file")
    ap.add_argument("-o", "--output", type=Path, help="Output object file (.o)", required=True)
    ap.add_argument("--emit-ir", action="store_true", help="Print the generated LLVM IR to stdout")
    ap.add_argument(
        "--ssa-check",
        action="store_true",
        help="Run the strict SSA lowering + verifier before codegen",
    )
    ap.add_argument(
        "--ssa-check-mode",
        choices=["fail", "warn"],
        default="fail",
        help="When --ssa-check is enabled: fail on SSA errors (default) or warn and continue",
    )
    ap.add_argument(
        "--ssa-simplify",
        action="store_true",
        help="Run SSA simplification (const folding, dead SSA removal) before SSA verification",
    )
    ap.add_argument(
        "--dump-ssa",
        action="store_true",
        help="When --ssa-check is enabled: dump the (optionally simplified) SSA blocks",
    )
    args = ap.parse_args(argv)

    return compile_file(
        args.source,
        args.output,
        args.emit_ir,
        args.ssa_check,
        args.ssa_check_mode,
        args.ssa_simplify,
        args.dump_ssa,
    )


if __name__ == "__main__":
    raise SystemExit(main())
