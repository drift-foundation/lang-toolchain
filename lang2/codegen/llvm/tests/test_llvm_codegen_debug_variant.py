# SSA-first LLVM debug info test for variant types.

from __future__ import annotations

from lang2.codegen.llvm import lower_module_to_llvm
from lang2.codegen.llvm.test_utils import host_word_bits
from lang2.driftc.checker import FnInfo, FnSignature
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.core.generic_type_expr import GenericTypeExpr
from lang2.driftc.core.span import Span
from lang2.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang2.driftc.stage2 import BasicBlock, ConstInt, ConstructVariant, MirFunc, Return, StoreLocal
from lang2.driftc.stage4 import MirToSSA


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


def test_debug_variant_emits_tag_and_payload_union() -> None:
	table = TypeTable()
	opt_base = _declare_optional_base(table)
	opt_int = table.ensure_instantiated(opt_base, [table.ensure_int()])

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="n", value=1),
			ConstructVariant(dest="opt_val", variant_ty=opt_int, ctor="Some", args=["n"]),
			StoreLocal(local="opt", value="opt_val"),
		],
		terminator=Return(value=None),
	)
	entry.instructions[0].span = Span(file="main.drift", line=1, column=1)
	entry.instructions[1].span = Span(file="main.drift", line=2, column=1)
	entry.instructions[2].span = Span(file="main.drift", line=3, column=1)
	entry.terminator.span = Span(file="main.drift", line=4, column=1)

	fn_id = FunctionId(module="main", name="main", ordinal=0)
	sig = FnSignature(name="main", return_type_id=table.ensure_void(), declared_can_throw=False)
	fn_info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, return_type_id=table.ensure_void(), signature=sig)
	mir = MirFunc(
		fn_id=fn_id,
		name="main",
		params=[],
		locals=["opt"],
		blocks={"entry": entry},
		entry="entry",
		local_types={"opt": opt_int},
	)
	ssa = MirToSSA().run(mir)
	mod = lower_module_to_llvm(
		funcs={fn_id: mir},
		ssa_funcs={fn_id: ssa},
		fn_infos={fn_id: fn_info},
		type_table=table,
		word_bits=host_word_bits(),
	)
	ir = mod.render()

	assert "DW_TAG_union_type" in ir
	assert "DW_TAG_enumeration_type" in ir
	assert '::Tag' in ir
	assert 'name: "tag"' in ir
	assert 'name: "payload"' in ir
	assert "::payload" in ir
