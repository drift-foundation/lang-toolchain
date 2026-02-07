# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Can-throw inference tests (surface `-> T`, internal FnResult ABI).

lang does not expose `FnResult` as a surface type. A function can still be
"can-throw" (it may throw exceptions) while declaring `-> T`. The compiler
tracks this as an effect and lowers can-throw functions to an internal
`FnResult<T, Error>` ABI.

These tests cover:
  - inference: uncaught `throw` forces a function to be can-throw
  - nothrow guard: an explicitly-not-throwing function that may throw yields a
    diagnostic with a span
"""

from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.checker import Checker, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable


def test_uncaught_throw_infers_can_throw_without_diagnostic():
	"""
	A function with an uncaught throw is inferred can-throw even when the
	signature does not explicitly opt into throwing.
	"""
	func_hirs = {
		"f": H.HBlock(
			statements=[
				H.HThrow(value=H.HExceptionInit(event_fqn="m:Boom", pos_args=[], kw_args=[])),
			]
		)
	}
	table = TypeTable()
	int_ty = table.ensure_int()
	table.exception_schemas = {"m:Boom": ("m:Boom", [])}
	signatures = {"f": FnSignature(name="f", param_type_ids=[], return_type_id=int_ty)}
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	checker = Checker(
		signatures_by_id={fn_id: signatures["f"]},
		hir_blocks_by_id={fn_id: func_hirs["f"]},
		call_info_by_callsite_id={},
		exception_catalog={"m:Boom": 1},
		type_table=table,
	)
	checked = checker.check_by_id([fn_id])
	assert checked.fn_infos_by_id[fn_id].declared_can_throw is True
	assert not checked.diagnostics


def test_explicit_nothrow_rejects_uncaught_throw_with_span():
	"""
	An explicit nothrow signature is treated as authoritative: if inference finds
	an uncaught throw, we emit a diagnostic anchored to the first throw site.
	"""
	func_hirs = {
		"f": H.HBlock(
			statements=[
				H.HThrow(value=H.HExceptionInit(event_fqn="m:Boom", pos_args=[], kw_args=[])),
			]
		)
	}
	table = TypeTable()
	int_ty = table.ensure_int()
	table.exception_schemas = {"m:Boom": ("m:Boom", [])}
	signatures = {"f": FnSignature(name="f", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False)}
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	checker = Checker(
		signatures_by_id={fn_id: signatures["f"]},
		hir_blocks_by_id={fn_id: func_hirs["f"]},
		call_info_by_callsite_id={},
		exception_catalog={"m:Boom": 1},
		type_table=table,
	)
	checked = checker.check_by_id([fn_id])
	msgs = [d.message for d in checked.diagnostics]
	assert any("declared nothrow but may throw" in m for m in msgs)
	assert any(getattr(d, "span", None) is not None for d in checked.diagnostics)


def test_try_catch_with_if_handles_throw_without_inference():
	func_hirs = {
		"f": H.HBlock(
			statements=[
				H.HTry(
					body=H.HBlock(
						statements=[
							H.HIf(
								cond=H.HLiteralBool(True),
								then_block=H.HBlock(
									statements=[
										H.HThrow(value=H.HExceptionInit(event_fqn="m:Boom", pos_args=[], kw_args=[])),
									]
								),
								else_block=None,
							),
						]
					),
					catches=[
						H.HCatchArm(
							event_fqn="m:Boom",
							binder=None,
							block=H.HBlock(statements=[]),
						)
					],
				)
			]
		)
	}
	table = TypeTable()
	int_ty = table.ensure_int()
	table.exception_schemas = {"m:Boom": ("m:Boom", [])}
	signatures = {"f": FnSignature(name="f", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False)}
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	checker = Checker(
		signatures_by_id={fn_id: signatures["f"]},
		hir_blocks_by_id={fn_id: func_hirs["f"]},
		call_info_by_callsite_id={},
		exception_catalog={"m:Boom": 1},
		type_table=table,
	)
	checked = checker.check_by_id([fn_id])
	assert checked.fn_infos_by_id[fn_id].declared_can_throw is False
	assert not checked.diagnostics
