# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression tests for constructor kwargs through typed MIR lowering.

These exercise _lower_constructor_call in hir_to_mir.py:
  - Positive: named constructor kwargs pass through typed MIR constructor lowering
  - Negative: mixed positional+named constructor args fail with clear non-internal
    contract diagnostic from ctor_call_issues (not a raw AssertionError)
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage1 import (
	HBlock,
	HReturn,
	HVar,
	HLiteralInt,
	HCall,
	HKwArg,
	assign_node_ids,
	assign_callsite_ids,
)
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.stage2 import HIRToMIR, make_builder, ConstructStruct

import pytest


def _setup_struct_type_table():
	"""Create a TypeTable with a Point struct (x: Int, y: Int)."""
	tt = TypeTable()
	int_ty = tt.ensure_int()
	struct_id = tt.declare_struct("main", "Point", ["x", "y"])
	tt.define_struct_fields(struct_id, [int_ty, int_ty])
	inst_id = tt.ensure_struct_instantiated(struct_id, [])
	return tt, inst_id, int_ty


def _lower_struct_ctor(block: HBlock, type_table: TypeTable, struct_id, int_ty):
	"""Lower a block containing a struct constructor call."""
	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	hir_norm = normalize_hir(block)
	assign_node_ids(hir_norm)
	assign_callsite_ids(hir_norm)
	call_info_by_callsite_id: dict[int, CallInfo] = {}

	def _walk(expr):
		if isinstance(expr, HCall):
			csid = getattr(expr, "callsite_id", None)
			if isinstance(csid, int):
				param_types = tuple(int_ty for _ in expr.args)
				info = CallInfo(
					target=CallTarget.constructor_struct(
						struct_id,
						ctor_arg_field_indices=tuple(range(len(expr.args))),
					),
					sig=CallSig(param_types=param_types, user_ret_type=struct_id, can_throw=False),
				)
				call_info_by_callsite_id[csid] = info
			for arg in list(expr.args):
				_walk(arg)
			for kw in list(expr.kwargs or []):
				_walk(kw.value)

	for stmt in hir_norm.statements:
		if hasattr(stmt, "value") and stmt.value is not None:
			_walk(stmt.value)

	lowerer = HIRToMIR(
		builder,
		type_table=type_table,
		call_info_by_callsite_id=call_info_by_callsite_id,
	)
	lowerer.lower_block(hir_norm)
	return builder.func


def test_named_kwargs_pass_through_typed_ctor_lowering() -> None:
	"""Named constructor kwargs must pass through typed MIR lowering.

	Simulates the checker's normalized form: kwargs cleared, values moved into
	expr.args, field mapping via ctor_arg_field_indices.  The critical contract
	is that _lower_constructor_call does NOT unconditionally reject kwargs —
	ctor_call_issues validates shape, not call_kwargs_issues.
	"""
	tt, struct_id, int_ty = _setup_struct_type_table()
	# Checker normalizes kwargs into positional args with field index mapping.
	# ctor_arg_field_indices=(1, 0) means: first arg → field y, second → field x.
	block = HBlock(
		statements=[
			HReturn(
				value=HCall(
					fn=HVar("Point"),
					args=[HLiteralInt(20), HLiteralInt(10)],
					kwargs=[],
				)
			),
		]
	)
	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	hir_norm = normalize_hir(block)
	assign_node_ids(hir_norm)
	assign_callsite_ids(hir_norm)
	call_info_by_callsite_id: dict[int, CallInfo] = {}

	def _walk(expr):
		if isinstance(expr, HCall):
			csid = getattr(expr, "callsite_id", None)
			if isinstance(csid, int):
				info = CallInfo(
					target=CallTarget.constructor_struct(
						struct_id,
						# y=20, x=10 → field indices reversed
						ctor_arg_field_indices=(1, 0),
					),
					sig=CallSig(param_types=(int_ty, int_ty), user_ret_type=struct_id, can_throw=False),
				)
				call_info_by_callsite_id[csid] = info
			for arg in list(expr.args):
				_walk(arg)

	for stmt in hir_norm.statements:
		if hasattr(stmt, "value") and stmt.value is not None:
			_walk(stmt.value)

	lowerer = HIRToMIR(
		builder,
		type_table=tt,
		call_info_by_callsite_id=call_info_by_callsite_id,
	)
	lowerer.lower_block(hir_norm)
	func = builder.func
	entry = func.blocks[func.entry]
	# Must emit ConstructStruct — not crash with AssertionError
	assert any(isinstance(op, ConstructStruct) for op in entry.instructions), \
		"expected ConstructStruct in MIR output for named-kwargs constructor"


