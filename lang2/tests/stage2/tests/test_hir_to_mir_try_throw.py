# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 2 tests: HIR→MIR lowering for throw/try (throw only for now).
"""

from lang2.driftc import stage1 as H
from lang2.driftc.stage2 import (
	MirBuilder,
	HIRToMIR,
	ConstInt,
	ConstString,
	ConstructDV,
	ConstructError,
	ErrorAddAttrDV,
	ConstructResultErr,
	Return,
)


def test_throw_lowers_to_error_and_result_err_return():
	"""
	`throw ExceptionInit` should:
	  - emit a ConstInt for the event code placeholder,
	  - ConstructError(code, first field DV, key=field name),
	  - ConstructResultErr(error),
	  - Return that result.
	"""
	builder = MirBuilder(name="throw_fn")
	lower = HIRToMIR(builder)

	exc = H.HExceptionInit(
		event_name="Boom",
		field_names=["msg"],
		field_values=[H.HDVInit(dv_type_name="Boom", args=[H.HLiteralString("boom")])],
	)
	hir_block = H.HBlock(statements=[H.HThrow(value=exc)])
	lower.lower_block(hir_block)

	entry = builder.func.blocks["entry"]
	instrs = entry.instructions

	# Expect: DV ctor, event-code const, ConstructError, ConstructResultErr.
	dv_ctor = next(i for i in instrs if isinstance(i, ConstructDV))
	payload_const = next(i for i in instrs if isinstance(i, ConstString) and i.value == "boom")
	const_int = next(i for i in instrs if isinstance(i, ConstInt))
	key_const = next(i for i in instrs if isinstance(i, ConstString) and i.value == "msg")
	event_name_const = next(i for i in instrs if isinstance(i, ConstString) and i.value == "Boom")
	err = next(i for i in instrs if isinstance(i, ConstructError))
	err_result = next(i for i in instrs if isinstance(i, ConstructResultErr))
	assert err.payload == dv_ctor.dest
	assert err.code == const_int.dest
	assert err.event_name == event_name_const.dest
	assert err.attr_key == key_const.dest
	assert err_result.error == err.dest

	term = entry.terminator
	assert isinstance(term, Return)
	assert term.value == err_result.dest


def test_exception_init_throw_attaches_all_fields():
	"""
	throw ExceptionInit{a, b} should store:
	  - first field under its declared name via ConstructError
	  - field 'a' under its declared name
	  - field 'b' under its declared name
	"""
	builder = MirBuilder(name="throw_exc")
	lower = HIRToMIR(builder)

	exc = H.HExceptionInit(
		event_name="Evt",
		field_names=["a", "b"],
		field_values=[
			H.HDVInit(dv_type_name="Evt", args=[H.HLiteralInt(1)]),
			H.HDVInit(dv_type_name="Evt", args=[H.HLiteralInt(2)]),
		],
	)
	hir_block = H.HBlock(statements=[H.HThrow(value=exc)])
	lower.lower_block(hir_block)

	entry = builder.func.blocks["entry"]
	add_attr_instrs = [i for i in entry.instructions if isinstance(i, ErrorAddAttrDV)]
	# First field is seeded via ConstructError; remaining fields are added via ErrorAddAttrDV.
	assert len(add_attr_instrs) == 1
	# Keys come from ConstString instructions; verify the literal values.
	key_literals = [i.value for i in entry.instructions if isinstance(i, ConstString)]
	assert "Evt" in key_literals  # event name
	assert "a" in key_literals and "b" in key_literals
