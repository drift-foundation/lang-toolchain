#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROGRAMS_DIR = ROOT / "tests" / "programs"
EXPECTATIONS_DIR = ROOT / "tests" / "expectations"
MIR_CASES_DIR = ROOT / "tests" / "mir_lowering"
CODEGEN_DIR = ROOT / "tests" / "mir_codegen"
VENV_SITE = ROOT / ".venv"


def load_expectation(name: str) -> Dict[str, object]:
    path = EXPECTATIONS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"missing expectation file for {name}: {path}")
    return json.loads(path.read_text())


def run_program(path: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "drift.py"), str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    failures = 0
    failures += _run_mir_tests()
    failures += _run_verifier_negative_tests()
    failures += _run_codegen_tests()
    return 1 if failures else 0


def _run_mir_tests() -> int:
    from lang import parser, checker
    from lang.runtime import builtin_signatures
    from lang.lower_to_mir import lower_straightline
    from lang.mir_printer import format_program
    from lang.mir_verifier import verify_program

    failures = 0
    for source_path in sorted(MIR_CASES_DIR.glob("*.drift")):
        name = source_path.stem
        expected_path = MIR_CASES_DIR / f"{name}.mir"
        if not expected_path.exists():
            print(f"[missing] {source_path}: expected MIR {expected_path} not found", file=sys.stderr)
            failures += 1
            continue
        source = source_path.read_text()
        prog = parser.parse_program(source)
        checked = checker.Checker(builtin_signatures()).check(prog)
        mir_prog = lower_straightline(
            checked, source_name=str(source_path), module_name=checked.module or prog.module
        )
        try:
            verify_program(mir_prog)
        except Exception as exc:
            failures += 1
            print(f"[fail] MIR {name}: verification error {exc}", file=sys.stderr)
            continue
        # Emit MIR text
        rendered = format_program(mir_prog)
        expected = expected_path.read_text().rstrip()
        if rendered.strip() != expected.strip():
            failures += 1
            print(f"[fail] MIR {name}: mismatch", file=sys.stderr)
            print("=== expected ===", file=sys.stderr)
            print(expected, file=sys.stderr)
            print("=== got ===", file=sys.stderr)
            print(rendered, file=sys.stderr)
        else:
            print(f"[ok] MIR {name}")
    return failures


