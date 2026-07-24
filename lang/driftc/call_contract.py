# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Mapping, Any

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.span import Span
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget, CallTargetKind, IntrinsicKind


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
			declared_terminal_throws=bool(getattr(sig, "declared_terminal_throws", False)),
		)
	return CallInfo(target=CallTarget.direct(fn_id), sig=repaired_sig)


# ---------------------------------------------------------------------------
# Intrinsic call-shape validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntrinsicSpec:
	expected_args: int
	code: str
	label: str
	kwargs_allowed: bool = False


INTRINSIC_ARITY_TABLE: dict[IntrinsicKind, IntrinsicSpec] = {
	IntrinsicKind.SWAP: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_SWAP", label="swap"),
	IntrinsicKind.REPLACE: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_REPLACE", label="replace"),
	IntrinsicKind.WRAPPING_ADD_U64: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_WRAPPING_U64", label="wrapping_add_u64"),
	IntrinsicKind.WRAPPING_MUL_U64: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_WRAPPING_U64", label="wrapping_mul_u64"),
	IntrinsicKind.BYTE_LENGTH: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_BYTE_LENGTH", label="byte_length"),
	IntrinsicKind.STRING_BYTE_AT: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_STRING_BYTE_AT", label="string_byte_at"),
	IntrinsicKind.STRING_BYTES_BASE: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_STRING_BYTES_BASE", label="string_bytes_base"),
	IntrinsicKind.STRING_EQ: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_STRING_OP", label="string_eq"),
	IntrinsicKind.STRING_CONCAT: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_STRING_OP", label="string_concat"),
	IntrinsicKind.CALLBACK0: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback0"),
	IntrinsicKind.CALLBACK1: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback1"),
	IntrinsicKind.CALLBACK2: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback2"),
	IntrinsicKind.CALLBACK3: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback3"),
	IntrinsicKind.CALLBACK4: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback4"),
	IntrinsicKind.CALLBACK5: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback5"),
	IntrinsicKind.CALLBACK6: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback6"),
	IntrinsicKind.CALLBACK_THROW0: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback_throw0"),
	IntrinsicKind.CALLBACK_THROW1: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback_throw1"),
	IntrinsicKind.CALLBACK_THROW2: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback_throw2"),
	IntrinsicKind.CALLBACK_THROW3: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback_throw3"),
	IntrinsicKind.CALLBACK_THROW4: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback_throw4"),
	IntrinsicKind.CALLBACK_THROW5: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback_throw5"),
	IntrinsicKind.CALLBACK_THROW6: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_CALLBACK", label="callback_throw6"),
	IntrinsicKind.TYPE_ID: IntrinsicSpec(expected_args=0, code="E_INTRINSIC_ARITY_TYPE_ID", label="type_id"),
	IntrinsicKind.DROP_VALUE: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_DROP_VALUE", label="drop_value"),
	IntrinsicKind.RAW_ALLOC: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_ALLOC_UNINIT", label="alloc_uninit"),
	IntrinsicKind.RAW_DEALLOC: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_DEALLOC", label="dealloc"),
	IntrinsicKind.RAWBUFFER_PTR: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_RAWBUFFER_VIEW", label="rawbuffer_ptr"),
	IntrinsicKind.RAWBUFFER_CAP: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_RAWBUFFER_VIEW", label="rawbuffer_cap"),
	IntrinsicKind.RAWBUFFER_FROM_PARTS: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_RAWBUFFER_FROM_PARTS", label="rawbuffer_from_parts"),
	IntrinsicKind.RAWBUFFER_EMPTY: IntrinsicSpec(expected_args=0, code="E_INTRINSIC_ARITY_RAWBUFFER_EMPTY", label="rawbuffer_empty"),
	IntrinsicKind.RAW_PTR_AT_REF: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_PTR_AT", label="ptr_at"),
	IntrinsicKind.RAW_PTR_AT_MUT: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_PTR_AT", label="ptr_at"),
	IntrinsicKind.RAW_WRITE: IntrinsicSpec(expected_args=3, code="E_INTRINSIC_ARITY_RAW_WRITE", label="write"),
	IntrinsicKind.RAW_READ: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_RAW_READ", label="read"),
	IntrinsicKind.PTR_FROM_REF: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_PTR_FROM_REF", label="ptr_from_ref"),
	IntrinsicKind.PTR_FROM_REF_MUT: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_PTR_FROM_REF", label="ptr_from_ref"),
	IntrinsicKind.PTR_OFFSET: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_PTR_OFFSET", label="ptr_offset"),
	IntrinsicKind.PTR_READ: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_PTR_READ", label="ptr_read"),
	IntrinsicKind.PTR_WRITE: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_PTR_WRITE", label="ptr_write"),
	IntrinsicKind.PTR_IS_NULL: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_PTR_IS_NULL", label="ptr_is_null"),
	IntrinsicKind.PTR_AS_MUT_REF: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_PTR_AS_MUT_REF", label="ptr_as_mut_ref"),
	IntrinsicKind.MAYBE_UNINIT: IntrinsicSpec(expected_args=0, code="E_INTRINSIC_ARITY_MAYBE_UNINIT", label="maybe_uninit"),
	IntrinsicKind.MAYBE_WRITE: IntrinsicSpec(expected_args=2, code="E_INTRINSIC_ARITY_MAYBE_WRITE", label="maybe_write"),
	IntrinsicKind.MAYBE_ASSUME_INIT_REF: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_MAYBE_ASSUME_INIT", label="maybe_assume_init_ref"),
	IntrinsicKind.MAYBE_ASSUME_INIT_MUT: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_MAYBE_ASSUME_INIT", label="maybe_assume_init_mut"),
	IntrinsicKind.MAYBE_ASSUME_INIT_READ: IntrinsicSpec(expected_args=1, code="E_INTRINSIC_ARITY_MAYBE_ASSUME_INIT", label="maybe_assume_init_read"),
	# Arc runtime boundary — all four methods are called with no
	# explicit args (receiver is implicit self).  See the Arc runtime
	# boundary comment in stage1/call_info.py.
	IntrinsicKind.ARC_CLONE: IntrinsicSpec(expected_args=0, code="E_INTRINSIC_ARITY_ARC", label="Arc.clone"),
	IntrinsicKind.ARC_GET: IntrinsicSpec(expected_args=0, code="E_INTRINSIC_ARITY_ARC", label="Arc.get"),
	IntrinsicKind.ARC_DESTROY: IntrinsicSpec(expected_args=0, code="E_INTRINSIC_ARITY_ARC", label="Arc.destroy"),
	IntrinsicKind.ARC_AS_INTERFACE: IntrinsicSpec(expected_args=0, code="E_INTRINSIC_ARITY_ARC", label="Arc.as_interface"),
}


