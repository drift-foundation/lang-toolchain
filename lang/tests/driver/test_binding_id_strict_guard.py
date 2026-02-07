# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from dataclasses import is_dataclass, fields

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.stage1.node_ids import assign_node_ids
from lang.driftc.stage2.hir_to_mir import HIRToMIR, make_builder


def _collect_node_ids(obj: object) -> set[int]:
	ids: set[int] = set()
	seen: set[int] = set()

	def walk(val: object) -> None:
		obj_id = id(val)
		if obj_id in seen:
			return
		seen.add(obj_id)
		if isinstance(val, H.HNode):
			node_id = getattr(val, "node_id", 0)
			if node_id:
				ids.add(node_id)
		if is_dataclass(val):
			for f in fields(val):
				walk(getattr(val, f.name))
		elif isinstance(val, list):
			for item in val:
				walk(item)
		elif isinstance(val, dict):
			for item in val.values():
				walk(item)

	walk(obj)
	return ids


def _lower_strict(block: H.HBlock) -> HIRToMIR:
	table = TypeTable()
	int_ty = table.ensure_int()
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	expr_types = {node_id: int_ty for node_id in _collect_node_ids(block)}
	lower = HIRToMIR(
		builder,
		type_table=table,
		expr_types=expr_types,
		return_type=int_ty,
		typed_mode="strict",
	)
	lower.lower_function_body(block)
	return lower


def test_missing_binding_id_in_place_is_strict_error() -> None:
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), binding_id=0, is_mutable=True),
			H.HReturn(value=H.HVar(name="x", binding_id=0)),
		]
	)
	assign_node_ids(block)
	lower = _lower_strict(block)
	with pytest.raises(AssertionError, match="missing binding_id for place base"):
		lower._lower_addr_of_place(H.HPlaceExpr(base=H.HVar(name="x", binding_id=None), projections=[]), is_mut=True)


def test_missing_binding_id_in_value_read_is_strict_error() -> None:
	assign_stmt = H.HAssign(
		target=H.HPlaceExpr(base=H.HVar(name="y", binding_id=1), projections=[]),
		value=H.HBinary(op=H.BinaryOp.ADD, left=H.HVar(name="x", binding_id=None), right=H.HLiteralInt(1)),
	)
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), binding_id=0, is_mutable=True),
			H.HLet(name="y", value=H.HLiteralInt(0), binding_id=1, is_mutable=True),
			assign_stmt,
			H.HReturn(value=H.HVar(name="y", binding_id=1)),
		]
	)
	assign_node_ids(block)
	with pytest.raises(AssertionError, match="missing binding_id for local read"):
		_lower_strict(block)
