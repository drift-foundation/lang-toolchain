# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.parser import ast as parser_ast
from lang.driftc.stage1.node_ids import assign_node_ids
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.stage2 import mir_nodes as M


def test_stage2_rejects_hcast_in_strict_mode() -> None:
	table = TypeTable()
	cast_expr = H.HCast(
		target_type_expr=parser_ast.TypeExpr(name="Int"),
		value=H.HLiteralInt(1),
	)
	block = H.HBlock(statements=[H.HExprStmt(expr=cast_expr)])
	assign_node_ids(block)

	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	lower = HIRToMIR(builder, type_table=table, typed_mode="strict")
	with pytest.raises(AssertionError) as excinfo:
		lower.lower_block(block)
	msg = str(excinfo.value)
	assert "HCast must be eliminated during typecheck" in msg
	assert "node_id=" in msg
	assert "target=" in msg


def test_stage2_allows_scalar_cast_in_non_strict_mode() -> None:
	table = TypeTable()
	cast_expr = H.HCast(
		target_type_expr=parser_ast.TypeExpr(name="Uint64"),
		value=H.HLiteralInt(1),
	)
	block = H.HBlock(statements=[H.HExprStmt(expr=cast_expr)])
	assign_node_ids(block)

	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	lower = HIRToMIR(builder, type_table=table, typed_mode="none")
	lower.lower_block(block)

	instrs = builder.func.blocks["entry"].instructions
	casts = [instr for instr in instrs if isinstance(instr, M.CastScalar)]
	assert casts
	cast = casts[0]
	assert cast.src_ty == table.ensure_int()
	assert cast.dst_ty == table.ensure_uint64()
