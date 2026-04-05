from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
import pytest

from lang.codegen.llvm import lower_ssa_func_to_llvm, LlvmModuleBuilder
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.stage1 import BinaryOp
from lang.driftc.stage2 import (
	BasicBlock,
	ConstructError,
	ConstructDV,
	ErrorAddAttrDV,
	ConstructResultErr,
	ErrorAttrsGetDV,
	DVAsInt,
	BinaryOpInstr,
	VariantGetField,
	VariantTag,
	IfTerminator,
	ConstInt,
	ConstUint,
	ConstUint64,
	ConstString,
	MirFunc,
	Return,
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
		)
	return table.ensure_instantiated(base, [inner])


def test_error_attrs_lookup_lowered_to_runtime_call():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	opt_int_ty = _ensure_optional(table, int_ty)
	opt_int_inst = table.get_variant_instance(opt_int_ty)
	assert opt_int_inst is not None
	opt_int_some_tag = next(a.tag for a in opt_int_inst.arms if a.name == "Some")
	fnres_ty = table.new_fnresult(int_ty, err_ty)
	sig = FnSignature(name="f", param_type_ids=[], return_type_id=fnres_ty, declared_can_throw=True)
	fn_info = FnInfo(fn_id=FunctionId(module="main", name="f", ordinal=0), name="f", declared_can_throw=True, return_type_id=fnres_ty, signature=sig)

	entry = BasicBlock(
		name="entry",
	instructions=[
		ConstUint64(dest="code", value=1),
		ConstString(dest="ename", value="m:Evt"),
		ConstString(dest="key", value="k"),
		ConstructDV(dest="dv", dv_type_name="Missing", args=[]),
		ConstructError(dest="err", code="code", event_fqn="ename", payload="dv", attr_key="key"),
		ErrorAttrsGetDV(dest="dv", error="err", key="key"),
		ConstructResultErr(dest="res", error="err"),
	],
		terminator=Return(value="res"),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="f", ordinal=0), name="f", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits()))
	ir = mod.render()

	assert "declare void @__exc_attrs_get_dv" in ir
	assert "call void @__exc_attrs_get_dv" in ir
	assert "%DriftDiagnosticValue" in ir


def test_dv_as_int_returns_optional_int():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	dv_ty = table.ensure_diagnostic_value()
	opt_int_ty = _ensure_optional(table, int_ty)
	opt_int_inst = table.get_variant_instance(opt_int_ty)
	assert opt_int_inst is not None
	opt_int_some_tag = next(a.tag for a in opt_int_inst.arms if a.name == "Some")
	fnres_ty = table.new_fnresult(int_ty, err_ty)
	sig = FnSignature(name="g", param_type_ids=[], return_type_id=fnres_ty, declared_can_throw=True)
	fn_info = FnInfo(fn_id=FunctionId(module="main", name="g", ordinal=0), name="g", declared_can_throw=True, return_type_id=fnres_ty, signature=sig)

	entry = BasicBlock(
		name="entry",
	instructions=[
		ConstUint64(dest="code", value=2),
		ConstString(dest="ename", value="m:Evt"),
		ConstString(dest="key", value="k"),
		ConstructDV(dest="dv", dv_type_name="Missing", args=[]),
		ConstructError(dest="err", code="code", event_fqn="ename", payload="dv", attr_key="key"),
		ErrorAttrsGetDV(dest="dv", error="err", key="key"),
		DVAsInt(dest="opt", dv="dv"),
		ConstructResultErr(dest="res", error="err"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="g", ordinal=0), name="g", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits()))
	ir = mod.render()

	assert "declare" in ir  # basic sanity
	assert "call i1 @drift_dv_as_int" in ir


def test_error_additional_attr_lowered_to_runtime_call():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	fnres_ty = table.new_fnresult(int_ty, err_ty)
	sig = FnSignature(name="h", param_type_ids=[], return_type_id=fnres_ty, declared_can_throw=True)
	fn_info = FnInfo(fn_id=FunctionId(module="main", name="h", ordinal=0), name="h", declared_can_throw=True, return_type_id=fnres_ty, signature=sig)

	entry = BasicBlock(
		name="entry",
	instructions=[
		ConstUint64(dest="code", value=3),
		ConstString(dest="ename", value="m:Evt"),
		ConstString(dest="k1", value="a"),
		ConstInt(dest="v1", value=10),
			ConstructDV(dest="dv1", dv_type_name="a", args=["v1"]),
			ConstructError(dest="err", code="code", event_fqn="ename", payload="dv1", attr_key="k1"),
			ConstString(dest="k2", value="b"),
			ConstInt(dest="v2", value=20),
			ConstructDV(dest="dv2", dv_type_name="b", args=["v2"]),
			ErrorAddAttrDV(error="err", key="k2", value="dv2"),
			ConstructResultErr(dest="res", error="err"),
		],
		terminator=Return(value="res"),
	)
	mir = MirFunc(fn_id=FunctionId(module="main", name="h", ordinal=0), name="h", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)

	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits()))
	ir = mod.render()

	assert "call void @drift_error_add_attr_dv" in ir


