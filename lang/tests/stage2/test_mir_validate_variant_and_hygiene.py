# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.mir_validate import validate_mir_basic_hygiene, validate_mir_variant_field_invariants
from lang.driftc.stage2 import BasicBlock, ConstInt, ConstructVariant, DropValue, MirFunc, Return, VariantGetField


def _variant_ty(table: TypeTable) -> int:
	base = table.declare_variant(
		"m",
		"V",
		[],
		[
			VariantArmSchema(
				name="A",
				fields=[VariantFieldSchema(name="x", type_expr=GenericTypeExpr(name="Int", args=[]))],
			)
		],
	)
	return table.ensure_variant_instantiated(base, [])


def test_variant_get_field_invariants_accept_valid_shape() -> None:
	table = TypeTable()
	vty = _variant_ty(table)
	ity = table.ensure_int()
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	mir = MirFunc(
		fn_id=fn_id,
		name="f",
		params=[],
		locals=[],
		blocks={
			"entry": BasicBlock(
				name="entry",
				instructions=[
					ConstInt(dest="x", value=1),
					ConstructVariant(dest="v", variant_ty=vty, ctor="A", args=["x"]),
					VariantGetField(dest="out", variant="v", variant_ty=vty, ctor="A", field_index=0, field_ty=ity),
				],
				terminator=Return(value=None),
			)
		},
		entry="entry",
	)
	validate_mir_variant_field_invariants({fn_id: mir}, table)


def test_variant_get_field_invariants_reject_bad_field_index() -> None:
	table = TypeTable()
	vty = _variant_ty(table)
	ity = table.ensure_int()
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	mir = MirFunc(
		fn_id=fn_id,
		name="f",
		params=[],
		locals=[],
		blocks={
			"entry": BasicBlock(
				name="entry",
				instructions=[
					ConstInt(dest="x", value=1),
					ConstructVariant(dest="v", variant_ty=vty, ctor="A", args=["x"]),
					VariantGetField(dest="out", variant="v", variant_ty=vty, ctor="A", field_index=1, field_ty=ity),
				],
				terminator=Return(value=None),
			)
		},
		entry="entry",
	)
	with pytest.raises(AssertionError, match="field index out of range"):
		validate_mir_variant_field_invariants({fn_id: mir}, table)


def test_mir_basic_hygiene_rejects_undefined_drop_operand() -> None:
	table = TypeTable()
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	mir = MirFunc(
		fn_id=fn_id,
		name="f",
		params=[],
		locals=[],
		blocks={
			"entry": BasicBlock(
				name="entry",
				instructions=[DropValue(value="missing", ty=table.ensure_int())],
				terminator=Return(value=None),
			)
		},
		entry="entry",
	)
	with pytest.raises(AssertionError, match="undefined SSA operand"):
		validate_mir_basic_hygiene({fn_id: mir})
