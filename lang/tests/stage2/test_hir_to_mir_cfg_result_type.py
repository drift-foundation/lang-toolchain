# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Unit pins for `HIRToMIR._cfg_result_type`
(work/control-flow-rvalue-ownership).

The result type of a value-producing control-flow expression (match /
ternary) must NOT be decided by the first arm alone:
  1. the checker-RECORDED whole-expression type wins when concrete (it
     already reflects any arm convergence / common-type coercion);
  2. concrete arms that DISAGREE with no recorded common type are a
     checker-invariant violation and fail loud (never silently pick one);
  3. a TypeVar result is PRESERVED, not collapsed to Unknown.
"""
from __future__ import annotations

import pytest

from lang.driftc import stage1 as H
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable, TypeKind, TypeParamId


def _h2m(tt: TypeTable) -> HIRToMIR:
	b = make_builder(FunctionId(module="m", name="f", ordinal=0))
	return HIRToMIR(b, type_table=tt)


def _match(then_val: H.HExpr, else_val: H.HExpr, node_id: int | None = None) -> H.HExpr:
	m = H.HMatchExpr(
		scrutinee=H.HVar(name="c"),
		arms=[
			H.HMatchArm(ctor="true", binders=[], block=H.HBlock(statements=[]), result=then_val),
			H.HMatchArm(ctor="false", binders=[], block=H.HBlock(statements=[]), result=else_val),
		],
	)
	if node_id is not None:
		m.node_id = node_id
	return m


def test_recorded_type_wins_over_first_arm():
	"""When the checker recorded a concrete whole-expression type, it is
	used — the first arm's locally-inferred type does not override it."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	int_ty = tt.ensure_int()
	h2m = _h2m(tt)
	h2m._typed_mode = "full"
	m = _match(H.HLiteralString("a"), H.HLiteralString("b"), node_id=101)
	# Arms locally infer String; recorded says Int -> recorded wins.
	h2m._expr_types = {101: int_ty}
	assert h2m._cfg_result_type(m) == int_ty


def test_disagreeing_concrete_arms_fail_loud():
	"""No recorded type + concrete arms of different types -> fail loud
	rather than silently selecting the first arm."""
	tt = TypeTable()
	h2m = _h2m(tt)
	h2m._typed_mode = "none"
	h2m._expr_types = {}
	# then: String literal; else: Int literal -> disagreeing concrete arms.
	m = _match(H.HLiteralString("a"), H.HLiteralInt(1))
	with pytest.raises(AssertionError):
		h2m._cfg_result_type(m)


def test_agreeing_concrete_arms_use_common_type():
	tt = TypeTable()
	int_ty = tt.ensure_int()
	h2m = _h2m(tt)
	h2m._typed_mode = "none"
	h2m._expr_types = {}
	m = _match(H.HLiteralInt(1), H.HLiteralInt(2))
	assert h2m._cfg_result_type(m) == int_ty


def test_mixed_typevar_and_concrete_arms_fail_loud():
	"""A TypeVar arm alongside a concrete arm is NOT a common type: with no
	recorded/coercion result to resolve them, `_cfg_result_type` must fail
	loud rather than silently returning the concrete arm's type (which would
	be a lie for the TypeVar arm)."""
	tt = TypeTable()
	int_ty = tt.ensure_int()
	tv_ty = tt.ensure_typevar(TypeParamId(owner=FunctionId(module="m", name="f", ordinal=0), index=0), name="T")
	h2m = _h2m(tt)
	h2m._typed_mode = "none"
	h2m._expr_types = {}

	# Arm results whose stage2-inferred types are one TypeVar + one concrete.
	then_e = H.HLiteralInt(1)
	else_e = H.HLiteralInt(2)
	m = _match(then_e, else_e)
	orig_infer = h2m._infer_expr_type

	def _infer(sub):
		if sub is then_e:
			return tv_ty
		if sub is else_e:
			return int_ty
		return orig_infer(sub)
	h2m._infer_expr_type = _infer
	with pytest.raises(AssertionError):
		h2m._cfg_result_type(m)


def test_shared_typevar_arms_preserved():
	"""All arms the SAME TypeVar -> that TypeVar is preserved (a generic
	match result monomorphized later), not collapsed to Unknown."""
	tt = TypeTable()
	tv_ty = tt.ensure_typevar(TypeParamId(owner=FunctionId(module="m", name="f", ordinal=0), index=0), name="T")
	h2m = _h2m(tt)
	h2m._typed_mode = "none"
	h2m._expr_types = {}
	then_e = H.HLiteralInt(1)
	else_e = H.HLiteralInt(2)
	m = _match(then_e, else_e)
	orig_infer = h2m._infer_expr_type

	def _infer(sub):
		if sub in (then_e, else_e):
			return tv_ty
		return orig_infer(sub)
	h2m._infer_expr_type = _infer
	assert h2m._cfg_result_type(m) == tv_ty


def test_ternary_uses_branch_types():
	tt = TypeTable()
	int_ty = tt.ensure_int()
	h2m = _h2m(tt)
	h2m._typed_mode = "none"
	h2m._expr_types = {}
	t = H.HTernary(cond=H.HVar("c"), then_expr=H.HLiteralInt(1), else_expr=H.HLiteralInt(2))
	assert h2m._cfg_result_type(t) == int_ty