def intrinsic_call_issues(kind: IntrinsicKind, call: object, *, kwargs: object) -> list[CallContractIssue]:
	issues: list[CallContractIssue] = []
	span = getattr(call, "loc", Span())
	spec = INTRINSIC_ARITY_TABLE.get(kind)
	if spec is None:
		issues.append(CallContractIssue(
			code="E_INTRINSIC_CALLINFO_UNKNOWN_KIND",
			message=f"unknown intrinsic '{kind.value}' in CallInfo validation",
			span=span,
		))
		return issues
	args = list(getattr(call, "args", []) or [])
	if kwargs and not spec.kwargs_allowed:
		n = spec.expected_args
		plural = "s" if n != 1 else ""
		issues.append(CallContractIssue(
			code=spec.code,
			message=f"{spec.label}(...) expects {n} positional argument{plural}",
			span=span,
		))
	elif len(args) != spec.expected_args:
		n = spec.expected_args
		plural = "s" if n != 1 else ""
		issues.append(CallContractIssue(
			code=spec.code,
			message=f"{spec.label}(...) expects {n} positional argument{plural}",
			span=span,
		))
	# SWAP: requires &mut place operands
	if kind is IntrinsicKind.SWAP:
		HBorrow = getattr(H, "HBorrow", None)
		if HBorrow is not None and not all(isinstance(a, HBorrow) and a.is_mut for a in args):
			issues.append(CallContractIssue(
				code="E_INTRINSIC_SWAP_MUT_BORROW_REQUIRED",
				message="swap(...) requires &mut place operands",
				span=span,
			))
	# REPLACE: the &mut T type check on the first argument is enforced
	# by the call resolver (`replace expects &mut T as the first
	# argument` at call_resolver.py).  We previously also ran a
	# *syntactic* check here that required the arg to be a literal
	# `&mut <place>` HBorrow expression — that was too strict: it
	# rejected named locals / parameters / method-call returns whose
	# resolved TYPE was already &mut T (bookkeeper customer report
	# 2026-05-14).  Removed — the type-level check in call_resolver
	# is correct and sufficient.  The hir_to_mir lowering filters
	# already drop MUT_BORROW_REQUIRED issues, so this branch was
	# only ever a hard error via the driftc.py top-level loop.
	return issues


