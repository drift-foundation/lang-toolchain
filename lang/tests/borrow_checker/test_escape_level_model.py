#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""A5: EscapeLevel taxonomy and Loan.max_escape foundation (Phase 0)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

import dataclasses

from lang.driftc.borrow_checker import EscapeLevel, Place, PlaceBase, PlaceKind
from lang.driftc.borrow_checker_pass import BorrowChecker, Loan, LoanKind
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeTable
from lang.driftc.checker import FnSignature
from lang.driftc import stage1 as H
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage1.node_ids import assign_node_ids
from lang.driftc.type_checker import TypedFn
from lang.driftc.core.function_id import FunctionId


def _make_loan(**kwargs):
	base = PlaceBase(kind=PlaceKind.LOCAL, local_id=1, name="x")
	place = Place(base=base, projections=())
	defaults = dict(
		place=place,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=None,
	)
	defaults.update(kwargs)
	return Loan(**defaults)


def _make_checker_with_ref_loan(ref_binding_id: int, max_escape: EscapeLevel):
	"""Build a BorrowChecker with a Loan for a &x ref binding with the given max_escape."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	x_id = 1
	r_id = ref_binding_id

	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
	])
	assign_node_ids(body)

	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})
	# Manually add a loan with the specified max_escape
	from lang.driftc.borrow_checker import PlaceBase as PB, PlaceKind as PK, Place as Pl
	base = PB(kind=PK.LOCAL, local_id=x_id, name="x")
	place = Pl(base=base, projections=())
	loan = Loan(
		place=place,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=max_escape,
	)
	return bc, loan


# ===== Phase 0 tests =====

def test_escape_level_ordering():
	assert EscapeLevel.IMMEDIATE < EscapeLevel.LOCAL
	assert EscapeLevel.LOCAL < EscapeLevel.SCOPED
	assert EscapeLevel.SCOPED < EscapeLevel.THREAD
	assert EscapeLevel.THREAD < EscapeLevel.STATIC


def test_loan_default_max_escape():
	loan = _make_loan()
	assert loan.max_escape == EscapeLevel.LOCAL


def test_loan_max_escape_propagation():
	loan = _make_loan(max_escape=EscapeLevel.LOCAL)
	cloned = dataclasses.replace(loan, ref_binding_id=99)
	assert cloned.max_escape == EscapeLevel.LOCAL


# ===== Phase 1 tests =====

def test_lambda_no_borrow_capture_is_static():
	"""Lambda with no REF/REF_MUT captures → STATIC."""
	table = TypeTable()
	int_ty = table.ensure_int()
	x_id = 1
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty},
		binding_names={x_id: "x"},
		binding_mutable={x_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})

	# Lambda with COPY capture (no REF/REF_MUT)
	from lang.driftc.stage1.capture_discovery import discover_captures
	lam = H.HLambda(params=[], body_expr=H.HLiteralInt(0), body_block=H.HBlock(statements=[]))
	# explicitly set no captures → discover_captures will find no ref captures
	from lang.driftc.borrow_checker_pass import _FlowState
	state = _FlowState()
	level = bc._lambda_escape_level(lam, state)
	assert level == EscapeLevel.STATIC


def test_lambda_ref_capture_is_local():
	"""Lambda with &T capture in active loan → LOCAL."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	x_id = 1
	r_id = 2
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})

	# Lambda that captures r by REF
	from lang.driftc.borrow_checker_pass import _FlowState
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind
	from lang.driftc.stage1 import closures as C
	lam = H.HLambda(params=[], body_expr=H.HVar(name="r", binding_id=r_id), body_block=H.HBlock(statements=[]))
	# The lambda body references r (a ref binding) → capture_discovery will find REF capture
	state = _FlowState()
	# Add a loan for x with ref_binding_id=r_id
	base = PlaceBase(kind=PlaceKind.LOCAL, local_id=x_id, name="x")
	place = Place(base=base, projections=())
	loan = Loan(
		place=place,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=EscapeLevel.LOCAL,
	)
	state.loans.add(loan)

	level = bc._lambda_escape_level(lam, state)
	assert level == EscapeLevel.LOCAL


