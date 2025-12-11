"""
End-to-end LLVM codegen test for Void-returning functions.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from lang2.driftc.checker import FnSignature
from lang2.driftc.driftc import compile_to_llvm_ir_for_tests
from lang2.driftc import stage1 as H


BUILD_ROOT = Path("build/tests/lang2/driftc_codegen_void")


def _run_ir_with_clang(ir: str) -> int:
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


def test_driftc_codegen_void_call_in_main():
	"""
	Full pipeline: Void-returning callee, called from drift_main, should emit
	dest-less call and `ret void` for the callee while main still returns Int.
	Exit code proves main kept its Int path (returns 3).
	"""
	table = None
	void_ty = None
	string_ty = None
	# Build a shared TypeTable to resolve TypeIds explicitly for clarity.
	from lang2.driftc.core.types_core import TypeTable

	table = TypeTable()
	void_ty = table.ensure_void()
	string_ty = table.ensure_string()
	int_ty = table.ensure_int()

	func_hirs = {
		"log": H.HBlock(
			statements=[
				H.HReturn(value=None),
			]
		),
		"drift_main": H.HBlock(
			statements=[
				H.HExprStmt(expr=H.HCall(fn=H.HVar(name="log"), args=[])),
				H.HReturn(value=H.HLiteralInt(value=3)),
			]
		),
	}
	signatures = {
		"log": FnSignature(name="log", return_type_id=void_ty),
		"drift_main": FnSignature(name="drift_main", return_type_id=int_ty),
	}

	ir, _ = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs, signatures=signatures, entry="drift_main", type_table=table
	)
	exit_code = _run_ir_with_clang(ir)
	assert exit_code == 3


def test_driftc_codegen_void_negative_returns_value_raises():
	"""
	A Void function returning a value should fail before codegen; ensure the
	pipeline raises rather than emitting IR.
	"""
	from pytest import raises
	from lang2.driftc.core.types_core import TypeTable

	table = TypeTable()
	void_ty = table.ensure_void()
	int_ty = table.ensure_int()

	func_hirs = {
		"log": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=1))]),
	}
	signatures = {
		"log": FnSignature(name="log", return_type_id=void_ty),
	}

	with raises(AssertionError):
		compile_to_llvm_ir_for_tests(func_hirs=func_hirs, signatures=signatures, entry="log", type_table=table)


def test_driftc_codegen_nonvoid_negative_bare_return_raises():
	"""
	A non-Void function with a bare return should fail before codegen.
	"""
	from pytest import raises
	from lang2.driftc.core.types_core import TypeTable

	table = TypeTable()
	int_ty = table.ensure_int()

	func_hirs = {
		"drift_main": H.HBlock(statements=[H.HReturn(value=None)]),
	}
	signatures = {
		"drift_main": FnSignature(name="drift_main", return_type_id=int_ty),
	}

	with raises(AssertionError):
		compile_to_llvm_ir_for_tests(
			func_hirs=func_hirs, signatures=signatures, entry="drift_main", type_table=table
		)
