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
	dv_with_attrs = H.HDVInit(dv_type_name="Exc", args=[H.HLiteralInt(7)], attr_names=["detail"])
	block = H.HBlock(statements=[H.HThrow(value=dv_with_attrs)])
	res = tc.check_function("f", block)
	assert any("attribute 'detail' value must be DiagnosticValue" in d.message for d in res.diagnostics)


def test_attr_names_values_length_mismatch_is_reported():
	tc = _tc()
	# Two names, one value: should be diagnosed rather than silently truncated.
	dv_with_bad_shape = H.HDVInit(dv_type_name="Exc", args=[H.HLiteralString("x")], attr_names=["a", "b"])
	block = H.HBlock(statements=[H.HThrow(value=dv_with_bad_shape)])
	res = tc.check_function("f", block)
	assert any("attribute names/values mismatch" in d.message for d in res.diagnostics)
