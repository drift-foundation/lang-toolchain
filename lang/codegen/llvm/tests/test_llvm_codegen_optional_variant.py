# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.codegen.llvm import LlvmModuleBuilder, lower_ssa_func_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import (
	TypeKind,
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.stage2 import (
	BasicBlock,
	ConstBool,
	ConstString,
	ConstructVariant,
	CopyValue,
	DropValue,
	MirFunc,
	Return,
	VariantGetField,
	ArrayLit,
)
from lang.driftc.stage4 import MirToSSA


def _declare_optional_base(table: TypeTable) -> int:
	return table.declare_variant(
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

def _enable_copy_for_scalars(table: TypeTable) -> None:
	def _copy_query(tid: int) -> bool | None:
		td = table.get(tid)
		if td.kind is TypeKind.SCALAR and td.name in {"Int", "Uint", "Bool", "Float", "String", "Void"}:
			return True
		if td.kind in {TypeKind.REF, TypeKind.VOID}:
			return bool(getattr(td, "ref_mut", False)) is False
		return None
	table.set_copy_query(_copy_query, allow_fallback=True)


def _lower_ir(mir: MirFunc, fn_info: FnInfo, table: TypeTable) -> str:
	ssa = MirToSSA().run(mir)
	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits()))
	return mod.render()


def _fn_info(name: str, ret_ty: int) -> FnInfo:
	sig = FnSignature(name=name, param_type_ids=[], return_type_id=ret_ty, declared_can_throw=False)
	return FnInfo(
		fn_id=FunctionId(module="main", name=name, ordinal=0),
		name=name,
		declared_can_throw=False,
		return_type_id=ret_ty,
		signature=sig,
	)


def test_optional_copyvalue_string_retains_and_drops() -> None:
	table = TypeTable()
	_enable_copy_for_scalars(table)
	opt_base = _declare_optional_base(table)
	opt_string = table.ensure_instantiated(opt_base, [table.ensure_string()])

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="s", value="hi"),
			ConstructVariant(dest="opt", variant_ty=opt_string, ctor="Some", args=["s"]),
			CopyValue(dest="opt2", value="opt", ty=opt_string),
			DropValue(value="opt", ty=opt_string),
			DropValue(value="opt2", ty=opt_string),
		],
		terminator=Return(value=None),
	)
	mir = MirFunc(
		fn_id=FunctionId(module="main", name="f", ordinal=0),
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	fn_info = _fn_info("f", table.ensure_void())
	ir = _lower_ir(mir, fn_info, table)

	assert "@drift_string_retain" in ir
	assert "call void @drift_string_release" in ir


def test_optional_drop_array_string_calls_array_drop() -> None:
	table = TypeTable()
	_enable_copy_for_scalars(table)
	opt_base = _declare_optional_base(table)
	string_ty = table.ensure_string()
	arr_string = table.new_array(string_ty)
	opt_arr_string = table.ensure_instantiated(opt_base, [arr_string])

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="s", value="x"),
			ArrayLit(dest="arr", elem_ty=string_ty, elements=["s"]),
			ConstructVariant(dest="opt", variant_ty=opt_arr_string, ctor="Some", args=["arr"]),
			DropValue(value="opt", ty=opt_arr_string),
		],
		terminator=Return(value=None),
	)
	mir = MirFunc(
		fn_id=FunctionId(module="main", name="f", ordinal=0),
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	fn_info = _fn_info("f", table.ensure_void())
	ir = _lower_ir(mir, fn_info, table)

	assert "@__drift_array_drop_" in ir
	assert "call void @drift_free_array" in ir


def test_optional_drop_optional_string_releases() -> None:
	table = TypeTable()
	_enable_copy_for_scalars(table)
	opt_base = _declare_optional_base(table)
	opt_string = table.ensure_instantiated(opt_base, [table.ensure_string()])
	opt_opt_string = table.ensure_instantiated(opt_base, [opt_string])

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="s", value="x"),
			ConstructVariant(dest="inner", variant_ty=opt_string, ctor="Some", args=["s"]),
			ConstructVariant(dest="outer", variant_ty=opt_opt_string, ctor="Some", args=["inner"]),
			DropValue(value="outer", ty=opt_opt_string),
		],
		terminator=Return(value=None),
	)
	mir = MirFunc(
		fn_id=FunctionId(module="main", name="f", ordinal=0),
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	fn_info = _fn_info("f", table.ensure_void())
	ir = _lower_ir(mir, fn_info, table)

	assert "call void @drift_string_release" in ir
	# Variant drop must lower via helper call so it doesn't inject CFG labels into the caller block.
	assert "var_drop_done" not in ir


def test_optional_bool_storage_decode_uses_icmp() -> None:
	table = TypeTable()
	_enable_copy_for_scalars(table)
	opt_base = _declare_optional_base(table)
	opt_bool = table.ensure_instantiated(opt_base, [table.ensure_bool()])

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstBool(dest="b", value=True),
			ConstructVariant(dest="opt", variant_ty=opt_bool, ctor="Some", args=["b"]),
			VariantGetField(dest="v", variant="opt", variant_ty=opt_bool, ctor="Some", field_index=0, field_ty=table.ensure_bool()),
		],
		terminator=Return(value="v"),
	)
	mir = MirFunc(
		fn_id=FunctionId(module="main", name="f", ordinal=0),
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	fn_info = _fn_info("f", table.ensure_bool())
	ir = _lower_ir(mir, fn_info, table)

	assert "icmp ne i8" in ir


def test_optional_ir_has_no_legacy_optional_types() -> None:
	table = TypeTable()
	_enable_copy_for_scalars(table)
	opt_base = _declare_optional_base(table)
	opt_int = table.ensure_instantiated(opt_base, [table.ensure_int()])

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstructVariant(dest="opt", variant_ty=opt_int, ctor="None", args=[]),
		],
		terminator=Return(value="opt"),
	)
	mir = MirFunc(
		fn_id=FunctionId(module="main", name="f", ordinal=0),
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	fn_info = _fn_info("f", opt_int)
	ir = _lower_ir(mir, fn_info, table)

	assert "DriftOptional" not in ir