def _run_codegen_tests() -> int:
    """End-to-end MIR -> LLVM -> clang-15 link/run tests."""
    _require_venv_site()
    try:
        from lang import parser, checker  # type: ignore
        from lang.runtime import builtin_signatures  # type: ignore
        from lang.lower_to_mir import lower_straightline  # type: ignore
        from lang.mir_verifier import verify_program  # type: ignore
        from lang.mir_to_llvm import lower_function  # type: ignore
    except ModuleNotFoundError as exc:
        print(f"[skip] codegen tests: {exc}", file=sys.stderr)
        return 0

    failures = 0
    out_dir = CODEGEN_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    for case_dir in sorted(CODEGEN_DIR.iterdir()):
        if not case_dir.is_dir():
            continue
        drift_path = case_dir / "input.drift"
        expect_path = case_dir / "expect.json"
        harness_path = case_dir / "main.c"
        if not drift_path.exists() or not expect_path.exists() or not harness_path.exists():
            continue
        expected = json.loads(expect_path.read_text())
        try:
            llvm_ir, exe_path = _build_and_link(drift_path, harness_path, out_dir, case_dir.name)
            exit_code, stdout, stderr = _run_exe(exe_path)
        except FileNotFoundError as exc:
            print(f"[skip] codegen {case_dir.name}: {exc}", file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[fail] codegen {case_dir.name}: {exc}", file=sys.stderr)
            continue

        ok = True
        if expected.get("exit") is not None and exit_code != expected["exit"]:
            ok = False
            print(f"[fail] codegen {case_dir.name}: exit {exit_code} != {expected['exit']}", file=sys.stderr)
        if expected.get("stdout") is not None and stdout != expected["stdout"]:
            ok = False
            print(f"[fail] codegen {case_dir.name}: stdout mismatch", file=sys.stderr)
            print("=== expected ===", file=sys.stderr)
            print(expected["stdout"], end="", file=sys.stderr)
            print("=== got ===", file=sys.stderr)
            print(stdout, end="", file=sys.stderr)
        if expected.get("stderr") is not None and stderr != expected["stderr"]:
            ok = False
            print(f"[fail] codegen {case_dir.name}: stderr mismatch", file=sys.stderr)
            print("=== expected ===", file=sys.stderr)
            print(expected["stderr"], end="", file=sys.stderr)
            print("=== got ===", file=sys.stderr)
            print(stderr, end="", file=sys.stderr)

        if ok:
            print(f"[ok] codegen {case_dir.name}")
        else:
            failures += 1
    return failures


def _build_and_link(drift_path: Path, harness_path: Path, out_dir: Path, case: str) -> tuple[str, Path]:
    _require_venv_site()
    from lang import parser, checker
    from lang.runtime import builtin_signatures
    from lang.lower_to_mir import lower_straightline
    from lang.mir_verifier import verify_program
    from lang.mir_to_llvm import lower_function
    import subprocess

    # Allow an optional run.drift wrapper; otherwise use input.drift.
    entry_path = drift_path
    run_path = drift_path.parent / "run.drift"
    src = entry_path.read_text()
    prog = parser.parse_program(src)
    checked = checker.Checker(builtin_signatures()).check(prog)
    mir_prog = lower_straightline(
        checked, source_name=str(drift_path), module_name=checked.module or prog.module
    )
    verify_program(mir_prog)
    llvm_ir = ""
    obj_paths: list[Path] = []
    for fn in mir_prog.functions.values():
        ir_text, obj_bytes = lower_function(fn)
        llvm_ir = ir_text  # keep last for logging
        obj_path = out_dir / f"{case}_{fn.name}.o"
        obj_path.write_bytes(obj_bytes)
        obj_paths.append(obj_path)
    # Compile shared runtime stubs (PIC for PIE linking)
    runtime_dir = CODEGEN_DIR / "runtime"
    c_objects = []
    for c_path in sorted(runtime_dir.glob("*.c")):
        c_obj = out_dir / f"{case}_{c_path.stem}_rt.o"
        subprocess.run(["clang-15", "-fPIC", "-c", str(c_path), "-o", str(c_obj)], check=True)
        c_objects.append(str(c_obj))
    # Compile all .c files in the case directory
    for c_path in sorted(drift_path.parent.glob("*.c")):
        c_obj = out_dir / f"{case}_{c_path.stem}.o"
        subprocess.run(["clang-15", "-fPIC", "-c", str(c_path), "-o", str(c_obj)], check=True)
        c_objects.append(str(c_obj))
    exe_path = out_dir / f"{case}_exe"
    # Link as PIE now that objects are PIC.
    subprocess.run(["clang-15", "-pie", *c_objects, *(str(p) for p in obj_paths), "-o", str(exe_path)], check=True)
    return llvm_ir, exe_path


def _run_exe(exe_path: Path) -> tuple[int, str, str]:
    proc = subprocess.run([str(exe_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _require_venv_site() -> None:
    """Force use of the local .venv site-packages for codegen."""
    if not VENV_SITE.exists():
        raise RuntimeError("codegen tests require .venv with llvmlite installed")
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    sp = VENV_SITE / "lib" / ver / "site-packages"
    if sp.exists():
        sys.path.insert(0, str(sp))
    else:
        raise RuntimeError(f"codegen tests require llvmlite in .venv (missing {sp})")


def _run_verifier_negative_tests() -> int:
    """Manually construct small MIR fragments that should fail verification."""
    from lang import mir
    from lang.mir_verifier import verify_function, VerificationError
    from lang.types import I64, BOOL

    failures = 0

    # Case: use-before-def in a single block.
    fn1 = mir.Function(
        name="bad_use",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={
            "bb0": mir.BasicBlock(
                name="bb0",
                instructions=[
                    mir.Binary(dest="_t1", op="+", left="x", right="y"),  # x, y undefined
                ],
                terminator=mir.Return(value="_t1"),
            )
        },
    )

    # Case: edge arg mismatch (passes wrong arity to successor).
    bb1 = mir.BasicBlock(name="bb1", terminator=mir.Return())
    bb2 = mir.BasicBlock(name="bb2", params=[mir.Param(name="p", type=I64)], terminator=mir.Return(value="p"))
    fn2 = mir.Function(
        name="bad_edge",
        params=[mir.Param(name="x", type=I64)],
        return_type=I64,
        entry="bb1",
        blocks={
            "bb1": mir.BasicBlock(
                name="bb1",
                params=[mir.Param(name="x", type=I64)],
                terminator=mir.Br(target=mir.Edge(target="bb2", args=[])),  # missing arg for bb2(p)
            ),
            "bb2": bb2,
        },
    )

    # Case: dominance violation (use a value defined only in a non-dominating branch).
    fn3 = mir.Function(
        name="bad_dom",
        params=[mir.Param(name="c", type=BOOL)],
        return_type=I64,
        entry="bb0",
        blocks={
            "bb0": mir.BasicBlock(
                name="bb0",
                params=[mir.Param(name="c", type=BOOL)],
                instructions=[],
                terminator=mir.CondBr(
                    cond="c",
                    then=mir.Edge(target="bb_then", args=[]),
                    els=mir.Edge(target="bb_else", args=[]),
                ),
            ),
            "bb_then": mir.BasicBlock(
                name="bb_then",
                instructions=[mir.Const(dest="x", type=I64, value=1)],
                terminator=mir.Br(target=mir.Edge(target="bb_join", args=["x"])),
            ),
            "bb_else": mir.BasicBlock(
                name="bb_else",
                instructions=[],
                terminator=mir.Br(target=mir.Edge(target="bb_join", args=[])),  # missing x
            ),
            "bb_join": mir.BasicBlock(
                name="bb_join",
                params=[mir.Param(name="x", type=I64)],
                terminator=mir.Return(value="x"),
            ),
        },
    )

    # Case: type mismatch on edge argument.
    fn4 = mir.Function(
        name="bad_edge_type",
        params=[mir.Param(name="c", type=BOOL)],
        return_type=I64,
        entry="bb0",
        blocks={
            "bb0": mir.BasicBlock(
                name="bb0",
                params=[mir.Param(name="c", type=BOOL)],
                instructions=[mir.Const(dest="b", type=BOOL, value=True)],
                terminator=mir.Br(target=mir.Edge(target="bb1", args=["b"])),  # bb1 expects Int
            ),
            "bb1": mir.BasicBlock(
                name="bb1",
                params=[mir.Param(name="x", type=I64)],
                terminator=mir.Return(value="x"),
            ),
        },
    )

    # Case: edge references undefined value from source block.
    fn5 = mir.Function(
        name="bad_edge_undef_arg",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={
            "bb0": mir.BasicBlock(
                name="bb0",
                instructions=[],
                terminator=mir.Br(target=mir.Edge(target="bb1", args=["z"])),  # z not defined
            ),
            "bb1": mir.BasicBlock(
                name="bb1",
                params=[mir.Param(name="p", type=I64)],
                terminator=mir.Return(value="p"),
            ),
        },
    )

    # Case: use-after-move.
    fn6 = mir.Function(
        name="bad_use_after_move",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={
            "bb0": mir.BasicBlock(
                name="bb0",
                instructions=[
                    mir.Const(dest="x", type=I64, value=1),
                    mir.Move(dest="y", source="x"),
                    mir.Binary(dest="z", op="+", left="x", right="y"),  # x was moved
                ],
                terminator=mir.Return(value="z"),
            )
        },
    )

    # Case: double-drop.
    fn7 = mir.Function(
        name="bad_double_drop",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={
            "bb0": mir.BasicBlock(
                name="bb0",
                instructions=[
                    mir.Const(dest="x", type=I64, value=1),
                    mir.Drop(value="x"),
                    mir.Drop(value="x"),  # double drop
                ],
                terminator=mir.Return(),
            )
        },
    )

    # Case: return with wrong type.
    fn8 = mir.Function(
        name="bad_return_type",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={
            "bb0": mir.BasicBlock(
                name="bb0",
                instructions=[mir.Const(dest="b", type=BOOL, value=True)],
                terminator=mir.Return(value="b"),  # returning Bool to Int function
            )
        },
    )

    # Case: error edge param type mismatch (first param not Error).
    fn9 = mir.Function(
        name="bad_error_edge",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={
            "bb0": mir.BasicBlock(
                name="bb0",
                instructions=[mir.Const(dest="e", type=I64, value=1)],  # wrong type for error
                terminator=mir.Raise(error="e"),
            )
        },
    )

    # Case: missing terminator.
    fn10 = mir.Function(
        name="bad_missing_term",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={
            "bb0": mir.BasicBlock(
                name="bb0",
                instructions=[mir.Const(dest="x", type=I64, value=1)],
                terminator=None,  # invalid
            )
        },
    )

    # Case: edge to unknown block.
    fn11 = mir.Function(
        name="bad_unknown_block",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={
            "bb0": mir.BasicBlock(
                name="bb0",
                instructions=[],
                terminator=mir.Br(target=mir.Edge(target="bb_missing", args=[])),
            )
        },
    )

    cases = [
        ("use_before_def", fn1),
        ("edge_arity_mismatch", fn2),
        ("dominance_violation", fn3),
        ("edge_type_mismatch", fn4),
        ("edge_undef_arg", fn5),
        ("use_after_move", fn6),
        ("double_drop", fn7),
        ("return_type_mismatch", fn8),
        ("error_edge_type", fn9),
        ("missing_terminator", fn10),
        ("unknown_block", fn11),
    ]

    for name, fn in cases:
        try:
            verify_function(fn)
            print(f"[fail] MIR verifier negative {name}: expected failure", file=sys.stderr)
            failures += 1
        except VerificationError:
            print(f"[ok] MIR verifier negative {name}")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
