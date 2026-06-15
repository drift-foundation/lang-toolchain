# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""ConstructVariant payload autoload is gated on VariantGetFieldAddr provenance.

The `ConstructVariant` lowering will load a payload *value* out of a pointer
argument when the LLVM types mismatch (pointer-where-value-expected).  That
autoload is legitimate for exactly ONE shape — the borrowed-match reconstruction
`match v { V::N(n) => V::N(n) }`, where the payload binder is a
`VariantGetFieldAddr` result.

Historically the autoload fired for ANY pointer-typed argument, which silently
masked the typed-catch LANGUAGE_BUG: an `Error` projection-view binder fed into a
native struct payload field was loaded as if it were that struct, producing a
double `drift_string_release` / SIGSEGV.  The checker now rejects that at the
constructor boundary; this test pins the defense-in-depth codegen guard so an
*arbitrary* (non-field-address) pointer reaching ConstructVariant raises an
internal lowering-contract failure instead of being autoloaded.
"""
from __future__ import annotations

import pytest

from lang.codegen.llvm import LlvmModuleBuilder, lower_ssa_func_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import (
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.stage2 import (
	AddrOfLocal,
	BasicBlock,
	ConstInt,
	ConstructVariant,
	MirFunc,
	Return,
	StoreLocal,
	VariantGetFieldAddr,
)
from lang.driftc.stage4 import MirToSSA


def _opt_int(table: TypeTable) -> int:
	base = table.declare_variant(
		"main",
		"Opt",
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
	return table.ensure_variant_instantiated(base, [table.ensure_int()])


def _fn_info(name: str, ret_ty: int, param_tys: list[int]) -> FnInfo:
	sig = FnSignature(name=name, param_type_ids=list(param_tys), return_type_id=ret_ty, declared_can_throw=False)
	return FnInfo(
		fn_id=FunctionId(module="main", name=name, ordinal=0),
		name=name,
		declared_can_throw=False,
		return_type_id=ret_ty,
		signature=sig,
	)


def _lower(mir: MirFunc, fn_info: FnInfo, table: TypeTable) -> str:
	ssa = MirToSSA().run(mir)
	mod = LlvmModuleBuilder(word_bits=host_word_bits())
	mod.emit_func(lower_ssa_func_to_llvm(mir, ssa, fn_info, type_table=table, word_bits=host_word_bits()))
	return mod.render()


def test_arbitrary_pointer_into_construct_variant_raises_contract_failure() -> None:
	"""An `AddrOfLocal` pointer (NOT a VariantGetFieldAddr) fed into a
	ConstructVariant Int payload must raise the internal lowering-contract
	failure rather than autoloading an unprovenanced address as a value."""
	table = TypeTable()
	opt_int = _opt_int(table)
	int_ty = table.ensure_int()

	entry = BasicBlock(
		name="entry",
		instructions=[
			# Initialise the slot so it gets stable alloca storage, then take
			# its address.  `ap` is a raw stack-slot pointer — an arbitrary
			# pointer with no VariantGetFieldAddr provenance.
			ConstInt(dest="c0", value=0),
			StoreLocal(local="slot", value="c0"),
			AddrOfLocal(dest="ap", local="slot"),
			ConstructVariant(dest="r", variant_ty=opt_int, ctor="Some", args=["ap"]),
		],
		terminator=Return(value="r"),
	)
	mir = MirFunc(
		fn_id=FunctionId(module="main", name="f", ordinal=0),
		name="f",
		params=[],
		locals=["slot"],
		blocks={"entry": entry},
		entry="entry",
		local_types={"slot": int_ty},
	)
	fn_info = _fn_info("f", opt_int, [])

	with pytest.raises(AssertionError, match="lowering-contract failure"):
		_lower(mir, fn_info, table)


def test_variant_field_addr_pointer_into_construct_variant_is_allowed() -> None:
	"""Positive control: a `VariantGetFieldAddr` result IS authorized to
	autoload into ConstructVariant (the borrowed-match reconstruct), so the
	same shape with provenance lowers without raising."""
	table = TypeTable()
	opt_int = _opt_int(table)
	int_ty = table.ensure_int()
	ref_opt = table.ensure_ref(opt_int)

	entry = BasicBlock(
		name="entry",
		instructions=[
			VariantGetFieldAddr(
				dest="p", variant_ref="w", variant_ty=opt_int,
				ctor="Some", field_index=0, field_ty=int_ty,
			),
			ConstructVariant(dest="r", variant_ty=opt_int, ctor="Some", args=["p"]),
		],
		terminator=Return(value="r"),
	)
	mir = MirFunc(
		fn_id=FunctionId(module="main", name="g", ordinal=0),
		name="g",
		params=["w"],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
		local_types={"w": ref_opt},
	)
	fn_info = _fn_info("g", opt_int, [ref_opt])

	ir = _lower(mir, fn_info, table)
	assert "autoload" in ir, "provenanced VariantGetFieldAddr pointer should autoload"
