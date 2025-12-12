from __future__ import annotations

from lang2.codegen.llvm import LlvmModuleBuilder, lower_ssa_func_to_llvm
from lang2.driftc.checker import FnInfo, FnSignature
from lang2.driftc.stage2 import (
	BasicBlock,
	Call,
	ConstBool,
	ConstInt,
	ConstString,
	ConstructError,
	ConstructDV,
	DVAsInt,
	DVAsString,
	ErrorAttrsGetDV,
	IfTerminator,
	MirFunc,
	OptionalIsSome,
	OptionalValue,
	Return,
	StringLen,
)
from lang2.driftc.stage4 import MirToSSA
from lang2.driftc.core.types_core import TypeTable


def test_optional_ops_round_trip_payload():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	dv_ty = table.ensure_diagnostic_value()
	opt_int_ty = table.new_optional(int_ty)

	fnres_main = FnSignature(name="drift_main", return_type_id=int_ty, declared_can_throw=False)
	fn_info_main = FnInfo(name="drift_main", declared_can_throw=False, return_type_id=int_ty, signature=fnres_main)

	# Runtime helper signature for error constructor
	sig_err_new = FnSignature(
		name="drift_error_new_with_payload",
		param_type_ids=[int_ty, table.ensure_string(), table.ensure_string(), dv_ty],
		return_type_id=err_ty,
		declared_can_throw=False,
	)
	fn_err_new = FnInfo(
		name="drift_error_new_with_payload",
		declared_can_throw=False,
		return_type_id=err_ty,
		signature=sig_err_new,
	)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="code", value=1),
			ConstInt(dest="payload_int", value=7),
			ConstructDV(dest="dv", dv_type_name="Payload", args=["payload_int"]),
		ConstString(dest="ename", value="m:Evt"),
		ConstString(dest="key", value="k"),
		Call(dest="err", fn="drift_error_new_with_payload", args=["code", "ename", "key", "dv"]),
		ErrorAttrsGetDV(dest="dv2", error="err", key="key"),
			DVAsInt(dest="opt", dv="dv2"),
			OptionalIsSome(dest="some", opt="opt"),
		],
		terminator=IfTerminator(cond="some", then_target="then", else_target="else"),
	)
	then_block = BasicBlock(
		name="then",
		instructions=[OptionalValue(dest="val", opt="opt")],
		terminator=Return(value="val"),
	)
	else_block = BasicBlock(
		name="else",
		instructions=[ConstInt(dest="zero", value=0)],
		terminator=Return(value="zero"),
	)
	mir = MirFunc(
		name="drift_main",
		params=[],
		locals=[],
		blocks={"entry": entry, "then": then_block, "else": else_block},
		entry="entry",
	)
	ssa = MirToSSA().run(mir)

	fn_infos = {
		"drift_main": fn_info_main,
		"drift_error_new_with_payload": fn_err_new,
	}

	mod = LlvmModuleBuilder()
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info_main, fn_infos=fn_infos, type_table=table))
	ir = mod.render()

	assert "call %DriftDiagnosticValue @drift_dv_int" in ir
	assert "call %DriftError* @drift_error_new_with_payload" in ir
	assert "call void @__exc_attrs_get_dv" in ir
	assert "call %DriftOptionalInt @drift_dv_as_int" in ir
	assert "extractvalue %DriftOptionalInt %opt" in ir
	assert "extractvalue %DriftOptionalInt %opt, 1" in ir


def test_optional_ops_round_trip_string_payload():
	table = TypeTable()
	int_ty = table.ensure_int()
	string_ty = table.ensure_string()
	err_ty = table.ensure_error()
	dv_ty = table.ensure_diagnostic_value()

	fnres_main = FnSignature(name="drift_main", return_type_id=int_ty, declared_can_throw=False)
	fn_info_main = FnInfo(name="drift_main", declared_can_throw=False, return_type_id=int_ty, signature=fnres_main)

	sig_err_new = FnSignature(
		name="drift_error_new_with_payload",
		param_type_ids=[int_ty, string_ty, string_ty, dv_ty],
		return_type_id=err_ty,
		declared_can_throw=False,
	)
	fn_err_new = FnInfo(
		name="drift_error_new_with_payload",
		declared_can_throw=False,
		return_type_id=err_ty,
		signature=sig_err_new,
	)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="code", value=1),
			ConstString(dest="payload_str", value="hello"),
			ConstructDV(dest="dv", dv_type_name="Payload", args=["payload_str"]),
		ConstString(dest="ename", value="m:Evt"),
		ConstString(dest="key", value="k"),
		Call(dest="err", fn="drift_error_new_with_payload", args=["code", "ename", "key", "dv"]),
			ErrorAttrsGetDV(dest="dv2", error="err", key="key"),
			DVAsString(dest="opt", dv="dv2"),
			OptionalIsSome(dest="some", opt="opt"),
		],
		terminator=IfTerminator(cond="some", then_target="then", else_target="else"),
	)
	then_block = BasicBlock(
		name="then",
		instructions=[
			OptionalValue(dest="sval", opt="opt"),
			StringLen(dest="len", value="sval"),
		],
		terminator=Return(value="len"),
	)
	else_block = BasicBlock(
		name="else",
		instructions=[ConstInt(dest="zero", value=0)],
		terminator=Return(value="zero"),
	)
	mir = MirFunc(
		name="drift_main",
		params=[],
		locals=[],
		blocks={"entry": entry, "then": then_block, "else": else_block},
		entry="entry",
	)
	ssa = MirToSSA().run(mir)

	fn_infos = {
		"drift_main": fn_info_main,
		"drift_error_new_with_payload": fn_err_new,
	}

	mod = LlvmModuleBuilder()
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info_main, fn_infos=fn_infos, type_table=table))
	ir = mod.render()

	assert "call %DriftError* @drift_error_new_with_payload" in ir
	assert "call void @__exc_attrs_get_dv" in ir
	assert "call %DriftOptionalString @drift_dv_as_string" in ir
	assert "extractvalue %DriftOptionalString %opt, 1" in ir
