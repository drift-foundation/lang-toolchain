from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.codegen.llvm import LlvmModuleBuilder, lower_ssa_func_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.stage1 import BinaryOp
from lang.driftc.stage2 import (
	BasicBlock,
	BinaryOpInstr,
	Call,
	ConstBool,
	ConstInt,
	ConstUint,
	ConstUint64,
	ConstString,
	ConstructError,
	ConstructDV,
	DVAsInt,
	DVAsString,
	ErrorAttrsGetDV,
	IfTerminator,
	MirFunc,
	Return,
	StringLen,
	VariantGetField,
	VariantTag,
)
from lang.driftc.stage4 import MirToSSA
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema


def _ensure_optional(table: TypeTable, inner: int) -> int:
	base = table.get_variant_base(module_id="lang.core", name="Optional")
	if base is None:
		base = table.declare_variant(
			"lang.core",
			"Optional",
			["T"],
			[
				VariantArmSchema(name="None", fields=[]),
				VariantArmSchema(
					name="Some",
					fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
				),
			],
			tombstone_ctor="None",
		)
	return table.ensure_instantiated(base, [inner])


def test_optional_ops_round_trip_payload():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	dv_ty = table.ensure_diagnostic_value()
	opt_int_ty = _ensure_optional(table, int_ty)
	opt_int_inst = table.get_variant_instance(opt_int_ty)
	assert opt_int_inst is not None
	opt_int_some_tag = next(a.tag for a in opt_int_inst.arms if a.name == "Some")

	fnres_main = FnSignature(name="drift_main", return_type_id=int_ty, declared_can_throw=False)
	main_id = FunctionId(module="main", name="drift_main", ordinal=0)
	fn_info_main = FnInfo(fn_id=main_id, name="drift_main", declared_can_throw=False, return_type_id=int_ty, signature=fnres_main)

	# Runtime helper signature for error constructor
	sig_err_new = FnSignature(
		name="drift_error_new_with_payload",
		param_type_ids=[table.ensure_uint64(), table.ensure_string(), table.ensure_string(), dv_ty],
		return_type_id=err_ty,
		declared_can_throw=False,
	)
	err_id = FunctionId(module="main", name="drift_error_new_with_payload", ordinal=0)
	fn_err_new = FnInfo(fn_id=err_id, name="drift_error_new_with_payload",
		declared_can_throw=False,
		return_type_id=err_ty,
		signature=sig_err_new,
	)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstUint64(dest="code", value=1),
			ConstInt(dest="payload_int", value=7),
			ConstructDV(dest="dv", dv_type_name="Payload", args=["payload_int"]),
			ConstString(dest="ename", value="m:Evt"),
			ConstString(dest="key", value="k"),
			Call(dest="err", fn_id=err_id, args=["code", "ename", "key", "dv"], can_throw=False),
			ErrorAttrsGetDV(dest="dv2", error="err", key="key"),
			DVAsInt(dest="opt", dv="dv2"),
			VariantTag(dest="tag", variant="opt", variant_ty=opt_int_ty),
			ConstUint(dest="tag_some", value=opt_int_some_tag),
			BinaryOpInstr(dest="some", op=BinaryOp.EQ, left="tag", right="tag_some"),
		],
		terminator=IfTerminator(cond="some", then_target="then", else_target="else"),
	)
	then_block = BasicBlock(
		name="then",
		instructions=[VariantGetField(dest="val", variant="opt", variant_ty=opt_int_ty, ctor="Some", field_index=0, field_ty=int_ty)],
		terminator=Return(value="val"),
	)
	else_block = BasicBlock(
		name="else",
		instructions=[ConstInt(dest="zero", value=0)],
		terminator=Return(value="zero"),
	)
	mir = MirFunc(
		fn_id=main_id,
		name="drift_main",
		params=[],
		locals=[],
		blocks={"entry": entry, "then": then_block, "else": else_block},
		entry="entry",
	)
	ssa = MirToSSA().run(mir)

	fn_infos = {
		main_id: fn_info_main,
		err_id: fn_err_new,
	}

	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info_main, fn_infos=fn_infos, type_table=table, word_bits=host_word_bits()))
	ir = mod.render()

	assert "call %DriftDiagnosticValue @drift_dv_int" in ir
	assert "call ptr @drift_error_new_with_payload" in ir
	assert "call void @__exc_attrs_get_dv" in ir
	assert "call i1 @drift_dv_as_int" in ir
	assert "Variant_" in ir


def test_optional_ops_round_trip_string_payload():
	table = TypeTable()
	int_ty = table.ensure_int()
	string_ty = table.ensure_string()
	err_ty = table.ensure_error()
	dv_ty = table.ensure_diagnostic_value()
	opt_string_ty = _ensure_optional(table, string_ty)
	opt_string_inst = table.get_variant_instance(opt_string_ty)
	assert opt_string_inst is not None
	opt_string_some_tag = next(a.tag for a in opt_string_inst.arms if a.name == "Some")

	fnres_main = FnSignature(name="drift_main", return_type_id=int_ty, declared_can_throw=False)
	main_id = FunctionId(module="main", name="drift_main", ordinal=0)
	fn_info_main = FnInfo(fn_id=main_id, name="drift_main", declared_can_throw=False, return_type_id=int_ty, signature=fnres_main)

	sig_err_new = FnSignature(
		name="drift_error_new_with_payload",
		param_type_ids=[table.ensure_uint64(), string_ty, string_ty, dv_ty],
		return_type_id=err_ty,
		declared_can_throw=False,
	)
	err_id = FunctionId(module="main", name="drift_error_new_with_payload", ordinal=0)
	fn_err_new = FnInfo(fn_id=err_id, name="drift_error_new_with_payload",
		declared_can_throw=False,
		return_type_id=err_ty,
		signature=sig_err_new,
	)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstUint64(dest="code", value=1),
			ConstString(dest="payload_str", value="hello"),
			ConstructDV(dest="dv", dv_type_name="Payload", args=["payload_str"]),
			ConstString(dest="ename", value="m:Evt"),
			ConstString(dest="key", value="k"),
			Call(dest="err", fn_id=err_id, args=["code", "ename", "key", "dv"], can_throw=False),
			ErrorAttrsGetDV(dest="dv2", error="err", key="key"),
			DVAsString(dest="opt", dv="dv2"),
			VariantTag(dest="tag", variant="opt", variant_ty=opt_string_ty),
			ConstUint(dest="tag_some", value=opt_string_some_tag),
			BinaryOpInstr(dest="some", op=BinaryOp.EQ, left="tag", right="tag_some"),
		],
		terminator=IfTerminator(cond="some", then_target="then", else_target="else"),
	)
	then_block = BasicBlock(
		name="then",
		instructions=[
			VariantGetField(dest="sval", variant="opt", variant_ty=opt_string_ty, ctor="Some", field_index=0, field_ty=string_ty),
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
		fn_id=main_id,
		name="drift_main",
		params=[],
		locals=[],
		blocks={"entry": entry, "then": then_block, "else": else_block},
		entry="entry",
	)
	ssa = MirToSSA().run(mir)

	fn_infos = {
		main_id: fn_info_main,
		err_id: fn_err_new,
	}

	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info_main, fn_infos=fn_infos, type_table=table, word_bits=host_word_bits()))
	ir = mod.render()

	assert "call ptr @drift_error_new_with_payload" in ir
	assert "call void @__exc_attrs_get_dv" in ir
	assert "call i1 @drift_dv_as_string" in ir
	assert "Variant_" in ir
