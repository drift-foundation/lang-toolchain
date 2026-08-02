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
	h2m._typed_mode = "strict"
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


def test_emit_cfg_result_extract_fails_loud_on_missing_type():
	"""Strict mode, MISSING type: fail OPEN (shallow LoadLocal, unregistered
	drop) — must raise regardless of allow_void (a missing type is never valid)."""
	tt = TypeTable()
	h2m = _h2m(tt)
	h2m._typed_mode = "strict"
	with pytest.raises(AssertionError):
		h2m._emit_cfg_result_extract("__match_result_missing", allow_void=True)


def test_emit_cfg_result_extract_fails_loud_on_unknown_type():
	"""Strict mode, UNKNOWN type: strict's contract is no Unknown, so this is a
	real join defect — must raise even with allow_void (Unknown != Void)."""
	tt = TypeTable()
	h2m = _h2m(tt)
	h2m._typed_mode = "strict"
	h2m._local_types["__match_result_unknown"] = h2m._unknown_type
	with pytest.raises(AssertionError):
		h2m._emit_cfg_result_extract("__match_result_unknown", allow_void=True)


def test_emit_cfg_result_extract_void_scoped_to_allow_void():
	"""Void is legitimate ONLY at a proven-unreachable (all-diverging) join —
	`allow_void=True`.  A Void result at a REACHABLE join (`allow_void=False`) is
	a value-producing control-flow expr mis-typed Void and MUST raise.  This pins
	the exemption to the all-diverging case, not arbitrary Void."""
	tt = TypeTable()
	void_ty = tt.ensure_void()
	h2m = _h2m(tt)
	h2m._typed_mode = "strict"
	h2m._local_types["__match_result_void"] = void_ty
	# all-diverging join: exempt.
	assert h2m._emit_cfg_result_extract("__match_result_void", allow_void=True) is not None
	# reachable join: NOT exempt — a value context with a Void result is a defect.
	with pytest.raises(AssertionError):
		h2m._emit_cfg_result_extract("__match_result_void", allow_void=False)


def test_production_ternary_routes_through_cfg_result_type(tmp_path):
	"""The PRODUCTION ternary lowering must type its hidden result local via the
	`_cfg_result_type` AUTHORITY (recorded/common-type), not by picking the first
	inferable branch. Spy that lowering a real ternary program calls
	`_cfg_result_type` with the HTernary node."""
	from pathlib import Path
	from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
	from lang.driftc.module_lowered import flatten_modules
	from lang.driftc import driftc as D
	import lang.driftc.stage2.hir_to_mir as HM

	src = Path(tmp_path) / "main.drift"
	src.write_text(
		"module m;\n"
		"fn mkStr() nothrow -> String { return \"aa\" + \"\"; }\n"
		"fn take(x: &String) nothrow -> Int { return x.byte_length(); }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval s = true ? mkStr() : mkStr();\n"
		"\treturn take(s) - 2;\n"
		"}\n"
	)
	modules, tt, exc, mexp, mdeps, pdiags = parse_drift_workspace_to_hir(
		[src], module_paths=[Path(tmp_path)], stdlib_root=stdlib_root(), test_build_only=True)
	assert not pdiags, [d.message for d in pdiags]
	fh, sig, _ = flatten_modules(modules)

	seen_ternary = []
	orig = HM.HIRToMIR._cfg_result_type

	def _spy(self, expr):
		if isinstance(expr, H.HTernary):
			seen_ternary.append(expr)
		return orig(self, expr)

	HM.HIRToMIR._cfg_result_type = _spy
	try:
		D.compile_to_llvm_ir_for_tests(
			func_hirs=fh, signatures=sig, exc_env=exc, type_table=tt,
			module_exports=mexp, module_deps=mdeps, enforce_entrypoint=True, entry="m::main")
	finally:
		HM.HIRToMIR._cfg_result_type = orig
	assert seen_ternary, "production ternary lowering must call _cfg_result_type(HTernary)"


def test_emit_cfg_result_extract_recover_mode_missing_type_does_not_raise():
	"""RECOVER mode: partial / Unknown result-local types are EXPECTED (invalid
	user source is already diagnosed). `_emit_cfg_result_extract` must NOT add a
	second internal-contract diagnostic — it lowers best-effort without raising."""
	tt = TypeTable()
	h2m = _h2m(tt)
	h2m._typed_mode = "recover"
	# Missing type: no raise, returns a dest.
	dest_missing = h2m._emit_cfg_result_extract("__match_result_missing", allow_void=False)
	assert dest_missing is not None
	# Unknown type: likewise no raise.
	unknown_ty = h2m._unknown_type
	h2m._local_types["__match_result_unknown"] = unknown_ty
	dest_unknown = h2m._emit_cfg_result_extract("__match_result_unknown", allow_void=False)
	assert dest_unknown is not None
