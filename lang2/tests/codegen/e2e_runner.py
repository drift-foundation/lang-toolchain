"""
LLVM IR end-to-end runner for lang2 codegen tests (clang-only).

Each test case lives under `lang2/tests/codegen/<case>/` and should contain:
  - main.drift (or a placeholder when parser/front-end are stubbed)
  - expected.json with at least:
      {
        "exit_code": <int>,
        "stdout": "<string>",
        "stderr": "<string>"
      }
    (Optional) "args": [...]               # argv passed to the executable

Build artifacts are written to `build/tests/lang2/<case>/` to keep them
isolated from lang/ tests. The runner compiles `ir.ll` with clang (env
`CLANG_BIN`, default clang-15, then clang) and compares exit/stdout/stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[3]
BUILD_ROOT = ROOT / "build" / "tests" / "lang2"


def _run_case(case_dir: Path) -> str:
	"""
	Run a single codegen e2e case:
	  1) use the prebuilt LLVM IR file under case_dir/ir.ll (for now),
	  2) compile it with clang, run the binary,
	  3) compare exit/stdout/stderr to expected.json.

	Note: until the lang2 parser/CLI is wired, tests are expected to supply
	ready-to-run LLVM IR (ir.ll) in the case dir. This runner focuses on
	execution/validation; wiring MIR→SSA→LLVM can be added later.
	"""
	expected = json.loads((case_dir / "expected.json").read_text())

	build_dir = BUILD_ROOT / case_dir.name
	if build_dir.exists():
		shutil.rmtree(build_dir)
	build_dir.mkdir(parents=True, exist_ok=True)

	ir_path = case_dir / "ir.ll"
	if not ir_path.exists():
		return "FAIL (missing ir.ll; wire codegen to emit it)"

	clang_bin: Optional[str] = os.environ.get("CLANG_BIN") or "clang-15"
	clang = shutil.which(clang_bin) or shutil.which("clang")
	if clang is None:
		return "skipped (clang not found)"

	bin_path = build_dir / "a.out"
	compile_res = subprocess.run(
		[clang, "-x", "ir", str(ir_path), "-o", str(bin_path)],
		cwd=ROOT,
		capture_output=True,
		text=True,
	)
	if compile_res.returncode != 0:
		return f"FAIL (clang failed: {compile_res.stderr.strip()})"

	run_res = subprocess.run(
		[str(bin_path)] + expected.get("args", []),
		cwd=ROOT,
		capture_output=True,
		text=True,
	)

	exit_expected = expected.get("exit_code", 0)
	if run_res.returncode != exit_expected:
		return f"FAIL (exit {run_res.returncode}, expected {exit_expected})"
	if run_res.stdout != expected.get("stdout", ""):
		return "FAIL (stdout mismatch)"
	if run_res.stderr != expected.get("stderr", ""):
		return "FAIL (stderr mismatch)"
	return "ok"


def main(argv: Iterable[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description="Run lang2 codegen e2e tests (lli-based)")
	ap.add_argument(
		"cases",
		nargs="*",
		help="Specific test case names to run (directories under lang2/tests/codegen)",
	)
	args = ap.parse_args(argv)

	case_dirs = sorted((ROOT / "lang2" / "tests" / "codegen").iterdir())
	if args.cases:
		names = set(args.cases)
		case_dirs = [d for d in case_dirs if d.name in names]

	failures: list[tuple[Path, str]] = []
	for case_dir in case_dirs:
		if not (case_dir / "expected.json").exists():
			continue
		status = _run_case(case_dir)
		print(f"{case_dir.name}: {status}")
		if not status.startswith("ok") and not status.startswith("skipped"):
			failures.append((case_dir, status))

	if failures:
		for case, status in failures:
			print(f"[codegen] {case.name}: {status}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
