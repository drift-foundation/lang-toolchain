from __future__ import annotations

from lang.driftc.stage1 import (
	HBlock,
	HLet,
	HReturn,
	HIndex,
	HField,
	HVar,
	HLiteralString,
	HMethodCall,
	HDVInit,
)
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.stage2 import (
	HIRToMIR,
	make_builder,
	ErrorAttrsGetDV,
	DVAsInt,
	ConstructDV,
	StoreLocal,
)
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable


def test_error_attrs_index_lowers_to_error_attrs_get_dv():
	table = TypeTable()
	err_ty = table.ensure_error()

	block = HBlock(
		statements=[
			HLet(
				name="attr",
				value=HIndex(subject=HField(subject=HVar("err"), name="attrs"), index=HLiteralString("code")),
			),
			HReturn(value=HVar("attr")),
		]
	)
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	builder.func.params = ["err"]
	sig = FnSignature(name="f", param_type_ids=[err_ty], return_type_id=table.ensure_unknown(), declared_can_throw=False)
	lowerer = HIRToMIR(
		builder,
		type_table=table,
		param_types={"err": err_ty},
		signatures_by_id={FunctionId(module="main", name="f", ordinal=0): sig},
		return_type=sig.return_type_id,
	)
	lowerer.lower_block(normalize_hir(block))

	entry = builder.func.blocks[builder.func.entry]
	assert any(isinstance(instr, ErrorAttrsGetDV) for instr in entry.instructions)
	assert any(isinstance(instr, StoreLocal) and instr.local == "attr" for instr in entry.instructions)


def test_dv_as_int_lowers_to_dvasint():
	table = TypeTable()
	block = HBlock(
		statements=[
			HLet(name="dv", value=HDVInit(dv_type_name="Dummy", args=[])),
			HLet(name="opt", value=HMethodCall(receiver=HVar("dv"), method_name="as_int", args=[])),
			HReturn(value=HVar("opt")),
		]
	)
	builder = make_builder(FunctionId(module="main", name="f_dv", ordinal=0))
	lowerer = HIRToMIR(builder, type_table=table)
	lowerer.lower_block(normalize_hir(block))

	entry = builder.func.blocks[builder.func.entry]
	assert any(isinstance(instr, ConstructDV) for instr in entry.instructions)
	assert any(isinstance(instr, DVAsInt) for instr in entry.instructions)
