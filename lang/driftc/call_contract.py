# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Mapping, Any

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.span import Span
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget, CallTargetKind


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


def _sig_matches_callsig(sig: Any, call_sig: CallSig) -> bool:
	params = tuple(getattr(sig, "param_type_ids", []) or [])
	ret = getattr(sig, "return_type_id", None)
	if ret is None:
		return False
	return params == tuple(call_sig.param_types) and ret == call_sig.user_ret_type


def repair_named_hcall_callinfo(
	expr: H.HExpr,
	info: CallInfo,
	signatures_by_id: Mapping[FunctionId, Any],
	*,
	verify_target_sig_match: bool,
	allow_arity_fallback: bool,
	preserve_instantiated_target: bool,
	rewrite_sig_on_param_count_mismatch: bool,
) -> CallInfo:
	if not (isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar)):
		return info
	if info.target.kind is not CallTargetKind.DIRECT or info.target.symbol is None:
		return info
	call_name = expr.fn.name
	call_module = getattr(expr.fn, "module_id", None)
	target = info.target.symbol
	target_sig = signatures_by_id.get(target)
	if preserve_instantiated_target and (call_module is None or target.module == call_module) and target.name.startswith(f"{call_name}__inst__"):
		if target_sig is None or _sig_matches_callsig(target_sig, info.sig):
			return info
	target_name_matches = target.name == call_name and (call_module is None or target.module == call_module)
	if target_name_matches:
		if not verify_target_sig_match:
			return info
		if target_sig is not None and _sig_matches_callsig(target_sig, info.sig):
			return info
	candidates: list[tuple[FunctionId, Any]] = []
	for fn_id, sig in signatures_by_id.items():
		if fn_id.name != call_name:
			continue
		if isinstance(call_module, str) and fn_id.module != call_module:
			continue
		if getattr(sig, "param_type_ids", None) is None or getattr(sig, "return_type_id", None) is None:
			continue
		candidates.append((fn_id, sig))
	if not candidates:
		return info
	exact = [(fn_id, sig) for fn_id, sig in candidates if _sig_matches_callsig(sig, info.sig)]
	if len(exact) == 1:
		fn_id, sig = exact[0]
	elif allow_arity_fallback:
		arity = [(fn_id, sig) for fn_id, sig in candidates if len(getattr(sig, "param_type_ids", []) or []) == len(expr.args)]
		if len(arity) != 1:
			return info
		fn_id, sig = arity[0]
	else:
		if len(candidates) != 1:
			return info
		fn_id, sig = candidates[0]
	repaired_sig = info.sig
	use_template_sig = not bool(getattr(sig, "type_params", None))
	if use_template_sig or (rewrite_sig_on_param_count_mismatch and len(info.sig.param_types) != len(expr.args)):
		repaired_sig = CallSig(
			param_types=tuple(getattr(sig, "param_type_ids", []) or []),
			user_ret_type=getattr(sig, "return_type_id"),
			can_throw=bool(getattr(sig, "declared_can_throw", False)),
			includes_callee=bool(getattr(info.sig, "includes_callee", False)),
		)
	return CallInfo(target=CallTarget.direct(fn_id), sig=repaired_sig)


__all__ = [
	"CallContractIssue",
	"call_kind_label",
	"call_arg_exprs_for_param_layout",
	"call_expected_param_count",
	"explicit_arg_param_types",
	"call_contract_issues",
	"repair_named_hcall_callinfo",
]
