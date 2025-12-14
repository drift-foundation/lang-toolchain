# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Can-throw inference tests (surface `returns T`, internal FnResult ABI).

lang2 does not expose `FnResult` as a surface type. A function can still be
"can-throw" (it may throw exceptions) while declaring `returns T`. The compiler
tracks this as an effect and lowers can-throw functions to an internal
`FnResult<T, Error>` ABI.

These tests cover:
  - inference: uncaught `throw` forces a function to be can-throw
  - nothrow guard: an explicitly-not-throwing function that may throw yields a
    diagnostic with a span
"""

from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.checker import Checker, FnSignature


def test_uncaught_throw_infers_can_throw_without_diagnostic():
	"""
	A function with an uncaught throw is inferred can-throw even when the
	signature does not explicitly opt into throwing.
	"""
	func_hirs = {
		"f": H.HBlock(
			statements=[
				H.HThrow(value=H.HExceptionInit(event_fqn="m:Boom", field_names=[], field_values=[])),
			]
		)
	}
	signatures = {"f": FnSignature(name="f", return_type="Int")}
	checker = Checker(signatures=signatures, hir_blocks=func_hirs, exception_catalog={"m:Boom": 1})
	checked = checker.check(["f"])
	assert checked.fn_infos["f"].declared_can_throw is True
	assert not checked.diagnostics


def test_explicit_nothrow_rejects_uncaught_throw_with_span():
	"""
	An explicit nothrow signature is treated as authoritative: if inference finds
	an uncaught throw, we emit a diagnostic anchored to the first throw site.
	"""
	func_hirs = {
		"f": H.HBlock(
			statements=[
				H.HThrow(value=H.HExceptionInit(event_fqn="m:Boom", field_names=[], field_values=[])),
			]
		)
	}
	signatures = {"f": FnSignature(name="f", return_type="Int", declared_can_throw=False)}
	checker = Checker(signatures=signatures, hir_blocks=func_hirs, exception_catalog={"m:Boom": 1})
	checked = checker.check(["f"])
	msgs = [d.message for d in checked.diagnostics]
	assert any("declared nothrow but may throw" in m for m in msgs)
	assert any(getattr(d, "span", None) is not None for d in checked.diagnostics)

