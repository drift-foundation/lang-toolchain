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
