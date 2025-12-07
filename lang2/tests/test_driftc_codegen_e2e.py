"""
Top-surface driftc → LLVM IR → clang execution smoke test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from lang2.checker import FnSignature
from lang2.driftc import compile_to_llvm_ir_for_tests
from lang2 import stage1 as H


BUILD_ROOT = Path("build/tests/lang2/driftc_codegen_scalar")


def _run_ir_with_clang(ir: str) -> int:
	"""Compile the provided LLVM IR with clang and return the process exit code."""
	clang = shutil.which("clang-15") or shutil.which("clang")
	if clang is None:
		pytest.skip("clang not available")

	BUILD_ROOT.mkdir(parents=True, exist_ok=True)
	ir_path = BUILD_ROOT / "ir.ll"
	bin_path = BUILD_ROOT / "a.out"
	ir_path.write_text(ir)

	compile_res = subprocess.run(
		[clang, "-x", "ir", str(ir_path), "-o", str(bin_path)],
		capture_output=True,
		text=True,
	)
	if compile_res.returncode != 0:
		raise RuntimeError(f"clang failed: {compile_res.stderr}")

	run_res = subprocess.run(
		[str(bin_path)],
		capture_output=True,
		text=True,
	)
	return run_res.returncode


def test_driftc_codegen_scalar_main():
	"""
	Full pipeline smoke: HIR -> MIR -> SSA -> LLVM -> clang.
	fn drift_main() returns Int { return 42; } should exit with code 42.
	"""
	func_hirs = {
		"drift_main": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=42))])
	}
	signatures = {"drift_main": FnSignature(name="drift_main", return_type="Int")}

	ir = compile_to_llvm_ir_for_tests(func_hirs=func_hirs, signatures=signatures, entry="drift_main")
	exit_code = _run_ir_with_clang(ir)
	assert exit_code == 42
