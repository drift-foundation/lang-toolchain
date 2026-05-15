# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.call_contract import (
	INTRINSIC_ARITY_TABLE,
	intrinsic_call_issues,
)
from lang.driftc.core.span import Span
from lang.driftc.stage1.call_info import IntrinsicKind


def _make_call(*, args: list, span: Span | None = None, kwargs: list | None = None) -> H.HCall:
	_span = span or Span()
	call = H.HCall(fn=H.HVar(name="f", loc=_span), args=args, callsite_id=1, loc=_span)
	if kwargs is not None:
		call.kwargs = kwargs
	return call


def test_intrinsic_call_issues_arity_mismatch() -> None:
	"""swap with 1 arg → E_INTRINSIC_ARITY_SWAP"""
	place = H.HPlaceExpr(base=H.HVar(name="x"), loc=Span())
	call = _make_call(args=[H.HBorrow(subject=place, is_mut=True)])
	issues = intrinsic_call_issues(IntrinsicKind.SWAP, call, kwargs=[])
	assert len(issues) >= 1
	arity = [i for i in issues if i.code == "E_INTRINSIC_ARITY_SWAP"]
	assert arity
	assert "expects 2 positional arguments" in arity[0].message


def test_intrinsic_call_issues_kwargs_rejected() -> None:
	"""Any intrinsic with kwargs → issue."""
	place = H.HPlaceExpr(base=H.HVar(name="a"), loc=Span())
	call = _make_call(args=[
		H.HBorrow(subject=place, is_mut=True),
		H.HBorrow(subject=H.HPlaceExpr(base=H.HVar(name="b"), loc=Span()), is_mut=True),
	])
	kw = H.HKwArg(name="extra", value=H.HLiteralInt(value=0))
	issues = intrinsic_call_issues(IntrinsicKind.SWAP, call, kwargs=[kw])
	assert issues
	assert issues[0].code == "E_INTRINSIC_ARITY_SWAP"


def test_intrinsic_call_issues_correct_arity_passes() -> None:
	"""swap with 2 &mut args → empty list."""
	place_a = H.HPlaceExpr(base=H.HVar(name="a"), loc=Span())
	place_b = H.HPlaceExpr(base=H.HVar(name="b"), loc=Span())
	call = _make_call(args=[
		H.HBorrow(subject=place_a, is_mut=True),
		H.HBorrow(subject=place_b, is_mut=True),
	])
	issues = intrinsic_call_issues(IntrinsicKind.SWAP, call, kwargs=[])
	assert issues == []


def test_intrinsic_call_issues_unknown_kind() -> None:
	"""Missing entry → E_INTRINSIC_CALLINFO_UNKNOWN_KIND (simulated with a fake kind)."""
	# All real kinds should be in the table; test the fallback path by
	# monkey-patching a missing lookup.  We verify the table covers all kinds
	# in a separate test, so here we just exercise the code path directly.
	from unittest.mock import patch
	call = _make_call(args=[])
	with patch.dict("lang.driftc.call_contract.INTRINSIC_ARITY_TABLE", clear=True):
		issues = intrinsic_call_issues(IntrinsicKind.SWAP, call, kwargs=[])
	assert issues
	assert issues[0].code == "E_INTRINSIC_CALLINFO_UNKNOWN_KIND"
	assert "unknown intrinsic" in issues[0].message


def test_intrinsic_call_issues_swap_mut_borrow_required() -> None:
	"""swap with 2 non-&mut args → E_INTRINSIC_SWAP_MUT_BORROW_REQUIRED."""
	call = _make_call(args=[H.HLiteralInt(value=1), H.HLiteralInt(value=2)])
	issues = intrinsic_call_issues(IntrinsicKind.SWAP, call, kwargs=[])
	mut_issues = [i for i in issues if i.code == "E_INTRINSIC_SWAP_MUT_BORROW_REQUIRED"]
	assert mut_issues
	assert "requires &mut place operands" in mut_issues[0].message


def test_intrinsic_call_issues_replace_mut_borrow_no_longer_in_contract() -> None:
	"""The `E_INTRINSIC_REPLACE_MUT_BORROW_REQUIRED` contract issue was
	removed in 0.31.81 (mariadb-team report: `mem.replace` rejected
	named `&mut T` values whose resolved type was correct).  That
	syntactic shape-check duplicated work the call resolver's type
	check (`replace expects &mut T as the first argument`) already
	does correctly, AND it produced false-positives on named refs.

	The correctness criterion for replace's first argument — must be
	`&mut T` — is now enforced solely at the call resolver
	(`lang/driftc/checker/call_resolver.py`, the `mut_inner is None`
	branch around line 4668), which inspects the *resolved type*
	rather than the *expression form*.

	This test pins the removal: the syntactic contract issue must NOT
	fire even when the first argument is a literal int (which clearly
	isn't `&mut T`).  Rejection of the literal-int case is the call
	resolver's job, exercised separately by
	`lang/tests/codegen/e2e/mem_replace_rejects_shared_ref/`.
	"""
	call = _make_call(args=[H.HLiteralInt(value=1), H.HLiteralInt(value=2)])
	issues = intrinsic_call_issues(IntrinsicKind.REPLACE, call, kwargs=[])
	mut_issues = [i for i in issues if i.code == "E_INTRINSIC_REPLACE_MUT_BORROW_REQUIRED"]
	assert not mut_issues, (
		"E_INTRINSIC_REPLACE_MUT_BORROW_REQUIRED was removed in 0.31.81; "
		"if it has returned, the named-`&mut T` mem.replace fix is at "
		f"risk of regression.  Issues: {[i.code for i in issues]}"
	)


def test_intrinsic_arity_table_covers_all_kinds() -> None:
	"""Every IntrinsicKind member must have an entry in the table."""
	for kind in IntrinsicKind:
		assert kind in INTRINSIC_ARITY_TABLE, f"missing table entry for {kind.name}"


def test_intrinsic_call_issues_span_propagation() -> None:
	"""Issue carries the call's span."""
	span = Span(file="test.drift", line=42, column=7)
	call = _make_call(args=[], span=span)
	issues = intrinsic_call_issues(IntrinsicKind.SWAP, call, kwargs=[])
	assert issues
	assert issues[0].span.line == 42
	assert issues[0].span.column == 7
