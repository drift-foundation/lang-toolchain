# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from lang.driftc import stage1 as H
from lang.driftc.core.span import Span
from lang.driftc.stage1.call_info import CallInfo, CallTargetKind


@dataclass(frozen=True)
class CallContractIssue:
	code: str
	message: str
	notes: tuple[str, ...] = ()
	span: Span = field(default_factory=Span)


def call_kind_label(expr: H.HExpr) -> str:
	if isinstance(expr, H.HMethodCall):
		return "method call"
	if isinstance(expr, H.HInvoke):
		return "invoke"
	return "call"


def call_arg_exprs_for_param_layout(expr: H.HExpr, info: CallInfo) -> list[H.HExpr]:
	kwargs = list(getattr(expr, "kwargs", []) or [])
	if isinstance(expr, H.HMethodCall):
		if info.target.kind is CallTargetKind.INDIRECT:
			args = list(expr.args)
		else:
			args = [expr.receiver] + list(expr.args)
	elif isinstance(expr, H.HInvoke):
		if info.sig.includes_callee:
			args = [expr.callee] + list(expr.args)
		else:
			args = list(expr.args)
	elif isinstance(expr, H.HCall):
		args = list(expr.args)
	else:
		args = []
	if kwargs:
		args.extend(kw.value for kw in kwargs)
	return args


def call_expected_param_count(expr: H.HExpr, info: CallInfo) -> int:
	return len(call_arg_exprs_for_param_layout(expr, info))


def explicit_arg_param_types(expr: H.HExpr, info: CallInfo) -> list[int]:
	param_types = list(info.sig.param_types)
	if isinstance(expr, H.HMethodCall):
		if info.target.kind is CallTargetKind.INDIRECT:
			return param_types[1:] if info.sig.includes_callee and param_types else param_types
		return param_types[1:] if param_types else []
	if isinstance(expr, H.HInvoke):
		return param_types[1:] if info.sig.includes_callee and param_types else param_types
	return param_types


def call_contract_issues(expr: H.HExpr, info: CallInfo) -> list[CallContractIssue]:
	issues: list[CallContractIssue] = []
	if isinstance(expr, H.HInvoke) and info.target.kind is not CallTargetKind.INDIRECT:
		issues.append(
			CallContractIssue(
				code="E_CALLINFO_INVOKE_TARGET_KIND",
				message="invoke CallInfo target must be INDIRECT (checker bug)",
				notes=(
					f"target_kind={info.target.kind.name}",
					f"callsite_id={getattr(expr, 'callsite_id', None)}",
				),
				span=getattr(expr, "loc", Span()),
			)
		)
	if isinstance(expr, H.HMethodCall) and info.target.kind is CallTargetKind.CONSTRUCTOR:
		issues.append(
			CallContractIssue(
				code="E_CALLINFO_METHOD_CONSTRUCTOR_TARGET",
				message="method call CallInfo target must not be CONSTRUCTOR (checker bug)",
				notes=(f"callsite_id={getattr(expr, 'callsite_id', None)}",),
				span=getattr(expr, "loc", Span()),
			)
		)
	if isinstance(expr, H.HInvoke) and info.sig.includes_callee:
		issues.append(
			CallContractIssue(
				code="E_CALLINFO_INVOKE_INCLUDES_CALLEE",
				message="invoke CallInfo must not set includes_callee (checker bug)",
				notes=(f"callsite_id={getattr(expr, 'callsite_id', None)}",),
				span=getattr(expr, "loc", Span()),
			)
		)
	if isinstance(expr, (H.HCall, H.HMethodCall)) and info.sig.includes_callee:
		call_kind = "method call" if isinstance(expr, H.HMethodCall) else "call"
		issues.append(
			CallContractIssue(
				code="E_CALLINFO_INCLUDES_CALLEE_INVALID",
				message=f"CallInfo includes_callee set on {call_kind} (checker bug)",
				notes=(f"callsite_id={getattr(expr, 'callsite_id', None)}",),
				span=getattr(expr, "loc", Span()),
			)
		)
	expected = call_expected_param_count(expr, info)
	actual = len(info.sig.param_types)
	if actual != expected:
		call_kind = call_kind_label(expr)
		target_note = f"target_kind={info.target.kind.name}"
		if info.target.kind is CallTargetKind.DIRECT and info.target.symbol is not None:
			target_note = f"target_kind={info.target.kind.name} target_symbol={info.target.symbol.module}::{info.target.symbol.name}"
		issues.append(
			CallContractIssue(
				code="E_CALLINFO_PARAM_LAYOUT",
				message=f"CallInfo param layout mismatch for {call_kind} (checker bug)",
				notes=(
					target_note,
					f"callsite_id={getattr(expr, 'callsite_id', None)} expected_params={expected} actual_params={actual}",
				),
				span=getattr(expr, "loc", Span()),
			)
		)
	return issues


__all__ = [
	"CallContractIssue",
	"call_kind_label",
	"call_arg_exprs_for_param_layout",
	"call_expected_param_count",
	"explicit_arg_param_types",
	"call_contract_issues",
]
