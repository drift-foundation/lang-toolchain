# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-07
"""
HIR → MIR lowering for array literals and indexing.
"""

from lang2.core.types_core import TypeTable
from lang2.stage1 import HArrayLiteral, HAssign, HBlock, HExprStmt, HField, HIndex, HLet, HLiteralInt, HVar
from lang2.stage2 import ArrayIndexLoad, ArrayIndexStore, ArrayLen, ArrayLit, HIRToMIR, MirBuilder


def _make_type_table():
	table = TypeTable()
	int_ty = table.ensure_int()
	table.ensure_bool()
	table.ensure_string()
	table.ensure_unknown()
	return table, int_ty


def test_array_literal_and_index_lowering():
	table, int_ty = _make_type_table()
	block = HBlock(
		statements=[
			HLet(name="xs", value=HArrayLiteral(elements=[HLiteralInt(1), HLiteralInt(2)])),
			HExprStmt(expr=HIndex(subject=HVar("xs"), index=HLiteralInt(1))),
			HExprStmt(expr=HField(subject=HVar("xs"), name="len")),
		]
	)
	builder = MirBuilder("f")
	HIRToMIR(builder, type_table=table).lower_block(block)
	entry = builder.func.blocks[builder.func.entry]
	kinds = {type(instr) for instr in entry.instructions}
	assert ArrayLit in kinds
	assert ArrayIndexLoad in kinds
	assert ArrayLen in kinds
	array_lits = [instr for instr in entry.instructions if isinstance(instr, ArrayLit)]
	assert array_lits and array_lits[0].elem_ty == int_ty
	loads = [instr for instr in entry.instructions if isinstance(instr, ArrayIndexLoad)]
	assert loads and loads[0].elem_ty == int_ty


def test_array_index_store_lowering():
	table, int_ty = _make_type_table()
	block = HBlock(
		statements=[
			HLet(name="xs", value=HArrayLiteral(elements=[HLiteralInt(1), HLiteralInt(2)])),
			HAssign(
				target=HIndex(subject=HVar("xs"), index=HLiteralInt(0)),
				value=HLiteralInt(5),
			),
		]
	)
	builder = MirBuilder("f")
	HIRToMIR(builder, type_table=table).lower_block(block)
	entry = builder.func.blocks[builder.func.entry]
	stores = [instr for instr in entry.instructions if isinstance(instr, ArrayIndexStore)]
	assert stores and stores[0].elem_ty == int_ty
