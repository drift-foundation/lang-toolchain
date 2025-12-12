from __future__ import annotations

import pytest

from lang2.codegen.llvm import lower_ssa_func_to_llvm, LlvmModuleBuilder
from lang2.driftc.checker import FnInfo, FnSignature
from lang2.driftc.stage2 import (
	BasicBlock,
	ConstructError,
	ConstructDV,
	ErrorAddAttrDV,
	ConstructResultErr,
	ErrorAttrsGetDV,
	DVAsInt,
	OptionalIsSome,
	OptionalValue,
	IfTerminator,
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
			ConstString(dest="ename", value="Evt"),
			ConstString(dest="key", value="payload"),
			ConstructDV(dest="dv", dv_type_name="Missing", args=[]),
			ConstructError(dest="err", code="code", event_name="ename", payload="dv", attr_key="key"),
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
			ConstString(dest="ename", value="Evt"),
			ConstString(dest="key", value="payload"),
			ConstructDV(dest="dv", dv_type_name="Missing", args=[]),
			ConstructError(dest="err", code="code", event_name="ename", payload="dv", attr_key="key"),
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


def test_error_additional_attr_lowered_to_runtime_call():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	fnres_ty = table.new_fnresult(int_ty, err_ty)
	sig = FnSignature(name="h", param_type_ids=[], return_type_id=fnres_ty, declared_can_throw=True)
	fn_info = FnInfo(name="h", declared_can_throw=True, return_type_id=fnres_ty, signature=sig)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="code", value=3),
			ConstString(dest="ename", value="Evt"),
			ConstString(dest="k1", value="a"),
			ConstInt(dest="v1", value=10),
			ConstructDV(dest="dv1", dv_type_name="a", args=["v1"]),
			ConstructError(dest="err", code="code", event_name="ename", payload="dv1", attr_key="k1"),
			ConstString(dest="k2", value="b"),
			ConstInt(dest="v2", value=20),
			ConstructDV(dest="dv2", dv_type_name="b", args=["v2"]),
			ErrorAddAttrDV(error="err", key="k2", value="dv2"),
			ConstructResultErr(dest="res", error="err"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(name="h", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	mod = LlvmModuleBuilder()
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table))
	ir = mod.render()

	assert "call void @drift_error_add_attr_dv" in ir


def test_error_attr_round_trip_additional_key():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()

	sig = FnSignature(name="attr_round_trip", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False)
	fn_info = FnInfo(name="attr_round_trip", declared_can_throw=False, return_type_id=int_ty, signature=sig)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="code", value=4),
			ConstString(dest="ename", value="Evt"),
			ConstString(dest="payload_key", value="payload"),
			ConstInt(dest="payload_int", value=0),
			ConstructDV(dest="payload_dv", dv_type_name="Evt", args=["payload_int"]),
			ConstructError(dest="err", code="code", event_name="ename", payload="payload_dv", attr_key="payload_key"),
			ConstString(dest="attr_key", value="answer"),
			ConstInt(dest="attr_val", value=7),
			ConstructDV(dest="attr_dv", dv_type_name="Evt", args=["attr_val"]),
			ErrorAddAttrDV(error="err", key="attr_key", value="attr_dv"),
			ErrorAttrsGetDV(dest="dv_out", error="err", key="attr_key"),
			DVAsInt(dest="opt", dv="dv_out"),
			OptionalIsSome(dest="some", opt="opt"),
		],
		terminator=IfTerminator(cond="some", then_target="then", else_target="else"),
	)
	then_block = BasicBlock(
		name="then",
		instructions=[
			OptionalValue(dest="val", opt="opt"),
		],
		terminator=Return(value="val"),
	)
	else_block = BasicBlock(
		name="else",
		instructions=[ConstInt(dest="zero", value=0)],
		terminator=Return(value="zero"),
	)
	mir = MirFunc(
		name="attr_round_trip",
		params=[],
		locals=[],
		blocks={"entry": entry, "then": then_block, "else": else_block},
		entry="entry",
	)
	ssa = MirToSSA().run(mir)

	mod = LlvmModuleBuilder()
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table))
	ir = mod.render()

	assert "call %DriftDiagnosticValue @drift_dv_int" in ir
	assert "call %DriftError* @drift_error_new_with_payload" in ir
	assert "call void @drift_error_add_attr_dv" in ir
	assert "call void @__exc_attrs_get_dv" in ir
	assert "@drift_dv_as_int" in ir
	assert "%DriftOptionalInt" in ir
	assert "extractvalue %DriftOptionalInt %opt, 0" in ir
	assert "extractvalue %DriftOptionalInt %opt, 1" in ir
