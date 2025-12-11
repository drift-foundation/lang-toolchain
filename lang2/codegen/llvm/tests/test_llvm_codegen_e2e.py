"""
End-to-end LLVM IR execution via lli (scalar + FnResult happy paths).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from lang2.codegen.llvm import lower_module_to_llvm
from lang2.checker import FnInfo
from lang2.driftc.stage2 import BasicBlock, MirFunc, ConstInt, Return, ConstructResultOk, Call
from lang2.stage4 import MirToSSA
from lang2.driftc.core.types_core import TypeTable


BUILD_ROOT = Path("build/tests/lang2")


def _run_ir_and_get_exit(ir: str) -> int:
	"""
	Run LLVM IR by compiling with clang and executing the resulting binary.
	We intentionally avoid lli to reduce dependencies.
	"""
	BUILD_ROOT.mkdir(parents=True, exist_ok=True)

	clang_bin: Optional[str] = os.environ.get("CLANG_BIN") or "clang-15"
	clang = shutil.which(clang_bin) or shutil.which("clang")
	if clang is None:
		pytest.skip("clang not available")

	ir_path = BUILD_ROOT / "tmp.ll"
	bin_path = BUILD_ROOT / "tmp.out"
	ir_path.write_text(ir)

	compile_res = subprocess.run(
		[clang, "-x", "ir", str(ir_path), "-o", str(bin_path)],
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
	)
	if compile_res.returncode != 0:
		raise RuntimeError(f"clang failed: {compile_res.stderr.decode()}")

	BUILD_ROOT.mkdir(parents=True, exist_ok=True)
	run_res = subprocess.run(
		[str(bin_path)],
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
	)
	return run_res.returncode


def test_e2e_scalar_main_returns_42():
	"""
	Drift_main returns Int 42; wrapper truncates to i32; lli exit code should be 42.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="v", value=42)],
		terminator=Return(value="v"),
	)
	mir = MirFunc(name="drift_main", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.new_scalar("Int")
	fn_infos = {"drift_main": FnInfo(name="drift_main", declared_can_throw=False, return_type_id=int_ty)}

	mod = lower_module_to_llvm({"drift_main": mir}, {"drift_main": ssa}, fn_infos)
	mod.emit_entry_wrapper("drift_main")
	ir = mod.render()

	exit_code = _run_ir_and_get_exit(ir)
	assert exit_code == 42


def test_e2e_fnresult_callee_ok_path():
	"""
	Can-throw callee returns Ok(1); drift_main calls it and returns the ok value.
	"""
	# callee: FnResult.Ok(1)
	callee_entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="c0", value=1), ConstructResultOk(dest="cres", value="c0")],
		terminator=Return(value="cres"),
	)
	callee_mir = MirFunc(name="callee", params=[], locals=[], blocks={"entry": callee_entry}, entry="entry")
	callee_ssa = MirToSSA().run(callee_mir)

	# drift_main: call callee, return ok part (extract handled by codegen).
	main_entry = BasicBlock(
		name="entry",
		instructions=[Call(dest="m0", fn="callee", args=[])],
		terminator=Return(value="m0"),
	)
	main_mir = MirFunc(name="drift_main", params=[], locals=[], blocks={"entry": main_entry}, entry="entry")
	main_ssa = MirToSSA().run(main_mir)

	table = TypeTable()
	int_ty = table.new_scalar("Int")
	err_ty = table.new_error("Error")
	fnresult_ty = table.new_fnresult(int_ty, err_ty)
	fn_infos = {
		"callee": FnInfo(name="callee", declared_can_throw=True, return_type_id=fnresult_ty, error_type_id=err_ty),
		"drift_main": FnInfo(name="drift_main", declared_can_throw=False, return_type_id=int_ty),
	}

	mod = lower_module_to_llvm(
		{"callee": callee_mir, "drift_main": main_mir},
		{"callee": callee_ssa, "drift_main": main_ssa},
		fn_infos,
	)
	mod.emit_entry_wrapper("drift_main")
	ir = mod.render()

	exit_code = _run_ir_and_get_exit(ir)
	assert exit_code == 1
