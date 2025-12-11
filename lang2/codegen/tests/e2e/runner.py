"""
Drift-source end-to-end runner (clang-based).

Each case lives under `lang2/codegen/tests/e2e/<case>/` and must provide:
  - main.drift   (parsed with the copied lang2 parser)
  - expected.json with exit_code/stdout/stderr fields

The runner:
  1) Parses main.drift with the lang2 parser copy.
  2) Lowers to HIR via AstToHIR.
  3) Runs the full stubbed pipeline to LLVM IR (`compile_to_llvm_ir_for_tests`).
  4) Compiles IR with clang and executes the binary.
  5) Asserts diagnostics are empty and compares exit/stdout/stderr to expected.json.

Artifacts are written to `build/tests/lang2/codegen/tests/e2e/<case>/`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

from lang2.driftc.parser import parse_drift_to_hir
from lang2.driftc.driftc import compile_to_llvm_ir_for_tests
from lang2.drift_core.runtime import get_runtime_sources


ROOT = Path(__file__).resolve().parents[4]
BUILD_ROOT = ROOT / "build" / "tests" / "lang2" / "codegen" / "tests" / "e2e"


def _run_ir_with_clang(ir: str, build_dir: Path, argv: list[str] | None = None) -> tuple[int, str, str]:
	"""Compile the provided LLVM IR with clang and return (exit, stdout, stderr)."""
	clang = shutil.which("clang-15") or shutil.which("clang")
	if clang is None:
		return -999, "", "clang not available"

	build_dir.mkdir(parents=True, exist_ok=True)
	ir_path = build_dir / "ir.ll"
	bin_path = build_dir / "a.out"
	ir_path.write_text(ir)

	runtime_sources = get_runtime_sources(ROOT)
	compile_res = subprocess.run(
		[
			clang,
			"-x",
			"ir",
			str(ir_path),
			"-x",
			"c",
			*(str(p) for p in runtime_sources),
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
		[str(bin_path), *(argv or [])],
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
	# Compile-error path: delegate to driftc --json for structured diags.
	if expected.get("diagnostics"):
		cmd = [str(Path(sys.executable)), "-m", "lang2.driftc.driftc", str(source_path), "--json"]
		res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
		try:
			payload = json.loads(res.stdout)
		except json.JSONDecodeError as err:
			return f"FAIL (expected JSON diagnostics, got parse error: {err})"
		exit_expected = expected.get("exit_code", 1)
		if payload.get("exit_code") != exit_expected:
			return f"FAIL (exit {payload.get('exit_code')} != expected {exit_expected})"
		diag_expect = expected.get("diagnostics", [])
		diags = payload.get("diagnostics", [])
		for exp in diag_expect:
			msg_sub = exp.get("message_contains")
			phase = exp.get("phase")
			match_found = False
			for d in diags:
				if phase is not None and d.get("phase") != phase:
					continue
				if msg_sub is not None and msg_sub not in d.get("message", ""):
					continue
				match_found = True
				break
			if not match_found:
				return "FAIL (missing expected diagnostic)"
		return "ok"
	func_hirs, signatures, type_table, exception_catalog, parse_diags = parse_drift_to_hir(source_path)
	expected_phase = expected.get("phase")

	if parse_diags:
		actual_exit = 1  # Parsing produced diagnostics; treat as compile failure.
		expected_exit = expected.get("exit_code", actual_exit)
		actual_stdout = ""
		actual_stderr = "\n".join(d.message for d in parse_diags)
		if expected_phase not in (None, "parser"):
			return f"FAIL (expected phase {expected_phase}, but diagnostics occurred during parser phase)"
		if expected_exit != actual_exit:
			return f"FAIL (parser phase exit {actual_exit}, expected {expected_exit})"
		if expected.get("stdout", actual_stdout) != actual_stdout:
			return "FAIL (parser phase stdout mismatch)"
		if expected.get("stderr", actual_stderr) != actual_stderr:
			return "FAIL (parser phase stderr mismatch)"
		return "ok"
	# Require exactly one user-facing main. Prefer a zero-arg Int main; if a single
	# param main exists, assume it's Array<String> and let the backend emit the
	# argv wrapper.
	main_funcs = [name for name in func_hirs if name == "main"]
	if len(main_funcs) != 1:
		return "FAIL (must define exactly one fn main)"
	entry = "main"
	main_sig = signatures.get("main")
	needs_argv = False
	if main_sig and main_sig.param_type_ids and len(main_sig.param_type_ids) == 1 and type_table is not None:
		param_ty = main_sig.param_type_ids[0]
		td = type_table.get(param_ty)
		if td.kind.name == "ARRAY" and td.param_types:
			elem_def = type_table.get(td.param_types[0])
			if elem_def.name == "String":
				needs_argv = True

	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry=entry,
		type_table=type_table,
	)

	if checked.diagnostics:
		expected_exit = expected.get("exit_code", 1)
		expected_phase = expected.get("phase", "typecheck")
		if expected.get("diagnostics") is None:
			return "FAIL (unexpected diagnostics during compilation)"
		if expected_exit != 1:
			return f"FAIL (diagnostics present, expected exit {expected_exit})"
		for exp in expected.get("diagnostics", []):
			msg_sub = exp.get("message_contains")
			phase = exp.get("phase", expected_phase)
			match_found = False
			for d in checked.diagnostics:
				if msg_sub is not None and msg_sub not in d.message:
					continue
				match_found = True
				break
			if not match_found:
				return "FAIL (missing expected diagnostic)"
		return "ok"

	build_dir = BUILD_ROOT / case_dir.name
	run_args = expected.get("args", [])
	if needs_argv and not run_args:
		return "FAIL (argv main requires args in expected.json)"
	exit_code, stdout, stderr = _run_ir_with_clang(ir, build_dir, argv=run_args)
	if exit_code == -999:
		return "skipped (clang not available)"

	if checked.diagnostics:
		actual_exit = 1
		actual_stdout = ""
		actual_stderr = "\n".join(d.message for d in checked.diagnostics)
		expected_exit = expected.get("exit_code", actual_exit)
		if expected_phase not in (None, "checker", "codegen"):
			return f"FAIL (expected phase {expected_phase}, but diagnostics occurred during checker/codegen phase)"
		if expected_exit != actual_exit:
			return f"FAIL (checker/codegen phase exit {actual_exit}, expected {expected_exit})"
		if expected.get("stdout", actual_stdout) != actual_stdout:
			return "FAIL (checker/codegen phase stdout mismatch)"
		if expected.get("stderr", actual_stderr) != actual_stderr:
			return "FAIL (checker/codegen phase stderr mismatch)"
		return "ok"

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
		help="Specific test case names to run (directories under lang2/codegen/tests/e2e)",
	)
	args = ap.parse_args(argv)

	case_root = ROOT / "lang2" / "codegen" / "tests" / "e2e"
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