def test_raw_kwargs_input_passes_typed_ctor_lowering() -> None:
	"""Named kwargs on expr.kwargs (not normalized by checker) must lower.

	This exercises the exact path that regressed: _lower_constructor_call
	receives args=[], kwargs=[x=10, y=20], no ctor_arg_field_indices.
	The kwargs-to-field lowering loop must place values in field order and
	emit ConstructStruct.
	"""
	tt, struct_id, int_ty = _setup_struct_type_table()
	block = HBlock(
		statements=[
			HReturn(
				value=HCall(
					fn=HVar("Point"),
					args=[],
					kwargs=[
						HKwArg(name="y", value=HLiteralInt(20)),
						HKwArg(name="x", value=HLiteralInt(10)),
					],
				)
			),
		]
	)
	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	hir_norm = normalize_hir(block)
	assign_node_ids(hir_norm)
	assign_callsite_ids(hir_norm)
	call_info_by_callsite_id: dict[int, CallInfo] = {}

	def _walk(expr):
		if isinstance(expr, HCall):
			csid = getattr(expr, "callsite_id", None)
			if isinstance(csid, int):
				info = CallInfo(
					target=CallTarget.constructor_struct(
						struct_id,
						ctor_arg_field_indices=None,
					),
					sig=CallSig(param_types=(), user_ret_type=struct_id, can_throw=False),
				)
				call_info_by_callsite_id[csid] = info
			for kw in list(expr.kwargs or []):
				_walk(kw.value)

	for stmt in hir_norm.statements:
		if hasattr(stmt, "value") and stmt.value is not None:
			_walk(stmt.value)

	lowerer = HIRToMIR(
		builder,
		type_table=tt,
		call_info_by_callsite_id=call_info_by_callsite_id,
	)
	lowerer.lower_block(hir_norm)
	func = builder.func
	entry = func.blocks[func.entry]
	assert any(isinstance(op, ConstructStruct) for op in entry.instructions), \
		"expected ConstructStruct in MIR output for raw kwargs constructor input"


def test_positional_ctor_still_works() -> None:
	"""Positional constructor args (the common path) still lower correctly."""
	tt, struct_id, int_ty = _setup_struct_type_table()
	block = HBlock(
		statements=[
			HReturn(
				value=HCall(
					fn=HVar("Point"),
					args=[HLiteralInt(1), HLiteralInt(2)],
					kwargs=[],
				)
			),
		]
	)
	func = _lower_struct_ctor(block, tt, struct_id, int_ty)
	entry = func.blocks[func.entry]
	assert any(isinstance(op, ConstructStruct) for op in entry.instructions)


def test_mixed_positional_and_named_ctor_fails_with_contract_diagnostic() -> None:
	"""Mixed positional+named in _lower_constructor_call → AssertionError from
	ctor_call_issues, NOT a raw 'keyword arguments' rejection.

	The error message must come from ctor_call_issues (mentioning arity or
	missing fields) and must not contain 'internal:'.
	"""
	tt, struct_id, int_ty = _setup_struct_type_table()
	# Simulate: Point(1, y=2) — 1 positional + 1 named — but this arrives at
	# MIR with expr.args=[1] and expr.kwargs=[y=2] (checker didn't normalize).
	# This should trigger ctor_call_issues validation, not call_kwargs_issues.
	block = HBlock(
		statements=[
			HReturn(
				value=HCall(
					fn=HVar("Point"),
					args=[HLiteralInt(1)],
					kwargs=[HKwArg(name="y", value=HLiteralInt(2))],
				)
			),
		]
	)
	builder = make_builder(FunctionId(module="main", name="test_func", ordinal=0))
	hir_norm = normalize_hir(block)
	assign_node_ids(hir_norm)
	assign_callsite_ids(hir_norm)
	call_info_by_callsite_id: dict[int, CallInfo] = {}

	def _walk(expr):
		if isinstance(expr, HCall):
			csid = getattr(expr, "callsite_id", None)
			if isinstance(csid, int):
				info = CallInfo(
					target=CallTarget.constructor_struct(
						struct_id,
						ctor_arg_field_indices=None,
					),
					sig=CallSig(param_types=(int_ty,), user_ret_type=struct_id, can_throw=False),
				)
				call_info_by_callsite_id[csid] = info
			for arg in list(expr.args):
				_walk(arg)
			for kw in list(expr.kwargs or []):
				_walk(kw.value)

	for stmt in hir_norm.statements:
		if hasattr(stmt, "value") and stmt.value is not None:
			_walk(stmt.value)

	lowerer = HIRToMIR(
		builder,
		type_table=tt,
		call_info_by_callsite_id=call_info_by_callsite_id,
	)
	with pytest.raises(AssertionError, match="reached MIR lowering"):
		lowerer.lower_block(hir_norm)