def test_lambda_mut_ref_capture_is_local():
	"""Lambda with &mut T capture in active loan → LOCAL."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_mut_int_ty = table.ensure_ref_mut(int_ty)
	x_id = 1
	r_id = 2
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=True), binding_id=r_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_mut_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})

	from lang.driftc.borrow_checker_pass import _FlowState
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind
	lam = H.HLambda(params=[], body_expr=H.HVar(name="r", binding_id=r_id), body_block=H.HBlock(statements=[]))
	state = _FlowState()
	base = PlaceBase(kind=PlaceKind.LOCAL, local_id=x_id, name="x")
	place = Place(base=base, projections=())
	loan = Loan(
		place=place,
		kind=LoanKind.MUT,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=EscapeLevel.LOCAL,
	)
	state.loans.add(loan)

	level = bc._lambda_escape_level(lam, state)
	assert level == EscapeLevel.LOCAL


# ===== Phase 2 tests =====

def test_borrowed_capture_to_thread_param_rejected():
	"""Lambda capturing &T, required=THREAD → E_ESCAPE_THREAD diagnostic emitted."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	x_id = 1
	r_id = 2
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})

	from lang.driftc.borrow_checker_pass import _FlowState
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind
	lam = H.HLambda(params=[], body_expr=H.HVar(name="r", binding_id=r_id), body_block=H.HBlock(statements=[]))
	state = _FlowState()
	base = PlaceBase(kind=PlaceKind.LOCAL, local_id=x_id, name="x")
	place = Place(base=base, projections=())
	loan = Loan(
		place=place,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=EscapeLevel.LOCAL,
	)
	state.loans.add(loan)

	bc._check_lambda_escape_level(lam, state, EscapeLevel.THREAD, Span())
	assert any(d.code == "E_ESCAPE_THREAD" and d.phase == "borrow_check" for d in bc.diagnostics), bc.diagnostics


