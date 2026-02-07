#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-15
"""Regression tests for BorrowChecker.from_typed_fn PlaceKind mapping."""

from lang.driftc import stage1 as H
from lang.driftc.borrow_checker_pass import BorrowChecker
from lang.driftc.borrow_checker import PlaceKind
from lang.driftc.core.function_id import FunctionId
from lang.driftc.type_checker import TypeChecker


def test_from_typed_fn_marks_params_as_params():
	"""
	`BorrowChecker.from_typed_fn` must preserve whether a binding is a param or a local.

	This matters for long-term correctness/clarity, and avoids subtle identity bugs once
	params/locals get different lowering/ABI behaviors.
	"""
	tc = TypeChecker()
	int_ty = tc.type_table.ensure_int()
	body = H.HBlock(
		statements=[
			# Touch the param so it has a binding_id and a type.
			H.HExprStmt(expr=H.HVar("x")),
			# Declare a local as well.
			H.HLet(name="y", value=H.HLiteralInt(1), declared_type_expr=None),
		]
	)
	res = tc.check_function(FunctionId(module="main", name="f", ordinal=0), body, param_types={"x": int_ty})
	assert res.diagnostics == []
	typed_fn = res.typed_fn

	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=tc.type_table)
	param_binding_id = typed_fn.param_bindings[0]
	assert any(pb.kind is PlaceKind.PARAM and pb.local_id == param_binding_id for pb in bc.fn_types.keys())
	assert any(pb.kind is PlaceKind.LOCAL for pb in bc.fn_types.keys())
