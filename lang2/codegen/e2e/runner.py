"""
Drift-source end-to-end runner (clang-based).

Each case lives under `lang2/codegen/e2e/<case>/` and must provide:
  - main.drift   (parsed with the copied lang2 parser)
  - expected.json with exit_code/stdout/stderr fields

The runner:
  1) Parses main.drift with the lang2 parser copy.
  2) Lowers to HIR via AstToHIR.
  3) Runs the full stubbed pipeline to LLVM IR (`compile_to_llvm_ir_for_tests`).
  4) Compiles IR with clang and executes the binary.
  5) Asserts diagnostics are empty and compares exit/stdout/stderr to expected.json.

Artifacts are written to `build/tests/lang2/codegen/e2e/<case>/`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

from lang2.parser import parse_drift_to_hir
from lang2.driftc import compile_to_llvm_ir_for_tests


ROOT = Path(__file__).resolve().parents[3]
BUILD_ROOT = ROOT / "build" / "tests" / "lang2" / "codegen" / "e2e"


def _run_ir_with_clang(ir: str, build_dir: Path) -> tuple[int, str, str]:
	"""Compile the provided LLVM IR with clang and return (exit, stdout, stderr)."""
	clang = shutil.which("clang-15") or shutil.which("clang")
	if clang is None:
		return -999, "", "clang not available"

	build_dir.mkdir(parents=True, exist_ok=True)
	ir_path = build_dir / "ir.ll"
	bin_path = build_dir / "a.out"
	ir_path.write_text(ir)

	compile_res = subprocess.run(
		[
			clang,
			"-x",
			"ir",
			str(ir_path),
			"-x",
			"c",
			# Link the runtimes for String/Array helpers used in codegen.
			str(ROOT / "lang2" / "codegen" / "runtime" / "array_runtime.c"),
			str(ROOT / "lang2" / "codegen" / "runtime" / "string_runtime.c"),
			"-o",
			str(bin_path),
		],
		capture_output=True,
		text=True,
		cwd=ROOT,
	)
	if compile_res.returncode != 0:
		return compile_res.returncode, "", compile_res.stderr

	run_res = subprocess.run(
		[str(bin_path)],
		capture_output=True,
		text=True,
		cwd=ROOT,
	)
	return run_res.returncode, run_res.stdout, run_res.stderr


def _run_case(case_dir: Path) -> str:
	expected_path = case_dir / "expected.json"
	source_path = case_dir / "main.drift"
	if not expected_path.exists() or not source_path.exists():
		return "skipped (missing expected.json or main.drift)"

	expected = json.loads(expected_path.read_text())
	func_hirs, signatures, type_table = parse_drift_to_hir(source_path)
	# Prefer a user-facing `main` entry when present; fall back to `drift_main`
	# or the first function as a last resort.
	if "main" in func_hirs:
		entry = "main"
	elif "drift_main" in func_hirs:
		entry = "drift_main"
	else:
		entry = next(iter(func_hirs))

	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		entry=entry,
		type_table=type_table,
	)

	build_dir = BUILD_ROOT / case_dir.name
	exit_code, stdout, stderr = _run_ir_with_clang(ir, build_dir)
	if exit_code == -999:
		return "skipped (clang not available)"

	if checked.diagnostics:
		return f"FAIL (diagnostics: {[d.message for d in checked.diagnostics]})"

	if exit_code != expected.get("exit_code", 0):
		return f"FAIL (exit {exit_code}, expected {expected.get('exit_code', 0)})"
	if stdout != expected.get("stdout", ""):
		return "FAIL (stdout mismatch)"
	if stderr != expected.get("stderr", ""):
		return "FAIL (stderr mismatch)"
	return "ok"


def main(argv: Iterable[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description="Run Drift-source codegen e2e cases (clang-based)")
	ap.add_argument(
		"cases",
		nargs="*",
		help="Specific test case names to run (directories under lang2/codegen/e2e)",
	)
	args = ap.parse_args(argv)

	case_root = ROOT / "lang2" / "codegen" / "e2e"
	case_dirs = sorted(d for d in case_root.iterdir() if d.is_dir()) if case_root.exists() else []
	if args.cases:
		names = set(args.cases)
		case_dirs = [d for d in case_dirs if d.name in names]

	failures: list[tuple[Path, str]] = []
	for case_dir in case_dirs:
		status = _run_case(case_dir)
		print(f"{case_dir.name}: {status}")
		if status.startswith("FAIL"):
			failures.append((case_dir, status))

	if failures:
		for case, status in failures:
			print(f"[codegen e2e] {case.name}: {status}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