def test_borrowed_capture_to_local_param_accepted():
	"""Lambda with &T capture, required=LOCAL → no error, loan added as temporary."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	x_id = 1
	r_id = 2
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})

	from lang.driftc.borrow_checker_pass import _FlowState
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind, PlaceState
	lam = H.HLambda(params=[], body_expr=H.HVar(name="r", binding_id=r_id), body_block=H.HBlock(statements=[]))
	state = _FlowState()
	# Set both x and r as VALID (as they would be after executing the body)
	base_x = PlaceBase(kind=PlaceKind.LOCAL, local_id=x_id, name="x")
	base_r = PlaceBase(kind=PlaceKind.LOCAL, local_id=r_id, name="r")
	place_x = Place(base=base_x, projections=())
	place_r = Place(base=base_r, projections=())
	state.place_states[place_x] = PlaceState.VALID
	state.place_states[place_r] = PlaceState.VALID
	loan = Loan(
		place=place_x,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=EscapeLevel.LOCAL,
	)
	state.loans.add(loan)

	bc._check_lambda_escape_level(lam, state, EscapeLevel.LOCAL, Span())
	assert not any(d.severity == "error" for d in bc.diagnostics), bc.diagnostics


def test_no_borrow_capture_to_thread_accepted():
	"""Lambda with COPY/MOVE captures only, required=THREAD → no error."""
	table = TypeTable()
	int_ty = table.ensure_int()
	x_id = 1
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty},
		binding_names={x_id: "x"},
		binding_mutable={x_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})

	from lang.driftc.borrow_checker_pass import _FlowState
	lam = H.HLambda(params=[], body_expr=H.HLiteralInt(0), body_block=H.HBlock(statements=[]))
	state = _FlowState()

	bc._check_lambda_escape_level(lam, state, EscapeLevel.THREAD, Span())
	assert not any(d.severity == "error" for d in bc.diagnostics), bc.diagnostics


def test_check_block_spawn_thread_escape_rejected():
	"""check_block integration: HCall to THREAD-annotated spawn-like fn with borrowed-capture lambda → E_ESCAPE_THREAD."""
	from lang.driftc.method_registry import CallableDecl, CallableKind, CallableSignature, Visibility
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	void_ty = table.ensure_void()
	x_id = 1
	r_id = 2
	spawn_fn_id = FunctionId(module="std.concurrent", name="spawn", ordinal=0)
	spawn_sig = FnSignature(
		name="spawn",
		param_escape_level=[EscapeLevel.THREAD],
	)
	# Lambda: body references r (causes capture_discovery to emit REF capture)
	lam = H.HLambda(
		params=[],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HExprStmt(expr=H.HVar(name="r", binding_id=r_id)),
			H.HReturn(value=None),
		]),
		span=Span(),
	)
	# HVar for the spawn callee (module-qualified, binding_id=None → not a value)
	spawn_var = H.HVar(name="spawn", binding_id=None, module_id="std.concurrent")
	call_node = H.HCall(fn=spawn_var, args=[lam])
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
		H.HExprStmt(expr=call_node),
		H.HReturn(value=H.HLiteralInt(0)),
	])
	assign_node_ids(body)
	# Build a CallableDecl for spawn with fn_id
	spawn_callable = CallableDecl(
		callable_id=1,
		name="spawn",
		kind=CallableKind.FREE_FUNCTION,
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(int_ty,), result_type=void_ty),
		fn_id=spawn_fn_id,
	)
	call_resolutions = {call_node.node_id: spawn_callable}
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
		call_resolutions=call_resolutions,
	)
	bc = BorrowChecker.from_typed_fn(
		typed_fn,
		type_table=table,
		signatures_by_id={spawn_fn_id: spawn_sig},
	)
	diags = bc.check_block(body)
	assert any(d.code == "E_ESCAPE_THREAD" and d.phase == "borrow_check" for d in diags), diags


# ===== Phase 3a tests =====

def test_scope_outer_closure_annotated_scoped_returns_scoped():
	"""FnSignature with param_escape_level=[SCOPED] → effective_param_escape_level(0) == SCOPED.

	Phase 3a created a conservative bridge: SCOPED→LOCAL. Phase 4 removes that bridge;
	SCOPED params now return SCOPED directly, and the scope-lifetime reasoning in
	_check_lambda_escape_level/_check_lambda_scope_escape provides the actual safety check.
	"""
	sig = FnSignature(
		name="scope",
		param_escape_level=[EscapeLevel.SCOPED],
	)
	level = sig.effective_param_escape_level(0)
	assert level == EscapeLevel.SCOPED


def test_sort_in_place_comparator_local_accepted():
	"""Lambda with &T capture passed to LOCAL-annotated param → no error."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	x_id = 1
	r_id = 2
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})

	from lang.driftc.borrow_checker_pass import _FlowState
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind, PlaceState
	lam = H.HLambda(params=[], body_expr=H.HVar(name="r", binding_id=r_id), body_block=H.HBlock(statements=[]))
	state = _FlowState()
	base_x = PlaceBase(kind=PlaceKind.LOCAL, local_id=x_id, name="x")
	base_r = PlaceBase(kind=PlaceKind.LOCAL, local_id=r_id, name="r")
	place_x = Place(base=base_x, projections=())
	place_r = Place(base=base_r, projections=())
	state.place_states[place_x] = PlaceState.VALID
	state.place_states[place_r] = PlaceState.VALID
	loan = Loan(
		place=place_x,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=EscapeLevel.LOCAL,
	)
	state.loans.add(loan)

	# Simulate sort_in_place with LOCAL param_escape_level
	sig = FnSignature(name="sort_in_place", param_escape_level=[EscapeLevel.LOCAL])
	required = sig.effective_param_escape_level(0)
	bc._check_lambda_escape_level(lam, state, required, Span())
	assert not any(d.severity == "error" for d in bc.diagnostics), bc.diagnostics


