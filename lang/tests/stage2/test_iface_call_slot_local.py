# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import InterfaceMethodSchema, InterfaceParamSchema, TypeTable
from lang.driftc.stage2 import BasicBlock, CallIface, IfaceUpcast, MirFunc, Return


def _iface_schema(table: TypeTable) -> tuple[int, int]:
	parent = table.declare_interface("m", "A", type_params=[])
	child = table.declare_interface("m", "B", type_params=[])
	table.define_interface_schema_methods(
		parent,
		[
			InterfaceMethodSchema(
				name="f",
				type_params=[],
				params=[
					InterfaceParamSchema(
						name="self",
						type_expr=GenericTypeExpr(name="&", args=[GenericTypeExpr(name="Self", args=[])]),
					),
				],
				return_type=GenericTypeExpr(name="Void", args=[]),
			)
		],
	)
	table.define_interface_schema_methods(
		child,
		[
			InterfaceMethodSchema(
				name="g",
				type_params=[],
				params=[
					InterfaceParamSchema(
						name="self",
						type_expr=GenericTypeExpr(name="&", args=[GenericTypeExpr(name="Self", args=[])]),
					),
				],
				return_type=GenericTypeExpr(name="Void", args=[]),
			)
		],
		parent_base_ids=[parent],
	)
	return parent, child


def test_call_iface_slot_index_is_segment_local() -> None:
	table = TypeTable()
	parent, child = _iface_schema(table)
	offsets = table.interface_segment_offsets(child)
	assert offsets[parent] > 0
	slot = table.interface_method_vtable_slot(child, parent, "f")
	assert slot == 1
	fn_id = FunctionId(module="m", name="f", ordinal=0)
	mir = MirFunc(
		fn_id=fn_id,
		name="m::f",
		params=[],
		locals=[],
		blocks={
			"entry": BasicBlock(
				name="entry",
				instructions=[
					IfaceUpcast(dest="up", iface="c", slot_offset=offsets[parent]),
					CallIface(
						dest=None,
						iface="up",
						args=[],
						param_types=[],
						user_ret_type=table.ensure_void(),
						can_throw=False,
						slot_index=slot,
					),
				],
				terminator=Return(value=None),
			)
		},
		entry="entry",
	)
	# Ensure the MIR uses segment-local slot index (1) with an explicit upcast offset.
	assert mir.blocks["entry"].instructions[1].slot_index == 1
