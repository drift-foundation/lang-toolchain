# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-07
"""
HIR → MIR lowering for array literals and indexing.
"""

from lang.driftc.core.types_core import TypeTable
from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage1 import (
	HArrayLiteral,
	HAssign,
	HBlock,
	HBorrow,
	HExprStmt,
	HField,
	HIndex,
	HLet,
	HLiteralInt,
	HLiteralString,
	HVar,
)
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.stage2 import (
	ArrayAlloc,
	AddrOfArrayElem,
	ArrayElemAssign,
	ArrayElemInitUnchecked,
	ArrayIndexLoad,
	ArrayIndexLoadUnchecked,
	ArrayLen,
	ArraySetLen,
	CopyValue,
	HIRToMIR,
	make_builder,
)


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
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	HIRToMIR(builder, type_table=table).lower_block(normalize_hir(block))
	all_instrs = [instr for block in builder.func.blocks.values() for instr in block.instructions]
	kinds = {type(instr) for instr in all_instrs}
	assert ArrayAlloc in kinds
	assert ArrayElemInitUnchecked in kinds
	assert ArraySetLen in kinds
	assert ArrayIndexLoadUnchecked in kinds
	assert ArrayLen in kinds
	array_allocs = [instr for instr in all_instrs if isinstance(instr, ArrayAlloc)]
	assert array_allocs and array_allocs[0].elem_ty == int_ty
	loads = [instr for instr in all_instrs if isinstance(instr, ArrayIndexLoadUnchecked)]
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
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	HIRToMIR(builder, type_table=table).lower_block(normalize_hir(block))
	entry = builder.func.blocks[builder.func.entry]
	assigns = [instr for instr in entry.instructions if isinstance(instr, ArrayElemAssign)]
	assert assigns and assigns[0].elem_ty == int_ty


def test_array_index_load_inserts_copyvalue_for_copy_elems():
	table, _int_ty = _make_type_table()
	string_ty = table._string_type  # type: ignore[attr-defined]
	block = HBlock(
		statements=[
			HLet(name="xs", value=HArrayLiteral(elements=[HLiteralInt(1), HLiteralInt(2)])),
			HExprStmt(expr=HIndex(subject=HVar("xs"), index=HLiteralInt(1))),
		]
	)
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	lowerer = HIRToMIR(builder, type_table=table)
	lowerer._local_types["xs"] = table.new_array(string_ty)
	lowerer.lower_block(normalize_hir(block))
	all_instrs = [instr for block in builder.func.blocks.values() for instr in block.instructions]
	load_idx = None
	copy_idx = None
	for idx, instr in enumerate(all_instrs):
		if isinstance(instr, ArrayIndexLoadUnchecked):
			load_idx = idx
		if isinstance(instr, CopyValue):
			copy_idx = idx
	if load_idx is None or copy_idx is None:
		raise AssertionError("expected ArrayIndexLoadUnchecked followed by CopyValue for Copy element type")
	assert copy_idx > load_idx


def test_array_literal_reuses_copy_value_for_string_lvalues():
	"""String Scope A: each String LVALUE element of an array literal is
	retained via its own CopyValue — the local `s` stays live and the
	array owns independent handles. Pre-Scope-A, an isolated stage2
	TypeTable classified String non-Copy (no structural Copy status), so
	this shape emitted NO CopyValue here and relied on later passes; the
	isolated behavior now matches real compiles."""
	table, _int_ty = _make_type_table()
	string_ty = table._string_type  # type: ignore[attr-defined]
	block = HBlock(
		statements=[
			HLet(name="s", value=HLiteralString(value="a")),
			HLet(
				name="xs",
				value=HArrayLiteral(elements=[HVar("s"), HVar("s")]),
			),
		]
	)
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	lowerer = HIRToMIR(builder, type_table=table)
	lowerer.lower_block(normalize_hir(block))
	entry = builder.func.blocks[builder.func.entry]
	copy_vals = [instr for instr in entry.instructions if isinstance(instr, CopyValue) and instr.ty == string_ty]
	assert len(copy_vals) == 2


def test_array_index_borrow_lowers_to_addrof_elem():
	table, _int_ty = _make_type_table()
	block = HBlock(
		statements=[
			HLet(name="xs", value=HArrayLiteral(elements=[HLiteralInt(1), HLiteralInt(2)])),
			HLet(
				name="r",
				value=HBorrow(subject=HIndex(subject=HVar("xs"), index=HLiteralInt(0)), is_mut=False),
			),
		]
	)
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	HIRToMIR(builder, type_table=table).lower_block(normalize_hir(block))
	entry = builder.func.blocks[builder.func.entry]
	assert any(isinstance(instr, AddrOfArrayElem) for instr in entry.instructions)


def test_array_literal_single_string_lvalue_emits_copyvalue():
	"""String Scope A: a single String LVALUE element emits exactly one
	CopyValue (retain) — the name of this test finally matches its
	assertion; pre-Scope-A the isolated-table classification made this 0
	(see the reuses-variant above for the divergence explanation)."""
	table, _int_ty = _make_type_table()
	string_ty = table._string_type  # type: ignore[attr-defined]
	block = HBlock(
		statements=[
			HLet(name="s", value=HLiteralString(value="a")),
			HLet(
				name="xs",
				value=HArrayLiteral(elements=[HVar("s")]),
			),
		]
	)
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	lowerer = HIRToMIR(builder, type_table=table)
	lowerer.lower_block(normalize_hir(block))
	entry = builder.func.blocks[builder.func.entry]
	copy_vals = [instr for instr in entry.instructions if isinstance(instr, CopyValue) and instr.ty == string_ty]
	assert len(copy_vals) == 1