def test_static_level_dry_run():
	"""dry-run: STATIC semantics exercised in isolation before Phase 3b."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	x_id = 1
	r_id = 2
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})

	from lang.driftc.borrow_checker_pass import _FlowState
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind
	lam = H.HLambda(params=[], body_expr=H.HVar(name="r", binding_id=r_id), body_block=H.HBlock(statements=[]))
	state = _FlowState()
	base = PlaceBase(kind=PlaceKind.LOCAL, local_id=x_id, name="x")
	place = Place(base=base, projections=())
	loan = Loan(
		place=place,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=EscapeLevel.LOCAL,
	)
	state.loans.add(loan)

	bc._check_lambda_escape_level(lam, state, EscapeLevel.STATIC, Span())
	assert any(d.code == "E_ESCAPE_STATIC" and d.phase == "borrow_check" for d in bc.diagnostics), bc.diagnostics


# ===== Phase 3b tests =====

def test_trait_object_callback_unannotated_thread_default():
	"""Unannotated param defaults to THREAD; note says 'no escape-level annotation; treated as THREAD in v1'."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	x_id = 1
	r_id = 2
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})

	from lang.driftc.borrow_checker_pass import _FlowState
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind
	lam = H.HLambda(params=[], body_expr=H.HVar(name="r", binding_id=r_id), body_block=H.HBlock(statements=[]))
	state = _FlowState()
	base = PlaceBase(kind=PlaceKind.LOCAL, local_id=x_id, name="x")
	place = Place(base=base, projections=())
	loan = Loan(
		place=place,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=EscapeLevel.LOCAL,
	)
	state.loans.add(loan)

	# Unannotated sig → THREAD default + from_unannotated=True
	sig = FnSignature(name="unannotated_fn", param_escape_level=None)
	required = sig.effective_param_escape_level(0)
	assert required == EscapeLevel.THREAD
	bc._check_lambda_escape_level(lam, state, required, Span(), from_unannotated=True)
	errors = [d for d in bc.diagnostics if d.severity == "error"]
	assert any(d.code == "E_ESCAPE_THREAD" for d in errors), errors
	assert any("no escape-level annotation" in note for d in errors for note in d.notes), errors


def test_hashmap_iter_callback_local_accepted():
	"""&T capture to HashMap LOCAL callback → no error."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	x_id = 1
	r_id = 2
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})

	from lang.driftc.borrow_checker_pass import _FlowState
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind, PlaceState
	lam = H.HLambda(params=[], body_expr=H.HVar(name="r", binding_id=r_id), body_block=H.HBlock(statements=[]))
	state = _FlowState()
	base_x = PlaceBase(kind=PlaceKind.LOCAL, local_id=x_id, name="x")
	base_r = PlaceBase(kind=PlaceKind.LOCAL, local_id=r_id, name="r")
	place_x = Place(base=base_x, projections=())
	place_r = Place(base=base_r, projections=())
	state.place_states[place_x] = PlaceState.VALID
	state.place_states[place_r] = PlaceState.VALID
	loan = Loan(
		place=place_x,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=EscapeLevel.LOCAL,
	)
	state.loans.add(loan)

	sig = FnSignature(name="hashmap_iter", param_escape_level=[EscapeLevel.LOCAL])
	required = sig.effective_param_escape_level(0)
	bc._check_lambda_escape_level(lam, state, required, Span())
	assert not any(d.severity == "error" for d in bc.diagnostics), bc.diagnostics


def test_spawn_thread_annotation_rejected():
	"""spawn param 0 annotated THREAD; borrowed-capture lambda → E_ESCAPE_THREAD."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	x_id = 1
	r_id = 2
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})
	from lang.driftc.borrow_checker_pass import _FlowState
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind
	lam = H.HLambda(params=[], body_expr=H.HVar(name="r", binding_id=r_id), body_block=H.HBlock(statements=[]))
	state = _FlowState()
	base = PlaceBase(kind=PlaceKind.LOCAL, local_id=x_id, name="x")
	place = Place(base=base, projections=())
	loan = Loan(
		place=place,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=EscapeLevel.LOCAL,
	)
	state.loans.add(loan)
	# spawn annotation: param 0 → THREAD
	sig = FnSignature(name="spawn", param_escape_level=[EscapeLevel.THREAD])
	required = sig.effective_param_escape_level(0)
	assert required == EscapeLevel.THREAD
	bc._check_lambda_escape_level(lam, state, required, Span())
	assert any(d.code == "E_ESCAPE_THREAD" and d.phase == "borrow_check" for d in bc.diagnostics), bc.diagnostics


