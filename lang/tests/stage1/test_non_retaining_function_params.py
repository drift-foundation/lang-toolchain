# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc import stage1 as H
from lang.driftc.borrow_checker import EscapeLevel
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage1.non_retaining_analysis import analyze_non_retaining_params
from lang.driftc.type_checker import TypedFn


def _typed_fn_with_direct_invoke(fn_id: FunctionId, *, param_name: str, binding_id: int = 1) -> TypedFn:
	call = H.HInvoke(callee=H.HVar(name=param_name, binding_id=binding_id), args=[H.HLiteralInt(1)])
	body = H.HBlock(statements=[H.HExprStmt(expr=call)])
	return TypedFn(
		fn_id=fn_id,
		name=fn_id.name,
		params=[binding_id],
		param_bindings=[binding_id],
		locals=[],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={binding_id: 0},
		binding_names={binding_id: param_name},
		binding_mutable={binding_id: False},
		call_resolutions={},
	)


def _typed_fn_with_retain(fn_id: FunctionId, *, param_name: str, binding_id: int = 1) -> TypedFn:
	tmp_id = binding_id + 1
	hold = H.HLet(name="tmp", value=H.HVar(name=param_name, binding_id=binding_id), binding_id=tmp_id)
	body = H.HBlock(statements=[hold])
	return TypedFn(
		fn_id=fn_id,
		name=fn_id.name,
		params=[binding_id],
		param_bindings=[binding_id],
		locals=[tmp_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={binding_id: 0, tmp_id: 0},
		binding_names={binding_id: param_name, tmp_id: "tmp"},
		binding_mutable={binding_id: False, tmp_id: False},
		call_resolutions={},
	)


def test_fn_param_typeid_callable_direct_invoke() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	fn_ty = table.ensure_function([int_ty], int_ty, can_throw=False)
	fn_id = FunctionId(module="main", name="takes_fp", ordinal=0)
	sig = FnSignature(name="takes_fp", param_type_ids=[fn_ty], return_type_id=int_ty)
	typed_fns = {fn_id: _typed_fn_with_direct_invoke(fn_id, param_name="f")}
	sigs = analyze_non_retaining_params(typed_fns, {fn_id: sig}, type_table=table)
	assert sigs[fn_id].param_escape_level == [EscapeLevel.LOCAL]


def test_fn_param_raw_typeexpr_callable_direct_invoke() -> None:
	fn_id = FunctionId(module="main", name="takes_fp", ordinal=0)
	raw = TypeExpr(name="fn", args=[TypeExpr(name="Int"), TypeExpr(name="Int")])
	sig = FnSignature(name="takes_fp", param_types=[raw])
	typed_fns = {fn_id: _typed_fn_with_direct_invoke(fn_id, param_name="f")}
	sigs = analyze_non_retaining_params(typed_fns, {fn_id: sig})
	assert sigs[fn_id].param_escape_level == [EscapeLevel.LOCAL]


@pytest.mark.parametrize("ref_name", ["&", "&mut"])
def test_fn_param_raw_ref_wrapped_callable(ref_name: str) -> None:
	fn_id = FunctionId(module="main", name="takes_fp", ordinal=0)
	raw = TypeExpr(name=ref_name, args=[TypeExpr(name="fn", args=[TypeExpr(name="Int"), TypeExpr(name="Int")])])
	sig = FnSignature(name="takes_fp", param_types=[raw])
	typed_fns = {fn_id: _typed_fn_with_direct_invoke(fn_id, param_name="f")}
	sigs = analyze_non_retaining_params(typed_fns, {fn_id: sig})
	assert sigs[fn_id].param_escape_level == [EscapeLevel.LOCAL]


def test_fn_param_retain_marks_false() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	fn_ty = table.ensure_function([int_ty], int_ty, can_throw=False)
	fn_id = FunctionId(module="main", name="takes_fp", ordinal=0)
	sig = FnSignature(name="takes_fp", param_type_ids=[fn_ty], return_type_id=int_ty)
	typed_fns = {fn_id: _typed_fn_with_retain(fn_id, param_name="f")}
	sigs = analyze_non_retaining_params(typed_fns, {fn_id: sig}, type_table=table)
	# Retaining param: analysis cannot prove non-retaining → param_escape_level stays None (THREAD default)
	assert sigs[fn_id].param_escape_level is None


def test_pre_seeded_local_downgraded_to_retaining() -> None:
	"""Fix 2 regression: _build_pel must clear a pre-seeded LOCAL annotation when
	analysis proves the param is retaining (v is False).

	If the incoming sig already has param_escape_level=[LOCAL] but the function body
	stores the param (retaining), the output must be None — not stale LOCAL.
	"""
	table = TypeTable()
	int_ty = table.ensure_int()
	fn_ty = table.ensure_function([int_ty], int_ty, can_throw=False)
	fn_id = FunctionId(module="main", name="takes_fp", ordinal=0)
	# Pre-seed sig with LOCAL (as if a previous annotation pass had set it)
	sig = FnSignature(name="takes_fp", param_type_ids=[fn_ty], return_type_id=int_ty, param_escape_level=[EscapeLevel.LOCAL])
	typed_fns = {fn_id: _typed_fn_with_retain(fn_id, param_name="f")}
	sigs = analyze_non_retaining_params(typed_fns, {fn_id: sig}, type_table=table)
	# Analysis proved retaining → stale LOCAL must be cleared → None (THREAD default)
	assert sigs[fn_id].param_escape_level is None


def test_immediate_level_treated_as_non_retaining() -> None:
	"""Fix 1 + Fix 4 regression: IMMEDIATE is the most restrictive non-escaping level.

	_pel_to_nr must treat IMMEDIATE as non-retaining (True) so it participates in the
	fixpoint as a candidate for non-retaining classification.

	_build_pel must preserve the stricter IMMEDIATE annotation when analysis confirms
	non-retaining (v is True): IMMEDIATE.value (0) <= LOCAL.value (1), so the existing
	annotation is kept rather than overwritten with LOCAL.

	Concretely: if a sig enters with param_escape_level=[IMMEDIATE] and the function
	body only calls the param directly (non-retaining), the output must preserve IMMEDIATE
	(not normalize to LOCAL, not clear to None).
	"""
	table = TypeTable()
	int_ty = table.ensure_int()
	fn_ty = table.ensure_function([int_ty], int_ty, can_throw=False)
	fn_id = FunctionId(module="main", name="takes_fp", ordinal=0)
	sig = FnSignature(name="takes_fp", param_type_ids=[fn_ty], return_type_id=int_ty, param_escape_level=[EscapeLevel.IMMEDIATE])
	typed_fns = {fn_id: _typed_fn_with_direct_invoke(fn_id, param_name="f")}
	sigs = analyze_non_retaining_params(typed_fns, {fn_id: sig}, type_table=table)
	# IMMEDIATE → _pel_to_nr → True; fixpoint → True; _build_pel preserves IMMEDIATE (stricter than LOCAL)
	assert sigs[fn_id].param_escape_level == [EscapeLevel.IMMEDIATE]
