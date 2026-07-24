"""
End-to-end LLVM codegen test for Void-returning functions.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from lang.driftc.checker import FnSignature
from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc import stage1 as H


BUILD_ROOT = Path("build/tests/lang/driftc_codegen_void")


def _run_ir_with_clang(ir: str) -> int:
	clang = shutil.which("clang")
	if clang is None:
		raise RuntimeError("clang not available")

	BUILD_ROOT.mkdir(parents=True, exist_ok=True)
	work_dir = Path(tempfile.mkdtemp(prefix="driftc-codegen-void-", dir=BUILD_ROOT))
	try:
		ir_path = work_dir / "ir.ll"
		bin_path = work_dir / "a.out"
		ir_path.write_text(ir)

		# ABI 22 (B-repr/B5): empty string literals reference the runtime
		# empty singleton — IR-only programs link the string runtime.
		runtime_dir = Path(__file__).resolve().parents[3] / "lang" / "language_runtime"
		compile_res = subprocess.run(
			[clang, "-x", "ir", str(ir_path), "-x", "none",
			 str(runtime_dir / "string_runtime.c"), str(runtime_dir / "ryu_d2s.c"),
			 "-I", str(runtime_dir), "-o", str(bin_path)],
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
	finally:
		shutil.rmtree(work_dir, ignore_errors=True)


def test_driftc_codegen_void_call_in_main():
	"""
	Full pipeline: Void-returning callee, called from drift_main, should emit
	dest-less call and `ret void` for the callee while main still -> Int.
	Exit code proves main kept its Int path (-> 3).
	"""
	table = None
	void_ty = None
	string_ty = None
	# Build a shared TypeTable to resolve TypeIds explicitly for clarity.
	from lang.driftc.core.types_core import TypeTable

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
		"log": FnSignature(name="log", return_type_id=void_ty, declared_can_throw=False),
		"drift_main": FnSignature(name="drift_main", return_type_id=int_ty, declared_can_throw=False),
	}

	ir, _ = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs, signatures=signatures, entry="drift_main", type_table=table,
		root_vt=False,
	)
	exit_code = _run_ir_with_clang(ir)
	assert exit_code == 3


def test_driftc_codegen_void_negative_returns_value_raises():
	"""
	A Void function returning a value should fail before codegen.

	Compiler should surface a diagnostic and stop lowering before emitting IR.
	"""
	from lang.driftc.core.types_core import TypeTable

	table = TypeTable()
	void_ty = table.ensure_void()

	func_hirs = {
		"log": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=1))]),
	}
	signatures = {
		"log": FnSignature(name="log", return_type_id=void_ty, declared_can_throw=False),
	}

	ir, checked = compile_to_llvm_ir_for_tests(func_hirs=func_hirs, signatures=signatures, entry="log", type_table=table)
	assert ir == ""
	assert any("cannot return a value from a Void function" in d.message for d in checked.diagnostics)


def test_driftc_codegen_nonvoid_negative_bare_return_raises():
	"""
	A non-Void function with a bare return should fail before codegen.

	Compiler should surface a diagnostic and stop lowering before emitting IR.
	"""
	from lang.driftc.core.types_core import TypeTable

	table = TypeTable()
	int_ty = table.ensure_int()

	func_hirs = {
		"drift_main": H.HBlock(statements=[H.HReturn(value=None)]),
	}
	signatures = {
		"drift_main": FnSignature(name="drift_main", return_type_id=int_ty, declared_can_throw=False),
	}

	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs, signatures=signatures, entry="drift_main", type_table=table,
		root_vt=False,
	)
	assert ir == ""
	assert any("non-void function must return a value" in d.message for d in checked.diagnostics)
