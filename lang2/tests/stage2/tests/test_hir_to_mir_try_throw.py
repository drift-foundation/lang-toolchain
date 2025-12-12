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
	ConstructError,
	ErrorAddAttrDV,
	ConstructResultErr,
	Return,
)


def test_throw_lowers_to_error_and_result_err_return():
	"""
	`throw payload` should:
	  - emit a ConstInt for the event code placeholder,
	  - ConstructError(code, payload),
	  - ConstructResultErr(error),
	  - Return that result.
	"""
	builder = MirBuilder(name="throw_fn")
	lower = HIRToMIR(builder)

	hir_block = H.HBlock(statements=[H.HThrow(value=H.HLiteralString("boom"))])
	lower.lower_block(hir_block)

	entry = builder.func.blocks["entry"]
	instrs = entry.instructions

	# Expect: payload literal, event-code const, ConstructError, ConstructResultErr.
	# HIRToMIR now injects a ConstString for String.EMPTY; filter out any leading
	# injected empty-string literal when asserting.
	payload_const = next(i for i in instrs if isinstance(i, ConstString) and i.value == "boom")
	const_int = next(i for i in instrs if isinstance(i, ConstInt))
	key_const = next(i for i in instrs if isinstance(i, ConstString) and i.value == "payload")
	err = next(i for i in instrs if isinstance(i, ConstructError))
	err_result = next(i for i in instrs if isinstance(i, ConstructResultErr))
	assert err.payload == payload_const.dest
	assert err.code == const_int.dest
	assert err.attr_key == key_const.dest
	assert err_result.error == err.dest

	term = entry.terminator
	assert isinstance(term, Return)
	assert term.value == err_result.dest


def test_exception_init_throw_attaches_all_fields():
	"""
	throw ExceptionInit{a, b} should store:
	  - payload under \"payload\"
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
	# Expect attrs for both fields (first field is stored under its name in addition to payload).
	assert len(add_attr_instrs) == 2
	# Keys come from ConstString instructions; verify the literal values.
	key_literals = [i.value for i in entry.instructions if isinstance(i, ConstString)]
	assert "a" in key_literals and "b" in key_literals