# ---------------------------------------------------------------------------
# Constructor call-shape validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CtorFieldSpec:
	field_names: tuple[str, ...]


def ctor_call_issues(
	pos_count: int,
	named_fields: tuple[str, ...],
	field_spec: CtorFieldSpec,
	*,
	ctor_label: str,
	span: Span | None = None,
) -> list[CallContractIssue]:
	issues: list[CallContractIssue] = []
	_span = span if span is not None else Span()
	n_fields = len(field_spec.field_names)
	if pos_count > 0 and named_fields:
		issues.append(CallContractIssue(
			code="E_CTOR_MIXED_ARGS",
			message=f"{ctor_label} constructor does not allow mixing positional and named arguments (checker bug)",
			span=_span,
		))
		return issues
	if named_fields:
		valid_set = set(field_spec.field_names)
		seen: set[str] = set()
		for name in named_fields:
			if name not in valid_set:
				issues.append(CallContractIssue(
					code="E_CTOR_UNKNOWN_FIELD",
					message=f"unknown {ctor_label} ctor field '{name}' (checker bug)",
					notes=(f"field={name}",),
					span=_span,
				))
			elif name in seen:
				issues.append(CallContractIssue(
					code="E_CTOR_DUPLICATE_FIELD",
					message=f"duplicate {ctor_label} ctor field '{name}' (checker bug)",
					notes=(f"field={name}",),
					span=_span,
				))
			else:
				seen.add(name)
		provided = seen & valid_set
		missing = [f for f in field_spec.field_names if f not in provided]
		if missing:
			issues.append(CallContractIssue(
				code="E_CTOR_MISSING_FIELDS",
				message=f"missing {ctor_label} ctor field(s): {', '.join(missing)} (checker bug)",
				notes=tuple(f"field={f}" for f in missing),
				span=_span,
			))
	else:
		if pos_count != n_fields:
			issues.append(CallContractIssue(
				code="E_CTOR_ARITY_MISMATCH",
				message=f"{ctor_label} constructor arity mismatch: expected {n_fields}, got {pos_count} (checker bug)",
				span=_span,
			))
	return issues


# ---------------------------------------------------------------------------
# Array method arity validation
# ---------------------------------------------------------------------------

ARRAY_METHOD_ARITY_TABLE: dict[str, int] = {
	"get": 1,
	"ref_at": 1,
	"pop": 0,
	"push": 1,
	"insert": 2,
	"remove": 1,
	"swap_remove": 1,
	"swap": 2,
	"set": 2,
	"clear": 0,
	"reserve": 1,
	"shrink_to_fit": 0,
	"extend": 1,
	"truncate": 1,
	"remove_range": 2,
}


def array_method_arity_issues(method_name: str, arg_count: int, *, span: Span | None = None) -> list[CallContractIssue]:
	expected = ARRAY_METHOD_ARITY_TABLE.get(method_name)
	if expected is None:
		return []
	if arg_count != expected:
		_span = span if span is not None else Span()
		return [CallContractIssue(
			code="E_ARRAY_METHOD_ARITY",
			message=f"Array.{method_name} arity mismatch reached MIR lowering (checker bug)",
			span=_span,
		)]
	return []


# ---------------------------------------------------------------------------
# Generic kwargs rejection
# ---------------------------------------------------------------------------

def call_kwargs_issues(call_kind: str, kwargs: object, *, span: Span | None = None) -> list[CallContractIssue]:
	if not kwargs:
		return []
	_span = span if span is not None else Span()
	return [CallContractIssue(
		code="E_CALL_KWARGS_REJECTED",
		message=f"keyword arguments are not supported for {call_kind} in MIR lowering (checker bug)",
		span=_span,
	)]


__all__ = [
	"CallContractIssue",
	"call_kind_label",
	"call_arg_exprs_for_param_layout",
	"call_expected_param_count",
	"explicit_arg_param_types",
	"call_contract_issues",
	"repair_named_hcall_callinfo",
	"IntrinsicSpec",
	"INTRINSIC_ARITY_TABLE",
	"intrinsic_call_issues",
	"CtorFieldSpec",
	"ctor_call_issues",
	"ARRAY_METHOD_ARITY_TABLE",
	"array_method_arity_issues",
	"call_kwargs_issues",
]
