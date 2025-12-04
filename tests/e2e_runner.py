"""SSA-first end-to-end runner (parse -> SSA -> optional codegen/run)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
from pathlib import Path

import argparse
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
DRIFTC = ROOT / ".venv" / "bin" / "python3"
DRIFTC_MODULE = "lang.driftc"
BUILD_ROOT = ROOT / "build" / "tests" / "e2e"


def _have_llvmlite() -> bool:
    try:
        import llvmlite  # noqa: F401
    except ImportError:
        return False
    return True


def _find_clang() -> str | None:
    for name in ("clang-15", "clang"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _run_case(case_dir: Path) -> str:
    expected = json.loads((case_dir / "expected.json").read_text())
    mode = expected.get("mode", "compile")  # "compile" or "run"
    requires_llvmlite = expected.get("skip_if", {}).get("requires_llvmlite", False)
    if (mode == "run" or requires_llvmlite) and not _have_llvmlite():
        return "skipped (llvmlite missing)"
    build_dir = BUILD_ROOT / case_dir.name
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    cmd = [
        str(DRIFTC),
        "-m",
        DRIFTC_MODULE,
        str(case_dir / "main.drift"),
        "-o",
        str(build_dir / "a.o"),
        "--ssa-check",
        "--ssa-simplify",
    ]
    compile_res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)

    compile_err = expected.get("compile_error")
    if compile_err is not None:
        if compile_res.returncode == 0:
            return f"FAIL (expected compile error containing {compile_err!r}, got success)"
        if compile_err not in compile_res.stderr:
            return f"FAIL (compile error mismatch; wanted substring {compile_err!r})"
        return "ok"

    if compile_res.returncode != 0:
        return f"FAIL (compile failed: {compile_res.stderr})"

    if mode == "compile":
        return "ok"

    # mode == "run": link and execute.
    clang = _find_clang()
    if not clang:
        return "skipped (clang not found)"
    exe_path = build_dir / "a.out"
    runtime_sources = [
        ROOT / "lang" / "runtime" / "string_runtime.c",
        ROOT / "lang" / "runtime" / "console_runtime.c",
        ROOT / "lang" / "runtime" / "error_dummy.c",
        ROOT / "lang" / "runtime" / "diagnostic_runtime.c",
    ]
    link_cmd = [clang, str(case_dir / "a.o")] + [str(p) for p in runtime_sources] + ["-o", str(exe_path)]
    link = subprocess.run(link_cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    if link.returncode != 0:
        return f"FAIL (link failed: {link.stderr})"

    run_res = subprocess.run([str(exe_path)] + expected.get("args", []), capture_output=True, text=True, env=env)
    exit_expected = expected.get("exit_code", 0)
    if run_res.returncode != exit_expected:
        return f"FAIL (exit {run_res.returncode}, expected {exit_expected})"
    if run_res.stdout != expected.get("stdout", ""):
        return "FAIL (stdout mismatch)"
    if run_res.stderr != expected.get("stderr", ""):
        return "FAIL (stderr mismatch)"
    return "ok"


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run e2e Drift tests")
    ap.add_argument("cases", nargs="*", help="Specific test case names to run (directories under tests/e2e)")
    args = ap.parse_args(argv)

    failures: list[tuple[Path, str]] = []
    case_dirs = sorted((ROOT / "tests" / "e2e").iterdir())
    if args.cases:
        names = set(args.cases)
        case_dirs = [d for d in case_dirs if d.name in names]
    for case_dir in case_dirs:
        if not (case_dir / "main.drift").exists():
            continue
        status = _run_case(case_dir)
        print(f"{case_dir.name}: {status}")
        if not status.startswith("ok") and not status.startswith("skipped"):
            failures.append((case_dir, status))
    if failures:
        for case, status in failures:
            print(f"[e2e] {case.name}: {status}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