# ===== Phase 3c gate test =====

def test_spawn_cb_ref_capture_caught_by_borrow_checker_directly():
	"""Phase 3c gate: borrow checker catches captures(&x) → spawn_cb (THREAD) via check_block.

	This test must be green BEFORE lambda_validate item 2 is removed. It proves
	the borrow checker independently catches the pattern that lambda_validate.py
	currently intercepts first in the production pipeline.
	"""
	from lang.driftc.method_registry import CallableDecl, CallableKind, CallableSignature, Visibility
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	void_ty = table.ensure_void()
	x_id = 1
	r_id = 2
	spawn_cb_fn_id = FunctionId(module="std.concurrent", name="spawn_cb", ordinal=0)
	spawn_cb_sig = FnSignature(
		name="spawn_cb",
		param_escape_level=[EscapeLevel.THREAD],
	)
	# Lambda body references r (ref to x); discover_captures infers REF capture of r.
	lam = H.HLambda(
		params=[],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HExprStmt(expr=H.HVar(name="r", binding_id=r_id)),
			H.HReturn(value=None),
		]),
		span=Span(),
	)
	spawn_cb_var = H.HVar(name="spawn_cb", binding_id=None, module_id="std.concurrent")
	call_node = H.HCall(fn=spawn_cb_var, args=[lam])
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
		H.HExprStmt(expr=call_node),
		H.HReturn(value=H.HLiteralInt(0)),
	])
	assign_node_ids(body)
	spawn_cb_callable = CallableDecl(
		callable_id=2,
		name="spawn_cb",
		kind=CallableKind.FREE_FUNCTION,
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(int_ty,), result_type=void_ty),
		fn_id=spawn_cb_fn_id,
	)
	call_resolutions = {call_node.node_id: spawn_cb_callable}
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
		call_resolutions=call_resolutions,
	)
	bc = BorrowChecker.from_typed_fn(
		typed_fn,
		type_table=table,
		signatures_by_id={spawn_cb_fn_id: spawn_cb_sig},
	)
	diags = bc.check_block(body)
	assert any(d.code == "E_ESCAPE_THREAD" and d.phase == "borrow_check" for d in diags), diags


# ===== Phase 4 regression tests =====
# These tests must FAIL before Phase 4 is implemented (SCOPED→LOCAL bridge still active
# or SCOPED escape check absent) and PASS after Phase 4 lands.

