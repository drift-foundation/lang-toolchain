# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 2 tests: HIR→MIR lowering for throw/try (throw only for now).
"""

from lang.driftc import stage1 as H
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.stage2 import (
	HIRToMIR,
	make_builder,
	ConstUint64,
	ConstString,
	ConstructDV,
	ConstructError,
	ErrorAddAttrDV,
	ConstructResultErr,
	Return,
)
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable


def test_throw_lowers_to_error_and_result_err_return():
	"""
	`throw ExceptionInit` should:
	  - emit a ConstUint64 for the event code placeholder,
	  - ConstructError(code, first field DV, key=field name),
	  - ConstructResultErr(error),
	  - Return that result.
	"""
	builder = make_builder(FunctionId(module="main", name="throw_fn", ordinal=0))
	type_table = TypeTable()
	type_table.exception_schemas = {"m:Boom": ("m:Boom", ["msg"])}
	fn_id = FunctionId(module="main", name="throw_fn", ordinal=0)
	lower = HIRToMIR(
		builder,
		type_table=type_table,
		current_fn_id=fn_id,
		can_throw_by_id={fn_id: True},
	)

	exc = H.HExceptionInit(
		event_fqn="m:Boom",
		pos_args=[],
		kw_args=[H.HKwArg(name="msg", value=H.HDVInit(dv_type_name="Boom", args=[H.HLiteralString("boom")]))],
	)
	hir_block = H.HBlock(statements=[H.HThrow(value=exc)])
	lower.lower_block(normalize_hir(hir_block))

	entry = builder.func.blocks["entry"]
	instrs = entry.instructions

	# Expect: DV ctor, event-code const, ConstructError, ConstructResultErr.
	dv_ctor = next(i for i in instrs if isinstance(i, ConstructDV))
	payload_const = next(i for i in instrs if isinstance(i, ConstString) and i.value == "boom")
	const_int = next(i for i in instrs if isinstance(i, ConstUint64))
	key_const = next(i for i in instrs if isinstance(i, ConstString) and i.value == "msg")
	event_name_const = next(i for i in instrs if isinstance(i, ConstString) and i.value == "m:Boom")
	err = next(i for i in instrs if isinstance(i, ConstructError))
	err_result = next(i for i in instrs if isinstance(i, ConstructResultErr))
	assert err.payload == dv_ctor.dest
	assert err.code == const_int.dest
	assert err.event_fqn == event_name_const.dest
	assert err.attr_key == key_const.dest
	assert err_result.error == err.dest

	term = entry.terminator
	assert isinstance(term, Return)
	assert term.value == err_result.dest


def test_exception_init_throw_attaches_all_fields():
	"""
	throw ExceptionInit(a = ..., b = ...) should store:
	  - first field under its declared name via ConstructError
	  - field 'a' under its declared name
	  - field 'b' under its declared name
	"""
	builder = make_builder(FunctionId(module="main", name="throw_exc", ordinal=0))
	type_table = TypeTable()
	type_table.exception_schemas = {"m:Evt": ("m:Evt", ["a", "b"])}
	fn_id = FunctionId(module="main", name="throw_exc", ordinal=0)
	lower = HIRToMIR(
		builder,
		type_table=type_table,
		current_fn_id=fn_id,
		can_throw_by_id={fn_id: True},
	)

	exc = H.HExceptionInit(
		event_fqn="m:Evt",
		pos_args=[],
		kw_args=[
			H.HKwArg(name="a", value=H.HDVInit(dv_type_name="Evt", args=[H.HLiteralInt(1)])),
			H.HKwArg(name="b", value=H.HDVInit(dv_type_name="Evt", args=[H.HLiteralInt(2)])),
		],
	)
	hir_block = H.HBlock(statements=[H.HThrow(value=exc)])
	lower.lower_block(normalize_hir(hir_block))

	entry = builder.func.blocks["entry"]
	add_attr_instrs = [i for i in entry.instructions if isinstance(i, ErrorAddAttrDV)]
	construct_err = next(i for i in entry.instructions if isinstance(i, ConstructError))
	# First field is seeded via ConstructError; remaining fields are added via ErrorAddAttrDV.
	assert len(add_attr_instrs) == 1
	# Keys come from ConstString instructions; verify the literal values.
	key_literals = [i.value for i in entry.instructions if isinstance(i, ConstString)]
	assert "m:Evt" in key_literals  # event FQN label
	assert "a" in key_literals and "b" in key_literals
	# The ConstructError seeds under the first declared field name.
	first_key_const = next(i for i in entry.instructions if isinstance(i, ConstString) and i.value == "a")
	assert construct_err.attr_key == first_key_const.dest
	# The appended ErrorAddAttrDV should use the remaining field name.
	add_key_const = next(i for i in entry.instructions if isinstance(i, ConstString) and i.value == "b")
	assert add_attr_instrs[0].key == add_key_const.dest
