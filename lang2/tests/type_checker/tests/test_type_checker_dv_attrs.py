#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from lang2.driftc import stage1 as H
from lang2.driftc.type_checker import TypeChecker
from lang2.driftc.core.types_core import TypeTable


def _tc() -> TypeChecker:
	return TypeChecker(TypeTable())


def test_throw_payload_must_be_diagnostic_value():
	tc = _tc()
	block = H.HBlock(statements=[H.HThrow(value=H.HLiteralInt(1))])
	res = tc.check_function("f", block)
	assert any("throw payload must be DiagnosticValue" in d.message for d in res.diagnostics)


def test_attr_payload_must_be_diagnostic_value():
	tc = _tc()
	exc = H.HExceptionInit(
		event_name="Exc",
		field_names=["detail"],
		field_values=[H.HLiteralInt(7)],
	)
	block = H.HBlock(statements=[H.HThrow(value=exc)])
	res = tc.check_function("f", block)
	assert any("attribute 'detail' value must be DiagnosticValue" in d.message for d in res.diagnostics)


def test_attr_names_values_length_mismatch_is_reported():
	tc = _tc()
	# Two names, one value: should be diagnosed rather than silently truncated.
	exc = H.HExceptionInit(
		event_name="Exc",
		field_names=["a", "b"],
		field_values=[H.HLiteralString("x")],
	)
	block = H.HBlock(statements=[H.HThrow(value=exc)])
	res = tc.check_function("f", block)
	assert any("attribute names/values mismatch" in d.message for d in res.diagnostics)


def test_dv_ctor_rejects_unsupported_arg_type():
	tc = _tc()
	# Array literal is not a supported DV ctor payload in v1.
	dv_with_array = H.HDVInit(dv_type_name="Evt", args=[H.HArrayLiteral(elements=[H.HLiteralInt(1)])])
	block = H.HBlock(statements=[H.HExprStmt(expr=dv_with_array)])
	res = tc.check_function("f", block)
	assert any("unsupported DiagnosticValue constructor argument type" in d.message for d in res.diagnostics)
