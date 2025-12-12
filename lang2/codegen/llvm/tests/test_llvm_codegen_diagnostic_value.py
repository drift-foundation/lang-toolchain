from __future__ import annotations

import pytest

from lang2.codegen.llvm import lower_ssa_func_to_llvm, LlvmModuleBuilder
from lang2.driftc.checker import FnInfo, FnSignature
from lang2.driftc.stage2 import (
	BasicBlock,
	ConstructError,
	ConstructDV,
	ConstructResultErr,
	ErrorAttrsGetDV,
	DVAsInt,
	ConstInt,
	ConstString,
	MirFunc,
	Return,
)
from lang2.driftc.stage4 import MirToSSA
from lang2.driftc.core.types_core import TypeTable


def test_error_attrs_lookup_lowered_to_runtime_call():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	fnres_ty = table.new_fnresult(int_ty, err_ty)
	sig = FnSignature(name="f", param_type_ids=[], return_type_id=fnres_ty, declared_can_throw=True)
	fn_info = FnInfo(name="f", declared_can_throw=True, return_type_id=fnres_ty, signature=sig)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="code", value=1),
			ConstString(dest="key", value="payload"),
			ConstructDV(dest="dv", dv_type_name="Missing", args=[]),
			ConstructError(dest="err", code="code", payload="dv", attr_key="key"),
			ErrorAttrsGetDV(dest="dv", error="err", key="key"),
			ConstructResultErr(dest="res", error="err"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(name="f", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	mod = LlvmModuleBuilder()
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table))
	ir = mod.render()

	assert "declare void @__exc_attrs_get_dv" in ir
	assert "call void @__exc_attrs_get_dv" in ir
	assert "%DriftDiagnosticValue" in ir


def test_dv_as_int_returns_optional_int():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	dv_ty = table.ensure_diagnostic_value()
	opt_int_ty = table.new_optional(int_ty)
	fnres_ty = table.new_fnresult(int_ty, err_ty)
	sig = FnSignature(name="g", param_type_ids=[], return_type_id=fnres_ty, declared_can_throw=True)
	fn_info = FnInfo(name="g", declared_can_throw=True, return_type_id=fnres_ty, signature=sig)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="code", value=2),
			ConstString(dest="key", value="payload"),
			ConstructDV(dest="dv", dv_type_name="Missing", args=[]),
			ConstructError(dest="err", code="code", payload="dv", attr_key="key"),
			ErrorAttrsGetDV(dest="dv", error="err", key="key"),
			DVAsInt(dest="opt", dv="dv"),
			ConstructResultErr(dest="res", error="err"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(name="g", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	mod = LlvmModuleBuilder()
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table))
	ir = mod.render()

	assert "declare" in ir  # basic sanity
	assert "@drift_dv_as_int" in ir
	assert "%DriftOptionalInt" in ir