def test_error_attr_round_trip_additional_key():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	opt_int_ty = _ensure_optional(table, int_ty)
	opt_int_inst = table.get_variant_instance(opt_int_ty)
	assert opt_int_inst is not None
	opt_int_some_tag = next(a.tag for a in opt_int_inst.arms if a.name == "Some")

	sig = FnSignature(name="attr_round_trip", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False)
	fn_info = FnInfo(fn_id=FunctionId(module="main", name="attr_round_trip", ordinal=0), name="attr_round_trip", declared_can_throw=False, return_type_id=int_ty, signature=sig)

	entry = BasicBlock(
		name="entry",
		instructions=[
		ConstUint64(dest="code", value=4),
			ConstString(dest="ename", value="m:Evt"),
			ConstString(dest="payload_key", value="k"),
			ConstInt(dest="payload_int", value=0),
			ConstructDV(dest="payload_dv", dv_type_name="Evt", args=["payload_int"]),
		ConstructError(dest="err", code="code", event_fqn="ename", payload="payload_dv", attr_key="payload_key"),
			ConstString(dest="attr_key", value="answer"),
			ConstInt(dest="attr_val", value=7),
			ConstructDV(dest="attr_dv", dv_type_name="Evt", args=["attr_val"]),
			ErrorAddAttrDV(error="err", key="attr_key", value="attr_dv"),
			ErrorAttrsGetDV(dest="dv_out", error="err", key="attr_key"),
			DVAsInt(dest="opt", dv="dv_out"),
			VariantTag(dest="tag", variant="opt", variant_ty=opt_int_ty),
			ConstUint(dest="tag_some", value=opt_int_some_tag),
			BinaryOpInstr(dest="some", op=BinaryOp.EQ, left="tag", right="tag_some"),
		],
		terminator=IfTerminator(cond="some", then_target="then", else_target="else"),
	)
	then_block = BasicBlock(
		name="then",
		instructions=[
			VariantGetField(dest="val", variant="opt", variant_ty=opt_int_ty, ctor="Some", field_index=0, field_ty=int_ty),
		],
		terminator=Return(value="val"),
	)
	else_block = BasicBlock(
		name="else",
		instructions=[ConstInt(dest="zero", value=0)],
		terminator=Return(value="zero"),
	)
	mir = MirFunc(
		fn_id=FunctionId(module="main", name="attr_round_trip", ordinal=0),
		name="attr_round_trip",
		params=[],
		locals=[],
		blocks={"entry": entry, "then": then_block, "else": else_block},
		entry="entry",
	)
	ssa = MirToSSA().run(mir)

	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits()))
	ir = mod.render()

	assert "call %DriftDiagnosticValue @drift_dv_int" in ir
	assert "call ptr @drift_error_new_with_payload" in ir
	assert "call void @drift_error_add_attr_dv" in ir
	assert "call void @__exc_attrs_get_dv" in ir
	assert "call i1 @drift_dv_as_int" in ir
	assert "Variant_" in ir


def test_construct_dv_string_owned_emits_move_and_declaration():
	"""ConstructDV(String, owns_string_arg=True) must emit
	drift_dv_string_move and declare it in the module.

	Regression: owned string temps passed to DiagnosticValue::String
	must use drift_dv_string_move (no retain, takes ownership).
	Without the declaration, clang fails with undefined symbol.
	"""
	table = TypeTable()
	int_ty = table.ensure_int()
	str_ty = table.ensure_string()
	dv_ty = table.ensure_diagnostic_value()
	sig = FnSignature(name="dv_str", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False)
	fn_info = FnInfo(
		fn_id=FunctionId(module="main", name="dv_str", ordinal=0),
		name="dv_str", declared_can_throw=False, return_type_id=int_ty, signature=sig,
	)

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="s", value="hello"),
			ConstructDV(dest="dv", dv_type_name="String", args=["s"], owns_string_arg=True),
			ConstInt(dest="zero", value=0),
		],
		terminator=Return(value="zero"),
	)
	mir = MirFunc(
		fn_id=FunctionId(module="main", name="dv_str", ordinal=0),
		name="dv_str", params=[], locals=[],
		blocks={"entry": entry}, entry="entry",
	)
	ssa = MirToSSA().run(mir)

	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits()))
	ir = mod.render()

	# ConstructDV(String) must use drift_dv_string_move (ownership transfer,
	# no retain) rather than drift_dv_string (borrow + retain).
	assert "call %DriftDiagnosticValue @drift_dv_string_move(" in ir
	# The module must declare drift_dv_string_move so clang can link it.
	assert "declare %DriftDiagnosticValue @drift_dv_string_move(" in ir
