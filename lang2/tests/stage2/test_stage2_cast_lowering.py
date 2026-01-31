# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.core.types_core import TypeTable
from lang2.driftc.parser import ast as parser_ast
from lang2.driftc.stage1.node_ids import assign_node_ids
from lang2.driftc.stage2 import HIRToMIR, make_builder
from lang2.driftc.stage2 import mir_nodes as M


def _lower_cast_target(name: str) -> list[M.MInstr]:
	table = TypeTable()
	cast_expr = H.HCast(
		target_type_expr=parser_ast.TypeExpr(name=name),
		value=H.HLiteralInt(1),
	)
	block = H.HBlock(statements=[H.HExprStmt(expr=cast_expr)])
	assign_node_ids(block)

	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	lower = HIRToMIR(builder, type_table=table, typed_mode="none")
	lower.lower_block(block)
	return builder.func.blocks["entry"].instructions


def test_stage2_cast_literal_to_byte_lowers_constbyte() -> None:
	instrs = _lower_cast_target("Byte")
	assert any(isinstance(instr, M.ConstByte) for instr in instrs)
	assert not any(isinstance(instr, M.CastScalar) for instr in instrs)


def test_stage2_cast_literal_to_uint_lowers_constuint() -> None:
	instrs = _lower_cast_target("Uint")
	assert any(isinstance(instr, M.ConstUint) for instr in instrs)
	assert not any(isinstance(instr, M.CastScalar) for instr in instrs)


def test_stage2_cast_literal_to_int_lowers_constint() -> None:
	instrs = _lower_cast_target("Int")
	assert any(isinstance(instr, M.ConstInt) for instr in instrs)
	assert not any(isinstance(instr, M.CastScalar) for instr in instrs)
