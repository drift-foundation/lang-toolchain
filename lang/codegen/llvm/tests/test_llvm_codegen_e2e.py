"""
End-to-end LLVM IR execution via lli (scalar + FnResult happy paths).
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import pytest

from lang.codegen.llvm import lower_module_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo
from lang.driftc.stage2 import BasicBlock, MirFunc, ConstInt, Return, ConstructResultOk, Call, ResultOk
from lang.driftc.stage4 import MirToSSA
from lang.driftc.core.types_core import TypeTable


BUILD_ROOT = Path("build/tests/lang")


def _run_ir_and_get_exit(ir: str) -> int:
	"""
	Run LLVM IR by compiling with clang and executing the resulting binary.
	We intentionally avoid lli to reduce dependencies.
	"""
	BUILD_ROOT.mkdir(parents=True, exist_ok=True)

	clang_bin: Optional[str] = os.environ.get("CLANG_BIN") or "clang-15"
	clang = shutil.which(clang_bin) or shutil.which("clang")
	if clang is None:
		raise RuntimeError("clang not available")

	work_dir = Path(tempfile.mkdtemp(prefix="llvm-e2e-", dir=BUILD_ROOT))
	try:
		ir_path = work_dir / "tmp.ll"
		bin_path = work_dir / "tmp.out"
		ir_path.write_text(ir)

		compile_res = subprocess.run(
			[clang, "-x", "ir", str(ir_path), "-o", str(bin_path)],
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
		)
		if compile_res.returncode != 0:
			raise RuntimeError(f"clang failed: {compile_res.stderr.decode()}")

		run_res = subprocess.run(
			[str(bin_path)],
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
		)
		return run_res.returncode
	finally:
		shutil.rmtree(work_dir, ignore_errors=True)


def test_e2e_scalar_main_returns_42():
	"""
	Drift_main -> Int 42; wrapper truncates to i32; lli exit code should be 42.
	"""
	entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="v", value=42)],
		terminator=Return(value="v"),
	)
	fn_id = FunctionId(module="main", name="drift_main", ordinal=0)
	mir = MirFunc(fn_id=fn_id, name="drift_main", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	fn_infos = {fn_id: FnInfo(fn_id=fn_id, name="drift_main", declared_can_throw=False, return_type_id=int_ty)}

	mod = lower_module_to_llvm({fn_id: mir}, {fn_id: ssa}, fn_infos, word_bits=host_word_bits())
	mod.emit_entry_wrapper("drift_main")
	ir = mod.render()

	exit_code = _run_ir_and_get_exit(ir)
	assert exit_code == 42


def test_e2e_fnresult_callee_ok_path():
	"""
	Can-throw callee -> Ok(1); drift_main calls it and -> the ok value.
	"""
	# callee: FnResult.Ok(1)
	callee_entry = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="c0", value=1), ConstructResultOk(dest="cres", value="c0")],
		terminator=Return(value="cres"),
	)
	callee_id = FunctionId(module="main", name="callee", ordinal=0)
	callee_mir = MirFunc(fn_id=callee_id, name="callee", params=[], locals=[], blocks={"entry": callee_entry}, entry="entry")
	callee_ssa = MirToSSA().run(callee_mir)

	# drift_main: call callee, extract ok part, and return it.
	main_entry = BasicBlock(
		name="entry",
		instructions=[Call(dest="mres", fn_id=callee_id, args=[], can_throw=True), ResultOk(dest="m0", result="mres")],
		terminator=Return(value="m0"),
	)
	main_id = FunctionId(module="main", name="drift_main", ordinal=0)
	main_mir = MirFunc(fn_id=main_id, name="drift_main", params=[], locals=[], blocks={"entry": main_entry}, entry="entry")
	main_ssa = MirToSSA().run(main_mir)

	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	fnresult_ty = table.new_fnresult(int_ty, err_ty)
	fn_infos = {
		callee_id: FnInfo(fn_id=callee_id, name="callee", declared_can_throw=True, return_type_id=fnresult_ty, error_type_id=err_ty),
		main_id: FnInfo(fn_id=main_id, name="drift_main", declared_can_throw=False, return_type_id=int_ty),
	}

	mod = lower_module_to_llvm(
		{callee_id: callee_mir, main_id: main_mir},
		{callee_id: callee_ssa, main_id: main_ssa},
		fn_infos,
		type_table=table, word_bits=host_word_bits())
	mod.emit_entry_wrapper("drift_main")
	ir = mod.render()

	exit_code = _run_ir_and_get_exit(ir)
	assert exit_code == 1
