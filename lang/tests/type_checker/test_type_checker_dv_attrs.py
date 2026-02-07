#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from lang.driftc import stage1 as H
from lang.driftc.type_checker import TypeChecker
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable


def _tc_with_exception_schema(event_fqn: str, fields: list[str]) -> TypeChecker:
	table = TypeTable()
	table.exception_schemas = {event_fqn: (event_fqn, fields)}
	return TypeChecker(table)


def _fn_id(name: str) -> FunctionId:
	return FunctionId(module="main", name=name, ordinal=0)


def test_throw_payload_must_be_exception_constructor():
	tc = TypeChecker(TypeTable())
	block = H.HBlock(statements=[H.HThrow(value=H.HLiteralInt(1))])
	res = tc.check_function(_fn_id("f"), block)
	assert any("throw payload must be an exception constructor" in d.message for d in res.diagnostics)


def test_attr_payload_must_be_diagnostic_value():
	tc = _tc_with_exception_schema("m:Exc", ["detail"])
	exc = H.HExceptionInit(
		event_fqn="m:Exc",
		pos_args=[],
		kw_args=[H.HKwArg(name="detail", value=H.HLiteralInt(7))],
	)
	block = H.HBlock(statements=[H.HThrow(value=exc)])
	res = tc.check_function(_fn_id("f"), block)
	# Primitive literals are allowed and auto-wrapped into DiagnosticValue during lowering.
	assert not res.diagnostics


def test_exception_ctor_duplicate_field_is_reported():
	tc = _tc_with_exception_schema("m:Exc", ["a"])
	exc = H.HExceptionInit(
		event_fqn="m:Exc",
		pos_args=[],
		kw_args=[
			H.HKwArg(name="a", value=H.HLiteralInt(1)),
			H.HKwArg(name="a", value=H.HLiteralInt(2)),
		],
	)
	block = H.HBlock(statements=[H.HThrow(value=exc)])
	res = tc.check_function(_fn_id("f"), block)
	assert any("duplicate exception field" in d.message for d in res.diagnostics)


def test_dv_ctor_rejects_unsupported_arg_type():
	tc = TypeChecker(TypeTable())
	# Array literal is not a supported DV ctor payload in v1.
	dv_with_array = H.HDVInit(dv_type_name="Evt", args=[H.HArrayLiteral(elements=[H.HLiteralInt(1)])])
	block = H.HBlock(statements=[H.HExprStmt(expr=dv_with_array)])
	res = tc.check_function(_fn_id("f"), block)
	assert any("unsupported DiagnosticValue constructor argument type" in d.message for d in res.diagnostics)


def test_exception_ctor_outside_throw_is_allowed():
	tc = _tc_with_exception_schema("m:Exc", ["detail"])
	exc_init = H.HExceptionInit(
		event_fqn="m:Exc",
		pos_args=[],
		kw_args=[H.HKwArg(name="detail", value=H.HDVInit(dv_type_name="D", args=[H.HLiteralInt(1)]))],
	)
	block = H.HBlock(statements=[H.HLet(name="x", value=exc_init)])
	res = tc.check_function(_fn_id("f"), block)
	assert res.diagnostics == []


def test_non_exception_throw_payload_is_rejected():
	tc = TypeChecker(TypeTable())
	block = H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="Evt", args=[]))])
	res = tc.check_function(_fn_id("f"), block)
	assert any("throw payload must be an exception constructor" in d.message for d in res.diagnostics)