def _make_scope_checker_with_loan(x_id, r_id, block_stmts):
	"""Build a BorrowChecker + state with a shared loan for x (ref_binding_id=r_id)."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	body = H.HBlock(statements=list(block_stmts))
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind, PlaceState
	from lang.driftc.borrow_checker_pass import _FlowState
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})
	state = _FlowState()
	base_x = PlaceBase(kind=PlaceKind.LOCAL, local_id=x_id, name="x")
	base_r = PlaceBase(kind=PlaceKind.LOCAL, local_id=r_id, name="r")
	place_x = Place(base=base_x, projections=())
	place_r = Place(base=base_r, projections=())
	state.place_states[place_x] = PlaceState.VALID
	state.place_states[place_r] = PlaceState.VALID
	loan = Loan(
		place=place_x,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=EscapeLevel.LOCAL,
	)
	state.loans.add(loan)
	lam = H.HLambda(params=[], body_expr=H.HVar(name="r", binding_id=r_id), body_block=H.HBlock(statements=[]))
	return bc, state, lam, place_x


def test_scoped_spawn_with_outlying_borrow_accepted():
	"""Phase 4: &x borrow passed to SCOPED param; x defined before scope call in direct block → accepted.

	Regression: must not emit any error once Phase 4 scope-level check is implemented.
	Before Phase 4: direct call with required=SCOPED and no scope-check logic emits E_ESCAPE_STORE.
	After Phase 4: _check_lambda_scope_escape succeeds → no error.
	"""
	x_id, r_id = 1, 2
	block_stmts = [
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
		# scope call conceptually at stmt index 2
	]
	bc, state, lam, place_x = _make_scope_checker_with_loan(x_id, r_id, block_stmts)
	# Simulate _transfer_block position: scope call is at stmt_index=2
	bc._current_stmt_index = 2
	bc._current_block_stmts = block_stmts
	bc._check_lambda_escape_level(lam, state, EscapeLevel.SCOPED, Span())
	assert not any(d.severity == "error" for d in bc.diagnostics), bc.diagnostics


def test_scoped_spawn_with_non_outlying_borrow_rejected():
	"""Phase 4: &x borrow passed to SCOPED param; x NOT defined before scope call → E_ESCAPE_SCOPE.

	Simulates: conc.scope(|s| => { var x = 42; s.spawn(|| => { use x }) })
	where x is defined inside the scope body, not in the outer function before the scope call.
	The outer function sees the scope call at stmt 0 with no prior definition of x in the block.

	Regression: must emit E_ESCAPE_SCOPE once Phase 4 is implemented.
	Before Phase 4: emits E_ESCAPE_STORE (wrong code — bridge maps SCOPED→LOCAL so this test fails).
	After Phase 4: emits E_ESCAPE_SCOPE.
	"""
	x_id, r_id = 1, 2
	# block_stmts has NO HLet for x before index 0 (scope call is first stmt)
	block_stmts = []
	bc, state, lam, place_x = _make_scope_checker_with_loan(x_id, r_id, block_stmts)
	bc._current_stmt_index = 0
	bc._current_block_stmts = block_stmts
	bc._check_lambda_escape_level(lam, state, EscapeLevel.SCOPED, Span())
	assert any(d.code == "E_ESCAPE_SCOPE" and d.phase == "borrow_check" for d in bc.diagnostics), bc.diagnostics


def test_scoped_spawn_nested_block_false_positive():
	"""Phase 4 conservative MVP: x defined in a nested block, not the direct enclosing block → E_ESCAPE_SCOPE.

	Even though this pattern is provably safe (x is in scope when the scope call executes),
	_place_is_defined_before_stmt only checks the DIRECT enclosing BasicBlock's statements.
	If x's HLet is in a different block (e.g., a predecessor block's statements in the CFG),
	the check conservatively returns False.

	This test documents intentional conservative behavior. Do not convert to an accept case
	without a corresponding design change to _place_is_defined_before_stmt.

	Before Phase 4: emits E_ESCAPE_STORE (no SCOPED handling).
	After Phase 4: emits E_ESCAPE_SCOPE (conservative rejection).
	"""
	x_id, r_id = 1, 2
	# block_stmts is the CURRENT BasicBlock's statements.
	# It does NOT contain HLet for x — x was defined in a predecessor block
	# (e.g., inside a nested HIR block that became a different BasicBlock in the CFG).
	block_stmts = [
		# Some other statement (not HLet for x or r) before scope call at idx 1
		H.HExprStmt(expr=H.HLiteralInt(0)),
		# scope call at stmt_index=1
	]
	bc, state, lam, place_x = _make_scope_checker_with_loan(x_id, r_id, block_stmts)
	bc._current_stmt_index = 1
	bc._current_block_stmts = block_stmts
	bc._check_lambda_escape_level(lam, state, EscapeLevel.SCOPED, Span())
	assert any(d.code == "E_ESCAPE_SCOPE" and d.phase == "borrow_check" for d in bc.diagnostics), bc.diagnostics


def test_scoped_spawn_assigned_before_scope_accepted():
	"""Phase 4 HAssign fix: x assigned (not let-bound) in the direct block before the scope call → accepted.

	Models:
	    var x: Int          // declared in a predecessor block (not in current BasicBlock)
	    x = 42              // HAssign in current block before scope call
	    val r = &x
	    conc.scope(|_s| captures(ref r) => { ... })

	Before the HAssign fix: _place_is_defined_before_stmt only checks HLet, so x is not found
	→ false positive E_ESCAPE_SCOPE.
	After the HAssign fix: HAssign to x is detected → True → no error.
	"""
	x_id, r_id = 1, 2
	block_stmts = [
		# HAssign for x (declared in predecessor block, assigned here before scope call)
		H.HAssign(
			target=H.HPlaceExpr(base=H.HVar(name="x", binding_id=x_id)),
			value=H.HLiteralInt(42),
		),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
		# scope call conceptually at stmt index 2
	]
	bc, state, lam, place_x = _make_scope_checker_with_loan(x_id, r_id, block_stmts)
	bc._current_stmt_index = 2
	bc._current_block_stmts = block_stmts
	bc._check_lambda_escape_level(lam, state, EscapeLevel.SCOPED, Span())
	assert not any(d.severity == "error" for d in bc.diagnostics), bc.diagnostics


def test_registry_set_dropper_static_annotation_rejected():
	"""runtime_registry_set param 2 annotated STATIC; borrowed-capture lambda at index 2 → E_ESCAPE_STATIC."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int_ty = table.ensure_ref(int_ty)
	x_id = 1
	r_id = 2
	body = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
		H.HLet(name="r", value=H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=False), binding_id=r_id, is_mutable=False),
	])
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, r_id: ref_int_ty},
		binding_names={x_id: "x", r_id: "r"},
		binding_mutable={x_id: True, r_id: False},
		call_info_by_callsite_id={},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})
	from lang.driftc.borrow_checker_pass import _FlowState
	from lang.driftc.borrow_checker import Place, PlaceBase, PlaceKind
	lam = H.HLambda(params=[], body_expr=H.HVar(name="r", binding_id=r_id), body_block=H.HBlock(statements=[]))
	state = _FlowState()
	base = PlaceBase(kind=PlaceKind.LOCAL, local_id=x_id, name="x")
	place = Place(base=base, projections=())
	loan = Loan(
		place=place,
		kind=LoanKind.SHARED,
		temporary=False,
		live_blocks=None,
		origin_span=Span(),
		ref_binding_id=r_id,
		max_escape=EscapeLevel.LOCAL,
	)
	state.loans.add(loan)
	# registry_set annotation: param 2 → STATIC (params 0 and 1 are None-annotated)
	sig = FnSignature(name="runtime_registry_set", param_escape_level=[None, None, EscapeLevel.STATIC])
	required = sig.effective_param_escape_level(2)
	assert required == EscapeLevel.STATIC
	bc._check_lambda_escape_level(lam, state, required, Span())
	assert any(d.code == "E_ESCAPE_STATIC" and d.phase == "borrow_check" for d in bc.diagnostics), bc.diagnostics
