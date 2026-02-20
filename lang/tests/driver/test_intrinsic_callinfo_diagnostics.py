from __future__ import annotations

from types import SimpleNamespace

from lang.driftc import stage1 as H
from lang.driftc.call_contract import call_contract_issues
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.span import Span
from lang.driftc.driftc import _validate_intrinsic_callinfo
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget, IntrinsicKind


def _swap_call_with_bad_arity(span: Span) -> H.HCall:
	place = H.HPlaceExpr(base=H.HVar(name="x", loc=span), loc=span)
	return H.HCall(
		fn=H.HVar(name="swap", loc=span),
		args=[H.HBorrow(subject=place, is_mut=True)],
		callsite_id=1,
		loc=span,
	)


def test_intrinsic_callinfo_arity_reports_structured_diag_with_span() -> None:
	span = Span(file="repro.drift", line=7, column=9)
	call = _swap_call_with_bad_arity(span)
	typed_fn = SimpleNamespace(
		fn_id=FunctionId(module="m", name="main", ordinal=0),
		body=H.HBlock(statements=[H.HExprStmt(expr=call, loc=span)]),
		call_info_by_callsite_id={
			1: CallInfo(
				target=CallTarget.intrinsic(IntrinsicKind.SWAP),
				sig=CallSig(param_types=(1, 1), user_ret_type=1, can_throw=False),
			)
		},
	)
	diags = _validate_intrinsic_callinfo(typed_fn)
	assert diags
	arity = [d for d in diags if d.code == "E_INTRINSIC_ARITY_SWAP"]
	assert arity, diags
	d = arity[0]
	assert d.severity == "error"
	assert d.phase == "typecheck"
	assert d.span.line == 7
	assert d.span.column == 9


def test_call_contract_issue_carries_source_span() -> None:
	span = Span(file="contract.drift", line=11, column=4)
	call = H.HCall(
		fn=H.HVar(name="f", loc=span),
		args=[],
		callsite_id=22,
		loc=span,
	)
	info = CallInfo(
		target=CallTarget.direct(FunctionId(module="m", name="f", ordinal=0)),
		sig=CallSig(param_types=(1,), user_ret_type=1, can_throw=False),
	)
	issues = call_contract_issues(call, info)
	assert issues
	assert issues[0].span.line == 11
	assert issues[0].span.column == 4
