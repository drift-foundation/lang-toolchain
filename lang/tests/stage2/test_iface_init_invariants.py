# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import InterfaceMethodSchema, TypeTable
from lang.driftc.mir_validate import validate_mir_iface_init_invariants
from lang.driftc.checker import FnSignature
from lang.driftc.stage2 import BasicBlock, DropValue, MirFunc, Return, ZeroValue


def _iface_type(table: TypeTable) -> int:
	base = table.declare_interface("m", "I", type_params=[])
	table.define_interface_schema_methods(
		base,
		methods=[
			InterfaceMethodSchema(
				name="f",
				type_params=[],
				params=[],
				return_type=GenericTypeExpr(name="Void", args=[]),
			)
		],
	)
	return base


def test_iface_drop_requires_initialized_value() -> None:
	table = TypeTable()
	iface_ty = _iface_type(table)
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
					DropValue(value="x", ty=iface_ty),
				],
				terminator=Return(value=None),
			)
		},
		entry="entry",
	)
	sig = FnSignature(name="f", param_type_ids=[], return_type_id=table.ensure_void(), declared_can_throw=False)
	with pytest.raises(AssertionError, match="DropValue of uninitialized iface value"):
		validate_mir_iface_init_invariants({fn_id: mir}, {fn_id: sig}, table)


def test_iface_drop_allows_zero_value() -> None:
	table = TypeTable()
	iface_ty = _iface_type(table)
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
					ZeroValue(dest="z", ty=iface_ty),
					DropValue(value="z", ty=iface_ty),
				],
				terminator=Return(value=None),
			)
		},
		entry="entry",
	)
	sig = FnSignature(name="f", param_type_ids=[], return_type_id=table.ensure_void(), declared_can_throw=False)
	validate_mir_iface_init_invariants({fn_id: mir}, {fn_id: sig}, table)
