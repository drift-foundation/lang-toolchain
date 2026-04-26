from __future__ import annotations

import os
import sys

from dataclasses import dataclass, replace
from typing import Callable, Optional

from lang.driftc.infer import InferContext, InferError, InferErrorKind, InferResult
from lang.driftc import debug as drift_debug
from lang.driftc.checker import FnSignature
from lang.driftc.core.types_core import (
	FunctionId,
	GenericTypeExpr,
	TypeId,
	TypeKind,
	TypeParamId,
)
from lang.driftc.core.function_id import method_wrapper_id
from lang.driftc.core.type_subst import Subst, apply_subst
from lang.driftc.checker import TypeParam
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget, CallTargetKind, IntrinsicKind
from lang.driftc.method_registry import CallableDecl, CallableSignature, CallableTemplateSignature, CallableKind, Visibility, SelfMode
from lang.driftc.checker.unsafe_gate import check_unsafe_call
from lang.driftc.traits.linked_world import BOOL_TRUE
from lang.driftc.traits.solver import Env as TraitEnv, Obligation, ObligationOrigin, ObligationOriginKind, ProofFailure, ProofFailureReason, ProofStatus, prove_expr, prove_obligation
from lang.driftc.traits.world import TraitKey
from lang.driftc.trait_index import TraitImplCandidate
from lang.driftc.traits.world import normalize_type_key, trait_key_from_expr, type_key_from_typeid
from lang.driftc.method_resolver import MethodResolution, ResolutionError
from lang.driftc.parser import ast as parser_ast
from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.stage1.place_expr import place_expr_from_lvalue_expr
from lang.driftc.core.span import Span
from lang.driftc.call_contract import CtorFieldSpec, ctor_call_issues, call_kwargs_issues, ARRAY_METHOD_ARITY_TABLE
from lang.driftc.core.type_resolve_common import resolve_opaque_type

FIXED_WIDTH_TYPE_NAMES = {
	"Int8",
	"Int16",
	# Int32 deliberately excluded: available in user code for C FFI interop.
	"Int64",
	"Uint8",
	"Uint16",
	# Uint32 deliberately excluded: available in user code for C FFI interop.
	# Uint64/u64 deliberately excluded: available in user code for portable
	# 64-bit unsigned arithmetic (crypto, hashing, bit manipulation).
	"F32",
	"F64",
	"Float32",
	"Float64",
}


def _best_effort_span(*items: object | None) -> Span:
	for item in items:
		if item is None:
			continue
		if isinstance(item, Span):
			if item.line is not None and item.column is not None:
				return item
			continue
		loc = getattr(item, "loc", None)
		if isinstance(loc, Span) and loc.line is not None and loc.column is not None:
			return loc
	return Span()


def _implicit_callback_wrap(
	ctx: object,
	*,
	arg: object,
	callback_arity: int,
	is_throw: bool,
	expected_type_hint: TypeId | None = None,
) -> object:
	# Sole constructor for implicit `core.callback{N}` / `core.callback_throw{N}`
	# wrap nodes. The borrow checker recognises wraps by module_id="std.core"
	# + name in {callback0..callback_throw2}, not by _is_implicit_wrap; routing
	# every implicit-wrap site through this helper preserves that invariant.
	# Construct-only: caller types the result with type_expr and splices it
	# back into expr.args / arg_types.
	if callback_arity == 0:
		cb_name = "callback_throw0" if is_throw else "callback0"
	elif callback_arity == 1:
		cb_name = "callback_throw1" if is_throw else "callback1"
	else:
		cb_name = "callback_throw2" if is_throw else "callback2"
	cb_var = H.HVar(name=cb_name, module_id="std.core")
	cb_call = H.HCall(fn=cb_var, args=[arg], kwargs=[])
	cb_call._is_implicit_wrap = True
	alloc_callsite = getattr(ctx, "alloc_callsite_id", None)
	if alloc_callsite is not None:
		cb_call.callsite_id = alloc_callsite()
	alloc_node = getattr(ctx, "alloc_node_id", None)
	if alloc_node is not None:
		alloc_node(cb_call)
	if expected_type_hint is not None:
		cb_call.expected_type_hint = expected_type_hint
	return cb_call


def _sig_from_decl_template(ctx: object, decl: CallableDecl, current_module_name: str) -> FnSignature | None:
	tpl = getattr(decl, "template_signature", None)
	if tpl is None:
		return None
	fn_id_local = decl.fn_id
	if fn_id_local is None:
		fn_id_local = FunctionId(module=current_module_name, name=f"__callable_{decl.callable_id}", ordinal=0)
	type_params: list[TypeParam] = []
	type_param_map: dict[str, TypeParamId] = {}
	for idx, name in enumerate(getattr(decl, "template_type_params", ()) or ()):
		param_id = TypeParamId(owner=fn_id_local, index=idx)
		type_params.append(TypeParam(id=param_id, name=name))
		type_param_map[name] = param_id
	impl_type_params: list[TypeParam] = []
	impl_param_map: dict[str, TypeParamId] = {}
	impl_names = getattr(decl, "template_impl_type_params", ()) or ()
	if impl_names:
		impl_owner = FunctionId(module=fn_id_local.module, name=f"{fn_id_local.name}::impl", ordinal=0)
		for idx, name in enumerate(impl_names):
			param_id = TypeParamId(owner=impl_owner, index=idx)
			impl_type_params.append(TypeParam(id=param_id, name=name))
			impl_param_map[name] = param_id
	local_type_params: dict[str, TypeParamId] = {}
	local_type_params.update(impl_param_map)
	local_type_params.update(type_param_map)
	module_for_types = fn_id_local.module or current_module_name
	param_type_ids = [
		resolve_opaque_type(p, ctx.type_table, module_id=module_for_types, type_params=local_type_params)
		for p in tpl.param_types
	]
	return_type_id = resolve_opaque_type(
		tpl.result_type,
		ctx.type_table,
		module_id=module_for_types,
		type_params=local_type_params,
	)
	self_mode = None
	if decl.self_mode is SelfMode.SELF_BY_VALUE:
		self_mode = "value"
	elif decl.self_mode is SelfMode.SELF_BY_REF:
		self_mode = "ref"
	elif decl.self_mode is SelfMode.SELF_BY_REF_MUT:
		self_mode = "ref_mut"
	return FnSignature(
		name=decl.name,
		method_name=decl.name,
		type_params=type_params,
		param_type_ids=param_type_ids,
		return_type_id=return_type_id,
		param_types=list(tpl.param_types),
		return_type=tpl.result_type,
		is_method=decl.kind is not CallableKind.FREE_FUNCTION,
		self_mode=self_mode,
		impl_target_type_id=decl.impl_target_type_id,
		impl_type_params=impl_type_params,
		module=module_for_types,
	)


def _candidate_visible(cand: CallableDecl, *, visible_modules_set: set, current_module_id: int | None) -> bool:
	return (cand.module_id in visible_modules_set and cand.visibility.is_public) or (current_module_id is not None and cand.module_id == current_module_id)


# Prelude visibility for pub methods on builtin/prelude types.
#
# Builtin types (String, Int, Array, Optional, …) are always available without
# import.  Their core methods — defined in known stdlib modules — must also be
# visible without an explicit import.  The predicate gates on BOTH the target
# type (module_id in {None, "lang.core"}) AND the defining module (must be a
# known stdlib implementation module).  This prevents user-defined
# `implement String { … }` blocks from gaining unintended cross-module
# visibility.
#
# Narrow std.core exemption (K28): `Result` is a conceptually-prelude
# variant — values of `Result<T, E>` appear in every consumer that calls
# a fallible package function — but it lives under module_id "std.core"
# rather than the lang.core/builtin scope.  Without an exemption,
# calling inherent methods on a package-returned `Result` requires the
# consumer to `import std.core` redundantly.  We allow-list *only*
# `Result` by name; do **not** broaden this to "all of std.core" —
# that would globally expose every std.core type's methods (e.g.
# `Cell.get`, `DiagnosticEntry`, `DefaultHasher`) to every consumer
# regardless of import scope.
#
# Note: `Optional` is NOT in this set because the parser seeds it under
# `module_id = "lang.core"` (see `ensure_optional_base` in
# lang/driftc/parser/__init__.py), so it already passes the
# `td.module_id in _PRELUDE_TYPE_MODULES` check above.  Add it here only
# if a real `std.core.Optional` receiver path emerges.
#
# See issues/k28-result-method-visibility-package-boundary/description.md
# and the regressions in lang/tests/driver/test_external_consumer.py
# (test_ext_cross_package_or_throw, test_ext_std_core_non_prelude_still_hidden).
_PRELUDE_TYPE_MODULES: frozenset[str | None] = frozenset({None, "lang.core"})
_PRELUDE_METHOD_SOURCE_MODULES: frozenset[str] = frozenset({"std.core", "std.iter", "std.containers", "lang.core"})
_PRELUDE_STD_CORE_TYPE_NAMES: frozenset[str] = frozenset({"Result"})

def _is_prelude_type_method(cand: CallableDecl, type_table: object) -> bool:
	if cand.impl_target_type_id is None or not cand.visibility.is_public:
		return False
	fn_id = cand.fn_id
	if fn_id is None:
		return False
	cand_module = getattr(fn_id, "module", None)
	if cand_module not in _PRELUDE_METHOD_SOURCE_MODULES:
		return False
	try:
		td = type_table.get(cand.impl_target_type_id)
	except (KeyError, IndexError):
		return False
	if td.module_id in _PRELUDE_TYPE_MODULES:
		return True
	if td.module_id == "std.core" and getattr(td, "name", None) in _PRELUDE_STD_CORE_TYPE_NAMES:
		return True
	return False


@dataclass(frozen=True)
class VariantCtorResolveResult:
	inst_return: TypeId
	inst_params: list[TypeId]
	ctor_arg_field_indices: list[int]
	ctor_args: list[object]


@dataclass(frozen=True)
class StructCtorResolveResult:
	inst_return: TypeId
	inst_params: list[TypeId]
	ctor_arg_field_indices: list[int]
	ctor_args: list[object]


@dataclass(frozen=True)
class MethodCallResult:
	return_type: TypeId
	call_info: object | None = None
	resolution: object | None = None


@dataclass
class CallIntent:
	expected_return: TypeId | None
	arg_expected_types: list[TypeId] | None = None


def _expected_arg_types_for_call(param_types: list[TypeId], arg_count: int) -> list[TypeId]:
	if not param_types:
		return []
	start_idx = 0
	if len(param_types) == arg_count + 1:
		start_idx = 1
	return list(param_types[start_idx:])


@dataclass(frozen=True)
class MethodResolverContext:
	type_table: object
	diagnostics: list
	current_module_name: str
	current_module: int
	default_package: Optional[str]
	module_packages: dict
	type_param_map: Optional[dict]
	preseed_type_params: Optional[dict]
	type_param_names: dict
	current_fn_id: FunctionId | None
	int_ty: TypeId
	uint_ty: TypeId
	byte_ty: TypeId
	bool_ty: TypeId
	float_ty: TypeId
	string_ty: TypeId
	void_ty: TypeId
	error_ty: TypeId
	dv_ty: TypeId
	unknown_ty: TypeId
	signatures_by_id: dict
	callable_registry: object
	trait_index: object
	trait_impl_index: object
	impl_index: object
	visible_modules: tuple
	visible_trait_world: object
	global_trait_world: object
	trait_scope_by_module: dict
	require_env_local: object
	fn_require_assumed: set
	traits_in_scope: Callable[[], list[TraitKey]]
	trait_key_for_id: Callable[[int], TraitKey | None] | None
	tc_diag: Callable[..., object]
	type_expr: Callable[..., TypeId]
	optional_variant_type: Callable[[TypeId], TypeId]
	unwrap_ref_type: Callable[[TypeId], TypeId]
	struct_base_and_args: Callable[[TypeId], tuple[TypeId, list[TypeId]]]
	receiver_place: Callable[[object], object | None]
	receiver_can_mut_borrow: Callable[[object, object | None], bool]
	receiver_compat: Callable[[TypeId, TypeId, object], tuple[bool, bool]]
	receiver_preference: Callable[[object, bool, bool, bool], int | None]
	args_match_params: Callable[[list[TypeId], list[TypeId]], bool]
	coerce_args_for_params: Callable[[list[TypeId], list[TypeId]], list[TypeId]]
	infer_receiver_arg_type: Callable[[object, TypeId, bool, bool], TypeId]
	instantiate_sig_with_subst: Callable[..., InferResult]
	apply_autoborrow_args: Callable[..., tuple[list[TypeId], bool]]
	label_typeid: Callable[[TypeId], str]
	trait_label: Callable[[TraitKey], str]
	require_for_fn: Callable[[FunctionId | None], parser_ast.TraitExpr | None]
	extract_conjunctive_facts: Callable[[parser_ast.TraitExpr], list[parser_ast.TraitExpr]]
	subject_name: Callable[[object], str | None]
	normalize_type_key: Callable[[str], str]
	collect_trait_subjects: Callable[[parser_ast.TraitExpr, set], None]
	require_failure: Callable[..., object | None]
	format_failure_message: Callable[[object], str]
	failure_code: Callable[[object], str | None]
	pick_best_failure: Callable[[list], object | None]
	requirement_notes: Callable[[object], list[str]] | None
	param_scope_map: Callable[[FnSignature], dict]
	candidate_key_for_decl: Callable[[CallableDecl], object]
	visibility_note: Callable[[int], str]
	intrinsic_method_fn_id: Callable[[str], FunctionId]
	instantiate_sig: Callable[..., object]
	self_mode_from_sig: Callable[[FnSignature], object]
	match_impl_type_args: Callable[..., object]
	format_infer_failure: Callable[..., tuple[str, list[str]]]
	visibility_provenance: dict | None = None
	module_ids_by_name: dict | None = None
	record_instantiation: Callable[[int | None, FunctionId | None, tuple[TypeId, ...], tuple[TypeId, ...]], None] | None = None
	alloc_callsite_id: Callable[[], int] | None = None
	alloc_node_id: Callable[[object], None] | None = None


@dataclass(frozen=True)
class ResolverContext:
	type_table: object
	diagnostics: list
	current_module_name: str
	default_package: Optional[str]
	module_packages: dict
	type_param_map: Optional[dict]
	preseed_type_params: Optional[dict]
	signatures_by_id: dict
	int_ty: TypeId
	uint_ty: TypeId
	uint64_ty: TypeId
	byte_ty: TypeId
	bool_ty: TypeId
	float_ty: TypeId
	string_ty: TypeId
	void_ty: TypeId
	error_ty: TypeId
	dv_ty: TypeId
	unknown_ty: TypeId
	tc_diag: Callable[..., object]
	fixed_width_allowed: Callable[[str], bool]
	reject_zst_array: Callable[[TypeId, Span], bool]
	pretty_type_name: Callable[[TypeId, str], str]
	format_ctor_signature_list: Callable[..., list[str]]
	instantiate_sig: Callable[..., object]
	enforce_struct_requires: Callable[[TypeId, Span], None]
	ensure_field_visible: Callable[[TypeId, str, Span], bool]
	visible_modules_for_free_call: Callable[[str | None], tuple[int, ...]]
	struct_base_and_args: Callable[[TypeId], tuple[TypeId, list[TypeId]]]
	receiver_compat: Callable[[TypeId, TypeId, object], tuple[bool, bool]]
	args_match_params: Callable[[list[TypeId], list[TypeId]], bool]
	coerce_args_for_params: Callable[[list[TypeId], list[TypeId]], list[TypeId]]
	self_mode_from_sig: Callable[[FnSignature], object]
	match_impl_type_args: Callable[..., object]
	module_ids_by_name: dict
	visibility_provenance: dict
	infer: Callable[..., InferResult]
	format_infer_failure: Callable[..., tuple[str, list[str]]]
	lambda_can_throw: Callable[..., bool]
	record_iface_coercion: Callable[[object, TypeId], None] | None
	iface_assignable: Callable[[TypeId, TypeId], bool] | None
	allow_unsafe: bool
	unsafe_context: bool
	allow_unsafe_without_block: bool
	allow_rawbuffer: bool


@dataclass(frozen=True)
class CallResolverContext:
	type_table: object
	diagnostics: list
	current_module_name: str
	current_module: int
	default_package: Optional[str]
	module_packages: dict
	type_param_map: Optional[dict]
	preseed_type_params: Optional[dict]
	type_param_names: dict
	current_fn_id: FunctionId | None
	int_ty: TypeId
	uint_ty: TypeId
	uint64_ty: TypeId
	byte_ty: TypeId
	bool_ty: TypeId
	float_ty: TypeId
	string_ty: TypeId
	void_ty: TypeId
	error_ty: TypeId
	dv_ty: TypeId
	unknown_ty: TypeId
	signatures_by_id: dict
	callable_registry: object
	trait_index: object
	trait_impl_index: object
	impl_index: object
	visible_modules: tuple
	visible_trait_world: object
	global_trait_world: object
	trait_scope_by_module: dict
	require_env_local: object
	fn_require_assumed: set
	binding_mutable: dict[int, bool]
	binding_id_by_name: dict[str, int] | None
	traits_in_scope: Callable[[], list[TraitKey]]
	trait_key_for_id: Callable[[int], TraitKey | None] | None
	tc_diag: Callable[..., object]
	type_expr: Callable[..., TypeId]
	optional_variant_type: Callable[[TypeId], TypeId]
	unwrap_ref_type: Callable[[TypeId], TypeId]
	struct_base_and_args: Callable[[TypeId], tuple[TypeId, list[TypeId]]]
	receiver_place: Callable[[object], object | None]
	receiver_can_mut_borrow: Callable[[object, object | None], bool]
	receiver_compat: Callable[[TypeId, TypeId, object], tuple[bool, bool]]
	receiver_preference: Callable[[object, bool, bool, bool], int | None]
	args_match_params: Callable[[list[TypeId], list[TypeId]], bool]
	coerce_args_for_params: Callable[[list[TypeId], list[TypeId]], list[TypeId]]
	infer_receiver_arg_type: Callable[[object, TypeId, bool, bool], TypeId]
	instantiate_sig_with_subst: Callable[..., InferResult]
	apply_autoborrow_args: Callable[..., tuple[list[TypeId], bool]]
	label_typeid: Callable[[TypeId], str]
	trait_label: Callable[[TraitKey], str]
	require_for_fn: Callable[[FunctionId | None], parser_ast.TraitExpr | None]
	extract_conjunctive_facts: Callable[[parser_ast.TraitExpr], list[parser_ast.TraitExpr]]
	subject_name: Callable[[object], str | None]
	normalize_type_key: Callable[[str], str]
	collect_trait_subjects: Callable[[parser_ast.TraitExpr, set], None]
	require_failure: Callable[..., object | None]
	format_failure_message: Callable[[object], str]
	failure_code: Callable[[object], str | None]
	pick_best_failure: Callable[[list], object | None]
	requirement_notes: Callable[[object], list[str]] | None
	param_scope_map: Callable[[FnSignature], dict]
	candidate_key_for_decl: Callable[[CallableDecl], object]
	visibility_note: Callable[[int], str]
	intrinsic_method_fn_id: Callable[[str], FunctionId]
	instantiate_sig: Callable[..., object]
	self_mode_from_sig: Callable[[FnSignature], object]
	match_impl_type_args: Callable[..., object]
	fixed_width_allowed: Callable[[str], bool]
	reject_zst_array: Callable[[TypeId, Span], bool]
	pretty_type_name: Callable[[TypeId, str], str]
	format_ctor_signature_list: Callable[..., list[str]]
	enforce_struct_requires: Callable[[TypeId, Span], None]
	ensure_field_visible: Callable[[TypeId, str, Span], bool]
	visible_modules_for_free_call: Callable[[str | None], tuple[int, ...]]
	module_ids_by_name: dict
	visibility_provenance: dict
	infer: Callable[..., InferResult]
	format_infer_failure: Callable[..., tuple[str, list[str]]]
	lambda_can_throw: Callable[..., bool]
	record_iface_coercion: Callable[[object, TypeId], None] | None
	iface_assignable: Callable[[TypeId, TypeId], bool] | None
	allow_unsafe: bool
	unsafe_context: bool
	allow_unsafe_without_block: bool
	allow_rawbuffer: bool
	record_call_resolution: Callable[[object, object], None] | None
	record_instantiation: Callable[[int | None, FunctionId | None, tuple[TypeId, ...], tuple[TypeId, ...]], None] | None = None
	alloc_callsite_id: Callable[[], int] | None = None
	alloc_node_id: Callable[[object], None] | None = None


def _require_preseed_type_params(ctx: CallResolverContext) -> dict:
	if ctx.preseed_type_params is None:
		raise AssertionError("preseed_type_params missing in CallResolverContext (checker bug)")
	return ctx.preseed_type_params


def _make_resolver_ctx(ctx: CallResolverContext, **overrides) -> ResolverContext:
	preseed_type_params = _require_preseed_type_params(ctx)
	base = dict(type_table=ctx.type_table, diagnostics=ctx.diagnostics, current_module_name=ctx.current_module_name, default_package=ctx.default_package, module_packages=ctx.module_packages, type_param_map=ctx.type_param_map, preseed_type_params=preseed_type_params, signatures_by_id=ctx.signatures_by_id, int_ty=ctx.int_ty, uint_ty=ctx.uint_ty, uint64_ty=ctx.uint64_ty, byte_ty=ctx.byte_ty, bool_ty=ctx.bool_ty, float_ty=ctx.float_ty, string_ty=ctx.string_ty, void_ty=ctx.void_ty, error_ty=ctx.error_ty, dv_ty=ctx.dv_ty, unknown_ty=ctx.unknown_ty, tc_diag=ctx.tc_diag, fixed_width_allowed=ctx.fixed_width_allowed, reject_zst_array=ctx.reject_zst_array, pretty_type_name=ctx.pretty_type_name, format_ctor_signature_list=ctx.format_ctor_signature_list, instantiate_sig=ctx.instantiate_sig, enforce_struct_requires=ctx.enforce_struct_requires, ensure_field_visible=ctx.ensure_field_visible, visible_modules_for_free_call=ctx.visible_modules_for_free_call, struct_base_and_args=ctx.struct_base_and_args, receiver_compat=ctx.receiver_compat, args_match_params=ctx.args_match_params, coerce_args_for_params=ctx.coerce_args_for_params, self_mode_from_sig=ctx.self_mode_from_sig, match_impl_type_args=ctx.match_impl_type_args, module_ids_by_name=ctx.module_ids_by_name, visibility_provenance=ctx.visibility_provenance, infer=ctx.infer, format_infer_failure=ctx.format_infer_failure, lambda_can_throw=ctx.lambda_can_throw, record_iface_coercion=ctx.record_iface_coercion, iface_assignable=ctx.iface_assignable, allow_unsafe=ctx.allow_unsafe, unsafe_context=ctx.unsafe_context, allow_unsafe_without_block=ctx.allow_unsafe_without_block, allow_rawbuffer=ctx.allow_rawbuffer)
	base.update(overrides)
	return ResolverContext(**base)


def _make_method_ctx(ctx: CallResolverContext, *, diagnostics: list, traits_in_scope: Callable[[], list[TraitKey]], trait_key: TraitKey | None) -> MethodResolverContext:
	preseed_type_params = _require_preseed_type_params(ctx)
	return MethodResolverContext(type_table=ctx.type_table, diagnostics=diagnostics, current_module_name=ctx.current_module_name, current_module=ctx.current_module, default_package=ctx.default_package, module_packages=ctx.module_packages, type_param_map=ctx.type_param_map, preseed_type_params=preseed_type_params, type_param_names=ctx.type_param_names, current_fn_id=ctx.current_fn_id, int_ty=ctx.int_ty, uint_ty=ctx.uint_ty, byte_ty=ctx.byte_ty, bool_ty=ctx.bool_ty, float_ty=ctx.float_ty, string_ty=ctx.string_ty, void_ty=ctx.void_ty, error_ty=ctx.error_ty, dv_ty=ctx.dv_ty, unknown_ty=ctx.unknown_ty, signatures_by_id=ctx.signatures_by_id, callable_registry=ctx.callable_registry, trait_index=ctx.trait_index, trait_impl_index=ctx.trait_impl_index, impl_index=ctx.impl_index, visible_modules=ctx.visible_modules, visible_trait_world=ctx.visible_trait_world, global_trait_world=ctx.global_trait_world, trait_scope_by_module=ctx.trait_scope_by_module, require_env_local=ctx.require_env_local, fn_require_assumed=ctx.fn_require_assumed, traits_in_scope=traits_in_scope, trait_key_for_id=ctx.trait_key_for_id, tc_diag=ctx.tc_diag, type_expr=ctx.type_expr, optional_variant_type=ctx.optional_variant_type, unwrap_ref_type=ctx.unwrap_ref_type, struct_base_and_args=ctx.struct_base_and_args, receiver_place=ctx.receiver_place, receiver_can_mut_borrow=ctx.receiver_can_mut_borrow, receiver_compat=ctx.receiver_compat, receiver_preference=ctx.receiver_preference, args_match_params=ctx.args_match_params, coerce_args_for_params=ctx.coerce_args_for_params, infer_receiver_arg_type=ctx.infer_receiver_arg_type, instantiate_sig_with_subst=ctx.instantiate_sig_with_subst, apply_autoborrow_args=ctx.apply_autoborrow_args, label_typeid=ctx.label_typeid, trait_label=ctx.trait_label, require_for_fn=ctx.require_for_fn, extract_conjunctive_facts=ctx.extract_conjunctive_facts, subject_name=ctx.subject_name, normalize_type_key=ctx.normalize_type_key, collect_trait_subjects=ctx.collect_trait_subjects, require_failure=ctx.require_failure, format_failure_message=ctx.format_failure_message, failure_code=ctx.failure_code, pick_best_failure=ctx.pick_best_failure, requirement_notes=ctx.requirement_notes, param_scope_map=ctx.param_scope_map, candidate_key_for_decl=ctx.candidate_key_for_decl, visibility_note=ctx.visibility_note, intrinsic_method_fn_id=ctx.intrinsic_method_fn_id, instantiate_sig=ctx.instantiate_sig, self_mode_from_sig=ctx.self_mode_from_sig, match_impl_type_args=ctx.match_impl_type_args, format_infer_failure=ctx.format_infer_failure, visibility_provenance=ctx.visibility_provenance, module_ids_by_name=ctx.module_ids_by_name, record_instantiation=ctx.record_instantiation, alloc_callsite_id=ctx.alloc_callsite_id, alloc_node_id=ctx.alloc_node_id)


def make_call_ctx(**kwargs) -> CallResolverContext:
	ctx = CallResolverContext(**kwargs)
	_require_preseed_type_params(ctx)
	return ctx


def make_resolver_ctx(ctx: CallResolverContext, **overrides) -> ResolverContext:
	return _make_resolver_ctx(ctx, **overrides)


def make_method_ctx(ctx: CallResolverContext, *, diagnostics: list, traits_in_scope: Callable[[], list[TraitKey]], trait_key: TraitKey | None) -> MethodResolverContext:
	return _make_method_ctx(ctx, diagnostics=diagnostics, traits_in_scope=traits_in_scope, trait_key=trait_key)


def resolve_qualified_member_call(
	ctx: ResolverContext,
	qm: object,
	*,
	arg_exprs: list[object],
	arg_types: list[TypeId],
	kw_pairs: list[object],
	expected_type: TypeId | None,
	type_arg_ids: list[TypeId] | None,
	allow_infer: bool,
	call_type_args_span: Span | None,
) -> VariantCtorResolveResult | StructCtorResolveResult | None:
	base_te = getattr(qm, "base_type_expr", None)
	if base_te is None:
		return None
	base_kind = None
	base_name = getattr(base_te, "name", None)
	base_module = getattr(base_te, "module_id", None) or getattr(base_te, "module_alias", None) or ctx.current_module_name
	if base_kind is None:
		base_tid = resolve_opaque_type(base_te, ctx.type_table, module_id=base_module, type_params=ctx.type_param_map, allow_generic_base=True)
		if base_tid is not None:
			base_def = ctx.type_table.get(base_tid)
			if base_def.kind is TypeKind.VARIANT:
				base_kind = TypeKind.VARIANT
			elif base_def.kind is TypeKind.STRUCT:
				base_kind = TypeKind.STRUCT
	if base_kind is TypeKind.VARIANT:
		return resolve_variant_ctor(
			ctx,
			qm,
			arg_exprs=arg_exprs,
			arg_types=arg_types,
			kw_pairs=kw_pairs,
			expected_type=expected_type,
			type_arg_ids=type_arg_ids,
			allow_infer=allow_infer,
			call_type_args_span=call_type_args_span,
		)
	return None


def resolve_nonvariant_qualified_static_call(
	ctx: ResolverContext,
	qm: object,
	*,
	arg_types: list[TypeId],
	expected_type: TypeId | None,
	type_arg_ids: list[TypeId] | None,
	allow_infer: bool,
	call_type_args_span: Span | None,
) -> MethodCallResult | None:
	base_te = getattr(qm, "base_type_expr", None)
	if base_te is None:
		return None
	base_module = getattr(base_te, "module_id", None) or getattr(base_te, "module_alias", None) or ctx.current_module_name
	base_tid = resolve_opaque_type(base_te, ctx.type_table, module_id=base_module, type_params=ctx.type_param_map, allow_generic_base=True)
	if base_tid is None:
		return None
	base_def = ctx.type_table.get(base_tid)
	if base_def.kind is TypeKind.VARIANT:
		return None
	base_base_id, base_args = ctx.struct_base_and_args(base_tid)
	candidates_seen = False
	receiver_required = False
	infer_error = None
	for fn_id, sig in (ctx.signatures_by_id or {}).items():
		impl_tid = getattr(sig, "impl_target_type_id", None)
		if impl_tid is None:
			continue
		if (sig.method_name or sig.name) != qm.member:
			continue
		impl_base_id, _impl_args = ctx.struct_base_and_args(impl_tid)
		if impl_base_id != base_base_id:
			continue
		candidates_seen = True
		param_type_ids = list(getattr(sig, "param_type_ids", []) or [])
		ret_tid = getattr(sig, "return_type_id", None)
		if ret_tid is None:
			continue
		impl_target_type_args = list(getattr(sig, "impl_target_type_args", None) or [])
		impl_type_params = list(getattr(sig, "impl_type_params", None) or [])
		impl_subst = None
		if impl_target_type_args and impl_type_params:
			impl_subst = ctx.match_impl_type_args(template_args=impl_target_type_args, recv_args=list(base_args), impl_type_params=impl_type_params)
			if impl_subst is None:
				continue
		if impl_subst is None and base_args and list(_impl_args) != list(base_args):
			continue
		if impl_subst is not None:
			param_type_ids = [apply_subst(p, impl_subst, ctx.type_table) for p in param_type_ids]
			ret_tid = apply_subst(ret_tid, impl_subst, ctx.type_table)
		self_mode = ctx.self_mode_from_sig(sig)
		if param_type_ids:
			compat_ok, _needs_autoborrow = ctx.receiver_compat(base_tid, param_type_ids[0], self_mode)
			if compat_ok:
				receiver_required = True
				continue
		sig_for_call = sig
		if impl_subst is not None:
			sig_for_call = replace(sig, param_type_ids=list(param_type_ids), return_type_id=ret_tid, impl_type_params=[], impl_target_type_args=[])
		inst_res = ctx.instantiate_sig(
			sig=sig_for_call,
			arg_types=arg_types,
			expected_type=expected_type,
			explicit_type_args=type_arg_ids,
			allow_infer=allow_infer,
			diag_span=call_type_args_span or getattr(qm, "loc", Span()),
			call_kind="associated",
			call_name=qm.member,
		)
		if inst_res.error:
			infer_error = inst_res
			continue
		if inst_res.inst_params is None or inst_res.inst_return is None:
			continue
		inst_params = list(inst_res.inst_params)
		inst_return = inst_res.inst_return
		coerced = list(arg_types)
		if not ctx.args_match_params(inst_params, coerced):
			coerced = ctx.coerce_args_for_params(inst_params, coerced)
		if not ctx.args_match_params(inst_params, coerced):
			continue
		can_throw = True
		if sig.declared_can_throw is not None:
			can_throw = bool(sig.declared_can_throw)
		info = CallInfo(target=CallTarget.direct(fn_id), sig=CallSig(param_types=tuple(inst_params), user_ret_type=inst_return, can_throw=can_throw, declared_terminal_throws=bool(getattr(sig, "declared_terminal_throws", False))))
		inferred_fn_args = tuple(getattr(getattr(inst_res, "subst", None), "args", []) or [])
		return MethodCallResult(inst_return, info, {"inferred_fn_args": inferred_fn_args})
	if receiver_required:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message=f"E-QMEM-RECEIVER-REQUIRED: '{qm.member}' on type '{ctx.pretty_type_name(base_tid, current_module=ctx.current_module_name)}' requires a receiver argument",
				severity="error",
				span=getattr(qm, "loc", Span()),
			)
		)
		return MethodCallResult(ctx.unknown_ty, None)
	if infer_error is not None:
		msg, notes = ctx.format_infer_failure(infer_error.context, infer_error)
		ctx.diagnostics.append(
			ctx.tc_diag(
				message=f"cannot infer type arguments for associated function '{qm.member}'",
				severity="error",
				span=call_type_args_span or getattr(qm, "loc", Span()),
				notes=[msg] + list(notes),
			)
		)
		return MethodCallResult(ctx.unknown_ty, None)
	if candidates_seen:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message=f"E-QMEM-NO-OVERLOAD: no overload of '{qm.member}' on type '{ctx.pretty_type_name(base_tid, current_module=ctx.current_module_name)}' matches provided arguments",
				severity="error",
				span=getattr(qm, "loc", Span()),
			)
		)
		return MethodCallResult(ctx.unknown_ty, None)
	if base_def.kind in (TypeKind.STRUCT, TypeKind.ARRAY, TypeKind.REF):
		ctx.diagnostics.append(
			ctx.tc_diag(
				message=f"E-QMEM-NO-MEMBER: member '{qm.member}' not found on type '{ctx.pretty_type_name(base_tid, current_module=ctx.current_module_name)}'",
				severity="error",
				span=getattr(qm, "loc", Span()),
			)
		)
		return MethodCallResult(ctx.unknown_ty, None)
	return None


def resolve_variant_ctor(
	ctx: ResolverContext,
	qm: object,
	*,
	arg_exprs: list[object],
	arg_types: list[TypeId],
	kw_pairs: list[object],
	expected_type: TypeId | None,
	type_arg_ids: list[TypeId] | None,
	allow_infer: bool,
	call_type_args_span: Span | None,
) -> VariantCtorResolveResult | None:
	if drift_debug.enabled("call_resolve"):
		member = getattr(qm, "member", None)
		if member == "Next":
			base_te_dbg = getattr(qm, "base_type_expr", None)
			base_mod_dbg = getattr(base_te_dbg, "module_id", None) if base_te_dbg is not None else None
			print(
				f"[call_resolve_debug] resolve_variant_ctor member=Next base_mod={base_mod_dbg} current_module={ctx.current_module_name}",
				file=sys.stderr,
			)
	base_te = getattr(qm, "base_type_expr", None)
	if base_te is None:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message="E-QMEM-NONVARIANT: qualified member base is not a variant type",
				severity="error",
				span=getattr(qm, "loc", Span()),
			)
		)
		return None
	base_tid = resolve_opaque_type(
		base_te,
		ctx.type_table,
		module_id=getattr(base_te, "module_id", None) or getattr(base_te, "module_alias", None) or ctx.current_module_name,
		type_params=ctx.type_param_map,
		allow_generic_base=True,
	)
	if base_tid is None:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message="E-QMEM-NONVARIANT: qualified member base is not a variant type",
				severity="error",
				span=getattr(qm, "loc", Span()),
			)
		)
		return None
	if ctx.type_table.get(base_tid).kind is not TypeKind.VARIANT:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message="E-QMEM-NONVARIANT: qualified member base is not a variant type",
				severity="error",
				span=getattr(qm, "loc", Span()),
			)
		)
		return None
	schema = ctx.type_table.get_variant_schema(base_tid)
	if schema is None:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message="internal: missing variant schema for qualified member base (compiler bug)",
				severity="error",
				span=getattr(qm, "loc", Span()),
			)
		)
		return None
	arm_schema = next((a for a in schema.arms if a.name == qm.member), None)
	if arm_schema is None:
		ctors = ctx.format_ctor_signature_list(schema=schema, instance=None, current_module=ctx.current_module_name)
		ctx.diagnostics.append(
			ctx.tc_diag(
				message=(
					f"E-QMEM-NO-CTOR: constructor '{qm.member}' not found in variant "
					f"'{ctx.pretty_type_name(base_tid, current_module=ctx.current_module_name)}'. "
					f"Available constructors: {', '.join(ctors)}"
				),
				severity="error",
				span=getattr(qm, "loc", Span()),
			)
		)
		return None
	if schema.tombstone_ctor == arm_schema.name:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message=f"E-QMEM-TOMBSTONE: constructor '{arm_schema.name}' is internal and cannot be constructed",
				severity="error",
				span=getattr(qm, "loc", Span()),
			)
		)
		return None
	if not arg_exprs and not kw_pairs and expected_type is not None and not type_arg_ids:
		exp_schema = ctx.type_table.get_variant_schema(expected_type)
		if exp_schema is not None:
			exp_arm = next((a for a in exp_schema.arms if a.name == qm.member), None)
			if exp_arm is not None and not exp_arm.fields:
				return VariantCtorResolveResult(expected_type, [], [], [])
	type_params: list[TypeParam] = []
	typevar_ids: list[TypeId] = []
	if schema.type_params:
		owner = FunctionId(module="lang.__internal", name=f"__variant_{schema.module_id}::{schema.name}", ordinal=0)
		for idx, tp_name in enumerate(schema.type_params):
			param_id = TypeParamId(owner=owner, index=idx)
			type_params.append(TypeParam(id=param_id, name=tp_name, span=None))
			typevar_ids.append(ctx.type_table.ensure_typevar(param_id, name=tp_name))
	type_cache: dict[tuple[TypeId, tuple[TypeId, ...]], TypeId] = {}

	def _lower_generic_expr(expr: GenericTypeExpr) -> TypeId:
		if expr.param_index is not None:
			idx = int(expr.param_index)
			if 0 <= idx < len(typevar_ids):
				return typevar_ids[idx]
			return ctx.unknown_ty
		name = expr.name
		if name in FIXED_WIDTH_TYPE_NAMES:
			if ctx.fixed_width_allowed(expr.module_id or schema.module_id or ctx.current_module_name):
				return ctx.type_table.ensure_named(name, module_id=expr.module_id or schema.module_id)
			ctx.diagnostics.append(
				ctx.tc_diag(
					message=(
						f"fixed-width type '{name}' is reserved in v1; "
						"use Int/Uint/Float or Byte"
					),
					code="E_FIXED_WIDTH_RESERVED",
					severity="error",
					span=_best_effort_span(expr),
				)
			)
			return ctx.unknown_ty
		if name == "Int":
			return ctx.int_ty
		if name == "Uint":
			return ctx.uint_ty
		if name in ("Uint64", "u64"):
			return ctx.uint64_ty
		if name == "Byte":
			return ctx.byte_ty
		if name == "Int32":
			return ctx.type_table.ensure_int32()
		if name == "Uint32":
			return ctx.type_table.ensure_uint32()
		if name == "Bool":
			return ctx.bool_ty
		if name == "Float":
			return ctx.float_ty
		if name == "String":
			return ctx.string_ty
		if name == "Void":
			return ctx.void_ty
		if name == "Error":
			return ctx.error_ty
		if name == "DiagnosticValue":
			return ctx.dv_ty
		if name == "Unknown":
			return ctx.unknown_ty
		if name in {"&", "&mut"} and expr.args:
			inner = _lower_generic_expr(expr.args[0])
			return ctx.type_table.ensure_ref_mut(inner) if name == "&mut" else ctx.type_table.ensure_ref(inner)
		if name == "Array" and expr.args:
			elem = _lower_generic_expr(expr.args[0])
			span = Span.from_loc(getattr(expr.args[0], "loc", None)) if expr.args else Span()
			if ctx.reject_zst_array(elem, span=span):
				return ctx.unknown_ty
			return ctx.type_table.new_array(elem)
		origin_mod = expr.module_id or schema.module_id
		alias_def = ctx.type_table.lookup_type_alias(module_id=origin_mod, name=name)
		if alias_def is None:
			unique_alias = ctx.type_table.find_unique_type_alias_by_name(name=name)
			if unique_alias is not None:
				origin_mod, alias_params_u, alias_target_u, alias_loc_u = unique_alias
				alias_def = (alias_params_u, alias_target_u, alias_loc_u)
		if alias_def is not None:
			alias_params, alias_target, _loc = alias_def
			if len(expr.args) != len(alias_params):
				return ctx.unknown_ty
			type_param_bindings: dict[str, TypeId] = {}
			for idx, param_name in enumerate(alias_params):
				type_param_bindings[param_name] = _lower_generic_expr(expr.args[idx])
			return resolve_opaque_type(
				alias_target,
				ctx.type_table,
				module_id=origin_mod,
				type_params=type_param_bindings,
				allow_generic_base=True,
			)
		base_id = (
			ctx.type_table.get_nominal(kind=TypeKind.STRUCT, module_id=origin_mod, name=name)
			or ctx.type_table.get_nominal(kind=TypeKind.VARIANT, module_id=origin_mod, name=name)
			or ctx.type_table.ensure_named(name, module_id=origin_mod)
		)
		if ctx.type_table.get(base_id).kind is TypeKind.FORWARD_NOMINAL:
			unique = (
				ctx.type_table.find_unique_nominal_by_name(kind=TypeKind.STRUCT, name=name)
				or ctx.type_table.find_unique_nominal_by_name(kind=TypeKind.VARIANT, name=name)
			)
			if unique is not None:
				base_id = unique
		if expr.args:
			if base_id in ctx.type_table.struct_bases:
				base_schema = ctx.type_table.struct_bases.get(base_id)
				if base_schema is not None and not base_schema.type_params:
					ctx.diagnostics.append(
						ctx.tc_diag(
							message=f"type '{name}' is not generic",
							code="E-TYPE-NOT-GENERIC",
							severity="error",
							span=Span.from_loc(getattr(expr, "loc", None)),
						)
					)
					return ctx.unknown_ty
			elif base_id in ctx.type_table.variant_schemas:
				base_schema = ctx.type_table.variant_schemas.get(base_id)
				if base_schema is not None and not base_schema.type_params:
					ctx.diagnostics.append(
						ctx.tc_diag(
							message=f"type '{name}' is not generic",
							code="E-TYPE-NOT-GENERIC",
							severity="error",
							span=Span.from_loc(getattr(expr, "loc", None)),
						)
					)
					return ctx.unknown_ty
			else:
				ctx.diagnostics.append(
					ctx.tc_diag(
						message=f"unknown generic type '{name}'",
						code="E-TYPE-UNKNOWN",
						severity="error",
						span=Span.from_loc(getattr(expr, "loc", None)),
					)
				)
				return ctx.unknown_ty
		if expr.args:
			arg_ids = [_lower_generic_expr(a) for a in expr.args]
			if base_id in ctx.type_table.variant_schemas:
				if any(ctx.type_table.get(a).kind is TypeKind.TYPEVAR for a in arg_ids):
					key = (base_id, tuple(arg_ids))
					if key not in type_cache:
						td = ctx.type_table.get(base_id)
						type_cache[key] = ctx.type_table._add(
							TypeKind.VARIANT,
							td.name,
							list(arg_ids),
							register_named=False,
							module_id=td.module_id,
						)
					return type_cache[key]
				return ctx.type_table.ensure_instantiated(base_id, arg_ids)
		return base_id

	param_type_ids: list[TypeId] = []
	for f in arm_schema.fields:
		param_type_ids.append(_lower_generic_expr(f.type_expr))
	ret_type_id = base_tid
	if schema.type_params:
		ret_type_id = _lower_generic_expr(
			GenericTypeExpr.named(
				schema.name,
				args=[GenericTypeExpr.param(i) for i in range(len(schema.type_params))],
				module_id=schema.module_id,
			)
		)
	if type_arg_ids is None and hasattr(base_te, "args") and getattr(base_te, "args"):
		try:
			type_arg_ids = [resolve_opaque_type(arg, ctx.type_table, module_id=ctx.current_module_name, type_params=ctx.type_param_map, allow_generic_base=True) for arg in getattr(base_te, "args", [])]
		except Exception:
			type_arg_ids = None
	elif type_arg_ids is not None and hasattr(base_te, "args") and getattr(base_te, "args"):
		ctx.diagnostics.append(
			ctx.tc_diag(
				message="E-QMEM-DUP-TYPEARGS: qualified constructor may specify type arguments only once",
				severity="error",
				span=call_type_args_span or getattr(qm, "loc", Span()),
			)
		)
		return None
	if schema.type_params and expected_type is None and not type_arg_ids and not arg_exprs:
		hint = (
			"Hint: qualify the constructor (e.g., `Optional<T>::None()` or `Optional::None<type T>()`)."
		)
		ctx.diagnostics.append(
			ctx.tc_diag(
				message=f"E-QMEM-CANNOT-INFER: constructor '{qm.member}' needs an expected type to infer type parameters (underconstrained). {hint}",
				severity="error",
				span=getattr(qm, "loc", Span()),
				notes=[hint, "underconstrained"],
			)
		)
		return None
	if not schema.type_params and not arm_schema.fields and not arg_exprs and not kw_pairs and not type_arg_ids:
		return VariantCtorResolveResult(base_tid, [], [], [])
	ctor_sig = FnSignature(
		name=qm.member,
		param_type_ids=param_type_ids,
		return_type_id=ret_type_id,
		type_params=type_params,
		module=ctx.current_module_name,
	)
	_ctor_field_spec = CtorFieldSpec(field_names=tuple(f.name for f in arm_schema.fields))
	_ctor_pre = ctor_call_issues(len(arg_exprs), tuple(kw.name for kw in kw_pairs), _ctor_field_spec, ctor_label="variant", span=getattr(qm, "loc", Span()))
	_ctor_pre_codes = {i.code for i in _ctor_pre}
	if "E_CTOR_MIXED_ARGS" in _ctor_pre_codes:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message="E-QMEM-MIXED-ARGS: constructor calls cannot mix positional and named arguments in v1",
				severity="error",
				span=getattr(qm, "loc", Span()),
			)
		)
		return None
	# K42: canonicalize FORWARD_NOMINAL expected_type to actual VARIANT so
	# that instantiate_sig can unify the constructor return type with the
	# expected type and the fallback can find the variant instance.
	if expected_type is not None:
		_etd = ctx.type_table.get(expected_type)
		if _etd.kind is TypeKind.FORWARD_NOMINAL and _etd.param_types:
			_resolved_base = (
				ctx.type_table.get_nominal(kind=TypeKind.VARIANT, module_id=_etd.module_id, name=_etd.name)
				or ctx.type_table.find_unique_nominal_by_name(kind=TypeKind.VARIANT, name=_etd.name)
			)
			if _resolved_base is not None:
				_has_tv = any(ctx.type_table.has_typevar(p) for p in _etd.param_types)
				try:
					if _has_tv:
						expected_type = ctx.type_table.ensure_variant_template(_resolved_base, list(_etd.param_types))
					else:
						expected_type = ctx.type_table.ensure_variant_instantiated(_resolved_base, list(_etd.param_types))
				except (ValueError, KeyError):
					pass
	inst_res = ctx.instantiate_sig(
		sig=ctor_sig,
		arg_types=arg_types,
		expected_type=expected_type,
		explicit_type_args=type_arg_ids,
		allow_infer=allow_infer,
		diag_span=call_type_args_span or getattr(qm, "loc", Span()),
		call_kind="ctor",
		call_name=qm.member,
	)
	if inst_res.error and expected_type is not None and not type_arg_ids:
		exp_inst = ctx.type_table.get_variant_instance(expected_type)
		if exp_inst is not None and exp_inst.type_args:
			inst_res = ctx.instantiate_sig(
				sig=ctor_sig,
				arg_types=arg_types,
				expected_type=expected_type,
				explicit_type_args=list(exp_inst.type_args),
				allow_infer=allow_infer,
				diag_span=call_type_args_span or getattr(qm, "loc", Span()),
				call_kind="ctor",
				call_name=qm.member,
			)
	if inst_res.error and inst_res.error.kind is InferErrorKind.TYPEARG_COUNT:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message=(
					f"E-QMEM-TYPEARGS-ARITY: expected {len(schema.type_params)} type arguments, got {len(type_arg_ids or [])}"
				),
				severity="error",
				span=call_type_args_span or getattr(qm, "loc", Span()),
			)
		)
		return None
	if inst_res.error and inst_res.error.kind is InferErrorKind.NO_TYPEPARAMS:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message="constructor does not accept type arguments; use the non-generic form instead",
				severity="error",
				span=call_type_args_span or getattr(qm, "loc", Span()),
			)
		)
		return None
	if inst_res.error:
		msg, notes = ctx.format_infer_failure(inst_res.context, inst_res)
		ctx.diagnostics.append(
			ctx.tc_diag(
				message=f"cannot infer type arguments for variant '{schema.name}'",
				severity="error",
				span=call_type_args_span or getattr(qm, "loc", Span()),
				notes=[msg] + list(notes),
			)
		)
		return None
	if inst_res.inst_params is None or inst_res.inst_return is None:
		return None
	inst_return = inst_res.inst_return
	ctor_arg_field_indices: list[int] = []
	if kw_pairs:
		_kw_by_name = {kw.name: kw for kw in kw_pairs}
		for issue in _ctor_pre:
			if issue.code == "E_CTOR_UNKNOWN_FIELD":
				_fn = issue.notes[0].removeprefix("field=") if issue.notes else "?"
				_kw_obj = _kw_by_name.get(_fn)
				ctx.diagnostics.append(
					ctx.tc_diag(
						message=f"E-QMEM-NO-FIELD: constructor field '{_fn}' not found on '{qm.member}'",
						severity="error",
						span=getattr(_kw_obj, "loc", Span()) if _kw_obj else Span(),
					)
				)
				return None
			if issue.code == "E_CTOR_DUPLICATE_FIELD":
				_fn = issue.notes[0].removeprefix("field=") if issue.notes else "?"
				_kw_obj = _kw_by_name.get(_fn)
				ctx.diagnostics.append(
					ctx.tc_diag(
						message=f"E-QMEM-DUP-FIELD: duplicate constructor field '{_fn}' on '{qm.member}'",
						severity="error",
						span=getattr(_kw_obj, "loc", Span()) if _kw_obj else Span(),
					)
				)
				return None
		field_indices = {f.name: idx for idx, f in enumerate(arm_schema.fields)}
		ordered_args: list[object] = []
		for kw in kw_pairs:
			ctor_arg_field_indices.append(field_indices[kw.name])
			ordered_args.append(kw.value)
		ctor_args = ordered_args
	else:
		ctor_arg_field_indices = list(range(len(arg_exprs)))
		ctor_args = arg_exprs
	return VariantCtorResolveResult(inst_return, list(inst_res.inst_params), ctor_arg_field_indices, ctor_args)


def resolve_struct_ctor(
	ctx: ResolverContext,
	*,
	struct_id: TypeId,
	struct_name: str,
	arg_exprs: list[object],
	arg_types: list[TypeId],
	kw_pairs: list[object],
	expected_type: TypeId | None,
	type_arg_ids: list[TypeId] | None,
	allow_infer: bool,
	call_type_args_span: Span | None,
	span: Span,
) -> StructCtorResolveResult | None:
	struct_def = ctx.type_table.get(struct_id)
	if struct_def.kind is not TypeKind.STRUCT:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message=f"internal: struct schema '{struct_name}' is not a STRUCT TypeId",
				severity="error",
				span=span,
			)
		)
		return None
	struct_inst = ctx.type_table.get_struct_instance(struct_id)
	base_id = struct_inst.base_id if struct_inst is not None else struct_id
	schema = ctx.type_table.get_struct_schema(base_id)
	if schema is None:
		ctx.diagnostics.append(ctx.tc_diag(message=f"internal: missing schema for struct '{struct_name}'", severity="error", span=span))
		return None
	if expected_type is not None:
		exp_inst = ctx.type_table.get_struct_instance(expected_type)
		if exp_inst is not None and exp_inst.base_id == base_id:
			struct_id = expected_type
			struct_inst = exp_inst
	if struct_inst is not None:
		field_names = list(struct_inst.field_names)
		field_types = list(struct_inst.field_types)
	else:
		field_names = [f.name for f in schema.fields]
		field_types = list(struct_def.param_types)
	if struct_inst is None and schema.type_params:
		if (not type_arg_ids) and expected_type is not None:
			exp_inst = ctx.type_table.get_struct_instance(expected_type)
			if exp_inst is not None and exp_inst.base_id == base_id:
				type_arg_ids = list(exp_inst.type_args)
		param_ids = ctx.type_table.get_struct_type_param_ids(base_id) or []
		type_params: list[TypeParam] = []
		typevar_ids: list[TypeId] = []
		for idx, name in enumerate(schema.type_params):
			if idx < len(param_ids):
				param_id = param_ids[idx]
			else:
				param_id = TypeParamId(owner=FunctionId(module="lang.__internal", name=f"__struct_{schema.module_id}::{schema.name}", ordinal=0), index=idx)
			type_params.append(TypeParam(id=param_id, name=name, span=None))
			typevar_ids.append(ctx.type_table.ensure_typevar(param_id, name=name))
		local_type_param_map = {name: typevar_ids[idx] for idx, name in enumerate(schema.type_params) if idx < len(typevar_ids)}
		def _lower_generic_expr(expr: object) -> TypeId:
			if hasattr(expr, "param_index") and getattr(expr, "param_index", None) is not None:
				idx = int(getattr(expr, "param_index"))
				if 0 <= idx < len(typevar_ids):
					return typevar_ids[idx]
				return ctx.unknown_ty
			return resolve_opaque_type(expr, ctx.type_table, module_id=schema.module_id or ctx.current_module_name, type_params=local_type_param_map)
		field_types = [_lower_generic_expr(f.type_expr) for f in schema.fields]
		ret_type_id = ctx.type_table.ensure_struct_template(base_id, typevar_ids)
		ctor_sig = FnSignature(name=struct_name, param_type_ids=list(field_types), return_type_id=ret_type_id, type_params=list(type_params), param_names=list(field_names), module=schema.module_id or ctx.current_module_name)
		inst_res = ctx.instantiate_sig(sig=ctor_sig, arg_types=arg_types, expected_type=expected_type, explicit_type_args=type_arg_ids, allow_infer=allow_infer, diag_span=call_type_args_span or span, call_kind="ctor", call_name=struct_name)
		if inst_res.error and inst_res.error.kind is InferErrorKind.TYPEARG_COUNT:
			ctx.diagnostics.append(ctx.tc_diag(message=(f"type argument count mismatch for struct '{struct_name}': expected {len(schema.type_params)}, got {len(type_arg_ids or [])}"), severity="error", span=call_type_args_span or span))
			return None
		if inst_res.error and inst_res.error.kind is InferErrorKind.NO_TYPEPARAMS:
			ctx.diagnostics.append(ctx.tc_diag(message=f"struct '{struct_name}' does not accept type arguments", severity="error", span=call_type_args_span or span))
			return None
		if inst_res.error:
			msg, notes = ctx.format_infer_failure(inst_res.context, inst_res)
			field_notes = [f"field '{n}'" for n in field_names]
			ctx.diagnostics.append(ctx.tc_diag(message=f"cannot infer type arguments for struct '{struct_name}'", severity="error", span=call_type_args_span or span, notes=[msg] + list(notes) + field_notes))
			return None
		if inst_res.inst_params is None or inst_res.inst_return is None:
			return None
		field_types = list(inst_res.inst_params)
		struct_id = inst_res.inst_return
	ctx.enforce_struct_requires(struct_id, span)
	for fname in field_names:
		ctx.ensure_field_visible(struct_id, fname, span)
	if len(field_names) != len(field_types):
		ctx.diagnostics.append(ctx.tc_diag(message=f"internal: struct '{struct_name}' schema/type mismatch", severity="error", span=span))
		return StructCtorResolveResult(struct_id, field_types, [], list(arg_exprs))
	def _dealias_zero_param(ty: TypeId, *, _seen: set[tuple[str | None, str]] | None = None) -> TypeId:
		seen = _seen if _seen is not None else set()
		td = ctx.type_table.get(ty)
		if td.kind is TypeKind.REF and td.param_types:
			inner = _dealias_zero_param(td.param_types[0], _seen=seen)
			return ctx.type_table.ensure_ref_mut(inner) if td.ref_mut else ctx.type_table.ensure_ref(inner)
		if td.kind is TypeKind.ARRAY and td.param_types:
			elem = _dealias_zero_param(td.param_types[0], _seen=seen)
			return ctx.type_table.new_array(elem)
		inst = ctx.type_table.get_struct_instance(ty)
		if inst is not None and inst.type_args:
			new_args = [_dealias_zero_param(arg, _seen=seen) for arg in inst.type_args]
			return ctx.type_table.ensure_struct_template(inst.base_id, new_args) if any(ctx.type_table.has_typevar(arg) for arg in new_args) else ctx.type_table.ensure_struct_instantiated(inst.base_id, new_args)
		vinst = ctx.type_table.get_variant_instance(ty)
		if vinst is not None and vinst.type_args:
			new_args = [_dealias_zero_param(arg, _seen=seen) for arg in vinst.type_args]
			return ctx.type_table.ensure_variant_template(vinst.base_id, new_args) if any(ctx.type_table.has_typevar(arg) for arg in new_args) else ctx.type_table.ensure_variant_instantiated(vinst.base_id, new_args)
		mod = td.module_id
		name = td.name
		alias_def = ctx.type_table.lookup_type_alias(module_id=mod, name=name)
		if alias_def is None:
			return ty
		alias_params, alias_target, _loc = alias_def
		if alias_params:
			return ty
		alias_key = (mod, name)
		if alias_key in seen:
			return ty
		resolved = resolve_opaque_type(alias_target, ctx.type_table, module_id=mod, type_params=None, allow_generic_base=True)
		return _dealias_zero_param(resolved, _seen=seen | {alias_key})
	def _same_type(a: TypeId, b: TypeId) -> bool:
		a = _dealias_zero_param(a)
		b = _dealias_zero_param(b)
		if a == b:
			return True
		key_a = normalize_type_key(
			type_key_from_typeid(ctx.type_table, a),
			module_name=ctx.current_module_name,
			default_package=ctx.default_package,
			module_packages=ctx.module_packages,
		)
		key_b = normalize_type_key(
			type_key_from_typeid(ctx.type_table, b),
			module_name=ctx.current_module_name,
			default_package=ctx.default_package,
			module_packages=ctx.module_packages,
		)
		if ctx.type_table.has_typevar(a) or ctx.type_table.has_typevar(b):
			if key_a.name == key_b.name and len(key_a.args) == len(key_b.args):
				return True
		if key_a == key_b:
			return True
		# nothrow fn is assignable to throwing fn type (subtyping):
		# Fn(...) nothrow -> R  ⊆  Fn(...) -> R
		if key_a.name == "fn" and key_b.name == "fn" and key_a.args == key_b.args and key_a.fn_throws is False and key_b.fn_throws is True:
			return True
		return False
	_struct_field_spec = CtorFieldSpec(field_names=tuple(field_names))
	_struct_pre = ctor_call_issues(len(arg_exprs), tuple(kw.name for kw in kw_pairs), _struct_field_spec, ctor_label="struct", span=span)
	_struct_pre_codes = {i.code for i in _struct_pre}
	if "E_CTOR_MIXED_ARGS" in _struct_pre_codes:
		ctx.diagnostics.append(ctx.tc_diag(message=f"cannot mix positional and named arguments for struct '{struct_name}'", severity="error", span=span))
		return None
	ctor_arg_field_indices: list[int] = []
	ctor_args: list[object] = []
	if arg_exprs:
		if "E_CTOR_ARITY_MISMATCH" in _struct_pre_codes:
			ctx.diagnostics.append(ctx.tc_diag(message=f"struct '{struct_name}' constructor expects {len(field_types)} args, got {len(arg_exprs)}", severity="error", span=span))
			return StructCtorResolveResult(struct_id, field_types, [], list(arg_exprs))
		ctor_arg_field_indices = list(range(len(arg_exprs)))
		ctor_args = list(arg_exprs)
		for idx, (have, want) in enumerate(zip(arg_types, field_types)):
			if ctx.type_table.has_typevar(have) or ctx.type_table.has_typevar(want):
				continue
			if not _same_type(have, want):
				want_def = ctx.type_table.get(want)
				have_def = ctx.type_table.get(have)
				if want_def.kind is TypeKind.INTERFACE:
					if have_def.kind is TypeKind.INTERFACE:
						if ctx.iface_assignable is not None and ctx.iface_assignable(have, want):
							if ctx.record_iface_coercion is not None:
								ctx.record_iface_coercion(arg_exprs[idx], want)
							continue
					else:
						if ctx.record_iface_coercion is not None:
							ctx.record_iface_coercion(arg_exprs[idx], want)
						continue
				ctx.diagnostics.append(ctx.tc_diag(message=(f"struct '{struct_name}' field '{field_names[idx]}' type mismatch (have {ctx.type_table.get(have).name}, expected {ctx.type_table.get(want).name})"), severity="error", span=getattr(arg_exprs[idx], "loc", Span())))
				return None
	else:
		_kw_by_name = {kw.name: kw for kw in kw_pairs}
		for issue in _struct_pre:
			if issue.code == "E_CTOR_UNKNOWN_FIELD":
				_fn = issue.notes[0].removeprefix("field=") if issue.notes else "?"
				_kw_obj = _kw_by_name.get(_fn)
				ctx.diagnostics.append(ctx.tc_diag(message=f"unknown field '{_fn}' for struct '{struct_name}'", severity="error", span=_best_effort_span(_kw_obj, _kw_obj.value if _kw_obj and hasattr(_kw_obj, "value") else None, span)))
				return None
			if issue.code == "E_CTOR_DUPLICATE_FIELD":
				_fn = issue.notes[0].removeprefix("field=") if issue.notes else "?"
				_kw_obj = _kw_by_name.get(_fn)
				ctx.diagnostics.append(ctx.tc_diag(message=f"duplicate field '{_fn}' for struct '{struct_name}'", severity="error", span=_best_effort_span(_kw_obj, _kw_obj.value if _kw_obj and hasattr(_kw_obj, "value") else None, span)))
				return None
			if issue.code == "E_CTOR_MISSING_FIELDS":
				_missing_names = [n.removeprefix("field=") for n in issue.notes]
				first_kw = kw_pairs[0] if kw_pairs else None
				ctx.diagnostics.append(ctx.tc_diag(message=f"missing field(s) for struct '{struct_name}': {', '.join(_missing_names)}", severity="error", span=_best_effort_span(first_kw, span)))
				return None
		for kw in kw_pairs:
			ctor_arg_field_indices.append(field_names.index(kw.name))
			ctor_args.append(kw.value)
		for idx, (have, field_idx) in enumerate(zip(arg_types, ctor_arg_field_indices)):
			want = field_types[field_idx]
			if ctx.type_table.has_typevar(have) or ctx.type_table.has_typevar(want):
				continue
			if not _same_type(have, want):
				want_def = ctx.type_table.get(want)
				have_def = ctx.type_table.get(have)
				if want_def.kind is TypeKind.INTERFACE:
					if have_def.kind is TypeKind.INTERFACE:
						if ctx.iface_assignable is not None and ctx.iface_assignable(have, want):
							if ctx.record_iface_coercion is not None:
								ctx.record_iface_coercion(ctor_args[idx], want)
							continue
					else:
						if ctx.record_iface_coercion is not None:
							ctx.record_iface_coercion(ctor_args[idx], want)
						continue
				ctx.diagnostics.append(ctx.tc_diag(message=(f"struct '{struct_name}' field '{field_names[field_idx]}' type mismatch (have {ctx.type_table.get(have).name}, expected {ctx.type_table.get(want).name})"), severity="error", span=getattr(ctor_args[idx], "loc", Span())))
				return None
	return StructCtorResolveResult(struct_id, field_types, ctor_arg_field_indices, ctor_args)


def resolve_unqualified_variant_ctor(ctx: ResolverContext, *, ctor_name: str, expected_type: TypeId, arg_exprs: list[object], kw_pairs: list[object], span: Span | None = None) -> VariantCtorResolveResult | None:
	try:
		exp_def = ctx.type_table.get(expected_type)
	except Exception:
		exp_def = None
	if exp_def is None or exp_def.kind is not TypeKind.VARIANT:
		return None
	inst = ctx.type_table.get_variant_instance(expected_type)
	if inst is None or ctor_name not in inst.arms_by_name:
		return None
	schema = ctx.type_table.get_variant_schema(inst.base_id)
	if schema is not None and schema.tombstone_ctor == ctor_name:
		ctx.diagnostics.append(
			ctx.tc_diag(
				message=f"E-CTOR-TOMBSTONE: constructor '{ctor_name}' is internal and cannot be constructed",
				severity="error",
				span=getattr(arg_exprs[0], "loc", Span()) if arg_exprs else Span(),
			)
		)
		return None
	arm_def = inst.arms_by_name[ctor_name]
	field_names = list(getattr(arm_def, "field_names", []) or [])
	field_types = list(arm_def.field_types)
	_uq_field_spec = CtorFieldSpec(field_names=tuple(field_names))
	_uq_pre = ctor_call_issues(len(arg_exprs), tuple(kw.name for kw in kw_pairs), _uq_field_spec, ctor_label="variant", span=_best_effort_span(kw_pairs[0] if kw_pairs else None, arg_exprs[0] if arg_exprs else None, span))
	_uq_pre_codes = {i.code for i in _uq_pre}
	if "E_CTOR_MIXED_ARGS" in _uq_pre_codes:
		ctx.diagnostics.append(ctx.tc_diag(message=f"constructor '{arm_def.name}' does not allow mixing positional and named arguments", severity="error", span=_best_effort_span(kw_pairs[0] if kw_pairs else None, arg_exprs[0] if arg_exprs else None, span)))
		return None
	if len(field_names) != len(field_types):
		ctx.diagnostics.append(ctx.tc_diag(message="internal: variant ctor schema/type mismatch (compiler bug)", severity="error", span=getattr(arg_exprs[0], "loc", Span()) if arg_exprs else Span()))
		return None
	ctor_arg_field_indices: list[int] = []
	ctor_args: list[object] = []
	if kw_pairs:
		_kw_by_name = {kw.name: kw for kw in kw_pairs}
		_had_field_error = False
		for issue in _uq_pre:
			if issue.code == "E_CTOR_UNKNOWN_FIELD":
				_fn = issue.notes[0].removeprefix("field=") if issue.notes else "?"
				_kw_obj = _kw_by_name.get(_fn)
				ctx.diagnostics.append(ctx.tc_diag(message=f"unknown field '{_fn}' for constructor '{arm_def.name}'", severity="error", span=_best_effort_span(_kw_obj, _kw_obj.value if _kw_obj and hasattr(_kw_obj, "value") else None)))
				_had_field_error = True
			elif issue.code == "E_CTOR_DUPLICATE_FIELD":
				_fn = issue.notes[0].removeprefix("field=") if issue.notes else "?"
				_kw_obj = _kw_by_name.get(_fn)
				ctx.diagnostics.append(ctx.tc_diag(message=f"duplicate field '{_fn}' for constructor '{arm_def.name}'", severity="error", span=_best_effort_span(_kw_obj, _kw_obj.value if _kw_obj and hasattr(_kw_obj, "value") else None)))
				_had_field_error = True
			elif issue.code == "E_CTOR_MISSING_FIELDS":
				_missing_names = [n.removeprefix("field=") for n in issue.notes]
				for _mf in _missing_names:
					ctx.diagnostics.append(ctx.tc_diag(message=f"missing field '{_mf}' for constructor '{arm_def.name}'", severity="error", span=_best_effort_span(kw_pairs[0] if kw_pairs else None, span)))
				_had_field_error = True
		if _had_field_error:
			return None
		for kw in kw_pairs:
			ctor_arg_field_indices.append(field_names.index(kw.name))
			ctor_args.append(kw.value)
	else:
		if "E_CTOR_ARITY_MISMATCH" in _uq_pre_codes:
			ctx.diagnostics.append(ctx.tc_diag(message=f"constructor '{arm_def.name}' expects {len(field_types)} arguments, got {len(arg_exprs)}", severity="error", span=getattr(arg_exprs[0], "loc", Span()) if arg_exprs else Span()))
			return None
		ctor_args = list(arg_exprs)
		ctor_arg_field_indices = list(range(len(field_types)))
	return VariantCtorResolveResult(expected_type, field_types, ctor_arg_field_indices, ctor_args)


def resolve_method_call(ctx: MethodResolverContext, expr: object, *, expected_type: TypeId | None) -> MethodCallResult:
	diagnostics = ctx.diagnostics
	_tc_diag = ctx.tc_diag
	type_expr = ctx.type_expr
	_optional_variant_type = ctx.optional_variant_type
	_unwrap_ref_type = ctx.unwrap_ref_type
	_struct_base_and_args = ctx.struct_base_and_args
	_receiver_place = ctx.receiver_place
	_receiver_can_mut_borrow = ctx.receiver_can_mut_borrow
	_receiver_compat = ctx.receiver_compat
	_receiver_preference = ctx.receiver_preference
	_label_typeid = ctx.label_typeid
	_trait_label = ctx.trait_label
	_require_for_fn = ctx.require_for_fn
	_extract_conjunctive_facts = ctx.extract_conjunctive_facts
	_subject_name = ctx.subject_name
	_normalize_type_key = ctx.normalize_type_key
	_collect_trait_subjects = ctx.collect_trait_subjects
	_require_failure = ctx.require_failure
	_format_failure_message = ctx.format_failure_message
	_failure_code = ctx.failure_code
	_pick_best_failure = ctx.pick_best_failure
	_param_scope_map = ctx.param_scope_map
	_candidate_key_for_decl = ctx.candidate_key_for_decl
	_visibility_note = ctx.visibility_note
	_intrinsic_method_fn_id = ctx.intrinsic_method_fn_id
	_instantiate_sig = ctx.instantiate_sig
	_self_mode_from_sig = ctx.self_mode_from_sig
	_match_impl_type_args = ctx.match_impl_type_args
	_format_infer_failure = ctx.format_infer_failure
	_infer_receiver_arg_type = ctx.infer_receiver_arg_type
	traits_in_scope = ctx.traits_in_scope
	current_module_name = ctx.current_module_name
	intent = CallIntent(expected_return=expected_type)
	debug_call_resolve = drift_debug.enabled("call_resolve")

	def _debug_call_resolve(msg: str) -> None:
		if not debug_call_resolve:
			return
		print(f"[call_resolve_debug] {msg}", file=sys.stderr)

	def _debug_if_target_call() -> None:
		if not debug_call_resolve:
			return
		method_name = getattr(expr, "method_name", None)
		if method_name not in ("throw_iterator_invalidated", "Next"):
			return
		_debug_call_resolve(
			f"method={method_name} current_module={current_module_name}"
		)

	_debug_if_target_call()
	call_type_args = list(getattr(expr, "type_args", None) or [])
	call_type_args_span = None
	if call_type_args:
		first_loc = getattr(call_type_args[0], "loc", None)
		if first_loc is not None:
			call_type_args_span = Span.from_loc(first_loc)
	type_arg_ids = [resolve_opaque_type(t, ctx.type_table, module_id=ctx.current_module_name, type_params=ctx.type_param_map) for t in call_type_args] if call_type_args else None

	def _call_info(param_types: list[TypeId], return_type: TypeId, can_throw: bool, target: FunctionId, declared_terminal_throws: bool = False) -> CallInfo:
		return CallInfo(target=CallTarget.direct(target), sig=CallSig(param_types=tuple(param_types), user_ret_type=return_type, can_throw=bool(can_throw), declared_terminal_throws=declared_terminal_throws))

	def _call_info_target(param_types: list[TypeId], return_type: TypeId, can_throw: bool, target: CallTarget, declared_terminal_throws: bool = False) -> CallInfo:
		return CallInfo(target=target, sig=CallSig(param_types=tuple(param_types), user_ret_type=return_type, can_throw=bool(can_throw), declared_terminal_throws=declared_terminal_throws))

	def _pick_most_specific_items(items: list[tuple], key_fn, require_info: dict[object, tuple[parser_ast.TraitExpr, dict[object, object], str, dict[TypeParamId, tuple[str, int]]]]) -> list[tuple]:
		if ctx.require_env_local is None:
			return items
		require_env_local = ctx.require_env_local
		formulas: dict[object, object] = {}
		for item in items:
			key = key_fn(item)
			info = require_info.get(key)
			if info is None:
				continue
			req_expr, subst, def_mod, scope_map = info
			formula = require_env_local.normalized(req_expr, subst=subst, default_module=def_mod, param_scope_map=scope_map)
			formulas[key] = formula
		winners: list[tuple] = []
		for item in items:
			key = key_fn(item)
			base = formulas.get(key)
			if base is None:
				winners.append(item)
				continue
			is_dominated = False
			for other in items:
				other_key = key_fn(other)
				if other_key == key:
					continue
				other_formula = formulas.get(other_key)
				if other_formula is None:
					continue
				if require_env_local.implies(other_formula, base) and not require_env_local.implies(base, other_formula):
					is_dominated = True
					break
			if not is_dominated:
				winners.append(item)
		return winners

	def _propagate_arg_expected_types(intent: CallIntent, arg_types: list[TypeId]) -> None:
		if not getattr(expr, "args", None):
			return
		expected_args = list(intent.arg_expected_types or [])
		if not expected_args:
			return
		for idx, arg in enumerate(expr.args):
			if idx >= len(expected_args):
				break
			if not isinstance(arg, (H.HCall, getattr(H, "HInvoke", ()), H.HMapLiteral, H.HArrayLiteral)):
				continue
			exp_ty = expected_args[idx]
			arg.defer_infer_diag = False
			arg_ty = arg_types[idx] if idx < len(arg_types) else None
			if arg_ty is not None and arg_ty != ctx.unknown_ty and not ctx.type_table.has_typevar(arg_ty):
				continue
			arg_types[idx] = type_expr(arg, expected_type=exp_ty, used_as_value=False)
			setattr(arg, "force_inferred_type", exp_ty)
			if arg_types[idx] is None or arg_types[idx] == ctx.unknown_ty:
				arg_types[idx] = exp_ty

	# Built-in DiagnosticValue helpers are reserved method names and take precedence.
	if getattr(expr, "method_name", None) in ("as_int", "as_bool", "as_float", "as_string", "as_object", "get"):
		recv_ty = type_expr(expr.receiver, used_as_value=False)
		recv_eff_ty = recv_ty
		recv_def = ctx.type_table.get(recv_eff_ty)
		while recv_def.kind is TypeKind.REF and recv_def.param_types:
			recv_eff_ty = recv_def.param_types[0]
			recv_def = ctx.type_table.get(recv_eff_ty)
		if recv_def.kind is not TypeKind.DIAGNOSTICVALUE:
			# Allow normal method resolution on non-DV receivers (e.g. JsonNode.get/as_object).
			pass
		else:
			if expr.method_name == "get":
				if len(getattr(expr, "args", []) or []) != 1:
					diagnostics.append(_tc_diag(message="DiagnosticValue.get expects exactly one key argument", severity="error", span=getattr(expr, "loc", Span())))
					info = _call_info([recv_ty], ctx.unknown_ty, False, _intrinsic_method_fn_id(expr.method_name))
					return MethodCallResult(ctx.unknown_ty, info)
				key_ty = type_expr(expr.args[0], used_as_value=False)
				if key_ty != ctx.string_ty:
					diagnostics.append(_tc_diag(message="DiagnosticValue.get key must be String", severity="error", span=getattr(expr.args[0], "loc", Span())))
					info = _call_info([recv_ty, key_ty], ctx.unknown_ty, False, _intrinsic_method_fn_id(expr.method_name))
					return MethodCallResult(ctx.unknown_ty, info)
				opt_dv = _optional_variant_type(ctx.dv_ty)
				info = _call_info([recv_ty, key_ty], opt_dv, False, _intrinsic_method_fn_id(expr.method_name))
				return MethodCallResult(opt_dv, info)
			if expr.method_name == "as_int":
				opt_int = _optional_variant_type(ctx.int_ty)
				info = _call_info([recv_ty], opt_int, False, _intrinsic_method_fn_id(expr.method_name))
				return MethodCallResult(opt_int, info)
			if expr.method_name == "as_bool":
				opt_bool = _optional_variant_type(ctx.bool_ty)
				info = _call_info([recv_ty], opt_bool, False, _intrinsic_method_fn_id(expr.method_name))
				return MethodCallResult(opt_bool, info)
			if expr.method_name == "as_float":
				opt_float = _optional_variant_type(ctx.float_ty)
				info = _call_info([recv_ty], opt_float, False, _intrinsic_method_fn_id(expr.method_name))
				return MethodCallResult(opt_float, info)
			if expr.method_name == "as_string":
				opt_string = _optional_variant_type(ctx.string_ty)
				info = _call_info([recv_ty], opt_string, False, _intrinsic_method_fn_id(expr.method_name))
				return MethodCallResult(opt_string, info)
			if expr.method_name == "as_object":
				opt_dv = _optional_variant_type(ctx.dv_ty)
				info = _call_info([recv_ty], opt_dv, False, _intrinsic_method_fn_id(expr.method_name))
				return MethodCallResult(opt_dv, info)
			info = _call_info([recv_ty], ctx.unknown_ty, False, _intrinsic_method_fn_id(expr.method_name))
			return MethodCallResult(ctx.unknown_ty, info)

	# DiagnosticValue.len() and .entries() — checked separately because "len"
	# is also a field-sugar name on Array/String. Only intercept when the
	# receiver is actually DiagnosticValue.
	if getattr(expr, "method_name", None) in ("len", "entries"):
		_dv_recv_ty = type_expr(expr.receiver, used_as_value=False)
		_dv_eff_ty = _dv_recv_ty
		_dv_td = ctx.type_table.get(_dv_eff_ty)
		while _dv_td.kind is TypeKind.REF and _dv_td.param_types:
			_dv_eff_ty = _dv_td.param_types[0]
			_dv_td = ctx.type_table.get(_dv_eff_ty)
		if _dv_td.kind is TypeKind.DIAGNOSTICVALUE:
			if expr.method_name == "len":
				if getattr(expr, "args", None):
					diagnostics.append(_tc_diag(message="DiagnosticValue.len takes no arguments", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
				if call_kwargs_issues(f"DiagnosticValue.{expr.method_name}", getattr(expr, "kwargs", None)):
					first = (getattr(expr, "kwargs", []) or [None])[0]
					diagnostics.append(_tc_diag(message="DiagnosticValue.len takes no keyword arguments", severity="error", span=_best_effort_span(first, expr)))
					return MethodCallResult(ctx.unknown_ty, None)
				info = _call_info([_dv_recv_ty], ctx.int_ty, False, _intrinsic_method_fn_id("len"))
				return MethodCallResult(ctx.int_ty, info)
			if expr.method_name == "entries":
				if getattr(expr, "args", None):
					diagnostics.append(_tc_diag(message="DiagnosticValue.entries takes no arguments", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
				if call_kwargs_issues(f"DiagnosticValue.{expr.method_name}", getattr(expr, "kwargs", None)):
					first = (getattr(expr, "kwargs", []) or [None])[0]
					diagnostics.append(_tc_diag(message="DiagnosticValue.entries takes no keyword arguments", severity="error", span=_best_effort_span(first, expr)))
					return MethodCallResult(ctx.unknown_ty, None)
				# Resolve canonical std.core:DiagnosticEntry via public API.
				# No fallback — this intrinsic returns a fixed C layout that
				# must match std.core's definition exactly.
				de_ty = ctx.type_table.get_nominal(kind=TypeKind.STRUCT, module_id="std.core", name="DiagnosticEntry")
				if de_ty is None:
					diagnostics.append(_tc_diag(message="internal: std.core:DiagnosticEntry not found in type table (compiler invariant violation)", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
				arr_de_ty = ctx.type_table.new_array(de_ty)
				info = _call_info([_dv_recv_ty], arr_de_ty, False, _intrinsic_method_fn_id("entries"))
				return MethodCallResult(arr_de_ty, info)

	if call_kwargs_issues("method calls", getattr(expr, "kwargs", None)):
		first = (getattr(expr, "kwargs", []) or [None])[0]
		diagnostics.append(_tc_diag(message="keyword arguments are not supported for method calls in v1", severity="error", span=_best_effort_span(first, expr)))
		return MethodCallResult(ctx.unknown_ty, None)

	recv_ty = getattr(expr, "receiver_type_id", None)
	if recv_ty is None:
		recv_ty = type_expr(expr.receiver, used_as_value=False)
	for arg in expr.args:
		if isinstance(arg, (H.HCall, getattr(H, "HInvoke", ()), H.HMapLiteral, H.HArrayLiteral)):
			arg.defer_infer_diag = True
	arg_types = getattr(expr, "arg_type_ids", None)
	cur_sig = ctx.signatures_by_id.get(ctx.current_fn_id) if ctx.signatures_by_id is not None else None
	instantiation_mode = bool(cur_sig and getattr(cur_sig, "is_instantiation", False))
	is_generic_body = bool(cur_sig and ((getattr(cur_sig, "type_params", None) or []) or (getattr(cur_sig, "impl_type_params", None) or [])))
	if arg_types is None:
		arg_types = []
		for arg in expr.args:
			if isinstance(arg, H.HLambda):
				arg_types.append(ctx.unknown_ty)
				continue
			arg_types.append(type_expr(arg, used_as_value=False))
	for idx, arg in enumerate(expr.args):
		if isinstance(arg, H.HLambda):
			continue

	recv_def = ctx.type_table.get(recv_ty)
	# Borrowed interface receivers (&Interface, &mut Interface) are valid:
	# the existing recv_nominal unwrap (a few lines below) routes them to
	# the interface-dispatch branch.  HIR→MIR (`_lower_method_call_with_info`
	# in stage2/hir_to_mir.py) recognises REF<INTERFACE> too and emits
	# CallIface, which the LLVM `_lower_call_iface` lowering loads through.

	recv_nominal = _unwrap_ref_type(recv_ty)
	recv_nominal_def = ctx.type_table.get(recv_nominal)
	if expr.method_name == "call" and recv_nominal_def.kind is TypeKind.FUNCTION:
		if call_type_args:
			diagnostics.append(_tc_diag(message="function call does not accept type arguments", severity="error", span=call_type_args_span or getattr(expr, "loc", Span())))
			return MethodCallResult(ctx.unknown_ty, None)
		fn_sig_params = list(recv_nominal_def.param_types[:-1]) if recv_nominal_def.param_types else []
		fn_sig_ret = recv_nominal_def.param_types[-1] if recv_nominal_def.param_types else ctx.unknown_ty
		if len(fn_sig_params) != len(arg_types):
			diagnostics.append(_tc_diag(message=f"function value expects {len(fn_sig_params)} arguments, got {len(arg_types)}", severity="error", span=getattr(expr, "loc", Span())))
			return MethodCallResult(fn_sig_ret, None)
		for want, have in zip(fn_sig_params, arg_types):
			if have is not None and want != have:
				# Implicit reborrow: allow &mut T where &T is expected.
				have_def = ctx.type_table.get(have)
				want_def = ctx.type_table.get(want)
				if (have_def.kind is TypeKind.REF and want_def.kind is TypeKind.REF
						and have_def.ref_mut is True and want_def.ref_mut is False
						and have_def.param_types and want_def.param_types
						and have_def.param_types[0] == want_def.param_types[0]):
					continue
				diagnostics.append(_tc_diag(message=f"function value argument type mismatch (have {ctx.type_table.get(have).name}, expected {ctx.type_table.get(want).name})", severity="error", span=getattr(expr, "loc", Span())))
		call_can_throw = recv_nominal_def.can_throw()
		target_id = getattr(expr.receiver, "node_id", None)
		if target_id is None:
			target_id = getattr(expr, "node_id", None)
		info = _call_info_target(fn_sig_params, fn_sig_ret, bool(call_can_throw), CallTarget.indirect(target_id))
		return MethodCallResult(fn_sig_ret, info)
	if recv_nominal_def.kind is TypeKind.INTERFACE:
		interface_inst = ctx.type_table.get_interface_instance(recv_nominal)
		base_id = interface_inst.base_id if interface_inst is not None else recv_nominal
		schema = ctx.type_table.interface_bases.get(base_id)
		if schema is None:
			diagnostics.append(_tc_diag(message="interface method schema missing (compiler bug)", severity="error", span=getattr(expr, "loc", Span())))
			return MethodCallResult(ctx.unknown_ty, None)
		if call_type_args:
			diagnostics.append(_tc_diag(message="interface methods do not accept type arguments in v1", severity="error", span=call_type_args_span or getattr(expr, "loc", Span())))
			return MethodCallResult(ctx.unknown_ty, None)
		try:
			owner_id, method_schema = ctx.type_table.interface_method_lookup(base_id, expr.method_name)
		except KeyError:
			diagnostics.append(_tc_diag(message=f"unknown method '{expr.method_name}' on interface '{schema.name}'", severity="error", span=getattr(expr, "loc", Span())))
			return MethodCallResult(ctx.unknown_ty, None)
		if method_schema.type_params:
			diagnostics.append(_tc_diag(message=f"interface method '{expr.method_name}' type parameters are not supported in v1", severity="error", span=getattr(expr, "loc", Span())))
			return MethodCallResult(ctx.unknown_ty, None)
		type_args = list(getattr(interface_inst, "type_args", []) or [])
		try:
			inst_map = ctx.type_table.interface_instance_view_map(recv_nominal)
			owner_inst = ctx.type_table.get_interface_instance(inst_map.get(owner_id)) if inst_map else None
			if owner_inst is not None:
				type_args = list(owner_inst.type_args)
		except Exception:
			pass
		param_types = []
		for param in method_schema.params:
			if param.name == "self":
				continue
			param_types.append(ctx.type_table._eval_generic_type_expr(param.type_expr, type_args, module_id=ctx.type_table.interface_bases.get(owner_id).module_id if ctx.type_table.interface_bases.get(owner_id) is not None else schema.module_id))
		# Phase 1 v3 of terminal-`throws`: interface methods declared with the
		# bare terminal form (`fn f() throws`) carry `return_type=None` on the
		# schema. Phase 2 will model the call result as a non-returning
		# (terminal) expression. For Phase 1 we report Unknown so the call
		# checker doesn't crash, deferring real semantics to Phase 2.
		if getattr(method_schema, "declared_terminal_throws", False) or method_schema.return_type is None:
			ret_ty = ctx.unknown_ty
		else:
			ret_ty = ctx.type_table._eval_generic_type_expr(method_schema.return_type, type_args, module_id=ctx.type_table.interface_bases.get(owner_id).module_id if ctx.type_table.interface_bases.get(owner_id) is not None else schema.module_id)
		if len(arg_types) != len(param_types):
			diagnostics.append(_tc_diag(message=f"{schema.name}.{expr.method_name} expects {len(param_types)} argument(s)", severity="error", span=getattr(expr, "loc", Span())))
			return MethodCallResult(ctx.unknown_ty, None)
		for idx, (arg_ty, param_ty) in enumerate(zip(arg_types, param_types)):
			if arg_ty is not None and arg_ty != param_ty and ctx.type_table.get(param_ty).kind is not TypeKind.UNKNOWN:
				# Implicit reborrow: allow &mut T where &T is expected.
				arg_def = ctx.type_table.get(arg_ty)
				param_def = ctx.type_table.get(param_ty)
				if (arg_def.kind is TypeKind.REF and param_def.kind is TypeKind.REF
						and arg_def.ref_mut is True and param_def.ref_mut is False
						and arg_def.param_types and param_def.param_types
						and arg_def.param_types[0] == param_def.param_types[0]):
					continue
				diagnostics.append(_tc_diag(message=f"{schema.name}.{expr.method_name} argument {idx + 1} type mismatch", severity="error", span=getattr(expr.args[idx], "loc", getattr(expr, "loc", Span()))))
				return MethodCallResult(ctx.unknown_ty, None)
		_iface_terminal = bool(getattr(method_schema, "declared_terminal_throws", False))
		info = CallInfo(target=CallTarget.indirect(getattr(expr, "node_id", None)), sig=CallSig(param_types=tuple(param_types), user_ret_type=ret_ty, can_throw=not bool(method_schema.declared_nothrow), declared_terminal_throws=_iface_terminal))
		return MethodCallResult(ret_ty, info)

	if expr.method_name == "dup" and not expr.args:
		recv_nominal = _unwrap_ref_type(recv_ty)
		recv_def = ctx.type_table.get(recv_nominal)
		if recv_def.kind is TypeKind.ARRAY and recv_def.param_types:
			elem_ty = recv_def.param_types[0]
			copy_status = ctx.type_table.copy_status(elem_ty)
			if copy_status is None:
				reason = ctx.type_table.copy_unknown_reason(elem_ty)
				diagnostics.append(
					_tc_diag(
						message=f"Array<T>.dup() requires element type to be Copy in v1 (Copy is unknown: {reason})",
						code="E-COPY-UNKNOWN",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
				return MethodCallResult(ctx.unknown_ty, None)
			if not copy_status:
				diagnostics.append(
					_tc_diag(
						message="Array<T>.dup() requires element type to be Copy in v1",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
				return MethodCallResult(ctx.unknown_ty, None)
			info = _call_info([recv_ty], recv_nominal, False, _intrinsic_method_fn_id(expr.method_name))
			return MethodCallResult(recv_nominal, info)

	if expr.method_name in ("push", "pop", "insert", "remove", "swap_remove", "swap", "clear", "reserve", "shrink_to_fit", "range", "range_mut", "get", "set", "extend", "truncate", "remove_range"):
		recv_nominal = _unwrap_ref_type(recv_ty)
		recv_def = ctx.type_table.get(recv_nominal)
		if recv_def.kind is TypeKind.ARRAY and recv_def.param_types:
			elem_ty = recv_def.param_types[0]
			recv_place = _receiver_place(expr.receiver)
			needs_mut = expr.method_name in ("push", "insert", "remove", "swap_remove", "swap", "clear", "reserve", "shrink_to_fit", "range_mut", "set", "pop", "extend", "truncate", "remove_range")
			if needs_mut and not _receiver_can_mut_borrow(expr.receiver, recv_place, recv_ty):
				diagnostics.append(_tc_diag(message=f"Array.{expr.method_name}() requires a mutable Array receiver", severity="error", span=getattr(expr, "loc", Span())))
				return MethodCallResult(ctx.unknown_ty, None)
			if expr.method_name == "get" and recv_place is None:
				diagnostics.append(_tc_diag(message="Array.get() requires an lvalue Array receiver", severity="error", span=getattr(expr, "loc", Span())))
				return MethodCallResult(ctx.unknown_ty, None)
			if expr.method_name in ("range", "range_mut") and recv_place is None:
				diagnostics.append(_tc_diag(message=f"Array.{expr.method_name}() requires an lvalue Array receiver", severity="error", span=getattr(expr, "loc", Span())))
				return MethodCallResult(ctx.unknown_ty, None)
			expected_args = {**ARRAY_METHOD_ARITY_TABLE, "range": 0, "range_mut": 0}
			want = expected_args.get(expr.method_name)
			if want is not None and len(expr.args) != want:
				diagnostics.append(_tc_diag(message=f"Array.{expr.method_name}() expects {want} argument(s)", severity="error", span=getattr(expr, "loc", Span())))
				return MethodCallResult(ctx.unknown_ty, None)
			if expr.method_name == "extend":
				copy_status = ctx.type_table.copy_status(elem_ty)
				if copy_status is None:
					reason = ctx.type_table.copy_unknown_reason(elem_ty)
					diagnostics.append(_tc_diag(message=f"Array<T>.extend() requires element type to be Copy (Copy is unknown: {reason})", code="E-COPY-UNKNOWN", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
				if not copy_status:
					diagnostics.append(_tc_diag(message="Array<T>.extend() requires element type to be Copy", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
			def _has_unknown(tid: TypeId) -> bool:
				td_local = ctx.type_table.get(tid)
				if td_local.kind is TypeKind.UNKNOWN:
					return True
				if td_local.kind is TypeKind.STRUCT:
					inst_local = ctx.type_table.get_struct_instance(tid)
					if inst_local is not None and any(_has_unknown(arg) for arg in inst_local.type_args):
						return True
				if td_local.kind is TypeKind.VARIANT:
					inst_local = ctx.type_table.get_variant_instance(tid)
					if inst_local is not None and any(_has_unknown(arg) for arg in inst_local.type_args):
						return True
				if td_local.kind is TypeKind.INTERFACE:
					inst_local = ctx.type_table.get_interface_instance(tid)
					if inst_local is not None and any(_has_unknown(arg) for arg in inst_local.type_args):
						return True
				for child in td_local.param_types:
					if _has_unknown(child):
						return True
				return False

			if expr.method_name == "push":
				arg_ty = arg_types[0] if arg_types else None
				if arg_ty is not None and arg_ty != elem_ty:
					elem_def = ctx.type_table.get(elem_ty)
					if arg_ty == ctx.unknown_ty or ctx.type_table.has_typevar(arg_ty) or ctx.type_table.has_typevar(elem_ty) or _has_unknown(arg_ty) or _has_unknown(elem_ty) or elem_def.name in ctx.type_param_map:
						pass
					elif ctx.normalize_type_key(type_key_from_typeid(ctx.type_table, arg_ty)) == ctx.normalize_type_key(
						type_key_from_typeid(ctx.type_table, elem_ty)
					):
						pass
					else:
						arg_name = ctx.type_table.get(arg_ty).name if arg_ty is not None else "Unknown"
						elem_name = ctx.type_table.get(elem_ty).name if elem_ty is not None else "Unknown"
						diagnostics.append(_tc_diag(message=f"Array element type mismatch (have {arg_name}, expected {elem_name})", severity="error", span=getattr(expr.args[0], "loc", getattr(expr, "loc", Span()))))
						return MethodCallResult(ctx.unknown_ty, None)
			if expr.method_name == "set":
				# Public API contract: `arr.set(index, value)`.
				# Pre-fix the checker shared push's "args[0] is value"
				# validator with set, but set's args[0] is actually the
				# Int INDEX (the MIR lowering at
				# `_lower_array_intrinsic_method` already enforces this
				# order — args[0]=Int, args[1]=elem_ty).  Sharing the
				# push validator made `arr.set(0, name)` reject with
				# "Array element type mismatch (have Int, expected
				# String)" because the checker compared the literal `0`
				# against the element type.  Treat set like insert:
				# args[0] = Int index, args[1] = element value —
				# mirroring the `.set` reconciliation that landed
				# array_set in the ownership-transfer matrix.
				if len(arg_types) == 2:
					idx_ty, val_ty = arg_types
					if idx_ty is not None:
						td_idx = ctx.type_table.get(idx_ty)
						if td_idx.kind is not TypeKind.TYPEVAR and idx_ty != ctx.int_ty:
							diagnostics.append(_tc_diag(message="array index must be an Int", severity="error", span=getattr(expr.args[0], "loc", getattr(expr, "loc", Span()))))
							return MethodCallResult(ctx.unknown_ty, None)
					if val_ty is not None and val_ty != elem_ty:
						elem_def = ctx.type_table.get(elem_ty)
						if val_ty == ctx.unknown_ty or ctx.type_table.has_typevar(val_ty) or ctx.type_table.has_typevar(elem_ty) or _has_unknown(val_ty) or _has_unknown(elem_ty) or elem_def.name in ctx.type_param_map:
							pass
						elif ctx.normalize_type_key(type_key_from_typeid(ctx.type_table, val_ty)) == ctx.normalize_type_key(
							type_key_from_typeid(ctx.type_table, elem_ty)
						):
							pass
						else:
							val_name = ctx.type_table.get(val_ty).name if val_ty is not None else "Unknown"
							elem_name = ctx.type_table.get(elem_ty).name if elem_ty is not None else "Unknown"
							diagnostics.append(_tc_diag(message=f"Array element type mismatch (have {val_name}, expected {elem_name})", severity="error", span=getattr(expr.args[1], "loc", getattr(expr, "loc", Span()))))
							return MethodCallResult(ctx.unknown_ty, None)
			if expr.method_name == "insert":
				if len(arg_types) == 2:
					idx_ty, val_ty = arg_types
					if idx_ty is not None:
						td_idx = ctx.type_table.get(idx_ty)
						if td_idx.kind is not TypeKind.TYPEVAR and idx_ty != ctx.int_ty:
							diagnostics.append(_tc_diag(message="array index must be an Int", severity="error", span=getattr(expr.args[0], "loc", getattr(expr, "loc", Span()))))
							return MethodCallResult(ctx.unknown_ty, None)
					if val_ty is not None and val_ty != elem_ty:
						elem_def = ctx.type_table.get(elem_ty)
						# OK paths: type-var / unknown arg, or arg type
						# normalizes equal to elem_ty.  Fall through to
						# the unified info-building / return below
						# (mirrors the `push`/`set` branch above which
						# uses `pass` for these same conditions).
						# Pre-fix this branch tried to `return
						# MethodCallResult(recv_nominal, info)`, but
						# `info` is built at line 1833 (now ~end of
						# block), so the early return crashed with
						# UnboundLocalError on the OK path for
						# Array<DiagnosticEntry>.insert(idx, HVar) etc.
						# See issues/array_insert_diag_entry_checker_unbound/.
						if val_ty == ctx.unknown_ty or ctx.type_table.has_typevar(val_ty) or ctx.type_table.has_typevar(elem_ty) or _has_unknown(val_ty) or _has_unknown(elem_ty) or elem_def.name in ctx.type_param_map:
							pass
						elif ctx.normalize_type_key(type_key_from_typeid(ctx.type_table, val_ty)) == ctx.normalize_type_key(
							type_key_from_typeid(ctx.type_table, elem_ty)
						):
							pass
						else:
							val_name = ctx.type_table.get(val_ty).name if val_ty is not None else "Unknown"
							elem_name = ctx.type_table.get(elem_ty).name if elem_ty is not None else "Unknown"
							diagnostics.append(_tc_diag(message=f"Array element type mismatch (have {val_name}, expected {elem_name})", severity="error", span=getattr(expr.args[1], "loc", getattr(expr, "loc", Span()))))
							return MethodCallResult(ctx.unknown_ty, None)
			if expr.method_name in ("remove", "swap_remove", "get"):
				if arg_types:
					idx_ty = arg_types[0]
					if idx_ty is not None:
						td_idx = ctx.type_table.get(idx_ty)
						if td_idx.kind is not TypeKind.TYPEVAR and idx_ty != ctx.int_ty:
							diagnostics.append(_tc_diag(message="array index must be an Int", severity="error", span=getattr(expr.args[0], "loc", getattr(expr, "loc", Span()))))
							return MethodCallResult(ctx.unknown_ty, None)
			if expr.method_name == "reserve" and arg_types:
				add_ty = arg_types[0]
				if add_ty is not None and add_ty != ctx.int_ty:
					diagnostics.append(_tc_diag(message="reserve expects an Int size", severity="error", span=getattr(expr.args[0], "loc", getattr(expr, "loc", Span()))))
					return MethodCallResult(ctx.unknown_ty, None)
			if expr.method_name in ("range", "range_mut"):
				range_name = "ArrayRangeMut" if expr.method_name == "range_mut" else "ArrayRange"
				range_base = ctx.type_table.get_nominal(kind=TypeKind.STRUCT, module_id="std.containers", name=range_name)
				if range_base is None:
					ret_ty = ctx.unknown_ty
				elif ctx.type_table.has_typevar(elem_ty):
					ret_ty = ctx.type_table.ensure_struct_template(range_base, [elem_ty])
				else:
					ret_ty = ctx.type_table.ensure_struct_instantiated(range_base, [elem_ty])
			elif expr.method_name == "get":
				ref_ty = ctx.type_table.ensure_ref(elem_ty)
				ret_ty = _optional_variant_type(ref_ty)
			elif expr.method_name == "pop":
				ret_ty = _optional_variant_type(elem_ty)
			elif expr.method_name in ("remove", "swap_remove"):
				ret_ty = elem_ty
			else:
				ret_ty = ctx.void_ty
			info = _call_info([recv_ty] + arg_types, ret_ty, False, _intrinsic_method_fn_id(expr.method_name))
			return MethodCallResult(ret_ty, info)

	# FnResult intrinsic methods.
	if expr.method_name in ("is_err", "unwrap", "unwrap_err") and not expr.args:
		recv_def = ctx.type_table.get(recv_ty)
		if recv_def.kind is TypeKind.FNRESULT and recv_def.param_types:
			ok_ty = recv_def.param_types[0] if len(recv_def.param_types) > 0 else ctx.unknown_ty
			err_ty = recv_def.param_types[1] if len(recv_def.param_types) > 1 else ctx.error_ty
			if expr.method_name == "is_err":
				ret_ty = ctx.bool_ty
			elif expr.method_name == "unwrap":
				ret_ty = ok_ty
			else:
				ret_ty = err_ty
			info = _call_info([recv_ty], ret_ty, False, _intrinsic_method_fn_id(expr.method_name))
			return MethodCallResult(ret_ty, info)

	if ctx.callable_registry:
		_require_for_fn = ctx.require_for_fn
		_require_failure = ctx.require_failure
		_format_failure_message = ctx.format_failure_message
		_failure_code = ctx.failure_code
		_pick_best_failure = ctx.pick_best_failure
		call_type_args = getattr(expr, "type_args", None) or []
		type_arg_ids: list[TypeId] | None = None
		call_type_args_span = None
		if call_type_args:
			first_loc = getattr(call_type_args[0], "loc", None)
			if first_loc is not None:
				call_type_args_span = Span.from_loc(first_loc)
		type_arg_ids = [resolve_opaque_type(t, ctx.type_table, module_id=ctx.current_module_name, type_params=ctx.type_param_map) for t in call_type_args]
		if ctx.type_param_names and ctx.type_param_map and not is_generic_body:
			_recv_def = ctx.type_table.get(recv_ty)
			if _recv_def.kind is TypeKind.TYPEVAR:
				_recv_name = ctx.type_param_names.get(_recv_def.type_param_id)
				if _recv_name is not None and _recv_name in ctx.type_param_map:
					recv_ty = ctx.type_param_map[_recv_name]
		if isinstance(recv_ty, TypeParamId):
			_tp_name = ctx.type_param_names.get(recv_ty) if ctx.type_param_names else None
			recv_ty = ctx.type_table.ensure_typevar(recv_ty, name=_tp_name)
		receiver_nominal = _unwrap_ref_type(recv_ty)
		receiver_base, receiver_args = _struct_base_and_args(receiver_nominal)
		# K26: canonicalize FORWARD_NOMINAL receiver to concrete struct/variant instance
		# so downstream receiver_compat and get_struct_instance work with canonical TypeIds.
		if receiver_base is not None and receiver_args:
			_fwd_def = ctx.type_table.get(receiver_nominal)
			if _fwd_def.kind is TypeKind.FORWARD_NOMINAL:
				try:
					_canonical = ctx.type_table.ensure_struct_instantiated(receiver_base, list(receiver_args))
				except (ValueError, KeyError):
					try:
						_canonical = ctx.type_table.ensure_variant_instantiated(receiver_base, list(receiver_args))
					except (ValueError, KeyError):
						_canonical = receiver_nominal
				if _canonical != receiver_nominal:
					_old_nominal = receiver_nominal
					receiver_nominal = _canonical
					if recv_ty == _old_nominal:
						recv_ty = _canonical
					else:
						_rty_def = ctx.type_table.get(recv_ty)
						if _rty_def.kind is TypeKind.REF and _rty_def.param_types and _rty_def.param_types[0] == _old_nominal:
							recv_ty = ctx.type_table.ensure_ref(_canonical) if not _rty_def.ref_mut else ctx.type_table.ensure_ref_mut(_canonical)
		if receiver_args is None:
			recv_nominal_def = ctx.type_table.get(receiver_nominal)
			if recv_nominal_def.kind is TypeKind.ARRAY and recv_nominal_def.param_types:
				receiver_base = ctx.type_table.array_base_id()
				receiver_args = list(recv_nominal_def.param_types)
			else:
				recv_struct = ctx.type_table.get_struct_instance(receiver_nominal)
				if recv_struct is not None:
					receiver_base = recv_struct.base_id
					receiver_args = list(recv_struct.type_args)
				else:
					recv_variant = ctx.type_table.get_variant_instance(receiver_nominal)
					if recv_variant is not None:
						receiver_base = recv_variant.base_id
						receiver_args = list(recv_variant.type_args)
		recv_def = ctx.type_table.get(receiver_nominal)
		if receiver_args is None and recv_def.kind is TypeKind.ARRAY and recv_def.param_types:
			receiver_args = list(recv_def.param_types)
		if receiver_args is None:
			recv_def_full = ctx.type_table.get(recv_ty)
			if recv_def_full.kind is TypeKind.REF and recv_def_full.param_types:
				inner = recv_def_full.param_types[0]
				inner_def = ctx.type_table.get(inner)
				if inner_def.kind is TypeKind.ARRAY and inner_def.param_types:
					receiver_base = ctx.type_table.array_base_id()
					receiver_args = list(inner_def.param_types)
		recv_type_param_id = recv_def.type_param_id if recv_def.kind is TypeKind.TYPEVAR else None
		if recv_type_param_id is None and ctx.type_param_map and ctx.type_param_names:
			for _tp_id, _tp_name in ctx.type_param_names.items():
				if _tp_name in ctx.type_param_map and ctx.type_param_map[_tp_name] == receiver_nominal:
					recv_type_param_id = _tp_id
					break
		recv_type_key = None
		recv_tp_name = ctx.type_param_names.get(recv_type_param_id) if recv_type_param_id is not None and ctx.type_param_names else None
		if recv_tp_name is None and ctx.type_param_map:
			_recv_key = _normalize_type_key(type_key_from_typeid(ctx.type_table, receiver_nominal))
			for _name, _tid in ctx.type_param_map.items():
				if isinstance(_tid, TypeParamId):
					continue
				try:
					_mapped_key = _normalize_type_key(type_key_from_typeid(ctx.type_table, _tid))
				except KeyError:
					continue
				if _mapped_key == _recv_key:
					recv_tp_name = _name
					break
		if recv_tp_name is None and ctx.preseed_type_params:
			_recv_key = _normalize_type_key(type_key_from_typeid(ctx.type_table, receiver_nominal))
			for _name, _tid in ctx.preseed_type_params.items():
				if isinstance(_tid, TypeParamId):
					continue
				if _tid == receiver_nominal:
					recv_tp_name = _name
					break
				try:
					_recv_base, _recv_args = _struct_base_and_args(receiver_nominal)
					_map_base, _map_args = _struct_base_and_args(_tid)
				except Exception:
					_recv_base = None
					_map_base = None
				if _recv_base is not None and _map_base is not None and _recv_base == _map_base:
					recv_tp_name = _name
					break
				try:
					_mapped_key = _normalize_type_key(type_key_from_typeid(ctx.type_table, _tid))
				except KeyError:
					continue
				if _mapped_key == _recv_key:
					recv_tp_name = _name
					break
		if recv_tp_name is None and ctx.preseed_type_params:
			recv_def = ctx.type_table.get(receiver_nominal)
			for _name, _tid in ctx.preseed_type_params.items():
				if isinstance(_tid, TypeParamId):
					continue
				try:
					_def = ctx.type_table.get(_tid)
				except Exception:
					continue
				if _def.kind == recv_def.kind and _def.name == recv_def.name and getattr(_def, "module_id", None) == getattr(recv_def, "module_id", None):
					recv_tp_name = _name
					break
		receiver_is_type_param = recv_type_param_id is not None
		if receiver_is_type_param:
			recv_type_key = _normalize_type_key(type_key_from_typeid(ctx.type_table, receiver_nominal))
		receiver_place = _receiver_place(expr.receiver)
		receiver_is_lvalue = receiver_place is not None
		receiver_can_mut_borrow = _receiver_can_mut_borrow(expr.receiver, receiver_place, recv_ty)
		recv_def_full = ctx.type_table.get(recv_ty)
		recv_is_ref = recv_def_full.kind is TypeKind.REF
		if receiver_is_type_param:
			if ctx.trait_index is None:
				diagnostics.append(_tc_diag(message=f"no matching method '{expr.method_name}' for receiver {_label_typeid(recv_ty)}", severity="error", span=getattr(expr, "loc", Span())))
				return MethodCallResult(ctx.unknown_ty, None)
			req_expr = _require_for_fn(ctx.current_fn_id)
			recv_owner = getattr(recv_type_param_id, "owner", None)
			trait_type_args_by_key: dict[TraitKey, list[TypeId]] = {}
			if req_expr is not None:
				for atom in _extract_conjunctive_facts(req_expr):
					subj = atom.subject
					subj_name = _subject_name(subj)
					if subj_name is None and isinstance(subj, TypeParamId):
						subj_name = ctx.type_param_names.get(subj)
					if subj_name is None and isinstance(subj, TypeParamId) and recv_tp_name is not None and recv_tp_name in ctx.type_param_map and not ctx.type_param_names:
						subj_name = recv_tp_name
					if recv_type_param_id is not None and isinstance(subj, TypeParamId) and recv_owner is not None and subj.index == recv_type_param_id.index and getattr(subj.owner, "module", None) == getattr(recv_owner, "module", None) and getattr(subj.owner, "name", None) == getattr(recv_owner, "name", None) and getattr(subj.owner, "ordinal", None) == getattr(recv_owner, "ordinal", None):
						subj = recv_type_param_id
					if subj_name is not None and ctx.type_param_map and subj_name in ctx.type_param_map:
						subj = ctx.type_param_map[subj_name]
					if subj != recv_type_param_id and (subj_name is None or recv_tp_name is None or subj_name != recv_tp_name):
						continue
					trait_key_req = trait_key_from_expr(atom.trait, default_module=ctx.current_module_name, default_package=ctx.default_package, module_packages=ctx.module_packages)
					arg_exprs = list(getattr(atom.trait, "args", []) or [])
					if arg_exprs:
						arg_ids = [resolve_opaque_type(a, ctx.type_table, module_id=trait_key_req.module or ctx.current_module_name, type_params=ctx.type_param_map) for a in arg_exprs]
					else:
						arg_ids = []
					trait_type_args_by_key[trait_key_req] = arg_ids
					if recv_type_param_id is not None:
						ctx.fn_require_assumed.add((recv_type_param_id, trait_key_req))
					if recv_type_key is not None:
						ctx.fn_require_assumed.add((recv_type_key, trait_key_req))
			if ctx.require_env_local is not None and ctx.require_env_local.trait_requires and ctx.trait_index is not None:
				for _base_key, _base_args in list(trait_type_args_by_key.items()):
					_req = ctx.require_env_local.trait_requires.get(_base_key)
					if _req is None:
						continue
					_trait_def = ctx.trait_index.traits_by_id.get(_base_key)
					_param_names = list(getattr(_trait_def, "type_params", []) or []) if _trait_def is not None else []
					_local_map = {name: _base_args[idx] for idx, name in enumerate(_param_names) if idx < len(_base_args)}
					for _atom in _extract_conjunctive_facts(_req):
						_req_key = trait_key_from_expr(_atom.trait, default_module=_base_key.module or ctx.current_module_name, default_package=ctx.default_package, module_packages=ctx.module_packages)
						_arg_exprs = list(getattr(_atom.trait, "args", []) or [])
						if _arg_exprs:
							_arg_ids = [resolve_opaque_type(a, ctx.type_table, module_id=_req_key.module or ctx.current_module_name, type_params=_local_map) for a in _arg_exprs]
						else:
							_arg_ids = []
						if _req_key not in trait_type_args_by_key:
							trait_type_args_by_key[_req_key] = _arg_ids
							if recv_type_param_id is not None:
								ctx.fn_require_assumed.add((recv_type_param_id, _req_key))
							if recv_type_key is not None:
								ctx.fn_require_assumed.add((recv_type_key, _req_key))
			missing_require_trait: TraitKey | None = None
			saw_method_in_scope = False
			matching_traits: list[TraitKey] = []
			scope_traits = traits_in_scope()
			_FN_SCOPE_TRAITS = {("std.core", "Fn0"), ("std.core", "Fn1"), ("std.core", "Fn2"), ("std.core", "FnThrow0"), ("std.core", "FnThrow1"), ("std.core", "FnThrow2")}
			_fn_require_keys = [k for k in trait_type_args_by_key if (getattr(k, "module", None), getattr(k, "name", None)) in _FN_SCOPE_TRAITS] if trait_type_args_by_key else []
			if not scope_traits and _fn_require_keys and (instantiation_mode or receiver_is_type_param):
				scope_traits = list(_fn_require_keys)
			elif receiver_is_type_param and not instantiation_mode and _fn_require_keys:
				_existing_keys = {(getattr(t, "module", None), getattr(t, "name", None)) for t in scope_traits}
				for _rk in _fn_require_keys:
					if (getattr(_rk, "module", None), getattr(_rk, "name", None)) not in _existing_keys:
						scope_traits.append(_rk)
			for trait_key in scope_traits:
				if ctx.trait_index.is_missing(trait_key):
					raise ResolutionError(f"missing trait metadata for '{_trait_label(trait_key)}'", span=getattr(expr, "loc", Span()))
				if not ctx.trait_index.has_method(trait_key, expr.method_name):
					continue
				saw_method_in_scope = True
				has_require_trait = (recv_type_param_id, trait_key) in ctx.fn_require_assumed or (recv_type_key, trait_key) in ctx.fn_require_assumed
				if not has_require_trait and ctx.fn_require_assumed:
					for _subj, _key in ctx.fn_require_assumed:
						if getattr(_key, "name", None) == getattr(trait_key, "name", None) and getattr(_key, "module", None) == getattr(trait_key, "module", None):
							has_require_trait = True
							break
				if not has_require_trait and trait_type_args_by_key:
					for _key in trait_type_args_by_key.keys():
						if getattr(_key, "name", None) == getattr(trait_key, "name", None) and getattr(_key, "module", None) == getattr(trait_key, "module", None):
							has_require_trait = True
							break
				if not has_require_trait and req_expr is not None:
					for _atom in _extract_conjunctive_facts(req_expr):
						_key = trait_key_from_expr(_atom.trait, default_module=ctx.current_module_name, default_package=ctx.default_package, module_packages=ctx.module_packages)
						if getattr(_key, "name", None) == getattr(trait_key, "name", None) and getattr(_key, "module", None) == getattr(trait_key, "module", None):
							has_require_trait = True
							break
				if not has_require_trait and ctx.trait_index is not None and ctx.fn_require_assumed:
					for _subj, _key in ctx.fn_require_assumed:
						if recv_type_param_id is not None and _subj != recv_type_param_id and _subj != recv_type_key:
							continue
						if recv_tp_name is not None and _subj == recv_tp_name:
							pass
						_trait_def = ctx.trait_index.traits_by_id.get(_key)
						_req_expr = getattr(_trait_def, "require_expr", None) if _trait_def is not None else None
						if _req_expr is None:
							continue
						for _atom in _extract_conjunctive_facts(_req_expr):
							_key2 = trait_key_from_expr(_atom.trait, default_module=_key.module or ctx.current_module_name, default_package=ctx.default_package, module_packages=ctx.module_packages)
							if getattr(_key2, "name", None) == getattr(trait_key, "name", None) and getattr(_key2, "module", None) == getattr(trait_key, "module", None):
								has_require_trait = True
								break
						if has_require_trait:
							break
				if not has_require_trait and instantiation_mode and req_expr is None and not ctx.fn_require_assumed and not trait_type_args_by_key:
					has_require_trait = True
				if not has_require_trait:
					if missing_require_trait is None:
						missing_require_trait = trait_key
					continue
				matching_traits.append(trait_key)
			if not matching_traits and instantiation_mode and ctx.trait_index is not None:
				if trait_type_args_by_key:
					for _key in trait_type_args_by_key.keys():
						if ctx.trait_index.has_method(_key, expr.method_name):
							matching_traits.append(_key)
					if matching_traits:
						pass
				inst_candidates: list[TraitKey] = []
				for _key in ctx.trait_index.traits_by_id.keys():
					if ctx.trait_index.has_method(_key, expr.method_name):
						inst_candidates.append(_key)
				if len(inst_candidates) == 1:
					matching_traits.append(inst_candidates[0])
				if not matching_traits and ctx.trait_impl_index is not None and receiver_nominal is not None and hasattr(ctx.trait_impl_index, "candidates_for_target_method"):
					for impl_cand in ctx.trait_impl_index.candidates_for_target_method(receiver_nominal, expr.method_name):
						if getattr(impl_cand, "trait", None) is not None:
							matching_traits.append(impl_cand.trait)
					if len(matching_traits) > 1:
						matching_traits = _dedupe_by_key(list(matching_traits), lambda item: getattr(item, "name", None) or item)
			if not matching_traits:
				if missing_require_trait is not None:
					diagnostics.append(_tc_diag(message=f"requirement not satisfied: expected {_trait_label(missing_require_trait)}", severity="error", span=getattr(expr, "loc", Span()), notes=[f"requirement_trait={_trait_label(missing_require_trait)}"]))
					return MethodCallResult(ctx.unknown_ty, None)
				if saw_method_in_scope:
					diagnostics.append(_tc_diag(message=f"no matching method '{expr.method_name}' for receiver {_label_typeid(recv_ty)}", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
				diagnostics.append(_tc_diag(message=f"no matching method '{expr.method_name}' for receiver {_label_typeid(recv_ty)}", severity="error", span=getattr(expr, "loc", Span())))
				return MethodCallResult(ctx.unknown_ty, None)
			if len(matching_traits) > 1:
				names = ", ".join(sorted(_trait_label(tr) for tr in matching_traits))
				raise ResolutionError(f"ambiguous method '{expr.method_name}' for receiver {_label_typeid(recv_ty)}; candidates from traits: {names}", span=getattr(expr, "loc", Span()), code="E-METHOD-AMBIGUOUS")
			trait_key = matching_traits[0]
			trait_def = ctx.trait_index.traits_by_id.get(trait_key)
			method_sig = None
			if trait_def is not None:
				for method in getattr(trait_def, "methods", []) or []:
					if getattr(method, "name", None) == expr.method_name:
						method_sig = method
						break
			if method_sig is None:
				diagnostics.append(_tc_diag(message=f"no matching method '{expr.method_name}' for receiver {_label_typeid(recv_ty)}", severity="error", span=getattr(expr, "loc", Span())))
				return MethodCallResult(ctx.unknown_ty, None)
			method_type_params = list(getattr(method_sig, "type_params", []) or [])
			local_type_param_map: dict[str, TypeId] = {"Self": receiver_nominal}
			trait_type_params = list(getattr(trait_def, "type_params", []) or []) if trait_def is not None else []
			trait_type_args = trait_type_args_by_key.get(trait_key, [])
			if not trait_type_args and trait_type_args_by_key:
				for key, vals in trait_type_args_by_key.items():
					if getattr(key, "name", None) == getattr(trait_key, "name", None) and getattr(key, "module", None) == getattr(trait_key, "module", None):
						trait_type_args = vals
						break
			if trait_type_params:
				if len(trait_type_args) != len(trait_type_params):
					diagnostics.append(_tc_diag(message=f"no matching method '{expr.method_name}' for receiver {_label_typeid(recv_ty)}", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
				for idx, name in enumerate(trait_type_params):
					local_type_param_map[name] = trait_type_args[idx]
			if method_type_params:
				if not type_arg_ids or len(type_arg_ids) != len(method_type_params):
					diagnostics.append(_tc_diag(message=f"type argument count mismatch for method '{expr.method_name}'", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
				for idx, name in enumerate(method_type_params):
					local_type_param_map[name] = type_arg_ids[idx]
			param_type_ids: list[TypeId] = []
			for param in list(getattr(method_sig, "params", []) or []):
				if param.type_expr is None:
					if param.name == "self":
						param_type_ids.append(receiver_nominal)
						continue
					diagnostics.append(_tc_diag(message="method parameter type missing", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
				param_type_ids.append(resolve_opaque_type(param.type_expr, ctx.type_table, module_id=trait_key.module or ctx.current_module_name, type_params=local_type_param_map))
			ret_id = resolve_opaque_type(method_sig.return_type, ctx.type_table, module_id=trait_key.module or ctx.current_module_name, type_params=local_type_param_map)
			direct_fn_id = None
			concrete_receiver = None
			if recv_tp_name is not None and ctx.type_param_map and recv_tp_name in ctx.type_param_map:
				_mapped = ctx.type_param_map[recv_tp_name]
				if not isinstance(_mapped, TypeParamId) and not ctx.type_table.has_typevar(_mapped):
					concrete_receiver = _mapped
			if concrete_receiver is None and recv_tp_name is not None and ctx.preseed_type_params and recv_tp_name in ctx.preseed_type_params:
				_mapped = ctx.preseed_type_params[recv_tp_name]
				if not isinstance(_mapped, TypeParamId) and not ctx.type_table.has_typevar(_mapped):
					concrete_receiver = _mapped
			if instantiation_mode and concrete_receiver is None:
				diagnostics.append(_tc_diag(message=f"no implementation for method '{expr.method_name}' on receiver {_label_typeid(recv_ty)}", severity="error", span=getattr(expr, "loc", Span())))
				return MethodCallResult(ctx.unknown_ty, None)
			if concrete_receiver is not None:
				_receiver_nominal_for_impl = _unwrap_ref_type(concrete_receiver)
				if ctx.trait_impl_index is not None and hasattr(ctx.trait_impl_index, "candidates_for_target_method"):
					for impl_cand in ctx.trait_impl_index.candidates_for_target_method(_receiver_nominal_for_impl, expr.method_name):
						if getattr(impl_cand, "trait", None) == trait_key:
							direct_fn_id = impl_cand.fn_id
							break
				if direct_fn_id is None and ctx.callable_registry is not None:
					diagnostics.append(_tc_diag(message=f"no implementation for method '{expr.method_name}' on receiver {_label_typeid(recv_ty)}", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
			call_target = CallTarget.direct(direct_fn_id) if direct_fn_id is not None else CallTarget.trait(trait_key, expr.method_name)
			_call_can_throw = not bool(getattr(method_sig, "declared_nothrow", False))
			_call_terminal = bool(getattr(method_sig, "declared_terminal_throws", False))
			info = CallInfo(target=call_target, sig=CallSig(param_types=tuple(param_type_ids), user_ret_type=ret_id, can_throw=_call_can_throw, declared_terminal_throws=_call_terminal))
			return MethodCallResult(ret_id, info)
		else:
			receiver_nominal_for_lookup = receiver_base if receiver_base is not None and receiver_args is not None else receiver_nominal
			visible_modules_for_methods = ctx.visible_modules or (ctx.current_module,)
			if instantiation_mode and ctx.module_ids_by_name:
				visible_modules_for_methods = tuple(ctx.module_ids_by_name.values())
			if ctx.module_ids_by_name is not None and any(isinstance(m, str) for m in visible_modules_for_methods):
				mapped: list[int] = []
				for m in visible_modules_for_methods:
					if isinstance(m, str):
						mid = ctx.module_ids_by_name.get(m)
						if mid is not None:
							mapped.append(mid)
					elif isinstance(m, int):
						mapped.append(m)
				visible_modules_for_methods = tuple(mapped)
			visible_modules_set = set(visible_modules_for_methods)
			ignore_visibility = getattr(expr, "origin", None) in ("for_iter", "for_next", "wrapper_call")
			def _collect_method_candidates(base_tid: TypeId) -> list[CallableDecl]:
				if ctx.callable_registry is None:
					return []
				cands = list(ctx.callable_registry.get_method_candidates_unscoped(receiver_nominal_type_id=base_tid, name=expr.method_name))
				if recv_is_ref and recv_ty != base_tid:
					cands.extend(ctx.callable_registry.get_method_candidates_unscoped(receiver_nominal_type_id=recv_ty, name=expr.method_name))
				if len(cands) <= 1:
					return cands
				seen: set[int] = set()
				deduped: list[CallableDecl] = []
				for cand in cands:
					cid = getattr(cand, "callable_id", None)
					key = cid if isinstance(cid, int) else id(cand)
					if key in seen:
						continue
					seen.add(key)
					deduped.append(cand)
				return deduped

			candidates = _collect_method_candidates(receiver_nominal_for_lookup)
			if not candidates and receiver_base is not None:
				candidates = _collect_method_candidates(receiver_base)
			if not candidates and ctx.impl_index is not None and receiver_nominal_for_lookup is not None and hasattr(ctx.impl_index, "get_candidates"):
				fallback_candidates: list[CallableDecl] = []
				for impl_cand in ctx.impl_index.get_candidates(receiver_nominal_for_lookup, expr.method_name):
					if impl_cand.fn_id is None:
						continue
					sig_local = ctx.signatures_by_id.get(impl_cand.fn_id) if ctx.signatures_by_id is not None else None
					if sig_local is None:
						continue
					if sig_local.return_type_id is None or sig_local.param_type_ids is None:
						continue
					self_mode = {
						"value": SelfMode.SELF_BY_VALUE,
						"ref": SelfMode.SELF_BY_REF,
						"ref_mut": SelfMode.SELF_BY_REF_MUT,
					}.get(sig_local.self_mode)
					if self_mode is None:
						continue
					template_signature = None
					if (sig_local.type_params or getattr(sig_local, "impl_type_params", [])) and sig_local.param_types is not None and sig_local.return_type is not None:
						template_signature = CallableTemplateSignature(param_types=tuple(sig_local.param_types), result_type=sig_local.return_type)
					fallback_candidates.append(
						CallableDecl(
							callable_id=impl_cand.impl_id,
							name=impl_cand.name,
							kind=CallableKind.METHOD_INHERENT,
							module_id=impl_cand.def_module_id,
							visibility=Visibility.public() if impl_cand.is_pub else Visibility.private(),
							signature=CallableSignature(param_types=tuple(sig_local.param_type_ids), result_type=sig_local.return_type_id),
							template_signature=template_signature,
							fn_id=impl_cand.fn_id,
							impl_id=impl_cand.impl_id,
							impl_target_type_id=receiver_nominal_for_lookup,
							self_mode=self_mode,
							template_type_params=tuple(tp.name for tp in (sig_local.type_params or [])),
							template_impl_type_params=tuple(tp.name for tp in (getattr(sig_local, "impl_type_params", []) or [])),
							is_generic=bool(sig_local.type_params or getattr(sig_local, "impl_type_params", [])),
						)
					)
				if fallback_candidates:
					candidates = fallback_candidates
			trait_candidates: list[CallableDecl] = []
			inherent_candidates: list[CallableDecl] = []
			traits_in_scope_set = set(traits_in_scope()) if traits_in_scope is not None else set()
			trait_impl_fn_id_to_trait: dict[FunctionId, TraitKey] = {}
			trait_impl_fn_id_to_require: dict[FunctionId, object] = {}
			if ctx.trait_impl_index is not None and receiver_nominal_for_lookup is not None and hasattr(ctx.trait_impl_index, "candidates_for_target_method"):
				for impl_cand in ctx.trait_impl_index.candidates_for_target_method(receiver_nominal_for_lookup, expr.method_name):
					trait_impl_fn_id_to_trait[impl_cand.fn_id] = impl_cand.trait
					if getattr(impl_cand, "require_expr", None) is not None:
						trait_impl_fn_id_to_require[impl_cand.fn_id] = impl_cand.require_expr
			is_generic_context = receiver_base is not None and receiver_args is not None and any(ctx.type_table.has_typevar(t) for t in receiver_args)
			for cand in candidates:
				inst_subst_args: list[TypeId] | None = None
				trait_key_for_cand = None
				if ctx.trait_impl_index is not None and cand.fn_id is not None and hasattr(ctx.trait_impl_index, "trait_key_for_fn_id"):
					trait_key_for_cand = ctx.trait_impl_index.trait_key_for_fn_id(cand.fn_id)
				if trait_key_for_cand is None and cand.fn_id is not None:
					trait_key_for_cand = trait_impl_fn_id_to_trait.get(cand.fn_id)
				if trait_key_for_cand is None and cand.fn_id is not None and ctx.trait_index is not None:
					name_parts = str(cand.fn_id.name).split("::")
					if len(name_parts) >= 3:
						trait_name = name_parts[-2]
						for _tkey in ctx.trait_index.traits_by_id.keys():
							if getattr(_tkey, "name", None) == trait_name:
								trait_key_for_cand = _tkey
								break
				if trait_key_for_cand is not None and ctx.trait_index is not None and ctx.trait_index.is_missing(trait_key_for_cand):
					raise ResolutionError(f"missing trait metadata for '{_trait_label(trait_key_for_cand)}'", span=getattr(expr, "loc", Span()))
				if trait_key_for_cand is not None:
					# Trait-in-scope gate: compiler-generated calls (wrapper_call,
					# for_iter, for_next) bypass because they target known-valid
					# methods from the package compilation context.
					if not (ignore_visibility or trait_key_for_cand in traits_in_scope_set):
						continue
					# Module visibility: for user-written code, also require
					# the impl-defining module is visible (K36 ensures package
					# modules are in the visible set).  Compiler-generated calls
					# bypass since the defining module may be internal to the package.
					if ignore_visibility or _candidate_visible(cand, visible_modules_set=visible_modules_set, current_module_id=ctx.current_module) or _is_prelude_type_method(cand, ctx.type_table):
						trait_candidates.append(cand)
					continue
				is_visible = True if ignore_visibility else (_candidate_visible(cand, visible_modules_set=visible_modules_set, current_module_id=ctx.current_module) or _is_prelude_type_method(cand, ctx.type_table))
				if cand.kind is CallableKind.METHOD_INHERENT:
					if is_visible:
						inherent_candidates.append(cand)
				else:
					if traits_in_scope_set and cand.kind is CallableKind.METHOD_TRAIT:
						if is_visible:
							trait_candidates.append(cand)
			if inherent_candidates:
				candidates = inherent_candidates
			elif trait_candidates:
				candidates = trait_candidates
			else:
				candidates = []
			require_failures: list[ProofFailure] = []
			require_info: dict[object, tuple[parser_ast.TraitExpr, dict[object, object], str, dict[TypeParamId, tuple[str, int]]]] = {}
			saw_require_failed = False
			require_missing_label: str | None = None
			if ctx.require_env_local is not None and candidates:
				filtered_candidates: list[CallableDecl] = []
				for cand in candidates:
					req_expr = trait_impl_fn_id_to_require.get(cand.fn_id) if cand.fn_id is not None else None
					if req_expr is None:
						req_expr = _require_for_fn(cand.fn_id if cand.fn_id is not None else None)
					if req_expr is None:
						filtered_candidates.append(cand)
						continue
					if any(isinstance(a, H.HLambda) for a in expr.args):
						atoms = _extract_conjunctive_facts(req_expr)
						if atoms and all(isinstance(a, parser_ast.TraitIs) and getattr(a.trait, "name", None) in {"Fn0", "Fn1", "Fn2", "FnThrow0", "FnThrow1", "FnThrow2"} for a in atoms):
							filtered_candidates.append(cand)
							continue
					world = ctx.global_trait_world or ctx.visible_trait_world
					if world is None:
						filtered_candidates.append(cand)
						continue
					if receiver_args and isinstance(req_expr, parser_ast.TraitIs):
						_req_key = trait_key_from_expr(req_expr.trait, default_module=ctx.current_module_name, default_package=ctx.default_package, module_packages=ctx.module_packages)
						if getattr(_req_key, "name", None) == "Copy" and ctx.type_table.is_copy(receiver_args[0]):
							filtered_candidates.append(cand)
							continue
					env = TraitEnv(default_module=(cand.fn_id.module if cand.fn_id is not None else ctx.current_module_name), default_package=ctx.default_package, module_packages=ctx.module_packages or {}, assumed_true=set(ctx.fn_require_assumed), type_table=ctx.type_table)
					subjects: set[object] = set()
					_collect_trait_subjects(req_expr, subjects)
					subst: dict[object, object] = {}
					sig_local = ctx.signatures_by_id.get(cand.fn_id) if cand.fn_id is not None and ctx.signatures_by_id is not None else None
					if sig_local is None:
						sig_local = _sig_from_decl_template(ctx, cand, current_module_name)
					impl_subst_req = None
					if sig_local and receiver_args is not None:
						impl_params = list(getattr(sig_local, "impl_type_params", []) or [])
						impl_args = list(getattr(sig_local, "impl_target_type_args", None) or [])
						if impl_args and impl_params:
							impl_subst_req = _match_impl_type_args(template_args=impl_args, recv_args=list(receiver_args), impl_type_params=impl_params)
						if impl_subst_req is None and impl_params and len(impl_params) == len(receiver_args):
							impl_subst_req = Subst(owner=impl_params[0].id.owner, args=list(receiver_args))
					if impl_subst_req is not None:
						for idx, tp in enumerate(impl_params):
							if idx < len(impl_subst_req.args):
								if ctx.type_table.has_typevar(impl_subst_req.args[idx]):
									continue
								key = _normalize_type_key(type_key_from_typeid(ctx.type_table, impl_subst_req.args[idx]))
								subst[tp.id] = key
								subst[tp.name] = key
					self_mode_for_infer = _self_mode_from_sig(sig_local) if sig_local is not None else None
					inferred_recv_ty = _infer_receiver_arg_type(self_mode_for_infer, recv_ty, receiver_is_lvalue=receiver_is_lvalue, receiver_can_mut_borrow=receiver_can_mut_borrow)
					arg_types_with_recv = [inferred_recv_ty] + list(arg_types)
					if sig_local and getattr(sig_local, "type_params", None) and receiver_args is not None:
						type_params = list(getattr(sig_local, "type_params", []) or [])
						for idx, tp in enumerate(type_params):
							if idx < len(receiver_args) and (tp.id in subjects or tp.name in subjects):
								if ctx.type_table.has_typevar(receiver_args[idx]):
									continue
								key = _normalize_type_key(type_key_from_typeid(ctx.type_table, receiver_args[idx]))
								subst[tp.id] = key
								subst[tp.name] = key
						if sig_local is not None and getattr(sig_local, "type_params", None):
							inst_res = ctx.instantiate_sig_with_subst(sig=sig_local, arg_types=arg_types_with_recv, expected_type=expected_type, explicit_type_args=type_arg_ids, allow_infer=True, diag_span=call_type_args_span, call_kind="method", call_name=expr.method_name, receiver_type=inferred_recv_ty)
							if getattr(inst_res, "subst", None) is not None:
								inst_args = list(getattr(inst_res.subst, "args", []) or [])
								for idx, tp in enumerate(list(getattr(sig_local, "type_params", []) or [])):
									if idx < len(inst_args) and tp.id not in subst and tp.name not in subst:
										if ctx.type_table.has_typevar(inst_args[idx]):
											continue
										key = _normalize_type_key(type_key_from_typeid(ctx.type_table, inst_args[idx]))
										subst[tp.id] = key
										subst[tp.name] = key
					if sig_local and sig_local.param_names:
						for idx, pname in enumerate(sig_local.param_names):
							if pname in subst:
								continue
							if pname in subjects and idx < len(arg_types_with_recv):
								key = _normalize_type_key(type_key_from_typeid(ctx.type_table, arg_types_with_recv[idx]))
								subst[pname] = key
					if sig_local is not None and any(isinstance(a, H.HLambda) for a in expr.args):
						local_param_map: dict[str, TypeId] = {}
						if sig_local.impl_type_params and receiver_args is not None:
							for idx, tp in enumerate(sig_local.impl_type_params):
								if idx < len(receiver_args):
									local_param_map[tp.name] = receiver_args[idx]
						inst_res = None
						if sig_local is not None and getattr(sig_local, "type_params", None):
							inst_res = ctx.instantiate_sig_with_subst(sig=sig_local, arg_types=arg_types_with_recv, expected_type=expected_type, explicit_type_args=type_arg_ids, allow_infer=True, diag_span=call_type_args_span, call_kind="method", call_name=expr.method_name, receiver_type=inferred_recv_ty)
							if getattr(inst_res, "subst", None) is not None:
								inst_args = list(getattr(inst_res.subst, "args", []) or [])
								for idx, tp in enumerate(list(getattr(sig_local, "type_params", []) or [])):
									if idx < len(inst_args):
										local_param_map[tp.name] = inst_args[idx]
						def _fn_trait_expected(trait_name: str) -> tuple[int, bool] | None:
							if trait_name == "Fn0":
								return (0, False)
							if trait_name == "Fn1":
								return (1, False)
							if trait_name == "Fn2":
								return (2, False)
							if trait_name == "FnThrow0":
								return (0, True)
							if trait_name == "FnThrow1":
								return (1, True)
							if trait_name == "FnThrow2":
								return (2, True)
							return None
						def _param_index_for_subject(subj: object) -> int | None:
							if sig_local is None or not sig_local.param_type_ids:
								return None
							subj_name = _subject_name(subj)
							subj_id = subj if isinstance(subj, TypeParamId) else None
							for idx, tid in enumerate(sig_local.param_type_ids):
								td_local = ctx.type_table.get(tid)
								if td_local.kind is not TypeKind.TYPEVAR:
									continue
								tp_id = td_local.type_param_id
								if subj_id is not None and tp_id == subj_id:
									return idx
								if subj_name is not None:
									if ctx.type_param_names and tp_id in ctx.type_param_names and ctx.type_param_names[tp_id] == subj_name:
										return idx
									for tp in list(getattr(sig_local, "type_params", []) or []):
										if tp.id == tp_id and tp.name == subj_name:
											return idx
							return None
						for atom in _extract_conjunctive_facts(req_expr):
							if not isinstance(atom, parser_ast.TraitIs):
								continue
							trait_name = getattr(atom.trait, "name", None)
							expect = _fn_trait_expected(trait_name) if trait_name is not None else None
							if expect is None:
								continue
							param_count, can_throw = expect
							param_idx = _param_index_for_subject(atom.subject)
							if param_idx is None:
								continue
							arg_idx = param_idx
							if sig_local.param_names and sig_local.param_names[0] == "self":
								arg_idx = param_idx - 1
							if arg_idx < 0 or arg_idx >= len(expr.args):
								continue
							arg = expr.args[arg_idx]
							if not isinstance(arg, H.HLambda):
								continue
							arg_ty = arg_types[arg_idx] if arg_idx < len(arg_types) else None
							if arg_ty is not None and arg_ty != ctx.unknown_ty and not ctx.type_table.has_typevar(arg_ty):
								continue
							trait_args = list(getattr(atom.trait, "args", []) or [])
							if len(trait_args) != param_count + 1:
								continue
							param_types: list[TypeId] = []
							for texpr in trait_args[:param_count]:
								param_types.append(resolve_opaque_type(texpr, ctx.type_table, module_id=ctx.current_module_name, type_params=local_param_map))
							ret_ty = resolve_opaque_type(trait_args[param_count], ctx.type_table, module_id=ctx.current_module_name, type_params=local_param_map)
							arg_expected_type = ctx.type_table.ensure_function(param_types, ret_ty, can_throw=can_throw)
							arg.allow_capture_invoke = True
							arg.expected_fn_inferred = True
							arg.expected_type_from_require = arg_expected_type
							arg_types[arg_idx] = arg_expected_type
					if sig_local is not None and getattr(sig_local, "type_params", None) and arg_types:
						type_params = list(getattr(sig_local, "type_params", []) or [])
						name_to_idx = {tp.name: idx for idx, tp in enumerate(type_params)}
						id_to_idx = {tp.id: idx for idx, tp in enumerate(type_params)}
						fn_arg = arg_types[0]
						fn_def = ctx.type_table.get(fn_arg)
						if fn_def.kind is TypeKind.FUNCTION and fn_def.param_types:
							for atom in _extract_conjunctive_facts(req_expr):
								if not isinstance(atom, parser_ast.TraitIs):
									continue
								trait_name = getattr(atom.trait, "name", None)
								if trait_name not in {"Fn0", "Fn1", "Fn2", "FnThrow0", "FnThrow1", "FnThrow2"}:
									continue
								subj_idx = None
								if isinstance(atom.subject, TypeParamId) and atom.subject in id_to_idx:
									subj_idx = id_to_idx[atom.subject]
								else:
									subj_name = _subject_name(atom.subject)
									if subj_name is not None and subj_name in name_to_idx:
										subj_idx = name_to_idx[subj_name]
								if subj_idx is not None:
									subst[type_params[subj_idx].id] = _normalize_type_key(type_key_from_typeid(ctx.type_table, fn_arg))
									subst[type_params[subj_idx].name] = _normalize_type_key(type_key_from_typeid(ctx.type_table, fn_arg))
								trait_args = list(getattr(atom.trait, "args", []) or [])
								if not trait_args:
									continue
								ret_ty = fn_def.param_types[-1]
								arg0 = trait_args[0]
								arg0_name = getattr(arg0, "name", None)
								if arg0_name is not None and arg0_name in name_to_idx:
									tp_idx = name_to_idx[arg0_name]
									key = _normalize_type_key(type_key_from_typeid(ctx.type_table, ret_ty))
									subst[type_params[tp_idx].id] = key
									subst[type_params[tp_idx].name] = key
					res = prove_expr(world, env, subst, req_expr)
					if res.status is not ProofStatus.PROVED:
						if res.status is ProofStatus.UNKNOWN:
							if _require_unknown_defer(ctx, arg_types=arg_types, receiver_args=list(receiver_args) if receiver_args is not None else None, type_arg_ids=type_arg_ids):
								filtered_candidates.append(cand)
								cand_key = _candidate_key_for_decl(cand)
								if sig_local is not None:
									scope_map = _param_scope_map(sig_local)
								else:
									scope_map = {}
								req_mod = cand.fn_id.module if cand.fn_id is not None else ctx.current_module_name
								require_info[cand_key] = (req_expr, subst, req_mod, scope_map)
								continue
						saw_require_failed = True
						if require_missing_label is None:
							_atoms = _extract_conjunctive_facts(req_expr)
							_req_type = _atoms[0].trait if _atoms else req_expr
							_req_key = trait_key_from_expr(_req_type, default_module=ctx.current_module_name, default_package=ctx.default_package, module_packages=ctx.module_packages)
							require_missing_label = _trait_label(_req_key)
						origin = ObligationOrigin(kind=ObligationOriginKind.CALLEE_REQUIRE, label=f"method '{expr.method_name}'", span=Span.from_loc(getattr(req_expr, "loc", None)))
						failure = _require_failure(req_expr=req_expr, subst=subst, origin=origin, span=call_type_args_span or getattr(expr, "loc", Span()), env=env, world=world, result=res)
						if failure is not None:
							require_failures.append(failure)
						continue
					filtered_candidates.append(cand)
					cand_key = _candidate_key_for_decl(cand)
					if sig_local is not None:
						scope_map = _param_scope_map(sig_local)
					else:
						scope_map = {}
					req_mod = cand.fn_id.module if cand.fn_id is not None else ctx.current_module_name
					require_info[cand_key] = (req_expr, subst, req_mod, scope_map)
				candidates = filtered_candidates
			subst_for_receiver: Subst | None = None
			receiver_inst_args: list[TypeId] | None = None
			if receiver_nominal is not None:
				receiver_inst_ty = ctx.unwrap_ref_type(receiver_nominal)
				receiver_inst = ctx.type_table.get_struct_instance(receiver_inst_ty)
				if receiver_inst is not None:
					receiver_inst_args = list(receiver_inst.type_args)
				else:
					receiver_vinst = ctx.type_table.get_variant_instance(receiver_inst_ty)
					if receiver_vinst is not None:
						receiver_inst_args = list(receiver_vinst.type_args)
			if receiver_base is not None and receiver_args is not None and receiver_args:
				param_ids = ctx.type_table.get_struct_type_param_ids(receiver_base)
				if param_ids and len(param_ids) == len(receiver_args):
					subst_for_receiver = Subst(owner=param_ids[0].owner, args=list(receiver_args))
			param_types_for_receiver: list[tuple[CallableDecl, CallableSignature, list[TypeId], Subst | None]] = []
			for cand in candidates:
				sig = cand.signature
				if sig is None:
					continue
				param_type_ids = list(sig.param_types or ())
				impl_subst: Subst | None = None
				if cand.fn_id is not None and receiver_args is not None:
					fn_sig = ctx.signatures_by_id.get(cand.fn_id) if ctx.signatures_by_id is not None else None
					if fn_sig is None:
						fn_sig = _sig_from_decl_template(ctx, cand, current_module_name)
					impl_args = list(getattr(fn_sig, "impl_target_type_args", None) or [])
					impl_type_params = list(getattr(fn_sig, "impl_type_params", None) or [])
					if impl_args and impl_type_params:
						impl_subst = _match_impl_type_args(template_args=impl_args, recv_args=list(receiver_args), impl_type_params=impl_type_params)
					if impl_subst is None and impl_type_params and receiver_args is not None and len(impl_type_params) == len(receiver_args):
						impl_subst = Subst(owner=impl_type_params[0].id.owner, args=list(receiver_args))
					if impl_subst is None and impl_type_params and receiver_inst_args is not None and len(impl_type_params) == len(receiver_inst_args):
						impl_subst = Subst(owner=impl_type_params[0].id.owner, args=list(receiver_inst_args))
					if impl_subst is not None:
						param_type_ids = [apply_subst(p, impl_subst, ctx.type_table) for p in param_type_ids]
				if subst_for_receiver is not None:
					param_type_ids = [apply_subst(p, subst_for_receiver, ctx.type_table) for p in param_type_ids]
				param_types_for_receiver.append((cand, sig, param_type_ids, impl_subst))
			# Tuple shape:
			#   0: cand                          (CallableDecl)
			#   1: sig                           (CallableSignature)
			#   2: param_type_ids                (list[TypeId], post-impl-subst)
			#   3: needs_autoborrow              (SelfMode | None)
			#   4: wants_mut_ref                 (bool)
			#   5: pref                          (int — receiver-preference rank)
			#   6: impl_subst                    (Subst | None)
			#   7: exact_param_match             (bool — non-receiver param-type match for the call's args)
			#   8: method_has_own_type_params    (bool — method-level <T>, not impl-block-level)
			receiver_candidates: list[tuple[CallableDecl, CallableSignature, list[TypeId], Optional[SelfMode], bool, int, Subst | None, bool, bool]] = []
			had_autoborrow_place_error = False
			saw_typed_nongeneric_with_type_args = False
			type_arg_counts: set[int] = set()
			for cand, sig, param_type_ids, impl_subst in param_types_for_receiver:
				if not param_type_ids:
					continue
				if type_arg_ids and sig is not None:
					fn_sig = ctx.signatures_by_id.get(cand.fn_id) if cand.fn_id is not None and ctx.signatures_by_id is not None else None
					if fn_sig is None:
						fn_sig = _sig_from_decl_template(ctx, cand, current_module_name)
					if fn_sig is not None:
						if not list(getattr(fn_sig, "type_params", []) or []):
							saw_typed_nongeneric_with_type_args = True
							continue
						if type_arg_ids and len(type_arg_ids) != len(getattr(fn_sig, "type_params", []) or []):
							type_arg_counts.add(len(getattr(fn_sig, "type_params", []) or []))
							continue
				self_mode = _self_mode_from_sig(sig)
				if len(param_type_ids) != len(arg_types) + 1:
					continue
				compat_ok, needs_autoborrow = _receiver_compat(recv_ty, param_type_ids[0], self_mode)
				if not compat_ok:
					continue
				if needs_autoborrow is not None and not receiver_is_lvalue:
					allow_rvalue_shared = (
						needs_autoborrow is SelfMode.SELF_BY_REF
						and isinstance(expr, H.HMethodCall)
					)
					if needs_autoborrow is SelfMode.SELF_BY_REF_MUT or not allow_rvalue_shared:
						if isinstance(expr, H.HMethodCall):
							had_autoborrow_place_error = True
						continue
				if self_mode is None:
					continue
				wants_mut_ref = self_mode.name == "SELF_BY_REF_MUT"
				pref = _receiver_preference(self_mode, receiver_is_lvalue=receiver_is_lvalue, receiver_can_mut_borrow=receiver_can_mut_borrow, autoborrow=needs_autoborrow)
				if pref is None:
					continue
				# Parameter-type overload disambiguation: an exact-match overload
				# is preferred over one that only matches by arity + receiver
				# compatibility.  This lets methods on the same receiver share a
				# name and disambiguate by argument types, the same way the
				# free-function overload resolver in lang/driftc/method_resolver.py
				# does.  See receiver_candidates[][7] consumer below.
				#
				# A non-receiver argument matches a parameter when either:
				#   (a) arg_type == param_type, OR
				#   (b) param_type is `&T` and arg_type == T (call-site auto-borrow).
				# Both forms count as "exact" for overload selection — what
				# matters is that the user-supplied argument unambiguously
				# identifies one overload regardless of whether they wrote `&x`
				# or `x`.
				#
				# Method-level generic methods (those with their OWN type
				# parameters, e.g. `pub fn pick<T>(self, x: T)`) are treated
				# as automatically passing the exact-match check, because
				# their parameter types contain unresolved type variables at
				# this point.  The downstream "concrete beats generic" filter
				# (see receiver_candidates[][7] consumer) will pick a more
				# specific concrete overload over the generic fallback when
				# both exist.  Impl-block-level generics (e.g.
				# `implement<T> Box<T> { fn poke(self, n: Int) }`) are NOT
				# treated as method-level generic — by this point param_type_ids
				# has already had impl_subst applied (line 2521), so the
				# regular exact-match check works on concrete types.
				#
				# Method-level type parameters live on the richer fn_sig from
				# ctx.signatures_by_id, not on the simple CallableSignature in
				# `sig` (which only has param_types/result_type).  We look up
				# fn_sig the same way the type_arg_ids block above does.
				def _arg_matches_param(_param_ty: TypeId, _arg_ty: TypeId) -> bool:
					if _param_ty == _arg_ty:
						return True
					_param_unwrapped = ctx.unwrap_ref_type(_param_ty)
					return _param_unwrapped == _arg_ty
				_fn_sig_for_overload = (
					ctx.signatures_by_id.get(cand.fn_id)
					if cand.fn_id is not None and ctx.signatures_by_id is not None
					else None
				)
				if _fn_sig_for_overload is None:
					_fn_sig_for_overload = _sig_from_decl_template(ctx, cand, current_module_name)
				method_has_own_type_params = bool(
					_fn_sig_for_overload is not None
					and (getattr(_fn_sig_for_overload, "type_params", None) or [])
				)
				if method_has_own_type_params:
					exact_param_match = True
				else:
					exact_param_match = all(
						_arg_matches_param(param_type_ids[1 + _i], arg_types[_i])
						for _i in range(len(arg_types))
					)
				receiver_candidates.append((cand, sig, param_type_ids, needs_autoborrow, wants_mut_ref, pref, impl_subst, exact_param_match, method_has_own_type_params))
			if not receiver_candidates:
				if getattr(expr, "origin", None) == "for_iter" and ctx.signatures_by_id is not None:
					array_base_id = ctx.type_table.array_base_id()
					local_receiver_args = receiver_args
					if local_receiver_args is None:
						recv_def_local = ctx.type_table.get(recv_ty)
						if recv_def_local.kind is TypeKind.REF and recv_def_local.param_types:
							inner = recv_def_local.param_types[0]
							inner_def = ctx.type_table.get(inner)
							if inner_def.kind is TypeKind.ARRAY and inner_def.param_types:
								local_receiver_args = list(inner_def.param_types)
					for fn_id, sig in ctx.signatures_by_id.items():
						if not getattr(sig, "is_method", False):
							continue
						if (sig.method_name or sig.name) != expr.method_name:
							continue
						impl_tid = sig.impl_target_type_id
						if impl_tid is None:
							continue
						impl_def = ctx.type_table.get(impl_tid)
						if impl_def.kind is TypeKind.REF and impl_def.param_types:
							impl_tid = impl_def.param_types[0]
							impl_def = ctx.type_table.get(impl_tid)
						if impl_def.kind is TypeKind.ARRAY:
							impl_tid = array_base_id
						if impl_tid != array_base_id:
							continue
						self_mode = _self_mode_from_sig(sig)
						if self_mode is None:
							continue
						param_type_ids = list(sig.param_type_ids or [])
						ret_id = sig.return_type_id or ctx.unknown_ty
						impl_subst = None
						impl_args = list(getattr(sig, "impl_target_type_args", None) or [])
						impl_type_params = list(getattr(sig, "impl_type_params", None) or [])
						if impl_args and impl_type_params and local_receiver_args is not None:
							impl_subst = _match_impl_type_args(template_args=impl_args, recv_args=list(local_receiver_args), impl_type_params=impl_type_params)
						if impl_subst is not None:
							param_type_ids = [apply_subst(p, impl_subst, ctx.type_table) for p in param_type_ids]
							ret_id = apply_subst(ret_id, impl_subst, ctx.type_table)
						compat_ok, needs_autoborrow = _receiver_compat(recv_ty, param_type_ids[0], self_mode)
						if not compat_ok:
							continue
						wants_mut_ref = self_mode.name == "SELF_BY_REF_MUT"
						pref = _receiver_preference(self_mode, receiver_is_lvalue=receiver_is_lvalue, receiver_can_mut_borrow=receiver_can_mut_borrow, autoborrow=needs_autoborrow)
						if pref is None:
							continue
						target_fn_id = fn_id
						can_throw = True
						if sig.declared_can_throw is not None:
							can_throw = bool(sig.declared_can_throw)
						info = _call_info_target(list(param_type_ids), ret_id, can_throw, CallTarget.direct(target_fn_id), declared_terminal_throws=bool(getattr(sig, "declared_terminal_throws", False)))
						return MethodCallResult(ret_id, info, None)
				if had_autoborrow_place_error:
					diagnostics.append(
						_tc_diag(
							message="borrow requires an addressable place; bind to a local first",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return MethodCallResult(ctx.unknown_ty, None)
				if receiver_nominal_for_lookup is not None and ctx.callable_registry is not None:
					hidden = _collect_method_candidates(receiver_nominal_for_lookup)
					visible_modules = visible_modules_for_methods or ()
					visible_modules_set = set(visible_modules)
					hidden_not_visible = [
						cand for cand in hidden
						if not (_candidate_visible(cand, visible_modules_set=visible_modules_set, current_module_id=ctx.current_module) or _is_prelude_type_method(cand, ctx.type_table))
					]
					if hidden_not_visible:
						hidden_decl = hidden_not_visible[0]
						label = None
						visibility_provenance = getattr(ctx, "visibility_provenance", None)
						mod_chain = visibility_provenance.get(hidden_decl.module_id) if visibility_provenance is not None else None
						mod_name = None
						if mod_chain:
							mod_name = mod_chain[-1]
						elif hidden_decl.fn_id is not None and getattr(hidden_decl.fn_id, "module", None):
							mod_name = hidden_decl.fn_id.module
						elif ctx.module_ids_by_name is not None:
							for name, mid in ctx.module_ids_by_name.items():
								if mid == hidden_decl.module_id:
									mod_name = name
									break
						if mod_name is None:
							mod_name = str(hidden_decl.module_id)
						if hidden_decl.kind is CallableKind.METHOD_TRAIT and hidden_decl.trait_id is not None and ctx.trait_key_for_id is not None:
							trait_key = ctx.trait_key_for_id(hidden_decl.trait_id)
							label = f"{_trait_label(trait_key)}@{mod_name}"
						elif label is None and ctx.trait_impl_index is not None and hidden_decl.fn_id is not None and hasattr(ctx.trait_impl_index, "trait_key_for_fn_id"):
							trait_key = ctx.trait_impl_index.trait_key_for_fn_id(hidden_decl.fn_id)
							if trait_key is not None:
								label = f"{_trait_label(trait_key)}@{mod_name}"
						elif mod_name:
							label = mod_name
						note = _visibility_note(hidden_decl.module_id) if hidden_decl is not None else None
						notes = [note] if note else []
						msg = f"method '{expr.method_name}' exists but is not visible here"
						if label:
							msg = f"{msg} ({label})"
						diagnostics.append(_tc_diag(message=msg, severity="error", span=getattr(expr, "loc", Span()), notes=notes))
						return MethodCallResult(ctx.unknown_ty, None)
				if saw_require_failed:
					if require_failures:
						failure = _pick_best_failure(require_failures)
						msg = _format_failure_message(failure) if failure is not None else "requirement not satisfied"
						notes = ctx.requirement_notes(failure) if failure is not None and getattr(ctx, "requirement_notes", None) is not None else []
						diagnostics.append(_tc_diag(message=msg, severity="error", span=getattr(expr, "loc", Span()), code=_failure_code(failure) if failure is not None else None, notes=notes))
					else:
						if require_missing_label is not None:
							diagnostics.append(_tc_diag(message=f"requirement not satisfied: expected {require_missing_label}", severity="error", span=getattr(expr, "loc", Span()), notes=[f"requirement_trait={require_missing_label}"]))
						else:
							diagnostics.append(_tc_diag(message="requirement not satisfied", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
				if type_arg_ids:
					if type_arg_counts:
						exp = ", ".join(str(n) for n in sorted(type_arg_counts))
						diagnostics.append(_tc_diag(message=f"type argument count mismatch for method '{expr.method_name}': expected one of ({exp}), got {len(type_arg_ids)}", severity="error", span=call_type_args_span or getattr(expr, "loc", Span())))
						return MethodCallResult(ctx.unknown_ty, None, None)
					if saw_typed_nongeneric_with_type_args:
						diagnostics.append(_tc_diag(message=f"type arguments require a generic signature for method '{expr.method_name}'", severity="error", span=call_type_args_span or getattr(expr, "loc", Span())))
						return MethodCallResult(ctx.unknown_ty, None, None)
				if ctx.trait_index is not None and traits_in_scope is not None:
					for _trait_key in traits_in_scope():
						if ctx.trait_index.is_missing(_trait_key):
							diagnostics.append(_tc_diag(message=f"missing trait metadata for '{_trait_label(_trait_key)}'", severity="error", span=getattr(expr, "loc", Span())))
							return MethodCallResult(ctx.unknown_ty, None)
				diagnostics.append(_tc_diag(message=f"no matching method '{expr.method_name}' for receiver {_label_typeid(recv_ty)}", severity="error", span=getattr(expr, "loc", Span())))
				return MethodCallResult(ctx.unknown_ty, None)
			max_pref = max(item[5] for item in receiver_candidates)
			pref_candidates = [item for item in receiver_candidates if item[5] == max_pref]
			# Parameter-type overload disambiguation: if any surviving
			# candidate has an exact match on its non-receiver parameter
			# types, drop the candidates that only matched on arity +
			# receiver compatibility.  Receiver auto-borrow is silent
			# ergonomics; argument type at the call site is the stronger
			# expression of user intent, so param-type exact match
			# dominates.
			#
			# Among exact-match candidates, a method WITHOUT its own
			# type parameters always beats a METHOD-level generic
			# fallback.  Method-level generic candidates are flagged as
			# "tentatively exact" upstream (since their parameter types
			# contain unresolved type variables at this point), but the
			# user's concrete-typed argument is stronger evidence than
			# a tentative type-parameter binding.  This makes the
			# canonical "concrete + generic fallback" pattern resolve
			# correctly:
			#
			#     pub fn pick(self: &Box, k: &String) -> Int { ... }
			#     pub fn pick<T>(self: &Box, k: T) -> Int { ... }
			#     b.pick("hello")  // → concrete &String overload
			#     b.pick(42)       // → generic fallback
			#
			# Note: this preference uses METHOD-level type params only
			# (`sig.type_params`), not impl-block-level type params
			# (`sig.impl_type_params`).  Impl-block specificity (e.g.
			# `implement<T> Box<T>` vs `implement Box<Int>`) follows the
			# v1 spec: no ranking, ambiguous if both apply.  By the time
			# we reach this code, impl-block substitution has already
			# been applied to param_type_ids (see line ~2521), so the
			# regular exact-match check works correctly for impl-block
			# generics whose method bodies have concrete parameters.
			#
			# When all exact-match candidates are method-level generic,
			# the existing generic dispatch (type inference +
			# trait-bound checking) at the bottom of the function picks
			# among them.
			#
			# If multiple candidates have the same name/arity/receiver but
			# none has an exact parameter-type match for the call's args,
			# the user has called an overloaded method with arguments that
			# match no overload — that's "no matching overload", not
			# ambiguity.  Single-candidate cases fall through to existing
			# behavior unchanged.
			# item[7] = exact_param_match, item[8] = method_has_own_type_params
			exact_match_candidates = [item for item in pref_candidates if item[7]]
			if exact_match_candidates:
				method_concrete_exacts = [item for item in exact_match_candidates if not item[8]]
				if method_concrete_exacts:
					pref_candidates = method_concrete_exacts
				else:
					pref_candidates = exact_match_candidates
			elif len(pref_candidates) > 1:
				arg_label = ", ".join(_label_typeid(t) for t in arg_types)
				diagnostics.append(_tc_diag(
					message=f"no matching overload for method '{expr.method_name}' on receiver {_label_typeid(recv_ty)} with args [{arg_label}]",
					severity="error",
					span=getattr(expr, "loc", Span()),
				))
				return MethodCallResult(ctx.unknown_ty, None)
			if len(pref_candidates) > 1:
				wants_mut = [item for item in pref_candidates if item[4]]
				if wants_mut and len(wants_mut) != len(pref_candidates):
					pref_candidates = wants_mut
			if len(pref_candidates) > 1:
				pref_candidates = _pick_most_specific_items(pref_candidates, lambda item: _candidate_key_for_decl(item[0]), require_info)
				keys = {_candidate_key_for_decl(item[0]) for item in pref_candidates}
				if len(keys) > 1:
					trait_labels: list[str] = []
					if ctx.trait_impl_index is not None or trait_impl_fn_id_to_trait:
						for cand, _sig, _param_type_ids, _needs_autoborrow, _wants_mut_ref, _pref, _impl_subst, _exact_match, _method_tps in pref_candidates:
							trait_key = None
							if cand.fn_id is not None and hasattr(ctx.trait_impl_index, "trait_key_for_fn_id"):
								trait_key = ctx.trait_impl_index.trait_key_for_fn_id(cand.fn_id)
							if trait_key is None and cand.fn_id is not None:
								trait_key = trait_impl_fn_id_to_trait.get(cand.fn_id)
							if trait_key is None and cand.trait_id is not None and ctx.trait_key_for_id is not None:
								trait_key = ctx.trait_key_for_id(cand.trait_id)
							mod_name = cand.fn_id.module if cand.fn_id is not None and getattr(cand.fn_id, "module", None) else None
							if trait_key is not None:
								if mod_name:
									trait_labels.append(f"{_trait_label(trait_key)}@{mod_name}")
								else:
									trait_labels.append(_trait_label(trait_key))
					if trait_labels:
						arg_label = ", ".join(_label_typeid(t) for t in arg_types)
						msg = f"ambiguous method '{expr.method_name}' for receiver {_label_typeid(recv_ty)} and args [{arg_label}]; candidates from traits: {', '.join(sorted(set(trait_labels)))}"
						diagnostics.append(_tc_diag(message=msg, severity="error", span=getattr(expr, "loc", Span()), code="E-METHOD-AMBIGUOUS"))
						return MethodCallResult(ctx.unknown_ty, None)
					mod_names: set[str] = set()
					notes: list[str] = []
					note_by_name: dict[str, str] = {}
					visibility_provenance = getattr(ctx, "visibility_provenance", None)
					for cand, _sig, _param_type_ids, _needs_autoborrow, _wants_mut_ref, _pref, _impl_subst, _exact_match, _method_tps in pref_candidates:
						mod_name = None
						if cand.fn_id is not None and getattr(cand.fn_id, "module", None):
							mod_name = cand.fn_id.module
						elif cand.fn_id is not None and getattr(cand.fn_id, "name", None) and "::" in cand.fn_id.name:
							mod_name = cand.fn_id.name.split("::", 1)[0]
						elif visibility_provenance is not None:
							mod_chain = visibility_provenance.get(cand.module_id)
							if mod_chain:
								mod_name = mod_chain[-1]
						if mod_name is None and cand.module_id is not None and ctx.module_ids_by_name is not None:
							for _n, _mid in ctx.module_ids_by_name.items():
								if _mid == cand.module_id:
									mod_name = _n
									break
						if mod_name:
							mod_names.add(mod_name)
							_note_mod_id = cand.module_id
							if _note_mod_id is None and cand.fn_id is not None and getattr(cand.fn_id, "module", None) and ctx.module_ids_by_name is not None:
								_note_mod_id = ctx.module_ids_by_name.get(cand.fn_id.module)
							note = _visibility_note(_note_mod_id) if _note_mod_id is not None else None
							if note is None and visibility_provenance is not None and mod_name is not None:
								for _mid, _chain in visibility_provenance.items():
									if _chain and _chain[-1] == mod_name:
										note = _visibility_note(_mid)
										break
							if note is None and visibility_provenance is not None and mod_name is not None:
								for _chain in visibility_provenance.values():
									if _chain and _chain[-1] == mod_name:
										_parts = [_chain[0]]
										for _idx in range(1, len(_chain)):
											_label = "import->" if _idx == 1 else "reexport->"
											_parts.append(f"{_label} {_chain[_idx]}")
										note = f"visible via: {' '.join(_parts)}"
										break
							if note is not None:
								note_by_name.setdefault(mod_name, f"{mod_name} {note}")
					if note_by_name:
						for name in sorted(note_by_name.keys()):
							notes.append(note_by_name[name])
					if mod_names:
						mod_list = ", ".join(sorted(mod_names))
						msg = f"ambiguous method '{expr.method_name}' for receiver {_label_typeid(recv_ty)} (candidates: {mod_list})"
					else:
						msg = f"ambiguous method '{expr.method_name}' for receiver {_label_typeid(recv_ty)}"
					diagnostics.append(_tc_diag(message=msg, severity="error", span=getattr(expr, "loc", Span()), notes=notes))
					return MethodCallResult(ctx.unknown_ty, None)
			cand, sig, param_type_ids, needs_autoborrow, wants_mut_ref, _pref, impl_subst, _exact_match, _method_tps = pref_candidates[0]
			ret_id = sig.result_type or ctx.unknown_ty
			fn_sig = ctx.signatures_by_id.get(cand.fn_id) if cand.fn_id is not None and ctx.signatures_by_id is not None else None
			if fn_sig is None:
				fn_sig = _sig_from_decl_template(ctx, cand, current_module_name)
			if fn_sig is not None:
				if fn_sig.param_type_ids is None and fn_sig.param_types is not None:
					local_type_params = {p.name: p.id for p in fn_sig.type_params}
					fn_sig = replace(fn_sig, param_type_ids=[resolve_opaque_type(p, ctx.type_table, module_id=fn_sig.module, type_params=local_type_params) for p in fn_sig.param_types])
				if fn_sig.return_type_id is None and fn_sig.return_type is not None:
					local_type_params = {p.name: p.id for p in fn_sig.type_params}
					fn_sig = replace(fn_sig, return_type_id=resolve_opaque_type(fn_sig.return_type, ctx.type_table, module_id=fn_sig.module, type_params=local_type_params))
				if impl_subst is not None and fn_sig.param_type_ids is not None and fn_sig.return_type_id is not None:
					fn_sig = replace(fn_sig, param_type_ids=[apply_subst(p, impl_subst, ctx.type_table) for p in fn_sig.param_type_ids], return_type_id=apply_subst(fn_sig.return_type_id, impl_subst, ctx.type_table))
				self_mode_for_infer = _self_mode_from_sig(fn_sig)
				inferred_recv_ty = _infer_receiver_arg_type(self_mode_for_infer, recv_ty, receiver_is_lvalue=receiver_is_lvalue, receiver_can_mut_borrow=receiver_can_mut_borrow)
				inst_arg_types = [inferred_recv_ty] + list(arg_types)
				inst_res = ctx.instantiate_sig_with_subst(sig=fn_sig, arg_types=inst_arg_types, expected_type=expected_type, explicit_type_args=type_arg_ids, allow_infer=True, diag_span=call_type_args_span, call_kind="method", call_name=expr.method_name, receiver_type=inferred_recv_ty)
				if inst_res.error and inst_res.error.kind in {InferErrorKind.CANNOT_INFER, InferErrorKind.CONFLICT}:
					msg, notes = _format_infer_failure(inst_res.context, inst_res)
					diagnostics.append(_tc_diag(message=msg, severity="error", span=getattr(expr, "loc", Span()), notes=notes))
					return MethodCallResult(ctx.unknown_ty, None, None)
				if inst_res.error and inst_res.error.kind is InferErrorKind.NO_TYPEPARAMS and type_arg_ids:
					diagnostics.append(_tc_diag(message=f"type arguments require a generic signature for method '{expr.method_name}'", severity="error", span=call_type_args_span or getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None, None)
				if inst_res.error and inst_res.error.kind is InferErrorKind.TYPEARG_COUNT and type_arg_ids:
					exp = inst_res.error.expected_count if inst_res.error.expected_count is not None else len(type_arg_ids)
					diagnostics.append(_tc_diag(message=f"type argument count mismatch for method '{expr.method_name}': expected {exp}, got {len(type_arg_ids)}", severity="error", span=call_type_args_span or getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None, None)
				if inst_res.error and inst_res.error.kind is InferErrorKind.NO_TYPES:
					return MethodCallResult(ctx.unknown_ty, None, None)
				if inst_res.error:
					return MethodCallResult(ctx.unknown_ty, None, None)
				if inst_res.inst_params is not None:
					param_type_ids = list(inst_res.inst_params)
				if inst_res.inst_return is not None:
					ret_id = inst_res.inst_return
				if getattr(inst_res, "subst", None) is not None:
					inst_subst_args = list(getattr(inst_res.subst, "args", []) or [])
				if fn_sig is not None and receiver_args is not None and getattr(fn_sig, "impl_type_params", None):
					impl_params = list(getattr(fn_sig, "impl_type_params", []) or [])
					impl_owner = impl_params[0].id.owner if impl_params else None
					if impl_owner is not None and receiver_args and not any(ctx.type_table.has_typevar(t) for t in receiver_args):
						def _has_owner_typevar(tid: TypeId) -> bool:
							td = ctx.type_table.get(tid)
							if td.kind is TypeKind.TYPEVAR and td.type_param_id is not None and td.type_param_id.owner == impl_owner:
								return True
							for sub in td.param_types or []:
								if _has_owner_typevar(sub):
									return True
							return False
						if any(_has_owner_typevar(t) for t in param_type_ids) or _has_owner_typevar(ret_id):
							fallback_subst = Subst(owner=impl_owner, args=list(receiver_args))
							param_type_ids = [apply_subst(p, fallback_subst, ctx.type_table) for p in param_type_ids]
							ret_id = apply_subst(ret_id, fallback_subst, ctx.type_table)
			if impl_subst is not None:
				ret_id = apply_subst(ret_id, impl_subst, ctx.type_table)
				if param_type_ids:
					param_type_ids = [apply_subst(p, impl_subst, ctx.type_table) for p in param_type_ids]
			if subst_for_receiver is not None:
				ret_id = apply_subst(ret_id, subst_for_receiver, ctx.type_table)
				if param_type_ids:
					param_type_ids = [apply_subst(p, subst_for_receiver, ctx.type_table) for p in param_type_ids]
			if param_type_ids:
				recv_nom = _unwrap_ref_type(recv_ty)
				first_nom = _unwrap_ref_type(param_type_ids[0])
				last_nom = _unwrap_ref_type(param_type_ids[-1])
				if first_nom != recv_nom:
					recv_base, _ = _struct_base_and_args(recv_nom)
					last_base, _ = _struct_base_and_args(last_nom)
					if last_base is not None and recv_base == last_base:
						param_type_ids = [param_type_ids[-1]] + list(param_type_ids[:-1])
			all_args = [expr.receiver] + list(expr.args)
			updated_arg_types, had_autoborrow_error = ctx.apply_autoborrow_args(
				all_args,
				[recv_ty] + arg_types,
				param_type_ids,
				span=getattr(expr, "loc", Span()),
				skip_first=True,
			)
			expr.receiver = all_args[0]
			expr.args = list(all_args[1:])
			if had_autoborrow_error:
				return MethodCallResult(ctx.unknown_ty, None, None)
			if updated_arg_types:
				recv_ty = updated_arg_types[0]
				arg_types = list(updated_arg_types[1:])
			expected_params = list(param_type_ids)
			if expected_params:
				recv_nom = _unwrap_ref_type(recv_ty)
				first_nom = _unwrap_ref_type(expected_params[0])
				if first_nom == recv_nom:
					expected_params = expected_params[1:]
				else:
					recv_base, _ = _struct_base_and_args(recv_nom)
					first_base, _ = _struct_base_and_args(first_nom)
					if recv_base == first_base:
						expected_params = expected_params[1:]
			for idx, arg in enumerate(expr.args):
				if isinstance(arg, H.HCall) and isinstance(arg.fn, H.HVar) and _is_std_core_module(arg.fn.module_id, ctx.module_ids_by_name, ctx.visibility_provenance) and arg.fn.name in ("callback0", "callback1", "callback2", "callback_throw0", "callback_throw1", "callback_throw2"):
					continue
				if idx >= len(expected_params):
					continue
				param_ty = expected_params[idx]
				schema = ctx.type_table.get_interface_schema(param_ty)
				schema_name = schema.name if schema is not None else None
				if schema_name is None:
					param_def = ctx.type_table.get(param_ty)
					if param_def.kind is TypeKind.INTERFACE:
						schema_name = param_def.name
				if schema_name not in ("Callback0", "Callback1", "Callback2", "CallbackThrow0", "CallbackThrow1", "CallbackThrow2"):
					continue
				arg_ty = arg_types[idx] if idx < len(arg_types) else None
				if not isinstance(arg, H.HLambda):
					if arg_ty is None:
						continue
					arg_def = ctx.type_table.get(arg_ty)
					if arg_def.kind is not TypeKind.FUNCTION or not arg_def.param_types:
						continue
				if isinstance(arg, H.HLambda):
					arity = len(arg.params)
				else:
					arity = len(arg_def.param_types) - 1
				is_throw = schema_name in ("CallbackThrow0", "CallbackThrow1", "CallbackThrow2")
				cb_call = _implicit_callback_wrap(
					ctx,
					arg=arg,
					callback_arity=arity,
					is_throw=is_throw,
					expected_type_hint=param_ty,
				)
				expr.args[idx] = cb_call
				if idx < len(arg_types):
					cb_ty = type_expr(cb_call, expected_type=param_ty, used_as_value=False)
					arg_types[idx] = param_ty if cb_ty == ctx.unknown_ty else cb_ty
			intent.arg_expected_types = _expected_arg_types_for_call(expected_params, len(expr.args))
			_propagate_arg_expected_types(intent, arg_types)
			match_recv_ty = recv_ty
			if needs_autoborrow is not None and param_type_ids:
				match_recv_ty = param_type_ids[0]
			if not ctx.args_match_params([match_recv_ty] + arg_types, param_type_ids):
				alt_arg_types = list(arg_types)
				for idx, arg in enumerate(expr.args):
					if idx >= len(expected_params):
						continue
					param_ty = expected_params[idx]
					schema = ctx.type_table.get_interface_schema(param_ty)
					schema_name = schema.name if schema is not None else None
					if schema_name is None:
						param_def = ctx.type_table.get(param_ty)
						if param_def.kind is TypeKind.INTERFACE:
							schema_name = param_def.name
					if schema_name not in ("Callback0", "Callback1", "Callback2", "CallbackThrow0", "CallbackThrow1", "CallbackThrow2"):
						continue
					if isinstance(arg, H.HLambda):
						arity = len(arg.params)
						is_throw = schema_name in ("CallbackThrow0", "CallbackThrow1", "CallbackThrow2")
						cb_call = _implicit_callback_wrap(
							ctx,
							arg=arg,
							callback_arity=arity,
							is_throw=is_throw,
							expected_type_hint=param_ty,
						)
						expr.args[idx] = cb_call
						cb_ty = type_expr(cb_call, expected_type=param_ty, used_as_value=False)
						alt_arg_types[idx] = param_ty if cb_ty == ctx.unknown_ty else cb_ty
					elif isinstance(arg, H.HCall):
						alt_arg_types[idx] = param_ty
				if ctx.args_match_params([match_recv_ty] + alt_arg_types, param_type_ids):
					arg_types = alt_arg_types
				else:
					diagnostics.append(_tc_diag(message=f"no matching method '{expr.method_name}' for receiver {_label_typeid(recv_ty)}", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None, None)
			coerced_params = ctx.coerce_args_for_params([match_recv_ty] + arg_types, param_type_ids)
			ret_id = ret_id or ctx.unknown_ty
			target_fn_id = cand.fn_id
			can_throw = True
			if cand.fn_id is not None and ctx.signatures_by_id is not None:
				fn_sig = ctx.signatures_by_id.get(cand.fn_id)
				if fn_sig is not None:
					if fn_sig.declared_can_throw is not None:
						can_throw = bool(fn_sig.declared_can_throw)
					upgrade_boundary = False
					if getattr(fn_sig, "is_pub", False) and fn_sig.declared_can_throw is False:
						pass  # Option B: no boundary wrapper upgrade
			call_target = CallTarget.direct(target_fn_id)
			_cand_terminal = bool(getattr(fn_sig, "declared_terminal_throws", False)) if fn_sig is not None else bool(getattr(cand, "declared_terminal_throws", False))
			info = _call_info_target(list(coerced_params), ret_id, can_throw, call_target, declared_terminal_throws=_cand_terminal)
			expr.arg_type_ids = list(arg_types)
			receiver_autoborrow = None
			if needs_autoborrow:
				receiver_autoborrow = SelfMode.SELF_BY_REF_MUT if wants_mut_ref else SelfMode.SELF_BY_REF
			if ctx.record_instantiation is not None and target_fn_id is not None:
				impl_args = tuple(
					receiver_args
					or receiver_inst_args
					or (list(getattr(impl_subst, "args", []) or []) if impl_subst is not None else [])
				)
				if not impl_args and recv_ty is not None:
					recv_inst = ctx.type_table.get_struct_instance(ctx.unwrap_ref_type(recv_ty))
					if recv_inst is not None:
						impl_args = tuple(recv_inst.type_args)
					else:
						recv_vinst = ctx.type_table.get_variant_instance(ctx.unwrap_ref_type(recv_ty))
						if recv_vinst is not None:
							impl_args = tuple(recv_vinst.type_args)
				if not impl_args and param_type_ids:
					recv_inst = ctx.type_table.get_struct_instance(ctx.unwrap_ref_type(param_type_ids[0]))
					if recv_inst is not None:
						impl_args = tuple(recv_inst.type_args)
					else:
						recv_vinst = ctx.type_table.get_variant_instance(ctx.unwrap_ref_type(param_type_ids[0]))
						if recv_vinst is not None:
							impl_args = tuple(recv_vinst.type_args)
				fn_args = tuple(inst_subst_args or [])
				if (impl_args or fn_args) and not any(ctx.type_table.has_typevar(t) for t in list(impl_args) + list(fn_args)):
					csid = getattr(expr, "callsite_id", None)
					if drift_debug.enabled("instantiate") and target_fn_id is not None and "or_throw" in target_fn_id.name:
						try:
							import sys
							print(f"[debug] inst {target_fn_id} impl_args={impl_args} fn_args={fn_args}", file=sys.stderr)
						except Exception:
							pass
					ctx.record_instantiation(callsite_id=csid, target_fn_id=target_fn_id, impl_args=impl_args, fn_args=fn_args)
			return MethodCallResult(ret_id, info, MethodResolution(decl=cand, receiver_autoborrow=receiver_autoborrow, result_type=ret_id))

	diagnostics.append(_tc_diag(message=f"no matching method '{expr.method_name}' for receiver {_label_typeid(recv_ty)}", severity="error", span=getattr(expr, "loc", Span())))
	return MethodCallResult(ctx.unknown_ty, None)


def _require_unknown_defer(ctx: MethodResolverContext, *, arg_types: list[TypeId], receiver_args: list[TypeId] | None = None, type_arg_ids: list[TypeId] | None = None) -> bool:
	if receiver_args is not None and any(ctx.type_table.has_typevar(t) for t in receiver_args):
		return True
	if arg_types and any(ctx.type_table.has_typevar(t) for t in arg_types):
		return True
	if type_arg_ids and any(ctx.type_table.has_typevar(t) for t in type_arg_ids):
		return True
	return False


def resolve_qualified_member_ufcs(ctx: MethodResolverContext, expr: object, qm: object, *, expected_type: TypeId | None, type_arg_ids: list[TypeId] | None, call_type_args_span: Span | None, call_origin: str | None, recv_arg_type: TypeId | None, arg_type_ids: list[TypeId] | None) -> MethodCallResult | None:
	diagnostics = ctx.diagnostics
	_tc_diag = ctx.tc_diag
	base_te = getattr(qm, "base_type_expr", None)
	if base_te is None:
		return None
	trait_index = ctx.trait_index
	if trait_index is None:
		return None
	trait_key = trait_key_from_expr(base_te, default_module=ctx.current_module_name, default_package=ctx.default_package, module_packages=ctx.module_packages)
	if drift_debug.enabled("call_resolve") and getattr(qm, "member", None) in ("build", "hash", "finish", "eq"):
		try:
			scope_traits = list(ctx.traits_in_scope()) if ctx.traits_in_scope is not None else []
		except Exception:
			scope_traits = []
		print(
			f"[call_resolve] ufcs member={qm.member} base_mod={getattr(base_te, 'module_id', None)} base_alias={getattr(base_te, 'module_alias', None)} trait_key={trait_key} traits_in_scope={scope_traits}",
			file=sys.stderr,
		)
	if trait_key not in trait_index.traits_by_id:
		for _k in trait_index.traits_by_id:
			if getattr(_k, "module", None) == getattr(trait_key, "module", None) and getattr(_k, "name", None) == getattr(trait_key, "name", None):
				trait_key = _k
				break
	base_mod = getattr(base_te, "module_id", None) or getattr(base_te, "module_alias", None)
	base_name = getattr(base_te, "name", None)
	if base_mod == "std.iter" and base_name == "Iterable" and qm.member == "iter" and ctx.signatures_by_id is not None and recv_arg_type is not None:
		recv_ty = recv_arg_type
		effective_recv_ty = recv_ty
		recv_def = ctx.type_table.get(recv_ty)
		receiver_args = None
		if recv_def.kind is TypeKind.REF and recv_def.param_types:
			inner = recv_def.param_types[0]
			inner_def = ctx.type_table.get(inner)
			# `for` desugaring passes a shared borrow by default. If the source value
			# is already a reference (for example `&Array<T>`), this yields `&&Array<T>`.
			# Nested-ref receivers are handled by the normal method-call path so it can
			# apply receiver coercions (e.g. implicit single deref `&&T -> &T`).
			if inner_def.kind is TypeKind.REF and inner_def.param_types:
				receiver_args = None
			elif inner_def.kind is TypeKind.ARRAY and inner_def.param_types:
				receiver_args = list(inner_def.param_types)
		elif recv_def.kind is TypeKind.ARRAY and recv_def.param_types:
			receiver_args = list(recv_def.param_types)
		if receiver_args is not None:
			array_base_id = ctx.type_table.array_base_id()
			for fn_id, sig in ctx.signatures_by_id.items():
				if not getattr(sig, "is_method", False):
					continue
				if (sig.method_name or sig.name) != qm.member:
					continue
				impl_tid = sig.impl_target_type_id
				if impl_tid is None:
					continue
				impl_def = ctx.type_table.get(impl_tid)
				if impl_def.kind is TypeKind.REF and impl_def.param_types:
					impl_tid = impl_def.param_types[0]
					impl_def = ctx.type_table.get(impl_tid)
				if impl_def.kind is TypeKind.ARRAY:
					impl_tid = array_base_id
				if impl_tid != array_base_id:
					continue
				if sig.param_type_ids is None or sig.return_type_id is None:
					continue
				param_type_ids = list(sig.param_type_ids)
				ret_id = sig.return_type_id
				impl_args = list(getattr(sig, "impl_target_type_args", None) or [])
				impl_type_params = list(getattr(sig, "impl_type_params", None) or [])
				impl_subst = None
				if impl_args and impl_type_params:
					impl_subst = ctx.match_impl_type_args(template_args=impl_args, recv_args=list(receiver_args), impl_type_params=impl_type_params)
				if impl_subst is not None:
					param_type_ids = [apply_subst(p, impl_subst, ctx.type_table) for p in param_type_ids]
					ret_id = apply_subst(ret_id, impl_subst, ctx.type_table)
				if not param_type_ids or param_type_ids[0] != effective_recv_ty:
					continue
				can_throw = True
				if sig.declared_can_throw is not None:
					can_throw = bool(sig.declared_can_throw)
				info = CallInfo(target=CallTarget.direct(fn_id), sig=CallSig(param_types=tuple(param_type_ids), user_ret_type=ret_id, can_throw=can_throw, declared_terminal_throws=bool(getattr(sig, "declared_terminal_throws", False))))
				if ctx.record_instantiation is not None and receiver_args is not None:
					impl_args = tuple(receiver_args)
					if impl_args and not any(ctx.type_table.has_typevar(t) for t in impl_args):
						csid = getattr(expr, "callsite_id", None)
						ctx.record_instantiation(callsite_id=csid, target_fn_id=fn_id, impl_args=impl_args, fn_args=tuple())
				return MethodCallResult(ret_id, info)
	base_args = list(getattr(base_te, "args", []) or [])
	if base_args and type_arg_ids is None:
		if call_type_args_span is None:
			first_loc = getattr(base_args[0], "loc", None)
			if first_loc is not None:
				call_type_args_span = Span.from_loc(first_loc)
		type_arg_ids = [
			resolve_opaque_type(a, ctx.type_table, module_id=base_mod or ctx.current_module_name, type_params=ctx.type_param_map)
			for a in base_args
		]
	if trait_key not in trait_index.traits_by_id:
		if call_origin == "for_iter" and base_mod == "std.iter" and base_name == "Iterable" and qm.member == "iter":
			diagnostics.append(_tc_diag(message="type is not iterable", code="E-NOT-ITERABLE", severity="error", span=getattr(expr, "loc", Span())))
			return MethodCallResult(ctx.unknown_ty, None)
		if call_origin == "for_next" and base_mod == "std.iter" and base_name == "SinglePassIterator" and qm.member == "next":
			diagnostics.append(_tc_diag(message="iter() result is not an iterator", code="E-ITER-RESULT-NOT-ITERATOR", severity="error", span=getattr(expr, "loc", Span())))
			return MethodCallResult(ctx.unknown_ty, None)
		base_tid = None
		try:
			base_tid = resolve_opaque_type(base_te, ctx.type_table, module_id=base_mod or ctx.current_module_name, type_params=ctx.type_param_map, allow_generic_base=True)
		except Exception:
			base_tid = None
		if base_tid is not None and ctx.type_table.get(base_tid).kind is TypeKind.INTERFACE:
			diagnostics.append(
				_tc_diag(
					message="UFCS interface dispatch is not supported yet; call through an interface value",
					severity="error",
					span=getattr(expr, "loc", Span()),
				)
			)
			return MethodCallResult(ctx.unknown_ty, None)
		return MethodCallResult(ctx.unknown_ty, None)
	if not getattr(expr, "args", None):
		diagnostics.append(_tc_diag(message="UFCS call requires a receiver argument", severity="error", span=getattr(expr, "loc", Span())))
		return MethodCallResult(ctx.unknown_ty, None)
	trait_def = trait_index.traits_by_id.get(trait_key)
	if trait_def is None or not list(getattr(trait_def, "methods", []) or []):
		world = ctx.global_trait_world or ctx.visible_trait_world
		if world is not None:
			trait_def = world.traits.get(trait_key)
	trait_method_declared_nothrow: bool | None = None
	method_sig = None
	if trait_def is not None:
		for method in getattr(trait_def, "methods", []) or []:
			if getattr(method, "name", None) == qm.member:
				method_sig = method
				trait_method_declared_nothrow = bool(getattr(method, "declared_nothrow", False))
				break
	trait_type_params = list(getattr(trait_def, "type_params", []) or []) if trait_def is not None else []
	if method_sig is not None and (not trait_type_params or type_arg_ids):
		recv_ty = recv_arg_type if recv_arg_type is not None else ctx.type_expr(expr.args[0], used_as_value=False)
		recv_nominal = ctx.unwrap_ref_type(recv_ty)
		if trait_type_params and type_arg_ids and ctx.type_table.get(recv_nominal).kind is not TypeKind.TYPEVAR:
			world = ctx.global_trait_world or ctx.visible_trait_world
			if world is not None:
				subject_key = ctx.normalize_type_key(
					type_key_from_typeid(ctx.type_table, recv_nominal),
				)
				trait_args = tuple(
					ctx.normalize_type_key(type_key_from_typeid(ctx.type_table, tid))
					for tid in type_arg_ids
				)
				env = TraitEnv(
					assumed_true=set(),
					assumed_false=set(),
					default_module=trait_key.module or ctx.current_module_name,
					default_package=ctx.default_package,
					module_packages=ctx.module_packages,
					type_table=ctx.type_table,
				)
				obl = Obligation(
					subject=subject_key,
					trait=trait_key,
					trait_args=trait_args,
					origin=ObligationOrigin(kind=ObligationOriginKind.METHOD_CALL, label="trait call", span=getattr(expr, "loc", None)),
					span=getattr(expr, "loc", None),
				)
				if prove_obligation(world, env, obl) is not None:
					diagnostics.append(
						_tc_diag(
							message=f"no implementation for trait '{ctx.trait_label(trait_key)}' on receiver {ctx.label_typeid(recv_ty)}",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return MethodCallResult(ctx.unknown_ty, None)
		local_type_param_map: dict[str, TypeId] = {"Self": recv_nominal}
		if trait_type_params:
			if not type_arg_ids or len(type_arg_ids) != len(trait_type_params):
				diagnostics.append(_tc_diag(message="type argument count mismatch for trait call", severity="error", span=getattr(expr, "loc", Span())))
				return MethodCallResult(ctx.unknown_ty, None)
			for idx, name in enumerate(trait_type_params):
				local_type_param_map[name] = type_arg_ids[idx]
		if ctx.type_table.get(recv_nominal).kind is TypeKind.TYPEVAR:
			param_type_ids: list[TypeId] = []
			for param in list(getattr(method_sig, "params", []) or []):
				if param.type_expr is None:
					if param.name == "self":
						param_type_ids.append(recv_nominal)
						continue
					diagnostics.append(_tc_diag(message="method parameter type missing", severity="error", span=getattr(expr, "loc", Span())))
					return MethodCallResult(ctx.unknown_ty, None)
				param_type_ids.append(resolve_opaque_type(param.type_expr, ctx.type_table, module_id=trait_key.module or ctx.current_module_name, type_params=local_type_param_map))
			ret_id = resolve_opaque_type(method_sig.return_type, ctx.type_table, module_id=trait_key.module or ctx.current_module_name, type_params=local_type_param_map)
			call_target = CallTarget.trait(trait_key, qm.member)
			call_can_throw = not bool(getattr(method_sig, "declared_nothrow", False))
			if trait_method_declared_nothrow is not None:
				call_can_throw = not trait_method_declared_nothrow
			_call_terminal = bool(getattr(method_sig, "declared_terminal_throws", False))
			info = CallInfo(target=call_target, sig=CallSig(param_types=tuple(param_type_ids), user_ret_type=ret_id, can_throw=call_can_throw, declared_terminal_throws=_call_terminal))
			return MethodCallResult(ret_id, info)
		param_type_ids: list[TypeId] = []
		for param in list(getattr(method_sig, "params", []) or []):
			if param.type_expr is None:
				if param.name == "self":
					param_type_ids.append(recv_nominal)
					continue
				diagnostics.append(_tc_diag(message="method parameter type missing", severity="error", span=getattr(expr, "loc", Span())))
				return MethodCallResult(ctx.unknown_ty, None)
			param_type_ids.append(resolve_opaque_type(param.type_expr, ctx.type_table, module_id=trait_key.module or ctx.current_module_name, type_params=local_type_param_map))
		ret_id = resolve_opaque_type(method_sig.return_type, ctx.type_table, module_id=trait_key.module or ctx.current_module_name, type_params=local_type_param_map)
		receiver_base, receiver_args = ctx.struct_base_and_args(recv_nominal)
		if ctx.trait_impl_index is None:
			diagnostics.append(
				_tc_diag(
					message=f"no implementation for trait '{ctx.trait_label(trait_key)}' on receiver {ctx.label_typeid(recv_ty)}",
					severity="error",
					span=getattr(expr, "loc", Span()),
				)
			)
			return MethodCallResult(ctx.unknown_ty, None)
		candidates = list(ctx.trait_impl_index.get_candidates(trait_key, receiver_base, qm.member))
		if not candidates:
			diagnostics.append(
				_tc_diag(
					message=f"no implementation for trait '{ctx.trait_label(trait_key)}' on receiver {ctx.label_typeid(recv_ty)}",
					severity="error",
					span=getattr(expr, "loc", Span()),
				)
			)
			return MethodCallResult(ctx.unknown_ty, None)
		visible_candidates = [
			c for c in candidates
			if (ctx.current_module is not None and c.def_module_id == ctx.current_module) or c.is_pub
		]
		if not visible_candidates:
			diagnostics.append(
				_tc_diag(
					message=f"method '{qm.member}' exists but is not visible here",
					severity="error",
					span=getattr(expr, "loc", Span()),
				)
			)
			return MethodCallResult(ctx.unknown_ty, None)
		world = ctx.global_trait_world or ctx.visible_trait_world
		if world is None:
			diagnostics.append(_tc_diag(message="trait world missing (compiler bug)", severity="error", span=getattr(expr, "loc", Span())))
			return MethodCallResult(ctx.unknown_ty, None)
		env = TraitEnv(default_module=trait_key.module or ctx.current_module_name, default_package=ctx.default_package, module_packages=ctx.module_packages or {}, assumed_true=set(ctx.fn_require_assumed), type_table=ctx.type_table)
		for cand in visible_candidates:
			req_expr = getattr(cand, "require_expr", None)
			if req_expr is not None:
				subst: dict[object, object] = {}
				sig_local = ctx.signatures_by_id.get(cand.fn_id) if cand.fn_id is not None and ctx.signatures_by_id is not None else None
				impl_subst_req = None
				if sig_local and receiver_args is not None:
					impl_params = list(getattr(sig_local, "impl_type_params", []) or [])
					impl_args = list(getattr(sig_local, "impl_target_type_args", None) or [])
					if impl_args and impl_params:
						impl_subst_req = ctx.match_impl_type_args(template_args=impl_args, recv_args=list(receiver_args), impl_type_params=impl_params)
					if impl_subst_req is None and impl_params and len(impl_params) == len(receiver_args):
						impl_subst_req = Subst(owner=impl_params[0].id.owner, args=list(receiver_args))
				if impl_subst_req is not None:
					for idx, tp in enumerate(impl_params):
						if idx < len(impl_subst_req.args):
							if ctx.type_table.has_typevar(impl_subst_req.args[idx]):
								continue
							key = ctx.normalize_type_key(type_key_from_typeid(ctx.type_table, impl_subst_req.args[idx]))
							subst[tp.id] = key
							subst[tp.name] = key
				res = prove_expr(world, env, subst, req_expr)
				if res.status is not ProofStatus.PROVED:
					if res.status is ProofStatus.UNKNOWN:
						if _require_unknown_defer(ctx, arg_types=arg_type_ids or [], receiver_args=list(receiver_args) if receiver_args is not None else None):
							continue
					origin = ObligationOrigin(
						kind=ObligationOriginKind.CALLEE_REQUIRE,
						label=f"method '{qm.member}'",
						span=Span.from_loc(getattr(req_expr, "loc", None)),
					)
					failure = ctx.require_failure(req_expr=req_expr, subst=subst, origin=origin, span=getattr(expr, "loc", Span()), env=env, world=world, result=res)
					msg = ctx.format_failure_message(failure) if failure is not None else "requirement not satisfied"
					code = ctx.failure_code(failure) if failure is not None else None
					req_label = None
					if isinstance(req_expr, parser_ast.TraitIs):
						req_key = trait_key_from_expr(
							req_expr.trait,
							default_module=ctx.current_module_name,
							default_package=ctx.default_package,
							module_packages=ctx.module_packages,
						)
						req_label = ctx.trait_label(req_key)
					if req_label and req_label not in msg:
						msg = f"{msg}: expected {req_label}"
					diagnostics.append(_tc_diag(message=msg, severity="error", span=getattr(expr, "loc", Span()), code=code))
					return MethodCallResult(ctx.unknown_ty, None)
		target_fn_id = visible_candidates[0].fn_id
		call_target = CallTarget.direct(target_fn_id)
		call_can_throw = not bool(getattr(method_sig, "declared_nothrow", False))
		if trait_method_declared_nothrow is not None:
			call_can_throw = not trait_method_declared_nothrow
		elif target_fn_id is not None and ctx.signatures_by_id is not None:
			target_sig = ctx.signatures_by_id.get(target_fn_id)
			if target_sig is not None and target_sig.declared_can_throw is not None:
				call_can_throw = bool(target_sig.declared_can_throw)
		_trait_terminal = bool(getattr(method_sig, "declared_terminal_throws", False))
		info = CallInfo(target=call_target, sig=CallSig(param_types=tuple(param_type_ids), user_ret_type=ret_id, can_throw=call_can_throw, declared_terminal_throws=_trait_terminal))
		return MethodCallResult(ret_id, info)
	class _TmpMethodCall:
		def __init__(self, receiver: object, method_name: str, args: list[object], loc: Span, type_args: list[object] | None):
			self.receiver = receiver
			self.method_name = method_name
			self.args = args
			self.kwargs = []
			self.loc = loc
			self.type_args = type_args or []
			self.receiver_type_id = None
			self.arg_type_ids = None
	receiver_expr = expr.args[0]
	adjusted_recv_arg_type = recv_arg_type
	if call_origin == "for_iter" and recv_arg_type is not None:
		recv_def = ctx.type_table.get(recv_arg_type)
		if recv_def.kind is TypeKind.REF and recv_def.param_types:
			inner = recv_def.param_types[0]
			inner_def = ctx.type_table.get(inner)
			if inner_def.kind is TypeKind.REF:
				receiver_expr = H.HUnary(op=H.UnaryOp.DEREF, expr=receiver_expr)
				if ctx.alloc_node_id is not None:
					ctx.alloc_node_id(receiver_expr)
				expr.args[0] = receiver_expr
				adjusted_recv_arg_type = inner
	tmp_expr = _TmpMethodCall(receiver_expr, qm.member, list(expr.args[1:]), getattr(expr, "loc", Span()), getattr(expr, "type_args", None))
	tmp_expr.callsite_id = getattr(expr, "callsite_id", None)
	tmp_expr.origin = call_origin
	if adjusted_recv_arg_type is not None:
		tmp_expr.receiver_type_id = adjusted_recv_arg_type
	if arg_type_ids is not None:
		tmp_expr.arg_type_ids = list(arg_type_ids[1:])
	tmp_ctx = _make_method_ctx(ctx, diagnostics=diagnostics, traits_in_scope=lambda: [trait_key], trait_key=trait_key)
	if call_origin in ("for_iter", "for_next") and ctx.module_ids_by_name:
		tmp_ctx = replace(tmp_ctx, visible_modules=tuple(ctx.module_ids_by_name.values()))
	diag_len_before = len(diagnostics)
	method_res = resolve_method_call(tmp_ctx, tmp_expr, expected_type=expected_type)
	if (
		call_origin == "for_iter"
		and method_res.call_info is not None
		and isinstance(getattr(method_res.call_info, "target", None), CallTarget)
		and method_res.call_info.target.kind is CallTargetKind.TRAIT
	):
		method_res = MethodCallResult(method_res.return_type, None, method_res.resolution)
	if method_res.call_info is None and call_origin == "for_iter" and ctx.signatures_by_id is not None:
		recv_ty = recv_arg_type if recv_arg_type is not None else ctx.type_expr(expr.args[0], used_as_value=False)
		recv_nominal = ctx.unwrap_ref_type(recv_ty)
		recv_def = ctx.type_table.get(recv_nominal)
		receiver_base = None
		receiver_args = None
		if recv_def.kind is TypeKind.ARRAY and recv_def.param_types:
			receiver_base = ctx.type_table.array_base_id()
			receiver_args = list(recv_def.param_types)
		else:
			recv_struct = ctx.type_table.get_struct_instance(recv_nominal)
			if recv_struct is not None:
				receiver_base = recv_struct.base_id
				receiver_args = list(recv_struct.type_args)
			else:
				recv_variant = ctx.type_table.get_variant_instance(recv_nominal)
				if recv_variant is not None:
					receiver_base = recv_variant.base_id
					receiver_args = list(recv_variant.type_args)
		if receiver_base is not None and receiver_args is not None:
			for fn_id, sig in ctx.signatures_by_id.items():
				if not getattr(sig, "is_method", False):
					continue
				if (sig.method_name or sig.name) != qm.member:
					continue
				impl_tid = sig.impl_target_type_id
				if impl_tid is None:
					continue
				impl_def = ctx.type_table.get(impl_tid)
				if impl_def.kind is TypeKind.REF and impl_def.param_types:
					impl_tid = impl_def.param_types[0]
					impl_def = ctx.type_table.get(impl_tid)
				if impl_def.kind is TypeKind.ARRAY:
					impl_tid = ctx.type_table.array_base_id()
				if impl_tid != receiver_base:
					continue
				if sig is None or sig.param_type_ids is None or sig.return_type_id is None:
					continue
				param_type_ids = list(sig.param_type_ids)
				ret_id = sig.return_type_id
				impl_args = list(getattr(sig, "impl_target_type_args", None) or [])
				impl_type_params = list(getattr(sig, "impl_type_params", None) or [])
				impl_subst = None
				if impl_args and impl_type_params:
					impl_subst = ctx.match_impl_type_args(template_args=impl_args, recv_args=list(receiver_args), impl_type_params=impl_type_params)
				if impl_subst is not None:
					param_type_ids = [apply_subst(p, impl_subst, ctx.type_table) for p in param_type_ids]
					ret_id = apply_subst(ret_id, impl_subst, ctx.type_table)
				can_throw = True
				if sig.declared_can_throw is not None:
					can_throw = bool(sig.declared_can_throw)
				info = CallInfo(target=CallTarget.direct(fn_id), sig=CallSig(param_types=tuple(param_type_ids), user_ret_type=ret_id, can_throw=can_throw, declared_terminal_throws=bool(getattr(sig, "declared_terminal_throws", False))))
				if ctx.record_instantiation is not None and receiver_args is not None:
					impl_args = tuple(receiver_args)
					if impl_args and not any(ctx.type_table.has_typevar(t) for t in impl_args):
						csid = getattr(expr, "callsite_id", None)
						ctx.record_instantiation(callsite_id=csid, target_fn_id=fn_id, impl_args=impl_args, fn_args=tuple())
				if len(diagnostics) > diag_len_before:
					del diagnostics[diag_len_before:]
				return MethodCallResult(ret_id, info)
	if call_origin == "for_iter" and method_res.call_info is None:
		diagnostics.append(_tc_diag(message="type is not iterable", code="E-NOT-ITERABLE", severity="error", span=getattr(expr, "loc", Span())))
		return MethodCallResult(ctx.unknown_ty, None)
	if call_origin == "for_next" and method_res.call_info is None:
		diagnostics.append(_tc_diag(message="iter() result is not an iterator", code="E-ITER-RESULT-NOT-ITERATOR", severity="error", span=getattr(expr, "loc", Span())))
		return MethodCallResult(ctx.unknown_ty, None)
	if method_res.call_info is not None and call_origin in ("for_iter", "for_next"):
		def _receiver_inst_args(tid: TypeId) -> list[TypeId] | None:
			inst = ctx.type_table.get_struct_instance(tid)
			if inst is not None:
				return list(inst.type_args)
			vinst = ctx.type_table.get_variant_instance(tid)
			if vinst is not None:
				return list(vinst.type_args)
			td = ctx.type_table.get(tid)
			if td.kind is TypeKind.ARRAY and td.param_types:
				return list(td.param_types)
			return None
		target = getattr(method_res.call_info, "target", None)
		target_fn_id = target.symbol if isinstance(target, CallTarget) and target.kind is CallTargetKind.DIRECT else None
		if ctx.record_instantiation is not None and target_fn_id is not None:
			recv_ty = recv_arg_type if recv_arg_type is not None else ctx.type_expr(expr.args[0], used_as_value=False)
			recv_nominal = ctx.unwrap_ref_type(recv_ty)
			receiver_args = _receiver_inst_args(recv_nominal)
			if receiver_args is None:
				_receiver_base, receiver_args = ctx.struct_base_and_args(recv_nominal)
			if receiver_args is not None:
				impl_args = tuple(receiver_args)
				if impl_args and not any(ctx.type_table.has_typevar(t) for t in impl_args):
					csid = getattr(expr, "callsite_id", None)
					ctx.record_instantiation(callsite_id=csid, target_fn_id=target_fn_id, impl_args=impl_args, fn_args=tuple())
	if method_res.call_info is not None:
		return method_res
	return method_res


def _is_std_core_module(mod_name: object | None, module_ids_by_name: dict[str, int] | None, visibility_provenance: dict[int, list[str]] | None) -> bool:
	if mod_name is None:
		return False
	if isinstance(mod_name, int):
		std_core_id = (module_ids_by_name or {}).get("std.core")
		return std_core_id is not None and mod_name == std_core_id
	if mod_name == "std.core":
		return True
	mod_id = (module_ids_by_name or {}).get(mod_name)
	if mod_id is None:
		return False
	std_core_id = (module_ids_by_name or {}).get("std.core")
	if std_core_id is not None and mod_id == std_core_id:
		return True
	chain = (visibility_provenance or {}).get(mod_id)
	if not chain:
		return False
	return "std.core" in chain or chain[-1] == "std.core"


def resolve_call_expr(
	ctx: CallResolverContext,
	expr: object,
	expected_type: TypeId | None,
	*,
	record_expr: Callable[[object, TypeId], TypeId],
	record_call_info: Callable[[object, list[TypeId], TypeId, bool, CallTarget], None],
	record_invoke_call_info: Callable[[object, list[TypeId], TypeId, bool], None],
) -> TypeId:
	diagnostics = ctx.diagnostics
	_tc_diag = ctx.tc_diag
	type_expr = ctx.type_expr
	_optional_variant_type = ctx.optional_variant_type
	_unwrap_ref_type = ctx.unwrap_ref_type
	_struct_base_and_args = ctx.struct_base_and_args
	_receiver_place = ctx.receiver_place
	_receiver_can_mut_borrow = ctx.receiver_can_mut_borrow
	_receiver_compat = ctx.receiver_compat
	_receiver_preference = ctx.receiver_preference
	_args_match_params = ctx.args_match_params
	_coerce_args_for_params = ctx.coerce_args_for_params
	_infer_receiver_arg_type = ctx.infer_receiver_arg_type
	_instantiate_sig_with_subst = ctx.instantiate_sig_with_subst
	_apply_autoborrow_args = ctx.apply_autoborrow_args
	_label_typeid = ctx.label_typeid
	_trait_label = ctx.trait_label
	_require_for_fn = ctx.require_for_fn
	_extract_conjunctive_facts = ctx.extract_conjunctive_facts
	_subject_name = ctx.subject_name
	_normalize_type_key = ctx.normalize_type_key
	_collect_trait_subjects = ctx.collect_trait_subjects
	_require_failure = ctx.require_failure
	_format_failure_message = ctx.format_failure_message
	_failure_code = ctx.failure_code
	_pick_best_failure = ctx.pick_best_failure
	_param_scope_map = ctx.param_scope_map
	_candidate_key_for_decl = ctx.candidate_key_for_decl
	_visibility_note = ctx.visibility_note
	_intrinsic_method_fn_id = ctx.intrinsic_method_fn_id
	_instantiate_sig = ctx.instantiate_sig
	_self_mode_from_sig = ctx.self_mode_from_sig
	_match_impl_type_args = ctx.match_impl_type_args
	_fixed_width_allowed = ctx.fixed_width_allowed
	_reject_zst_array = ctx.reject_zst_array
	_pretty_type_name = ctx.pretty_type_name
	_format_ctor_signature_list = ctx.format_ctor_signature_list
	_enforce_struct_requires = ctx.enforce_struct_requires
	_ensure_field_visible = ctx.ensure_field_visible
	_visible_modules_for_free_call = ctx.visible_modules_for_free_call
	_infer = ctx.infer
	_format_infer_failure = ctx.format_infer_failure
	_lambda_can_throw = ctx.lambda_can_throw
	module_ids_by_name = ctx.module_ids_by_name
	visibility_provenance = ctx.visibility_provenance
	current_module_name = ctx.current_module_name
	current_module = ctx.current_module
	_debug_stderr = sys.stderr
	type_param_map = ctx.type_param_map
	def _contains_foreign_typevar(ty: TypeId, allowed: set[TypeParamId]) -> bool:
		td = ctx.type_table.get(ty)
		if td.kind is TypeKind.TYPEVAR:
			return td.type_param_id not in allowed
		if td.kind is TypeKind.STRUCT:
			inst = ctx.type_table.get_struct_instance(ty)
			if inst is not None:
				return any(_contains_foreign_typevar(arg, allowed) for arg in inst.type_args)
		if td.kind is TypeKind.VARIANT:
			inst = ctx.type_table.get_variant_instance(ty)
			if inst is not None:
				return any(_contains_foreign_typevar(arg, allowed) for arg in inst.type_args)
		if td.kind is TypeKind.INTERFACE:
			inst = ctx.type_table.get_interface_instance(ty)
			if inst is not None:
				return any(_contains_foreign_typevar(arg, allowed) for arg in inst.type_args)
		for child in td.param_types:
			if _contains_foreign_typevar(child, allowed):
				return True
		return False
	allowed_type_params = set(type_param_map.values()) if isinstance(type_param_map, dict) else set()
	if expected_type is not None and _contains_foreign_typevar(expected_type, allowed_type_params):
		expected_type = None
	intent = CallIntent(expected_return=expected_type)
	default_package = ctx.default_package
	module_packages = ctx.module_packages
	type_param_names = ctx.type_param_names
	fn_id = ctx.current_fn_id
	signatures_by_id = ctx.signatures_by_id
	callable_registry = ctx.callable_registry
	trait_index = ctx.trait_index
	trait_impl_index = ctx.trait_impl_index
	impl_index = ctx.impl_index
	visible_modules = ctx.visible_modules
	visible_trait_world = ctx.visible_trait_world
	global_trait_world = ctx.global_trait_world
	trait_scope_by_module = ctx.trait_scope_by_module
	require_env_local = ctx.require_env_local
	fn_require_assumed = ctx.fn_require_assumed
	_traits_in_scope = ctx.traits_in_scope
	debug_call_resolve = drift_debug.enabled("call_resolve")

	def _debug_call_resolve(msg: str) -> None:
		if not debug_call_resolve:
			return
		print(f"[call_resolve_debug] {msg}", file=_debug_stderr)

	def _debug_if_target_call() -> None:
		if not debug_call_resolve:
			return
		fn_obj = getattr(expr, "fn", None)
		if fn_obj is None:
			return
		fn_name = getattr(fn_obj, "name", None)
		if fn_name not in ("throw_iterator_invalidated", "Next"):
			return
		base_te = getattr(fn_obj, "base_type_expr", None)
		base_mod = getattr(base_te, "module_id", None) if base_te is not None else None
		if fn_name == "Next" and base_te is not None and base_mod != "std.err":
			return
		if fn_name == "throw_iterator_invalidated" and getattr(fn_obj, "module_id", None) != "std.err":
			return
		_debug_call_resolve(
			f"fn={fn_name} base_mod={base_mod} fn_mod={getattr(fn_obj, 'module_id', None)} "
			f"current_module={current_module_name} visible_modules={visible_modules} "
			f"module_ids_by_name_has={('std.err' in module_ids_by_name)}"
		)

	def _propagate_arg_expected_types(intent: CallIntent, arg_types: list[TypeId | None]) -> None:
		if not getattr(expr, "args", None):
			return
		expected_args = list(intent.arg_expected_types or [])
		if not expected_args:
			return
		for idx, arg in enumerate(expr.args):
			if idx >= len(expected_args):
				break
			if not isinstance(arg, (H.HCall, getattr(H, "HInvoke", ()), H.HMapLiteral, H.HArrayLiteral)):
				continue
			exp_ty = expected_args[idx]
			if ctx.type_table.has_typevar(exp_ty):
				continue
			arg.defer_infer_diag = False
			if idx < len(arg_types):
				arg_types[idx] = type_expr(arg, expected_type=exp_ty, used_as_value=False)
				arg.force_inferred_type = exp_ty
				if arg_types[idx] is None or arg_types[idx] == ctx.unknown_ty:
					arg_types[idx] = exp_ty
			else:
				ty = type_expr(arg, expected_type=exp_ty, used_as_value=False)
				arg.force_inferred_type = exp_ty
	def _ref_param_info(param_ty: TypeId) -> tuple[bool, TypeId] | None:
		pdef = ctx.type_table.get(param_ty)
		if pdef.kind is not TypeKind.REF or not pdef.param_types:
			return None
		return bool(pdef.ref_mut), pdef.param_types[0]

	def _can_borrow_coerce(
		params: list[TypeId],
		arg_types: list[TypeId],
	) -> bool:
		world = global_trait_world or visible_trait_world
		if world is None:
			return False
		env = TraitEnv(
			default_module=current_module_name,
			default_package=default_package,
			module_packages=module_packages or {},
			assumed_true=set(fn_require_assumed),
			type_table=ctx.type_table,
		)
		saw_ref_mismatch = False
		for param_ty, arg_ty in zip(params, arg_types):
			if arg_ty is None:
				return False
			ref_info = _ref_param_info(param_ty)
			if ref_info is None:
				if arg_ty != param_ty:
					return False
				continue
			ref_mut, inner = ref_info
			if arg_ty == param_ty or arg_ty == inner:
				continue
			saw_ref_mismatch = True
			trait_name = "BorrowMut" if ref_mut else "Borrow"
			trait_key = trait_key_from_expr(
				parser_ast.TypeExpr(name=trait_name, module_id="std.core"),
				default_module=current_module_name,
				default_package=default_package,
				module_packages=module_packages,
			)
			subject_key = type_key_from_typeid(ctx.type_table, arg_ty)
			inner_key = type_key_from_typeid(ctx.type_table, inner)
			origin = ObligationOrigin(
				kind=ObligationOriginKind.CALLEE_REQUIRE,
				label=f"borrow coercion for '{trait_name}'",
				span=getattr(expr, "loc", Span()),
			)
			obligation = Obligation(
				subject=subject_key,
				trait=trait_key,
				origin=origin,
				trait_args=(inner_key,),
				span=getattr(expr, "loc", Span()),
			)
			failure = prove_obligation(world, env, obligation)
			if failure is not None:
				return False
		return saw_ref_mismatch

	def _type_expr_from_key(key: TypeKey) -> parser_ast.TypeExpr:
		return parser_ast.TypeExpr(
			name=key.name,
			args=[_type_expr_from_key(a) for a in key.args],
			module_id=key.module,
		)

	def _apply_typekey_subst(
		key: TypeKey,
		subst: dict[str, TypeKey],
		type_params: set[str],
	) -> TypeKey:
		if key.name in type_params and key.name in subst:
			return subst[key.name]
		if not key.args:
			return key
		return TypeKey(
			package_id=key.package_id,
			module=key.module,
			name=key.name,
			args=tuple(_apply_typekey_subst(a, subst, type_params) for a in key.args),
		)

	def _match_typekey(
		template: TypeKey,
		actual: TypeKey,
		subst: dict[str, TypeKey],
		type_params: set[str],
	) -> bool:
		if template.name in type_params:
			prev = subst.get(template.name)
			if prev is None:
				subst[template.name] = actual
				return True
			return prev == actual
		if template.name != actual.name or template.module != actual.module or len(template.args) != len(actual.args):
			return False
		return all(_match_typekey(t, a, subst, type_params) for t, a in zip(template.args, actual.args))

	def _borrow_inner_type(
		arg_ty: TypeId,
		*,
		trait_key: TraitKey,
	) -> TypeId | None:
		world = global_trait_world or visible_trait_world
		if world is None:
			return None
		subject_key = type_key_from_typeid(ctx.type_table, arg_ty)
		impl_ids = world.impls_by_trait_target.get((trait_key, subject_key.head()), [])
		for impl_id in impl_ids:
			impl = world.impls[impl_id]
			type_params = set(impl.type_params or [])
			subst: dict[str, TypeKey] = {}
			if not _match_typekey(impl.target, subject_key, subst, type_params):
				continue
			if not impl.trait_args:
				continue
			inner_key = _apply_typekey_subst(impl.trait_args[0], subst, type_params)
			inner_expr = _type_expr_from_key(inner_key)
			try:
				return resolve_opaque_type(inner_expr, ctx.type_table, module_id=current_module_name)
			except Exception:
				return None
		return None

	def _borrow_infer_arg_types(
		params: list[TypeId],
		arg_types: list[TypeId],
	) -> list[TypeId]:
		if len(params) != len(arg_types):
			return list(arg_types)
		out = list(arg_types)
		for idx, (param_ty, arg_ty) in enumerate(zip(params, arg_types)):
			if arg_ty is None:
				continue
			ref_info = _ref_param_info(param_ty)
			if ref_info is None:
				continue
			ref_mut, inner = ref_info
			if arg_ty == param_ty or arg_ty == inner:
				continue
			trait_name = "BorrowMut" if ref_mut else "Borrow"
			trait_key = trait_key_from_expr(
				parser_ast.TypeExpr(name=trait_name, module_id="std.core"),
				default_module=current_module_name,
				default_package=default_package,
				module_packages=module_packages,
			)
			inner_ty = _borrow_inner_type(arg_ty, trait_key=trait_key)
			if inner_ty is None:
				continue
			out[idx] = ctx.type_table.ensure_ref_mut(inner_ty) if ref_mut else ctx.type_table.ensure_ref(inner_ty)
		return out

	if hasattr(H, "HTypeApp") and isinstance(expr.fn, getattr(H, "HTypeApp")):
		type_app = expr.fn
		if getattr(expr, "type_args", None):
			diagnostics.append(_tc_diag(message="E-TYPEARGS-DUP: duplicate type arguments on call", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		expr.type_args = list(type_app.type_args or [])
		expr.fn = type_app.fn
	if isinstance(expr.fn, H.HLambda):
		arg_types = [type_expr(a) for a in expr.args]
		for idx, arg in enumerate(expr.args):
			if isinstance(arg, H.HLambda):
				ty = arg_types[idx]
				if ty is None or ty == ctx.unknown_ty:
					arg_types[idx] = type_expr(arg)
		for idx, arg in enumerate(expr.args):
			if isinstance(arg, H.HLambda):
				ty = arg_types[idx]
				if ty is None or ty == ctx.unknown_ty:
					arg_types[idx] = type_expr(arg)
		kw_pairs = list(getattr(expr, "kwargs", []) or [])
		if getattr(expr, "type_args", None):
			diagnostics.append(_tc_diag(message="type arguments are not supported on lambda calls; apply them on the named function", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		if kw_pairs:
			diagnostics.append(_tc_diag(message="keyword arguments are not supported on lambda calls in v1", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		fn_params = [t if t is not None else ctx.unknown_ty for t in arg_types]
		fn_ret = expected_type if expected_type is not None else ctx.unknown_ty
		callee_expected = ctx.type_table.ensure_function(fn_params, fn_ret, can_throw=True)
		expr.fn.allow_capture_invoke = True
		callee_ty = type_expr(expr.fn, expected_type=callee_expected)
		if callee_ty is None:
			return record_expr(expr, ctx.unknown_ty)
		callee_def = ctx.type_table.get(callee_ty)
		if callee_def.kind is not TypeKind.FUNCTION:
			if drift_debug.enabled("call"):
				try:
					print(f"[debug] call target not function (lambda-call) fn={expr.fn} module={current_module_name}", file=_debug_stderr)
				except Exception:
					pass
			diagnostics.append(_tc_diag(message="call target is not a function value", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		fn_sig_params = list(callee_def.param_types[:-1]) if callee_def.param_types else []
		fn_sig_ret = callee_def.param_types[-1] if callee_def.param_types else ctx.unknown_ty
		if len(fn_sig_params) != len(arg_types):
			diagnostics.append(_tc_diag(message=f"function value expects {len(fn_sig_params)} arguments, got {len(arg_types)}", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, fn_sig_ret)
		for want, have in zip(fn_sig_params, arg_types):
			if have is not None and want != have:
				diagnostics.append(_tc_diag(message=(f"function value argument type mismatch (have {ctx.type_table.get(have).name}, expected {ctx.type_table.get(want).name})"), severity="error", span=getattr(expr, "loc", Span())))
		call_can_throw = callee_def.can_throw()
		if getattr(expr.fn, "can_throw_effective", None) is not None:
			call_can_throw = bool(expr.fn.can_throw_effective)
		record_call_info(expr, param_types=fn_sig_params, return_type=fn_sig_ret, can_throw=call_can_throw, target=CallTarget.indirect(expr.fn.node_id))
		return record_expr(expr, fn_sig_ret)
	if isinstance(expr.fn, H.HField) and isinstance(expr.fn.subject, H.HVar) and (expr.fn.subject.binding_id is None or expr.fn.subject.module_id is not None):
		_module_id = expr.fn.subject.module_id or expr.fn.subject.name
		expr.fn = H.HVar(name=expr.fn.name, binding_id=None, module_id=_module_id)
	if getattr(expr, "type_args", None) and not (isinstance(expr.fn, H.HVar) or (hasattr(H, "HQualifiedMember") and isinstance(expr.fn, getattr(H, "HQualifiedMember")))):
		diagnostics.append(_tc_diag(message="E-TYPEARGS-NOT-ALLOWED: type arguments are only supported on named call targets", severity="error", span=getattr(expr, "loc", Span())))
		return record_expr(expr, ctx.unknown_ty)
	def _is_std_mem_module(mod_name: str | None) -> bool:
		if mod_name is None:
			return False
		if isinstance(mod_name, int):
			std_mem_id = module_ids_by_name.get("std.mem")
			return std_mem_id is not None and mod_name == std_mem_id
		if mod_name == "std.mem":
			return True
		mod_id = module_ids_by_name.get(mod_name)
		if mod_id is None:
			return False
		std_mem_id = module_ids_by_name.get("std.mem")
		if std_mem_id is not None and mod_id == std_mem_id:
			return True
		chain = visibility_provenance.get(mod_id)
		if not chain:
			return False
		return "std.mem" in chain or chain[-1] == "std.mem"
	def _rawbuffer_elem_type(tid: TypeId | None) -> TypeId | None:
		if tid is None:
			return None
		td = ctx.type_table.get(tid)
		if td.kind is TypeKind.REF:
			inner = td.param_types[0] if td.param_types else None
			if inner is None:
				return None
			tid = inner
		base_id, args = ctx.struct_base_and_args(tid)
		if base_id is None:
			return None
		base_td = ctx.type_table.get(base_id)
		if base_td.kind is not TypeKind.STRUCT or base_td.module_id != "std.mem" or base_td.name != "RawBuffer":
			return None
		return args[0] if args else None
	def _raw_ptr_elem_type(tid: TypeId | None, table: object) -> TypeId | None:
		if tid is None:
			return None
		td = table.get(tid)
		if td.kind is not TypeKind.RAW_PTR or not td.param_types:
			return None
		return td.param_types[0]
	def _mut_ref_inner(tid: TypeId | None) -> TypeId | None:
		if tid is None:
			return None
		td = ctx.type_table.get(tid)
		if td.kind is TypeKind.REF and td.ref_mut and td.param_types:
			return td.param_types[0]
		return None
	def _ref_inner(tid: TypeId | None) -> TypeId | None:
		if tid is None:
			return None
		td = ctx.type_table.get(tid)
		if td.kind is TypeKind.REF and not td.ref_mut and td.param_types:
			return td.param_types[0]
		return None
	def _maybe_uninit_inner(tid: TypeId | None) -> TypeId | None:
		if tid is None:
			return None
		td = ctx.type_table.get(tid)
		if td.kind is not TypeKind.REF or not td.param_types:
			return None
		inner = td.param_types[0]
		inner_td = ctx.type_table.get(inner)
		if inner_td.kind is not TypeKind.STRUCT or inner_td.name != "MaybeUninit" or inner_td.module_id != "std.mem":
			return None
		inst = ctx.type_table.get_struct_instance(inner)
		if inst is not None and inst.type_args:
			return inst.type_args[0]
		if inner_td.param_types:
			return inner_td.param_types[0]
		return None
	def _borrowed_place(arg: H.HExpr) -> H.HPlaceExpr | None:
		if isinstance(arg, H.HBorrow) and arg.is_mut:
			return place_expr_from_lvalue_expr(arg.subject)
		if isinstance(arg, H.HPlaceExpr):
			return arg
		return None
	def _canonical_tid(tid: TypeId | TypeParamId | None) -> TypeId | None:
		if tid is None:
			return None
		if isinstance(tid, TypeParamId):
			tp_name = ctx.type_param_names.get(tid) if ctx.type_param_names else None
			return ctx.type_table.ensure_typevar(tid, name=tp_name)
		return tid

	def _intrinsic_kind_for_decl(decl: CallableDecl, sig: object | None) -> IntrinsicKind | None:
		if sig is None or not getattr(sig, "is_intrinsic", False):
			return None
		return getattr(sig, "intrinsic_kind", None)

	if isinstance(expr.fn, H.HVar) and _is_std_mem_module(expr.fn.module_id) and expr.fn.name in ("alloc_uninit", "dealloc", "rawbuffer_ptr", "rawbuffer_cap", "rawbuffer_from_parts", "ptr_at_ref", "ptr_at_mut", "write", "read", "ptr_from_ref", "ptr_from_ref_mut", "ptr_offset", "ptr_read", "ptr_write", "ptr_is_null", "replace", "swap", "maybe_uninit", "maybe_write", "maybe_assume_init_ref", "maybe_assume_init_mut", "maybe_assume_init_read"):
		rawbuffer_allowed = bool(ctx.allow_rawbuffer)
		if call_kwargs_issues(expr.fn.name, getattr(expr, "kwargs", None)):
			first_kw = (getattr(expr, "kwargs", []) or [None])[0]
			diagnostics.append(_tc_diag(message=f"{expr.fn.name} does not support keyword arguments", severity="error", span=getattr(first_kw, "loc", getattr(expr, "loc", Span()))))
			return record_expr(expr, ctx.unknown_ty)
		if expr.fn.name in ("alloc_uninit", "dealloc", "ptr_at_ref", "ptr_at_mut", "write", "read", "ptr_from_ref", "ptr_from_ref_mut", "ptr_offset", "ptr_read", "ptr_write", "ptr_is_null", "maybe_uninit", "maybe_write", "maybe_assume_init_ref", "maybe_assume_init_mut", "maybe_assume_init_read"):
			rawbuffer_only = expr.fn.name in ("alloc_uninit", "dealloc", "ptr_at_ref", "ptr_at_mut", "write", "read")
			if not check_unsafe_call(allow_unsafe=ctx.allow_unsafe, allow_unsafe_without_block=ctx.allow_unsafe_without_block, unsafe_context=ctx.unsafe_context, trusted_module=rawbuffer_allowed, rawbuffer_only=rawbuffer_only, diagnostics=diagnostics, tc_diag=_tc_diag, span=getattr(expr, "loc", Span())):
				return record_expr(expr, ctx.unknown_ty)
		call_type_args = getattr(expr, "type_args", None) or []
		type_arg_ids = [resolve_opaque_type(t, ctx.type_table, module_id=current_module_name, type_params=type_param_map) for t in call_type_args]
		arg_types_local = [type_expr(a, used_as_value=False) for a in expr.args]
		ret_ty: TypeId | None = None
		param_types: list[TypeId] = []
		intrinsic_kind: IntrinsicKind | None = None
		if expr.fn.name == "alloc_uninit":
			if len(type_arg_ids) != 1:
				diagnostics.append(_tc_diag(message="E-RAWBUFFER-TYPEARGS: alloc_uninit<T> requires exactly one type argument", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, ctx.unknown_ty)
			t_elem = type_arg_ids[0]
			rawbuf_tid = ctx.type_table.ensure_named("RawBuffer", module_id="std.mem")
			rawbuf_inst = ctx.type_table.ensure_struct_template(rawbuf_tid, [t_elem]) if ctx.type_table.has_typevar(t_elem) else ctx.type_table.ensure_struct_instantiated(rawbuf_tid, [t_elem])
			param_types = [ctx.int_ty]
			ret_ty = rawbuf_inst
			intrinsic_kind = IntrinsicKind.RAW_ALLOC
		elif expr.fn.name in ("rawbuffer_ptr", "rawbuffer_cap", "rawbuffer_from_parts"):
			if expr.fn.name in ("rawbuffer_ptr", "rawbuffer_cap"):
				if len(expr.args) != 1:
					diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects one argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if len(type_arg_ids) > 1:
					diagnostics.append(_tc_diag(message=f"{expr.fn.name} accepts at most one type argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				t_elem = _rawbuffer_elem_type(arg_types_local[0])
				if t_elem is None and type_arg_ids:
					t_elem = type_arg_ids[0]
				t_elem = _canonical_tid(t_elem)
				if t_elem is None:
					diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects &RawBuffer<T> as the first argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				rawbuf_tid = ctx.type_table.ensure_named("RawBuffer", module_id="std.mem")
				rawbuf_inst = ctx.type_table.ensure_struct_template(rawbuf_tid, [t_elem]) if ctx.type_table.has_typevar(t_elem) else ctx.type_table.ensure_struct_instantiated(rawbuf_tid, [t_elem])
				param_types = [ctx.type_table.ensure_ref(rawbuf_inst)]
				if arg_types_local[0] is not None and arg_types_local[0] != param_types[0]:
					diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects &RawBuffer<T> as the first argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if expr.fn.name == "rawbuffer_ptr":
					ret_ty = ctx.type_table.new_ptr(ctx.byte_ty, module_id="std.mem")
					intrinsic_kind = IntrinsicKind.RAWBUFFER_PTR
				else:
					ret_ty = ctx.int_ty
					intrinsic_kind = IntrinsicKind.RAWBUFFER_CAP
			else:
				if len(expr.args) != 2:
					diagnostics.append(_tc_diag(message="rawbuffer_from_parts expects two arguments", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if len(type_arg_ids) != 1:
					diagnostics.append(_tc_diag(message="rawbuffer_from_parts<T> requires exactly one type argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				t_elem = _canonical_tid(type_arg_ids[0])
				rawbuf_tid = ctx.type_table.ensure_named("RawBuffer", module_id="std.mem")
				rawbuf_inst = ctx.type_table.ensure_struct_template(rawbuf_tid, [t_elem]) if ctx.type_table.has_typevar(t_elem) else ctx.type_table.ensure_struct_instantiated(rawbuf_tid, [t_elem])
				param_types = [ctx.type_table.new_ptr(ctx.byte_ty, module_id="std.mem"), ctx.int_ty]
				ret_ty = rawbuf_inst
				intrinsic_kind = IntrinsicKind.RAWBUFFER_FROM_PARTS
				if arg_types_local[0] is not None and arg_types_local[0] != param_types[0]:
					diagnostics.append(_tc_diag(message="rawbuffer_from_parts expects Ptr<Byte> as the first argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if arg_types_local[1] is not None and arg_types_local[1] != ctx.int_ty:
					diagnostics.append(_tc_diag(message="rawbuffer_from_parts expects Int as the second argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
		elif expr.fn.name in ("maybe_uninit", "maybe_write", "maybe_assume_init_ref", "maybe_assume_init_mut", "maybe_assume_init_read"):
			if len(type_arg_ids) > 1:
				diagnostics.append(_tc_diag(message=f"{expr.fn.name} accepts at most one type argument", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, ctx.unknown_ty)
			t_elem = type_arg_ids[0] if type_arg_ids else None
			if expr.fn.name == "maybe_uninit":
				if len(expr.args) != 0:
					diagnostics.append(_tc_diag(message="maybe_uninit expects no arguments", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if t_elem is None:
					diagnostics.append(_tc_diag(message="maybe_uninit<T> requires a type argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				maybe_tid = ctx.type_table.ensure_named("MaybeUninit", module_id="std.mem")
				maybe_inst = ctx.type_table.ensure_struct_template(maybe_tid, [t_elem]) if ctx.type_table.has_typevar(t_elem) else ctx.type_table.ensure_struct_instantiated(maybe_tid, [t_elem])
				param_types = []
				ret_ty = maybe_inst
				intrinsic_kind = IntrinsicKind.MAYBE_UNINIT
			elif expr.fn.name == "maybe_write":
				if len(expr.args) != 2:
					diagnostics.append(_tc_diag(message="maybe_write expects two arguments", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if t_elem is None:
					t_elem = _maybe_uninit_inner(arg_types_local[0])
				if t_elem is None:
					diagnostics.append(_tc_diag(message="maybe_write expects &mut MaybeUninit<T> as the first argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				maybe_tid = ctx.type_table.ensure_named("MaybeUninit", module_id="std.mem")
				maybe_inst = ctx.type_table.ensure_struct_template(maybe_tid, [t_elem]) if ctx.type_table.has_typevar(t_elem) else ctx.type_table.ensure_struct_instantiated(maybe_tid, [t_elem])
				param_types = [ctx.type_table.ensure_ref_mut(maybe_inst), t_elem]
				if arg_types_local[0] is not None and arg_types_local[0] != param_types[0]:
					diagnostics.append(_tc_diag(message="maybe_write expects &mut MaybeUninit<T> as the first argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if arg_types_local[1] is not None and arg_types_local[1] != t_elem:
					diagnostics.append(_tc_diag(message="maybe_write value type does not match MaybeUninit<T>", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				ret_ty = ctx.type_table.ensure_ref_mut(t_elem)
				intrinsic_kind = IntrinsicKind.MAYBE_WRITE
			elif expr.fn.name in ("maybe_assume_init_ref", "maybe_assume_init_mut", "maybe_assume_init_read"):
				if len(expr.args) != 1:
					diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects one argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if t_elem is None:
					t_elem = _maybe_uninit_inner(arg_types_local[0])
				if t_elem is None:
					diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects a MaybeUninit<T> reference argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				maybe_tid = ctx.type_table.ensure_named("MaybeUninit", module_id="std.mem")
				maybe_inst = ctx.type_table.ensure_struct_template(maybe_tid, [t_elem]) if ctx.type_table.has_typevar(t_elem) else ctx.type_table.ensure_struct_instantiated(maybe_tid, [t_elem])
				if expr.fn.name == "maybe_assume_init_ref":
					param_types = [ctx.type_table.ensure_ref(maybe_inst)]
					ret_ty = ctx.type_table.ensure_ref(t_elem)
					intrinsic_kind = IntrinsicKind.MAYBE_ASSUME_INIT_REF
				elif expr.fn.name == "maybe_assume_init_mut":
					param_types = [ctx.type_table.ensure_ref_mut(maybe_inst)]
					ret_ty = ctx.type_table.ensure_ref_mut(t_elem)
					intrinsic_kind = IntrinsicKind.MAYBE_ASSUME_INIT_MUT
				else:
					param_types = [ctx.type_table.ensure_ref_mut(maybe_inst)]
					ret_ty = t_elem
					intrinsic_kind = IntrinsicKind.MAYBE_ASSUME_INIT_READ
				if arg_types_local[0] is not None and arg_types_local[0] != param_types[0]:
					diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects a MaybeUninit<T> reference argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
		elif expr.fn.name in ("dealloc", "ptr_at_ref", "ptr_at_mut", "write", "read", "ptr_from_ref", "ptr_from_ref_mut", "ptr_offset", "ptr_read", "ptr_write", "ptr_is_null", "replace", "swap"):
			if len(arg_types_local) < 1:
				diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects at least 1 argument", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, ctx.unknown_ty)
			if expr.fn.name == "replace":
				mut_inner = _mut_ref_inner(arg_types_local[0])
				t_elem = _canonical_tid(type_arg_ids[0]) if type_arg_ids else _canonical_tid(mut_inner)
				if t_elem is None:
					diagnostics.append(_tc_diag(message="replace requires a concrete element type", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if len(arg_types_local) != 2:
					diagnostics.append(_tc_diag(message="replace expects two arguments", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if mut_inner is None:
					diagnostics.append(_tc_diag(message="replace expects &mut T as the first argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if arg_types_local[1] is not None and _canonical_tid(arg_types_local[1]) != _canonical_tid(t_elem):
					diagnostics.append(_tc_diag(message="cannot infer type arguments for 'replace': conflicting constraints", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				place_expr = _borrowed_place(expr.args[0])
				if place_expr is None:
					diagnostics.append(_tc_diag(message="replace expects &mut T as the first argument", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if any(isinstance(p, H.HPlaceDeref) for p in place_expr.projections) and isinstance(place_expr.base, H.HVar):
					base_ty = ctx.type_expr(place_expr.base, used_as_value=False)
					if base_ty is not None:
						base_def = ctx.type_table.get(base_ty)
						if base_def.kind is TypeKind.REF and not base_def.ref_mut:
							diagnostics.append(_tc_diag(message=f"cannot write through *{place_expr.base.name} unless {place_expr.base.name} is a mutable reference", severity="error", span=getattr(expr, "loc", Span())))
							return record_expr(expr, ctx.unknown_ty)
				param_types = [ctx.type_table.ensure_ref_mut(t_elem), t_elem]
				ret_ty = t_elem
				intrinsic_kind = IntrinsicKind.REPLACE
				record_call_info(expr, param_types=param_types, return_type=ret_ty, can_throw=False, target=CallTarget.intrinsic(intrinsic_kind))
				return record_expr(expr, ret_ty)
			if expr.fn.name == "swap":
				t_elem = _canonical_tid(type_arg_ids[0]) if type_arg_ids else _canonical_tid(_mut_ref_inner(arg_types_local[0]))
				if t_elem is None:
					diagnostics.append(_tc_diag(message="swap requires a concrete element type", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if len(arg_types_local) != 2:
					diagnostics.append(_tc_diag(message="swap expects two arguments", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if type_arg_ids and _canonical_tid(_mut_ref_inner(arg_types_local[0])) != _canonical_tid(t_elem):
					diagnostics.append(_tc_diag(message="cannot infer type arguments for 'swap': conflicting constraints", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if _mut_ref_inner(arg_types_local[0]) is None or _mut_ref_inner(arg_types_local[1]) is None:
					diagnostics.append(_tc_diag(message="swap expects &mut T arguments", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if _canonical_tid(_mut_ref_inner(arg_types_local[1])) != _canonical_tid(t_elem):
					diagnostics.append(_tc_diag(message="cannot infer type arguments for 'swap': conflicting constraints", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				left_place = _borrowed_place(expr.args[0])
				right_place = _borrowed_place(expr.args[1])
				if left_place is None or right_place is None:
					diagnostics.append(_tc_diag(message="swap expects &mut T arguments", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if left_place.base.binding_id == right_place.base.binding_id and left_place.projections == right_place.projections:
					diagnostics.append(_tc_diag(message="swap operands must be distinct non-overlapping places", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				if any(isinstance(p, H.HPlaceDeref) for p in left_place.projections) and isinstance(left_place.base, H.HVar):
					base_ty = ctx.type_expr(left_place.base, used_as_value=False)
					if base_ty is not None:
						base_def = ctx.type_table.get(base_ty)
						if base_def.kind is TypeKind.REF and not base_def.ref_mut:
							diagnostics.append(_tc_diag(message=f"cannot write through *{left_place.base.name} unless {left_place.base.name} is a mutable reference", severity="error", span=getattr(expr, "loc", Span())))
							return record_expr(expr, ctx.unknown_ty)
				if any(isinstance(p, H.HPlaceDeref) for p in right_place.projections) and isinstance(right_place.base, H.HVar):
					base_ty = ctx.type_expr(right_place.base, used_as_value=False)
					if base_ty is not None:
						base_def = ctx.type_table.get(base_ty)
						if base_def.kind is TypeKind.REF and not base_def.ref_mut:
							diagnostics.append(_tc_diag(message=f"cannot write through *{right_place.base.name} unless {right_place.base.name} is a mutable reference", severity="error", span=getattr(expr, "loc", Span())))
							return record_expr(expr, ctx.unknown_ty)
				param_types = [ctx.type_table.ensure_ref_mut(t_elem), ctx.type_table.ensure_ref_mut(t_elem)]
				ret_ty = ctx.void_ty
				intrinsic_kind = IntrinsicKind.SWAP
				record_call_info(expr, param_types=param_types, return_type=ret_ty, can_throw=False, target=CallTarget.intrinsic(intrinsic_kind))
				return record_expr(expr, ret_ty)
			if expr.fn.name in ("ptr_from_ref", "ptr_from_ref_mut", "ptr_offset", "ptr_read", "ptr_write", "ptr_is_null", "ptr_as_mut_ref"):
				t_elem = None
				if expr.fn.name in ("ptr_from_ref", "ptr_from_ref_mut"):
					if len(arg_types_local) != 1:
						diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects exactly one argument", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					t_elem = _ref_inner(arg_types_local[0]) if expr.fn.name == "ptr_from_ref" else _mut_ref_inner(arg_types_local[0])
					if t_elem is None:
						diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects a reference argument", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					param_types = [ctx.type_table.ensure_ref(t_elem)] if expr.fn.name == "ptr_from_ref" else [ctx.type_table.ensure_ref_mut(t_elem)]
					ret_ty = ctx.type_table.new_ptr(t_elem, module_id="std.mem")
					intrinsic_kind = IntrinsicKind.PTR_FROM_REF if expr.fn.name == "ptr_from_ref" else IntrinsicKind.PTR_FROM_REF_MUT
				elif expr.fn.name == "ptr_offset":
					if len(arg_types_local) != 2:
						diagnostics.append(_tc_diag(message="ptr_offset expects two arguments", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					t_elem = _raw_ptr_elem_type(arg_types_local[0], ctx.type_table)
					if t_elem is None:
						diagnostics.append(_tc_diag(message="ptr_offset expects Ptr<T> as the first argument", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					param_types = [ctx.type_table.new_ptr(t_elem, module_id="std.mem"), ctx.int_ty]
					ret_ty = ctx.type_table.new_ptr(t_elem, module_id="std.mem")
					intrinsic_kind = IntrinsicKind.PTR_OFFSET
				elif expr.fn.name == "ptr_read":
					if len(arg_types_local) != 1:
						diagnostics.append(_tc_diag(message="ptr_read expects exactly one argument", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					t_elem = _raw_ptr_elem_type(arg_types_local[0], ctx.type_table)
					if t_elem is None:
						diagnostics.append(_tc_diag(message="ptr_read expects Ptr<T> as the first argument", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					param_types = [ctx.type_table.new_ptr(t_elem, module_id="std.mem")]
					ret_ty = t_elem
					intrinsic_kind = IntrinsicKind.PTR_READ
				elif expr.fn.name == "ptr_write":
					t_elem = _raw_ptr_elem_type(arg_types_local[0], ctx.type_table)
					if t_elem is None:
						diagnostics.append(_tc_diag(message="ptr_write expects Ptr<T> as the first argument", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					if len(arg_types_local) != 2:
						diagnostics.append(_tc_diag(message="ptr_write expects two arguments", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					if arg_types_local[1] is not None and _canonical_tid(arg_types_local[1]) != _canonical_tid(t_elem):
						diagnostics.append(_tc_diag(message="ptr_write value type does not match Ptr<T>", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					param_types = [ctx.type_table.new_ptr(t_elem, module_id="std.mem"), t_elem]
					ret_ty = ctx.void_ty
					intrinsic_kind = IntrinsicKind.PTR_WRITE
				elif expr.fn.name == "ptr_is_null":
					if len(arg_types_local) != 1:
						diagnostics.append(_tc_diag(message="ptr_is_null expects exactly one argument", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					t_elem = _raw_ptr_elem_type(arg_types_local[0], ctx.type_table)
					if t_elem is None:
						diagnostics.append(_tc_diag(message="ptr_is_null expects Ptr<T> as the first argument", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					param_types = [ctx.type_table.new_ptr(t_elem, module_id="std.mem")]
					ret_ty = ctx.bool_ty
					intrinsic_kind = IntrinsicKind.PTR_IS_NULL
				elif expr.fn.name == "ptr_as_mut_ref":
					if len(arg_types_local) != 1:
						diagnostics.append(_tc_diag(message="ptr_as_mut_ref expects exactly one argument", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					t_elem = _raw_ptr_elem_type(arg_types_local[0], ctx.type_table)
					if t_elem is None:
						diagnostics.append(_tc_diag(message="ptr_as_mut_ref expects Ptr<T> as the first argument", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					param_types = [ctx.type_table.new_ptr(t_elem, module_id="std.mem")]
					ret_ty = ctx.type_table.ensure_ref_mut(t_elem)
					intrinsic_kind = IntrinsicKind.PTR_AS_MUT_REF
			else:
				t_elem = _rawbuffer_elem_type(arg_types_local[0])
				if t_elem is None and type_arg_ids:
					t_elem = type_arg_ids[0]
				t_elem = _canonical_tid(t_elem)
				if t_elem is None:
					diagnostics.append(_tc_diag(message=f"{expr.fn.name} requires RawBuffer<T> receiver", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
				rawbuf_tid = ctx.type_table.ensure_named("RawBuffer", module_id="std.mem")
				rawbuf_inst = ctx.type_table.ensure_struct_template(rawbuf_tid, [t_elem]) if ctx.type_table.has_typevar(t_elem) else ctx.type_table.ensure_struct_instantiated(rawbuf_tid, [t_elem])
				if expr.fn.name == "dealloc":
					param_types = [rawbuf_inst]
					ret_ty = ctx.void_ty
					intrinsic_kind = IntrinsicKind.RAW_DEALLOC
				elif expr.fn.name == "ptr_at_ref":
					param_types = [ctx.type_table.ensure_ref(rawbuf_inst), ctx.int_ty]
					ret_ty = ctx.type_table.ensure_ref(t_elem)
					intrinsic_kind = IntrinsicKind.RAW_PTR_AT_REF
				elif expr.fn.name == "ptr_at_mut":
					param_types = [ctx.type_table.ensure_ref_mut(rawbuf_inst), ctx.int_ty]
					ret_ty = ctx.type_table.ensure_ref_mut(t_elem)
					intrinsic_kind = IntrinsicKind.RAW_PTR_AT_MUT
				elif expr.fn.name == "write":
					if len(arg_types_local) != 3:
						diagnostics.append(_tc_diag(message="write expects three arguments", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					param_types = [ctx.type_table.ensure_ref_mut(rawbuf_inst), ctx.int_ty, t_elem]
					ret_ty = ctx.void_ty
					intrinsic_kind = IntrinsicKind.RAW_WRITE
					if arg_types_local[1] is not None and arg_types_local[1] != ctx.int_ty:
						diagnostics.append(_tc_diag(message="write expects Int as the index", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					if arg_types_local[2] is not None and _canonical_tid(arg_types_local[2]) != _canonical_tid(t_elem):
						# Allow value-type mismatch for generic MVP paths; field type checking is enforced at use-sites.
						return record_expr(expr, ret_ty)
				elif expr.fn.name == "read":
					if len(arg_types_local) != 2:
						diagnostics.append(_tc_diag(message="read expects two arguments", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					param_types = [ctx.type_table.ensure_ref_mut(rawbuf_inst), ctx.int_ty]
					ret_ty = t_elem
					intrinsic_kind = IntrinsicKind.RAW_READ
					if arg_types_local[1] is not None and arg_types_local[1] != ctx.int_ty:
						diagnostics.append(_tc_diag(message="read expects Int as the index", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
		if ret_ty is None:
			return record_expr(expr, ctx.unknown_ty)
		if intrinsic_kind is None:
			diagnostics.append(_tc_diag(message=f"{expr.fn.name} intrinsic kind missing (checker bug)", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		record_call_info(expr, param_types=param_types, return_type=ret_ty, can_throw=False, target=CallTarget.intrinsic(intrinsic_kind))
		return record_expr(expr, ret_ty)

	if isinstance(expr.fn, H.HVar) and expr.fn.name in ("byte_length", "string_byte_at", "string_eq", "string_concat"):
		if call_kwargs_issues(expr.fn.name, getattr(expr, "kwargs", None)):
			first_kw = (getattr(expr, "kwargs", []) or [None])[0]
			diagnostics.append(_tc_diag(message=f"{expr.fn.name} does not support keyword arguments", severity="error", span=getattr(first_kw, "loc", getattr(expr, "loc", Span()))))
			return record_expr(expr, ctx.unknown_ty)
		if getattr(expr, "type_args", None):
			diagnostics.append(_tc_diag(message=f"{expr.fn.name} does not accept type arguments", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		arg_types_local = [type_expr(a) for a in expr.args]
		if expr.fn.name == "byte_length":
			if not (isinstance(current_module_name, str) and current_module_name.startswith("std.")):
				diagnostics.append(_tc_diag(message="global byte_length(...) is not exposed; use s.byte_length()", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, ctx.unknown_ty)
			if len(arg_types_local) != 1:
				diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects 1 argument", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, ctx.unknown_ty)
			arg_ty = arg_types_local[0]
			if arg_ty == ctx.string_ty:
				place_expr = place_expr_from_lvalue_expr(expr.args[0])
				if place_expr is None:
					expr.args[0] = H.HBorrow(subject=expr.args[0], is_mut=False, allow_rvalue=True)
				else:
					expr.args[0] = H.HBorrow(subject=place_expr, is_mut=False)
				param_types = [ctx.type_table.ensure_ref(ctx.string_ty)]
			else:
				param_types = [arg_ty] if arg_ty is not None else []
				if arg_ty is None:
					return record_expr(expr, ctx.unknown_ty)
				td = ctx.type_table.get(arg_ty)
				if td.kind is not TypeKind.REF or not td.param_types or td.param_types[0] != ctx.string_ty:
					diagnostics.append(_tc_diag(message=f"no matching overload for function '{expr.fn.name}' with args {arg_types_local}", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
			record_call_info(expr, param_types=param_types, return_type=ctx.int_ty, can_throw=False, target=CallTarget.intrinsic(IntrinsicKind.BYTE_LENGTH))
			return record_expr(expr, ctx.int_ty)
		if expr.fn.name == "string_byte_at":
			if len(arg_types_local) != 2:
				diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects 2 arguments", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, ctx.unknown_ty)
			arg0_ty = arg_types_local[0]
			arg1_ty = arg_types_local[1]
			if arg0_ty == ctx.string_ty:
				place_expr = place_expr_from_lvalue_expr(expr.args[0])
				if place_expr is None:
					expr.args[0] = H.HBorrow(subject=expr.args[0], is_mut=False, allow_rvalue=True)
				else:
					expr.args[0] = H.HBorrow(subject=place_expr, is_mut=False)
				arg0_ty = ctx.type_table.ensure_ref(ctx.string_ty)
			if arg0_ty is None or arg1_ty is None:
				return record_expr(expr, ctx.unknown_ty)
			td0 = ctx.type_table.get(arg0_ty)
			if td0.kind is not TypeKind.REF or not td0.param_types or td0.param_types[0] != ctx.string_ty:
				diagnostics.append(_tc_diag(message=f"no matching overload for function '{expr.fn.name}' with args {arg_types_local}", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, ctx.unknown_ty)
			if arg1_ty != ctx.int_ty:
				diagnostics.append(_tc_diag(message=f"no matching overload for function '{expr.fn.name}' with args {arg_types_local}", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, ctx.unknown_ty)
			record_call_info(expr, param_types=[arg0_ty, arg1_ty], return_type=ctx.byte_ty, can_throw=False, target=CallTarget.intrinsic(IntrinsicKind.STRING_BYTE_AT))
			return record_expr(expr, ctx.byte_ty)
		if expr.fn.name in ("string_eq", "string_concat"):
			if len(arg_types_local) != 2:
				diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects 2 arguments", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, ctx.unknown_ty)
			if arg_types_local[0] != ctx.string_ty or arg_types_local[1] != ctx.string_ty:
				diagnostics.append(_tc_diag(message=f"no matching overload for function '{expr.fn.name}' with args {arg_types_local}", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, ctx.unknown_ty)
			ret_ty = ctx.string_ty if expr.fn.name == "string_concat" else ctx.bool_ty
			intrinsic = IntrinsicKind.STRING_CONCAT if expr.fn.name == "string_concat" else IntrinsicKind.STRING_EQ
			record_call_info(expr, param_types=[ctx.string_ty, ctx.string_ty], return_type=ret_ty, can_throw=False, target=CallTarget.intrinsic(intrinsic))
			return record_expr(expr, ret_ty)

	if isinstance(expr.fn, H.HVar) and _is_std_core_module(expr.fn.module_id, module_ids_by_name, visibility_provenance) and expr.fn.name in ("callback0", "callback1", "callback2", "callback_throw0", "callback_throw1", "callback_throw2"):
		if getattr(expr, "type_args", None):
			diagnostics.append(_tc_diag(message=f"{expr.fn.name} does not accept type arguments", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		if call_kwargs_issues(expr.fn.name, getattr(expr, "kwargs", None)):
			first_kw = (getattr(expr, "kwargs", []) or [None])[0]
			diagnostics.append(_tc_diag(message=f"{expr.fn.name} does not support keyword arguments", severity="error", span=getattr(first_kw, "loc", getattr(expr, "loc", Span()))))
			return record_expr(expr, ctx.unknown_ty)
		if len(expr.args) != 1:
			diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects exactly one argument", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		if expr.fn.name == "callback0":
			want_args = 0
			cb_base = ctx.type_table.get_interface_base(module_id="std.core", name="Callback0")
			intrinsic_kind = IntrinsicKind.CALLBACK0
			expected_type_args = 1
		elif expr.fn.name == "callback1":
			want_args = 1
			cb_base = ctx.type_table.get_interface_base(module_id="std.core", name="Callback1")
			intrinsic_kind = IntrinsicKind.CALLBACK1
			expected_type_args = 2
		elif expr.fn.name == "callback2":
			want_args = 2
			cb_base = ctx.type_table.get_interface_base(module_id="std.core", name="Callback2")
			intrinsic_kind = IntrinsicKind.CALLBACK2
			expected_type_args = 3
		elif expr.fn.name == "callback_throw0":
			want_args = 0
			cb_base = ctx.type_table.get_interface_base(module_id="std.core", name="CallbackThrow0")
			intrinsic_kind = IntrinsicKind.CALLBACK_THROW0
			expected_type_args = 1
		elif expr.fn.name == "callback_throw1":
			want_args = 1
			cb_base = ctx.type_table.get_interface_base(module_id="std.core", name="CallbackThrow1")
			intrinsic_kind = IntrinsicKind.CALLBACK_THROW1
			expected_type_args = 2
		else:
			want_args = 2
			cb_base = ctx.type_table.get_interface_base(module_id="std.core", name="CallbackThrow2")
			intrinsic_kind = IntrinsicKind.CALLBACK_THROW2
			expected_type_args = 3
		if cb_base is None:
			diagnostics.append(_tc_diag(message="callback interface type not found (compiler bug)", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		arg_expr = expr.args[0]
		arg_expected_type: TypeId | None = None
		if isinstance(arg_expr, H.HLambda):
			# Guard: reject user-written callback0(lambda_with_borrow).  Implicit
			# wraps (created by _wrap_explicit_capture_callbacks) skip this check;
			# escape enforcement for those is deferred to the borrow checker.
			if not getattr(expr, "_is_implicit_wrap", False):
				_ec = getattr(arg_expr, "explicit_captures", None) or []
				if any(getattr(c, "kind", None) in ("ref", "ref_mut") for c in _ec):
					diagnostics.append(_tc_diag(
						message="closures with borrowed captures are non-escaping in v0; only immediate invocation or proven non-retaining params are supported",
						severity="error",
						span=getattr(arg_expr, "loc", getattr(expr, "loc", Span())),
					))
					return record_expr(expr, ctx.unknown_ty)
			arg_expr.allow_capture_invoke = True
			arg_expr.capture_as_move = True
			is_throw = expr.fn.name in ("callback_throw0", "callback_throw1", "callback_throw2")
			if expected_type is None:
				hint = getattr(expr, "expected_type_hint", None)
				if hint is not None:
					expected_type = hint
			if expected_type is not None:
				inst = ctx.type_table.get_interface_instance(expected_type)
				if inst is None or inst.base_id != cb_base:
					hint = getattr(expr, "expected_type_hint", None)
					if hint is not None:
						expected_type = hint
						inst = ctx.type_table.get_interface_instance(expected_type)
				if inst is not None and inst.base_id == cb_base and len(inst.type_args) == expected_type_args:
					if expr.fn.name in ("callback0", "callback_throw0"):
						ret_ty = inst.type_args[0]
						param_types = []
					elif expr.fn.name in ("callback1", "callback_throw1"):
						param_types = [inst.type_args[0]]
						ret_ty = inst.type_args[1]
					else:
						param_types = [inst.type_args[0], inst.type_args[1]]
						ret_ty = inst.type_args[2]
					arg_expected_type = ctx.type_table.ensure_function(param_types, ret_ty, can_throw=is_throw)
			if arg_expected_type is None:
				fallback_params: list[TypeId] = []
				for p in arg_expr.params:
					if getattr(p, "type", None) is None:
						fallback_params.append(ctx.unknown_ty)
						continue
					try:
						fallback_params.append(resolve_opaque_type(p.type, ctx.type_table, module_id=current_module_name))
					except Exception:
						fallback_params.append(ctx.unknown_ty)
				if getattr(arg_expr, "ret_type", None) is not None:
					try:
						ret_ty = resolve_opaque_type(arg_expr.ret_type, ctx.type_table, module_id=current_module_name)
					except Exception:
						ret_ty = ctx.unknown_ty
				else:
					ret_ty = ctx.unknown_ty
				arg_expected_type = ctx.type_table.ensure_function(fallback_params, ret_ty, can_throw=is_throw)
				arg_expr.expected_fn_inferred = True
		if arg_expected_type is not None:
			arg_ty = type_expr(arg_expr, expected_type=arg_expected_type, used_as_value=False)
		else:
			arg_ty = type_expr(arg_expr, used_as_value=False)
		if arg_ty is None:
			return record_expr(expr, ctx.unknown_ty)
		arg_def = ctx.type_table.get(arg_ty)
		if arg_def.kind is not TypeKind.FUNCTION:
			diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects a function value", severity="error", span=getattr(expr.args[0], "loc", getattr(expr, "loc", Span()))))
			return record_expr(expr, ctx.unknown_ty)
		param_types = list(arg_def.param_types or [])
		if not param_types:
			diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects a function value", severity="error", span=getattr(expr.args[0], "loc", getattr(expr, "loc", Span()))))
			return record_expr(expr, ctx.unknown_ty)
		ret_ty = param_types[-1]
		argc = len(param_types) - 1
		if argc != want_args:
			diagnostics.append(_tc_diag(message=f"{expr.fn.name} expects a function with {want_args} argument(s)", severity="error", span=getattr(expr.args[0], "loc", getattr(expr, "loc", Span()))))
			return record_expr(expr, ctx.unknown_ty)
		is_throw = expr.fn.name in ("callback_throw0", "callback_throw1", "callback_throw2")
		if not is_throw and arg_def.fn_throws:
			diagnostics.append(_tc_diag(message=f"{expr.fn.name} requires a nothrow function", severity="error", span=getattr(expr.args[0], "loc", getattr(expr, "loc", Span()))))
			return record_expr(expr, ctx.unknown_ty)
		if expr.fn.name in ("callback0", "callback_throw0"):
			type_args = [ret_ty]
		elif expr.fn.name in ("callback1", "callback_throw1"):
			type_args = [param_types[0], ret_ty]
		else:
			type_args = [param_types[0], param_types[1], ret_ty]
		if any(ctx.type_table.has_typevar(t) for t in type_args):
			cb_ty = ctx.type_table.ensure_interface_template(cb_base, type_args)
		else:
			cb_ty = ctx.type_table.ensure_interface_instantiated(cb_base, type_args)
		if expected_type is not None:
			inst = ctx.type_table.get_interface_instance(expected_type)
			if inst is not None and inst.base_id == cb_base and len(inst.type_args) == expected_type_args:
				if not any(ctx.type_table.has_typevar(t) for t in inst.type_args):
					if list(inst.type_args) == list(type_args):
						cb_ty = expected_type
		record_call_info(expr, param_types=[arg_ty], return_type=cb_ty, can_throw=False, target=CallTarget.intrinsic(intrinsic_kind))
		return record_expr(expr, cb_ty)

	if isinstance(expr.fn, H.HLambda):
		lam = expr.fn
		if call_kwargs_issues("lambda calls", getattr(expr, "kwargs", None)):
			diagnostics.append(_tc_diag(message="keyword arguments are only supported for struct constructors in v1", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		arg_types = [type_expr(a) for a in expr.args]
		if len(arg_types) != len(lam.params):
			diagnostics.append(_tc_diag(message=f"lambda expects {len(lam.params)} arguments, got {len(arg_types)}", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		lambda_ret_type: TypeId | None = None
		if getattr(lam, "ret_type", None) is not None:
			try:
				lambda_ret_type = resolve_opaque_type(lam.ret_type, ctx.type_table, module_id=current_module_name)
			except Exception:
				lambda_ret_type = None
		if expected_type is not None:
			lambda_ret_type = expected_type
		call_ret = lambda_ret_type or ctx.unknown_ty
		can_throw = _lambda_can_throw(lam, None)
		lam.can_throw_effective = bool(can_throw)
		fn_ty = ctx.type_table.ensure_function(arg_types, call_ret, can_throw=bool(can_throw))
		record_call_info(expr, param_types=arg_types, return_type=call_ret, can_throw=can_throw, target=CallTarget.indirect(lam.node_id))
		intent.arg_expected_types = _expected_arg_types_for_call(list(arg_types), len(expr.args))
		_propagate_arg_expected_types(intent, arg_types)
		return record_expr(expr, call_ret)

	if hasattr(H, "HQualifiedMember") and isinstance(expr.fn, getattr(H, "HQualifiedMember")):
		qm = expr.fn
		kw_pairs = getattr(expr, "kwargs", []) or []
		arg_exprs = list(expr.args)
		for arg in arg_exprs:
			if isinstance(arg, (H.HCall, getattr(H, "HInvoke", ()))):
				if isinstance(arg.fn, H.HQualifiedMember):
					arg.defer_infer_diag = True
				else:
					arg.defer_infer_diag = False
			elif isinstance(arg, (H.HMapLiteral, H.HArrayLiteral)):
				arg.defer_infer_diag = True
		kw_value_types = [type_expr(kw.value, used_as_value=False) for kw in kw_pairs]
		call_type_args = getattr(expr, "type_args", None) or []
		call_type_args_span = None
		type_arg_ids: list[TypeId] | None = None
		if call_type_args:
			first_loc = getattr(call_type_args[0], "loc", None)
			if first_loc is not None:
				call_type_args_span = Span.from_loc(first_loc)
			type_arg_ids = [resolve_opaque_type(t, ctx.type_table, module_id=current_module_name, type_params=type_param_map) for t in call_type_args]
		ctor_res = None
		if not call_type_args and not getattr(expr, "kwargs", None) and not expr.args:
			base_te = getattr(qm, "base_type_expr", None)
			if base_te is not None:
				base_mod = getattr(base_te, "module_id", None) or getattr(base_te, "module_alias", None) or current_module_name
				base_tid = resolve_opaque_type(base_te, ctx.type_table, module_id=base_mod, type_params=type_param_map, allow_generic_base=True)
				if drift_debug.enabled("call_resolve") and getattr(qm, "member", None) in ("ReturnBusy", "Closed", "Cancelled", "Failed"):
					schema_dbg = ctx.type_table.get_variant_schema(base_tid) if base_tid is not None else None
					arm_names = [a.name for a in schema_dbg.arms] if schema_dbg is not None else None
					print(f"[call_resolve] qmem {qm.member} csid={getattr(expr, 'callsite_id', None)} base_mod={base_mod} base_tid={base_tid} arms={arm_names}", file=_debug_stderr)
				if base_tid is not None:
					schema = ctx.type_table.get_variant_schema(base_tid)
					if schema is not None and not schema.type_params:
						arm_schema = next((a for a in schema.arms if a.name == qm.member), None)
						if arm_schema is not None and not arm_schema.fields:
							if drift_debug.enabled("call_resolve") and getattr(qm, "member", None) in ("ReturnBusy", "Closed", "Cancelled", "Failed"):
								print(f"[call_resolve] qmem {qm.member} csid={getattr(expr, 'callsite_id', None)} ctor_res preset via zero-field fastpath", file=_debug_stderr)
							ctor_res = VariantCtorResolveResult(base_tid, [], [], [])
		arg_types = [type_expr(a, used_as_value=False) for a in arg_exprs]
		for idx, arg in enumerate(arg_exprs):
			if isinstance(arg, H.HLambda):
				ty = arg_types[idx]
				if ty is None or ty == ctx.unknown_ty:
					arg_types[idx] = type_expr(arg)
		if kw_pairs and not arg_exprs:
			arg_types = list(kw_value_types)
		if ctor_res is None:
			ctor_res = resolve_qualified_member_call(_make_resolver_ctx(ctx, diagnostics=diagnostics, current_module_name=current_module_name, default_package=default_package, module_packages=module_packages, type_param_map=type_param_map, tc_diag=_tc_diag, fixed_width_allowed=_fixed_width_allowed, reject_zst_array=_reject_zst_array, pretty_type_name=_pretty_type_name, format_ctor_signature_list=_format_ctor_signature_list, instantiate_sig=_instantiate_sig, enforce_struct_requires=_enforce_struct_requires, ensure_field_visible=_ensure_field_visible, visible_modules_for_free_call=_visible_modules_for_free_call, module_ids_by_name=module_ids_by_name, visibility_provenance=visibility_provenance, infer=_infer, format_infer_failure=_format_infer_failure, lambda_can_throw=_lambda_can_throw), qm, arg_exprs=list(arg_exprs), arg_types=arg_types, kw_pairs=kw_pairs, expected_type=expected_type, type_arg_ids=type_arg_ids, allow_infer=True, call_type_args_span=call_type_args_span)
		if ctor_res is None:
			base_te = getattr(qm, "base_type_expr", None)
			if base_te is not None and not arg_exprs and not kw_pairs and not call_type_args:
				base_mod = getattr(base_te, "module_id", None) or getattr(base_te, "module_alias", None) or current_module_name
				base_tid = resolve_opaque_type(base_te, ctx.type_table, module_id=base_mod, type_params=type_param_map, allow_generic_base=True)
				if base_tid is not None:
					schema = ctx.type_table.get_variant_schema(base_tid)
					if schema is not None and not schema.type_params:
						arm_schema = next((a for a in schema.arms if a.name == qm.member), None)
						if arm_schema is not None and not arm_schema.fields:
							ctor_res = VariantCtorResolveResult(base_tid, [], [], [])
		if ctor_res is not None:
			inst_params = list(ctor_res.inst_params)
			inst_return = ctor_res.inst_return
			expr.args = list(ctor_res.ctor_args)
			expr.kwargs = []
			expr.ctor_arg_field_indices = list(ctor_res.ctor_arg_field_indices)
			ctor_mod = getattr(qm.base_type_expr, "module_id", None) or getattr(qm.base_type_expr, "module_alias", None) or current_module_name
			# Replace qualified member with a plain name to satisfy typed-mode invariants.
			expr.fn = H.HVar(name=qm.member, module_id=ctor_mod)
			setattr(expr, "_resolved_ctor_return", inst_return)
			setattr(expr, "_resolved_ctor_info", (tuple(inst_params), inst_return, tuple(ctor_res.ctor_arg_field_indices), qm.member))
			intent.arg_expected_types = _expected_arg_types_for_call(list(inst_params), len(arg_exprs))
			_propagate_arg_expected_types(intent, arg_types)
			record_call_info(expr, param_types=inst_params, return_type=inst_return, can_throw=False, target=CallTarget.constructor(ctor_res.inst_return, qm.member, ctor_arg_field_indices=tuple(ctor_res.ctor_arg_field_indices)))
			return record_expr(expr, inst_return)
		method_res = resolve_nonvariant_qualified_static_call(
			_make_resolver_ctx(ctx, diagnostics=diagnostics, current_module_name=current_module_name, default_package=default_package, module_packages=module_packages, type_param_map=type_param_map, tc_diag=_tc_diag, fixed_width_allowed=_fixed_width_allowed, reject_zst_array=_reject_zst_array, pretty_type_name=_pretty_type_name, format_ctor_signature_list=_format_ctor_signature_list, instantiate_sig=_instantiate_sig, enforce_struct_requires=_enforce_struct_requires, ensure_field_visible=_ensure_field_visible, visible_modules_for_free_call=_visible_modules_for_free_call, module_ids_by_name=module_ids_by_name, visibility_provenance=visibility_provenance, infer=_infer, format_infer_failure=_format_infer_failure, lambda_can_throw=_lambda_can_throw),
			qm,
			arg_types=arg_types,
			expected_type=expected_type,
			type_arg_ids=type_arg_ids,
			allow_infer=True,
			call_type_args_span=call_type_args_span,
		)
		if method_res is None:
			method_ctx = _make_method_ctx(ctx, diagnostics=diagnostics, traits_in_scope=_traits_in_scope, trait_key=None)
			method_res = resolve_qualified_member_ufcs(method_ctx, expr, qm, expected_type=expected_type, type_arg_ids=type_arg_ids, call_type_args_span=call_type_args_span, call_origin=getattr(expr, "origin", None), recv_arg_type=arg_types[0] if arg_types else None, arg_type_ids=arg_types)
		if method_res is not None and method_res.call_info is None and method_res.resolution is not None and getattr(method_res.resolution, "decl", None) is not None:
			decl = method_res.resolution.decl
			fn_id_local = getattr(decl, "fn_id", None)
			if fn_id_local is not None:
				sig_for_throw = signatures_by_id.get(fn_id_local) if signatures_by_id is not None else None
				fallback_param_types = tuple(decl.signature.param_types) if decl.signature is not None else tuple()
				fallback_ret = decl.signature.result_type if decl.signature is not None else ctx.unknown_ty
				if sig_for_throw is not None:
					if sig_for_throw.param_type_ids:
						fallback_param_types = tuple(sig_for_throw.param_type_ids)
					if sig_for_throw.return_type_id is not None:
						fallback_ret = sig_for_throw.return_type_id
				fallback_can_throw = bool(getattr(sig_for_throw, "declared_can_throw", False)) if sig_for_throw is not None else False
				fallback_terminal = bool(getattr(sig_for_throw, "declared_terminal_throws", False)) if sig_for_throw is not None else False
				method_res = MethodCallResult(
					method_res.return_type,
					CallInfo(
						target=CallTarget.direct(fn_id_local),
						sig=CallSig(
							param_types=fallback_param_types,
							user_ret_type=fallback_ret,
							can_throw=fallback_can_throw,
							includes_callee=False,
							declared_terminal_throws=fallback_terminal,
						),
					),
					method_res.resolution,
				)
		if (
			method_res is not None
			and method_res.call_info is not None
			and method_res.resolution is not None
			and getattr(method_res.resolution, "decl", None) is not None
		):
			decl = method_res.resolution.decl
			fn_id_local = getattr(decl, "fn_id", None)
			if fn_id_local is not None and method_res.call_info.target.kind is CallTargetKind.INDIRECT:
				method_res = MethodCallResult(
					method_res.return_type,
					CallInfo(
						target=CallTarget.direct(fn_id_local),
						sig=CallSig(
							param_types=method_res.call_info.sig.param_types,
							user_ret_type=method_res.call_info.sig.user_ret_type,
							can_throw=bool(method_res.call_info.sig.can_throw),
							includes_callee=method_res.call_info.sig.includes_callee,
							declared_terminal_throws=bool(getattr(method_res.call_info.sig, "declared_terminal_throws", False)),
						),
					),
					method_res.resolution,
				)
		if method_res is not None and method_res.call_info is not None:
			intent.arg_expected_types = _expected_arg_types_for_call(list(method_res.call_info.sig.param_types), len(expr.args))
			_propagate_arg_expected_types(intent, arg_types)
			setattr(expr, "_resolved_method_call_info", (list(method_res.call_info.sig.param_types), method_res.return_type, bool(method_res.call_info.sig.can_throw), method_res.call_info.target, bool(getattr(method_res.call_info.sig, "declared_terminal_throws", False))))
			setattr(expr, "_resolved_method_return", method_res.return_type)
			record_call_info(expr, param_types=list(method_res.call_info.sig.param_types), return_type=method_res.return_type, can_throw=bool(method_res.call_info.sig.can_throw), target=method_res.call_info.target, declared_terminal_throws=bool(getattr(method_res.call_info.sig, "declared_terminal_throws", False)))
			if ctx.record_instantiation is not None and isinstance(getattr(method_res.call_info, "target", None), CallTarget):
				target = method_res.call_info.target
				target_fn_id = target.symbol if target.kind is CallTargetKind.DIRECT else None
				if target_fn_id is not None:
					receiver_args = None
					if arg_types:
						recv_nominal = _unwrap_ref_type(arg_types[0])
						inst = ctx.type_table.get_struct_instance(recv_nominal)
						if inst is not None:
							receiver_args = list(inst.type_args)
						else:
							vinst = ctx.type_table.get_variant_instance(recv_nominal)
							if vinst is not None:
								receiver_args = list(vinst.type_args)
							else:
								recv_def = ctx.type_table.get(recv_nominal)
								if recv_def.kind is TypeKind.ARRAY and recv_def.param_types:
									receiver_args = list(recv_def.param_types)
					if receiver_args is None:
						base_te = getattr(qm, "base_type_expr", None)
						if base_te is not None:
							base_mod = getattr(base_te, "module_id", None) or getattr(base_te, "module_alias", None) or current_module_name
							base_tid = resolve_opaque_type(base_te, ctx.type_table, module_id=base_mod, type_params=type_param_map, allow_generic_base=True)
							if base_tid is not None:
								base_inst = ctx.type_table.get_struct_instance(base_tid)
								if base_inst is not None:
									receiver_args = list(base_inst.type_args)
								else:
									base_vinst = ctx.type_table.get_variant_instance(base_tid)
									if base_vinst is not None:
										receiver_args = list(base_vinst.type_args)
									else:
										base_def = ctx.type_table.get(base_tid)
										if base_def.kind is TypeKind.ARRAY and base_def.param_types:
											receiver_args = list(base_def.param_types)
					if receiver_args:
						impl_args = tuple(receiver_args)
						if not any(ctx.type_table.has_typevar(t) for t in impl_args):
							csid = getattr(expr, "callsite_id", None)
							inferred_fn_args: tuple[TypeId, ...] = tuple()
							if isinstance(getattr(method_res, "resolution", None), dict):
								inferred_fn_args = tuple(getattr(method_res, "resolution", {}).get("inferred_fn_args", ()) or ())
							explicit_fn_args = tuple(type_arg_ids or [])
							fn_args = inferred_fn_args or explicit_fn_args
							ctx.record_instantiation(callsite_id=csid, target_fn_id=target_fn_id, impl_args=impl_args, fn_args=fn_args)
			return record_expr(expr, method_res.return_type)
		return record_expr(expr, ctx.unknown_ty)
	if isinstance(expr.fn, H.HVar):
		resolved_ctor_info = getattr(expr, "_resolved_ctor_info", None)
		if resolved_ctor_info is not None:
			param_types, ctor_return, ctor_arg_fields, ctor_name = resolved_ctor_info
			record_call_info(expr, param_types=list(param_types), return_type=ctor_return, can_throw=False, target=CallTarget.constructor(ctor_return, ctor_name, ctor_arg_field_indices=tuple(ctor_arg_fields)))
			return record_expr(expr, ctor_return)
		resolved_method_call = getattr(expr, "_resolved_method_call_info", None)
		if resolved_method_call is not None:
			if len(resolved_method_call) >= 5:
				param_types, ret_type, can_throw, target, _terminal = resolved_method_call
			else:
				param_types, ret_type, can_throw, target = resolved_method_call
				_terminal = False
			record_call_info(expr, param_types=list(param_types), return_type=ret_type, can_throw=bool(can_throw), target=target, declared_terminal_throws=bool(_terminal))
			return record_expr(expr, ret_type)
		resolved_ctor_return = getattr(expr, "_resolved_ctor_return", None)
		if resolved_ctor_return is not None:
			return record_expr(expr, resolved_ctor_return)
		if expected_type is None and getattr(expr, "defer_infer_diag", False):
			return record_expr(expr, ctx.unknown_ty)
		if debug_call_resolve and expr.fn.name == "Next":
			_debug_call_resolve(
				f"HVar Next module_id={getattr(expr.fn, 'module_id', None)} expected_type={expected_type} "
				f"current_module={current_module_name} visible_modules={visible_modules}"
			)
		if drift_debug.enabled("call_resolve") and expr.fn.name in ("ReturnBusy", "Busy", "Timeout", "Failed", "VirtualThread", "Closed", "Cancelled"):
			print(
				f"[call_resolve] HVar {expr.fn.name} csid={getattr(expr, 'callsite_id', None)} module_id={getattr(expr.fn, 'module_id', None)} expected_type={expected_type} current_module={current_module_name}",
				file=_debug_stderr,
			)
		kw_pairs = getattr(expr, "kwargs", []) or []
		kw_value_types = [type_expr(kw.value, used_as_value=False) for kw in kw_pairs]
		call_type_args = list(getattr(expr, "type_args", None) or [])
		call_type_args_span = None
		if call_type_args:
			first_loc = getattr(call_type_args[0], "loc", None)
			if first_loc is not None:
				call_type_args_span = Span.from_loc(first_loc)
		call_type_arg_ids = [resolve_opaque_type(t, ctx.type_table, module_id=current_module_name, type_params=type_param_map) for t in call_type_args] if call_type_args else None
		if drift_debug.enabled("err_call") and isinstance(expr.fn, H.HVar) and expr.fn.name == "Err":
			try:
				print(f"[debug] Err call: binding_id={getattr(expr.fn,'binding_id',None)} expected_type={expected_type} module={current_module_name}", file=_debug_stderr)
			except Exception:
				pass
		binding_id = getattr(expr.fn, "binding_id", None)
		if binding_id is None and getattr(expr.fn, "module_id", None) is None:
			name_to_id = getattr(ctx, "binding_id_by_name", None)
			if name_to_id is not None:
				bid = name_to_id.get(expr.fn.name)
				if bid is not None:
					expr.fn.binding_id = bid
					binding_id = bid
		if drift_debug.enabled("call_resolve") and binding_id is None and expr.fn.name == "f":
			print(f"[call_resolve] HVar f with no binding_id module_id={getattr(expr.fn, 'module_id', None)} expected_type={expected_type} span={getattr(expr, 'loc', None)}", file=_debug_stderr)
		if binding_id is not None:
			fn_val_ty = type_expr(expr.fn, used_as_value=True)
			if fn_val_ty is not None:
				fn_def = ctx.type_table.get(fn_val_ty)
				if drift_debug.enabled("call_resolve"):
					print(f"[call_resolve] binding call name={expr.fn.name} binding_id={binding_id} fn_val_ty={fn_val_ty} fn_kind={fn_def.kind} param_types={getattr(fn_def, 'param_types', None)} args={len(expr.args)}", file=_debug_stderr)
				if fn_def.kind is TypeKind.FUNCTION and fn_def.param_types:
					if call_type_args:
						diagnostics.append(_tc_diag(message="type arguments are not supported on function values", severity="error", span=call_type_args_span or getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					param_types = list(fn_def.param_types[:-1])
					ret_type = fn_def.param_types[-1]
					if drift_debug.enabled("call_resolve"):
						print(f"[call_resolve] binding call name={expr.fn.name} param_types={param_types} ret_type={ret_type} arg_count={len(expr.args)}", file=_debug_stderr)
					if len(param_types) != len(expr.args):
						if drift_debug.enabled("call_resolve"):
							print(f"[call_resolve] binding mismatch name={expr.fn.name} param_len={len(param_types)} arg_len={len(expr.args)} arg_types={arg_types}", file=_debug_stderr)
						diagnostics.append(_tc_diag(message=f"no matching overload for function '{expr.fn.name}' with args {arg_types}", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, ctx.unknown_ty)
					if drift_debug.enabled("call_resolve"):
						print(f"[call_resolve] binding resolved name={expr.fn.name} csid={getattr(expr, 'callsite_id', None)} return={ret_type}", file=_debug_stderr)
					record_call_info(expr, param_types=param_types, return_type=ret_type, can_throw=fn_def.can_throw(), target=CallTarget.indirect(binding_id))
					return record_expr(expr, ret_type)
				if drift_debug.enabled("call"):
					try:
						print(f"[debug] call target not function (binding call) fn={expr.fn} module={current_module_name} binding_id={binding_id}", file=_debug_stderr)
					except Exception:
						pass
				diagnostics.append(_tc_diag(message="call target is not a function value", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, ctx.unknown_ty)
		if expected_type is not None:
			ctor_res = resolve_unqualified_variant_ctor(_make_resolver_ctx(ctx, diagnostics=diagnostics, current_module_name=current_module_name, default_package=default_package, module_packages=module_packages, type_param_map=type_param_map, tc_diag=_tc_diag, fixed_width_allowed=_fixed_width_allowed, reject_zst_array=_reject_zst_array, pretty_type_name=_pretty_type_name, format_ctor_signature_list=_format_ctor_signature_list, instantiate_sig=_instantiate_sig, enforce_struct_requires=_enforce_struct_requires, ensure_field_visible=_ensure_field_visible, visible_modules_for_free_call=_visible_modules_for_free_call, module_ids_by_name=module_ids_by_name, visibility_provenance=visibility_provenance, infer=_infer, format_infer_failure=_format_infer_failure, lambda_can_throw=_lambda_can_throw), ctor_name=expr.fn.name, expected_type=expected_type, arg_exprs=list(expr.args), kw_pairs=kw_pairs, span=getattr(expr, "loc", None))
			if ctor_res is not None:
				expr.args = list(ctor_res.ctor_args)
				expr.kwargs = []
				expr.ctor_arg_field_indices = list(ctor_res.ctor_arg_field_indices)
				arg_exprs = list(expr.args)
				arg_types = [type_expr(a, used_as_value=False) for a in arg_exprs]
				intent.arg_expected_types = _expected_arg_types_for_call(list(ctor_res.inst_params), len(arg_exprs))
				_propagate_arg_expected_types(intent, arg_types)
				record_call_info(expr, param_types=list(ctor_res.inst_params), return_type=ctor_res.inst_return, can_throw=False, target=CallTarget.constructor(ctor_res.inst_return, expr.fn.name, ctor_arg_field_indices=tuple(ctor_res.ctor_arg_field_indices)))
				return record_expr(expr, ctor_res.inst_return)
		if expected_type is None and getattr(expr.fn, "module_id", None):
			schema_map = getattr(ctx.type_table, "variant_schemas", {})
			candidate = None
			candidate_arm = None
			for _base_id, schema in getattr(schema_map, "items", lambda: [])():
				if getattr(schema, "module_id", None) != expr.fn.module_id:
					continue
				arm = next((a for a in getattr(schema, "arms", []) or [] if a.name == expr.fn.name), None)
				if arm is None:
					continue
				if schema.type_params:
					continue
				if candidate is not None:
					candidate = None
					candidate_arm = None
					break
				candidate = schema
				candidate_arm = arm
			if drift_debug.enabled("call_resolve") and expr.fn.name in ("Closed", "Cancelled", "Failed"):
				print(
					f"[call_resolve] HVar {expr.fn.name} module_id={expr.fn.module_id} candidate={getattr(candidate, 'name', None)}",
					file=_debug_stderr,
				)
			if candidate is not None:
				base_id = ctx.type_table.get_variant_base(module_id=candidate.module_id, name=candidate.name)
				if base_id is not None:
					inst = ctx.type_table.ensure_variant_instantiated(base_id, [])
					if candidate_arm is not None and not candidate_arm.fields:
						record_call_info(expr, param_types=[], return_type=inst, can_throw=False, target=CallTarget.constructor(inst, expr.fn.name, ctor_arg_field_indices=()))
						return record_expr(expr, inst)
					ctor_res = resolve_unqualified_variant_ctor(
						_make_resolver_ctx(
							ctx,
							diagnostics=diagnostics,
							current_module_name=current_module_name,
							default_package=default_package,
							module_packages=module_packages,
							type_param_map=type_param_map,
							tc_diag=_tc_diag,
							fixed_width_allowed=_fixed_width_allowed,
							reject_zst_array=_reject_zst_array,
							pretty_type_name=_pretty_type_name,
							format_ctor_signature_list=_format_ctor_signature_list,
							instantiate_sig=_instantiate_sig,
							enforce_struct_requires=_enforce_struct_requires,
							ensure_field_visible=_ensure_field_visible,
							visible_modules_for_free_call=_visible_modules_for_free_call,
							module_ids_by_name=module_ids_by_name,
							visibility_provenance=visibility_provenance,
							infer=_infer,
							format_infer_failure=_format_infer_failure,
							lambda_can_throw=_lambda_can_throw,
						),
						ctor_name=expr.fn.name,
						expected_type=inst,
						arg_exprs=list(expr.args),
						kw_pairs=kw_pairs,
						span=getattr(expr, "loc", None),
					)
					if ctor_res is not None:
						expr.args = list(ctor_res.ctor_args)
						expr.kwargs = []
						expr.ctor_arg_field_indices = list(ctor_res.ctor_arg_field_indices)
						record_call_info(
							expr,
							param_types=list(ctor_res.inst_params),
							return_type=ctor_res.inst_return,
							can_throw=False,
							target=CallTarget.constructor(ctor_res.inst_return, expr.fn.name, ctor_arg_field_indices=tuple(ctor_res.ctor_arg_field_indices)),
						)
						return record_expr(expr, ctor_res.inst_return)
		if expected_type is None:
			schema_map = getattr(ctx.type_table, "variant_schemas", {})
			for _base_id, schema in getattr(schema_map, "items", lambda: [])():
				if getattr(schema, "module_id", None) not in (current_module_name, "lang.core"):
					continue
				if any(getattr(arm, "name", None) == expr.fn.name for arm in getattr(schema, "arms", []) or []):
					diagnostics.append(_tc_diag(message="E-CTOR-EXPECTED-TYPE: constructor calls require an expected variant type in v1", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, ctx.unknown_ty)
		struct_base = ctx.type_table.get_struct_base(module_id=expr.fn.module_id or current_module_name, name=expr.fn.name)
		if struct_base is None:
			struct_base = ctx.type_table.get_nominal(kind=TypeKind.STRUCT, module_id=expr.fn.module_id or current_module_name, name=expr.fn.name)
		if struct_base is not None:
			struct_id = struct_base
			if call_type_arg_ids:
				struct_id = ctx.type_table.ensure_struct_template(struct_base, call_type_arg_ids) if any(ctx.type_table.has_typevar(t) for t in call_type_arg_ids) else ctx.type_table.ensure_struct_instantiated(struct_base, call_type_arg_ids)
			arg_exprs = list(expr.args)
			arg_types = [type_expr(a, used_as_value=False) for a in arg_exprs]
			for idx, arg in enumerate(arg_exprs):
				if isinstance(arg, H.HLambda):
					ty = arg_types[idx]
					if ty is None or ty == ctx.unknown_ty:
						arg_types[idx] = type_expr(arg)
			if kw_pairs and not arg_exprs:
				arg_types = list(kw_value_types)
			ctor_res = resolve_struct_ctor(_make_resolver_ctx(ctx, diagnostics=diagnostics, current_module_name=current_module_name, default_package=default_package, module_packages=module_packages, type_param_map=type_param_map, tc_diag=_tc_diag, fixed_width_allowed=_fixed_width_allowed, reject_zst_array=_reject_zst_array, pretty_type_name=_pretty_type_name, format_ctor_signature_list=_format_ctor_signature_list, instantiate_sig=_instantiate_sig, enforce_struct_requires=_enforce_struct_requires, ensure_field_visible=_ensure_field_visible, visible_modules_for_free_call=_visible_modules_for_free_call, module_ids_by_name=module_ids_by_name, visibility_provenance=visibility_provenance, infer=_infer, format_infer_failure=_format_infer_failure, lambda_can_throw=_lambda_can_throw), struct_id=struct_id, struct_name=expr.fn.name, arg_exprs=list(arg_exprs), arg_types=arg_types, kw_pairs=kw_pairs, expected_type=expected_type, type_arg_ids=call_type_arg_ids, allow_infer=True, call_type_args_span=call_type_args_span, span=getattr(expr, "loc", Span()))
			if ctor_res is not None:
				expr.args = list(ctor_res.ctor_args)
				expr.kwargs = []
				expr.ctor_arg_field_indices = list(ctor_res.ctor_arg_field_indices)
				if drift_debug.enabled("call_resolve") and expr.fn.name in ("VirtualThread", "Busy", "Timeout", "Failed"):
					print(f"[call_resolve] struct/ctor resolved {expr.fn.name} csid={getattr(expr, 'callsite_id', None)} return={ctor_res.inst_return}", file=_debug_stderr)
				record_call_info(expr, param_types=list(ctor_res.inst_params), return_type=ctor_res.inst_return, can_throw=False, target=CallTarget.constructor_struct(ctor_res.inst_return, ctor_arg_field_indices=tuple(ctor_res.ctor_arg_field_indices)))
				return record_expr(expr, ctor_res.inst_return)
		def _resolve_free_call_with_require_local(*, name: str, module_name: str | None, arg_types: list[TypeId], call_type_args: list[TypeId] | None = None, call_type_args_span: Span | None = None, expected_type: TypeId | None = None) -> tuple[CallableDecl, CallableSignature, Subst | None, ResolutionError | None]:
			def _dedupe_by_key(items: list[tuple], key_fn) -> list[tuple]:
				seen: set[object] = set()
				out: list[tuple] = []
				for item in items:
					key = key_fn(item)
					if key in seen:
						continue
					seen.add(key)
					out.append(item)
				return out
			def _pick_most_specific_items(items: list[tuple], key_fn, require_info: dict[object, tuple[parser_ast.TraitExpr, dict[object, object], str, dict[TypeParamId, tuple[str, int]]]]) -> list[tuple]:
				if len(items) <= 1:
					return items
				if require_env_local is None:
					return items
				formulas: dict[object, object] = {}
				for item in items:
					key = key_fn(item)
					info = require_info.get(key)
					if info is None:
						formula = BOOL_TRUE
					else:
						req_expr, subst, def_mod, scope_map = info
						formula = require_env_local.normalized(req_expr, subst=subst, default_module=def_mod, param_scope_map=scope_map)
					formulas[key] = formula
				winners: list[tuple] = []
				for item in items:
					key = key_fn(item)
					base = formulas.get(key, BOOL_TRUE)
					is_dominated = False
					for other in items:
						other_key = key_fn(other)
						if other_key == key:
							continue
						other_formula = formulas.get(other_key, BOOL_TRUE)
						if require_env_local.implies(other_formula, base) and not require_env_local.implies(base, other_formula):
							is_dominated = True
							break
					if not is_dominated:
						winners.append(item)
				return winners
			if callable_registry is None:
				raise ResolutionError(f"no matching overload for function '{name}' with args {arg_types}")
			include_private = current_module if module_name is None else None
			candidates = callable_registry.get_free_candidates(name=name, visible_modules=_visible_modules_for_free_call(module_name), include_private_in=include_private)
			viable: list[tuple[CallableDecl, CallableSignature, Subst | None]] = []
			type_arg_counts: set[int] = set()
			saw_registry_only_with_type_args = False
			saw_typed_nongeneric_with_type_args = False
			saw_infer_incomplete = False
			saw_require_failed = False
			infer_failures: list[InferResult] = []
			for decl in candidates:
				sig = None
				if decl.fn_id is not None and signatures_by_id is not None:
					sig = signatures_by_id.get(decl.fn_id)
				if sig is None:
					sig = _sig_from_decl_template(ctx, decl, current_module_name)
				if sig is None:
					if call_type_args:
						saw_registry_only_with_type_args = True
						continue
					params = list(decl.signature.param_types)
					result_type = decl.signature.result_type
					if len(params) != len(arg_types):
						continue
					# Substitute deferred-literal Unknown args with param types for matching.
					_match_args = list(arg_types)
					_local_arg_exprs = list(getattr(expr, "args", []) or [])
					for _di, _da in enumerate(_local_arg_exprs):
						if _di < len(_match_args) and _match_args[_di] == ctx.unknown_ty and getattr(_da, "defer_infer_diag", False) and _di < len(params):
							_match_args[_di] = params[_di]
					if _args_match_params(list(params), _match_args):
						viable.append((decl, CallableSignature(param_types=tuple(params), result_type=result_type), None))
					elif _can_borrow_coerce(list(params), arg_types):
						viable.append((decl, CallableSignature(param_types=tuple(params), result_type=result_type), None))
					continue
				param_needs_resolve = sig.param_type_ids is None
				if not param_needs_resolve and sig.param_type_ids is not None:
					param_needs_resolve = any(p is None or p == ctx.unknown_ty for p in sig.param_type_ids)
				if param_needs_resolve and sig.param_types is not None:
					local_type_params = {p.name: p.id for p in sig.type_params}
					param_type_ids = [resolve_opaque_type(p, ctx.type_table, module_id=sig.module, type_params=local_type_params) for p in sig.param_types]
					sig = replace(sig, param_type_ids=param_type_ids)
				return_needs_resolve = sig.return_type_id is None
				if not return_needs_resolve and sig.return_type_id is not None:
					ret_def = ctx.type_table.get(sig.return_type_id)
					if sig.return_type_id == ctx.unknown_ty:
						return_needs_resolve = True
					elif ret_def.kind is TypeKind.STRUCT:
						base = ctx.type_table.struct_bases.get(sig.return_type_id)
						inst = ctx.type_table.get_struct_instance(sig.return_type_id)
						if inst is None and base is not None and base.type_params:
							return_needs_resolve = True
					elif ret_def.kind is TypeKind.INTERFACE:
						base = ctx.type_table.interface_bases.get(sig.return_type_id)
						inst = ctx.type_table.get_interface_instance(sig.return_type_id)
						if inst is None and base is not None and base.type_params:
							return_needs_resolve = True
					elif ret_def.kind is TypeKind.VARIANT:
						base = ctx.type_table.variant_schemas.get(sig.return_type_id)
						inst = ctx.type_table.get_variant_instance(sig.return_type_id)
						if inst is None and base is not None and base.type_params:
							return_needs_resolve = True
				if return_needs_resolve and sig.return_type is not None:
					local_type_params = {p.name: p.id for p in sig.type_params}
					ret_id = resolve_opaque_type(sig.return_type, ctx.type_table, module_id=sig.module, type_params=local_type_params)
					sig = replace(sig, return_type_id=ret_id)
				if sig.param_type_ids is None or sig.return_type_id is None:
					continue
				infer_arg_types = _borrow_infer_arg_types(list(sig.param_type_ids), arg_types)
				inst_arg_types = _coerce_args_for_params(list(sig.param_type_ids), infer_arg_types)
				req_for_infer = _require_for_fn(decl.fn_id) if decl.fn_id is not None else None
				inst_res = _instantiate_sig_with_subst(sig=sig, arg_types=inst_arg_types, expected_type=expected_type, explicit_type_args=call_type_args, allow_infer=True, require_expr=req_for_infer, diag_span=call_type_args_span, call_kind="free", call_name=name)
				needs_require_infer = False
				if inst_res.error and inst_res.error.kind is InferErrorKind.CANNOT_INFER:
					needs_require_infer = True
				elif inst_res.inst_return is not None and ctx.type_table.has_typevar(inst_res.inst_return):
					needs_require_infer = True
				elif inst_res.inst_params is not None and any(ctx.type_table.has_typevar(t) for t in inst_res.inst_params):
					needs_require_infer = True
				if needs_require_infer and not call_type_args:
					req_expr = _require_for_fn(decl.fn_id) if decl.fn_id is not None else None
					if req_expr is not None:
						type_params = list(getattr(sig, "type_params", []) or [])
						if type_params:
							name_to_idx = {tp.name: idx for idx, tp in enumerate(type_params)}
							id_to_idx = {tp.id: idx for idx, tp in enumerate(type_params)}
							inferred_args: list[TypeId | None] = [None for _ in type_params]
							if sig.param_type_ids:
								for idx, pty in enumerate(sig.param_type_ids):
									if idx >= len(inst_arg_types):
										break
									td = ctx.type_table.get(pty)
									if td.kind is TypeKind.TYPEVAR and td.type_param_id is not None:
										tp_idx = id_to_idx.get(td.type_param_id)
										if tp_idx is not None and inferred_args[tp_idx] is None:
											inferred_args[tp_idx] = inst_arg_types[idx]
							for atom in _extract_conjunctive_facts(req_expr):
								if not isinstance(atom, parser_ast.TraitIs):
									continue
								trait_name = getattr(atom.trait, "name", None)
								if trait_name not in {"Fn0", "Fn1", "Fn2"}:
									continue
								subj_name = _subject_name(atom.subject)
								subj_idx = None
								if subj_name is not None and subj_name in name_to_idx:
									subj_idx = name_to_idx[subj_name]
								elif isinstance(atom.subject, TypeParamId) and atom.subject in id_to_idx:
									subj_idx = id_to_idx[atom.subject]
								if subj_idx is None:
									continue
								subj_ty = inferred_args[subj_idx]
								if subj_ty is None:
									continue
								subj_def = ctx.type_table.get(subj_ty)
								if subj_def.kind is not TypeKind.FUNCTION or not subj_def.param_types:
									continue
								ret_ty = subj_def.param_types[-1]
								trait_args = list(getattr(atom.trait, "args", []) or [])
								if not trait_args:
									continue
								arg0 = trait_args[0]
								arg0_name = getattr(arg0, "name", None)
								if arg0_name is not None and arg0_name in name_to_idx:
									tp_idx = name_to_idx[arg0_name]
									if inferred_args[tp_idx] is None:
										inferred_args[tp_idx] = ret_ty
							if all(arg is not None for arg in inferred_args):
								inst_res = _instantiate_sig_with_subst(
									sig=sig,
									arg_types=inst_arg_types,
									expected_type=expected_type,
									explicit_type_args=list(inferred_args),
									allow_infer=True,
									diag_span=call_type_args_span,
									call_kind="free",
									call_name=name,
								)
				if inst_res.error and inst_res.error.kind is InferErrorKind.NO_TYPEPARAMS and call_type_args:
					saw_typed_nongeneric_with_type_args = True
					continue
				if inst_res.error and inst_res.error.kind is InferErrorKind.TYPEARG_COUNT and call_type_args:
					if inst_res.error.expected_count is not None:
						type_arg_counts.add(inst_res.error.expected_count)
					continue
				if inst_res.error and inst_res.error.kind in {InferErrorKind.CANNOT_INFER, InferErrorKind.CONFLICT}:
					saw_infer_incomplete = True
					infer_failures.append(inst_res)
					continue
				if inst_res.error:
					continue
				params = inst_res.inst_params
				result_type = inst_res.inst_return
				inst_subst = inst_res.subst
				if len(params) != len(arg_types):
					continue
				_match_args2 = list(arg_types)
				for _di2, _da2 in enumerate(expr.args):
					if _di2 < len(_match_args2) and _match_args2[_di2] == ctx.unknown_ty and getattr(_da2, "defer_infer_diag", False) and _di2 < len(params):
						_match_args2[_di2] = params[_di2]
				if _args_match_params(list(params), _match_args2):
					viable.append((decl, CallableSignature(param_types=tuple(params), result_type=result_type), inst_subst))
				elif _can_borrow_coerce(list(params), arg_types):
					viable.append((decl, CallableSignature(param_types=tuple(params), result_type=result_type), inst_subst))
			if not viable:
				if call_type_args:
					if type_arg_counts:
						exp = ", ".join(str(n) for n in sorted(type_arg_counts))
						raise ResolutionError(f"type argument count mismatch for '{name}': expected one of ({exp}), got {len(call_type_args)}", span=call_type_args_span)
					if saw_typed_nongeneric_with_type_args:
						raise ResolutionError(f"type arguments require a generic signature for function '{name}'", span=call_type_args_span)
					if saw_registry_only_with_type_args:
						raise ResolutionError(f"type arguments require a typed signature for function '{name}'", span=call_type_args_span)
					raise ResolutionError(f"no matching overload for function '{name}' with provided type arguments")
				if saw_infer_incomplete and infer_failures:
					failure = infer_failures[0]
					ctx_fail = failure.context or InferContext(call_kind="free", call_name=name, span=call_type_args_span or Span(), type_param_ids=[], type_param_names={}, param_types=[], param_names=None, return_type=None, arg_types=[])
					msg, notes = _format_infer_failure(ctx_fail, failure)
					raise ResolutionError(msg, span=call_type_args_span, notes=notes)
				if saw_infer_incomplete:
					ctx_fail = InferContext(call_kind="free", call_name=name, span=call_type_args_span or Span(), type_param_ids=[], type_param_names={}, param_types=[], param_names=None, return_type=None, arg_types=[])
					res = InferResult(ok=False, subst=None, inst_params=None, inst_return=None, error=InferError(kind=InferErrorKind.CANNOT_INFER), context=ctx_fail)
					msg, notes = _format_infer_failure(ctx_fail, res)
					raise ResolutionError(msg, span=call_type_args_span, notes=notes)
					raise ResolutionError(f"no matching overload for function '{name}' with args {arg_types}")
			world = None
			applicable: list[tuple[CallableDecl, CallableSignature, Subst | None]] = []
			require_rejected: list[tuple[CallableDecl, CallableSignature, Subst | None]] = []
			require_info: dict[object, tuple[parser_ast.TraitExpr, dict[object, object], str, dict[TypeParamId, tuple[str, int]]]] = {}
			require_failures: list[ProofFailure] = []
			for decl, sig_inst, inst_subst in viable:
				cand_key = decl.fn_id if decl.fn_id is not None else ("callable", decl.callable_id)
				fn_id_local = decl.fn_id
				if fn_id_local is None:
					applicable.append((decl, sig_inst, inst_subst))
					continue
				world = global_trait_world or visible_trait_world
				req = _require_for_fn(fn_id_local)
				if req is None:
					applicable.append((decl, sig_inst, inst_subst))
					continue
				if any(isinstance(a, H.HLambda) for a in expr.args):
					atoms = _extract_conjunctive_facts(req)
					if atoms and all(isinstance(a, parser_ast.TraitIs) and getattr(a.trait, "name", None) in {"Fn0", "Fn1", "Fn2"} for a in atoms):
						applicable.append((decl, sig_inst, inst_subst))
						continue
				subjects: set[object] = set()
				_collect_trait_subjects(req, subjects)
				subst: dict[object, object] = {}
				sig_local = signatures_by_id.get(decl.fn_id) if decl.fn_id is not None and signatures_by_id is not None else None
				if sig_local is None:
					sig_local = _sig_from_decl_template(ctx, decl, current_module_name)
				if sig_local and getattr(sig_local, "type_params", None) and inst_subst is not None:
					type_params = list(getattr(sig_local, "type_params", []) or [])
					# Bind every instantiated type parameter, not just
					# those that appear as require-clause subjects.  A
					# type parameter can also appear on the trait side
					# of `T is I` (as a bound-to-caller interface), and
					# the solver needs its binding in `subst` to
					# substitute the trait key correctly via
					# `trait_key_from_expr(..., type_param_subst=subst)`.
					for idx, tp in enumerate(type_params):
						if idx < len(inst_subst.args):
							key = _normalize_type_key(type_key_from_typeid(ctx.type_table, inst_subst.args[idx]))
							subst[tp.id] = key
							subst[tp.name] = key
				if sig_local and getattr(sig_local, "type_params", None) and sig_local.param_type_ids:
					type_params = list(getattr(sig_local, "type_params", []) or [])
					type_param_ids = {tp.id for tp in type_params}
					for idx, pty in enumerate(sig_local.param_type_ids):
						if idx >= len(arg_types):
							break
						pdef = ctx.type_table.get(pty)
						if pdef.kind is not TypeKind.TYPEVAR or pdef.type_param_id not in type_param_ids:
							continue
						tp_id = pdef.type_param_id
						tp_name = next((tp.name for tp in type_params if tp.id == tp_id), None)
						if tp_id in subst or (tp_name is not None and tp_name in subst):
							continue
						key = _normalize_type_key(type_key_from_typeid(ctx.type_table, arg_types[idx]))
						subst[tp_id] = key
						if tp_name is not None:
							subst[tp_name] = key
				if sig_local and sig_local.param_names:
					for idx, pname in enumerate(sig_local.param_names):
						if pname in subst:
							continue
						if pname in subjects and idx < len(arg_types):
							key = _normalize_type_key(type_key_from_typeid(ctx.type_table, arg_types[idx]))
							subst[pname] = key
				if world is None:
					continue
				env = TraitEnv(default_module=fn_id_local.module or current_module_name, default_package=default_package, module_packages=module_packages or {}, assumed_true=set(fn_require_assumed), type_table=ctx.type_table)
				res = prove_expr(world, env, subst, req)
				if res.status is ProofStatus.PROVED:
					applicable.append((decl, sig_inst, inst_subst))
					scope_map = _param_scope_map(sig_local)
					require_info[cand_key] = (req, subst, fn_id_local.module or current_module_name, scope_map)
				else:
					if res.status is ProofStatus.UNKNOWN:
						if _require_unknown_defer(ctx, arg_types=arg_types, type_arg_ids=type_arg_ids):
							applicable.append((decl, sig_inst, inst_subst))
							scope_map = _param_scope_map(sig_local)
							require_info[cand_key] = (req, subst, fn_id_local.module or current_module_name, scope_map)
							continue
					saw_require_failed = True
					origin = ObligationOrigin(kind=ObligationOriginKind.CALLEE_REQUIRE, label=f"function '{name}'", span=Span.from_loc(getattr(req, "loc", None)))
					failure = _require_failure(req_expr=req, subst=subst, origin=origin, span=call_type_args_span or Span(), env=env, world=world, result=res)
					if failure is not None:
						require_failures.append(failure)
					require_rejected.append((decl, sig_inst, inst_subst))
			if not applicable:
				if saw_require_failed and require_rejected:
					require_failure_error: ResolutionError | None = None
					failure = _pick_best_failure(require_failures)
					if failure is not None:
						require_failure_error = ResolutionError(_format_failure_message(failure), code=_failure_code(failure), span=call_type_args_span, notes=ctx.requirement_notes(failure) if ctx.requirement_notes is not None else list(getattr(failure.obligation, "notes", []) or []))
					else:
						require_failure_error = ResolutionError(f"trait requirements not met for function '{name}'")
					winners = _pick_most_specific_items(require_rejected, lambda item: _candidate_key_for_decl(item[0]), require_info)
					if len(winners) != 1:
						return require_rejected[0][0], require_rejected[0][1], require_rejected[0][2], require_failure_error
					return winners[0][0], winners[0][1], winners[0][2], require_failure_error
				if saw_require_failed:
					failure = _pick_best_failure(require_failures)
					if failure is not None:
						raise ResolutionError(_format_failure_message(failure), code=_failure_code(failure), span=call_type_args_span, notes=ctx.requirement_notes(failure) if ctx.requirement_notes is not None else list(getattr(failure.obligation, "notes", []) or []))
					raise ResolutionError(f"trait requirements not met for function '{name}'")
				raise ResolutionError(f"no matching overload for function '{name}' with args {arg_types}")
			applicable = _dedupe_by_key(applicable, lambda item: _candidate_key_for_decl(item[0]))
			if len(applicable) == 1:
				return applicable[0][0], applicable[0][1], applicable[0][2], None
			winners = _pick_most_specific_items(applicable, lambda item: _candidate_key_for_decl(item[0]), require_info)
			if len(winners) != 1:
				raise ResolutionError(f"ambiguous call to function '{name}' with args {arg_types}")
			return winners[0][0], winners[0][1], winners[0][2], None
		if call_kwargs_issues("constructors", kw_pairs):
			first = (kw_pairs or [None])[0]
			diagnostics.append(_tc_diag(message="keyword arguments are only supported for constructors in v1", severity="error", span=_best_effort_span(first, expr)))
			return record_expr(expr, ctx.unknown_ty)
		arg_types: list[TypeId | None] = []
		lambda_arg_indices: list[int] = []
		# Pre-scan: propagate expected Callback types to explicit callbackN(lambda)
		# args so the inner lambda gets concrete param types from the outer call
		# context. Without this, callback2(|req, ctx| ...) inside add_route(...)
		# leaves the lambda params as Unknown.
		_CB_NAMES_PRE = frozenset({"callback0", "callback1", "callback2", "callback_throw0", "callback_throw1", "callback_throw2"})
		_CB_IFACE_NAMES = frozenset({"Callback0", "Callback1", "Callback2", "CallbackThrow0", "CallbackThrow1", "CallbackThrow2"})
		if ctx.callable_registry is not None and any(
			isinstance(a, H.HCall) and isinstance(getattr(a, "fn", None), H.HVar) and a.fn.name in _CB_NAMES_PRE
			for a in expr.args
		):
			# Try to find the expected Callback type from function candidates.
			# If all candidates agree on the param type at a given index,
			# propagate it as expected_type_hint to the callbackN HCall.
			_pre_cands = list(ctx.callable_registry.get_free_candidates_unscoped(name=expr.fn.name))
			for idx, arg in enumerate(expr.args):
				if not isinstance(arg, H.HCall):
					continue
				if not isinstance(getattr(arg, "fn", None), H.HVar):
					continue
				if arg.fn.name not in _CB_NAMES_PRE:
					continue
				# Find the unique expected param type across all candidates.
				_expected_pty: TypeId | None = None
				_ambiguous = False
				for _pc in _pre_cands:
					_ps = _pc.signature
					if _ps is None or not _ps.param_types or idx >= len(_ps.param_types):
						continue
					_pty = _ps.param_types[idx]
					_ptd = ctx.type_table.get(_pty)
					if _ptd.kind is not TypeKind.INTERFACE or _ptd.name not in _CB_IFACE_NAMES:
						continue
					if _expected_pty is None:
						_expected_pty = _pty
					elif _expected_pty != _pty:
						_ambiguous = True
						break
				if _expected_pty is not None and not _ambiguous:
					# Only propagate if the Callback type has fully concrete
					# type args (no type variables). Generic callbacks like
					# Callback0<T> from spawn_cb<T> must not be propagated —
					# the type vars aren't resolved yet.
					_inst = ctx.type_table.get_interface_instance(_expected_pty)
					if _inst is not None and _inst.type_args and not any(ctx.type_table.has_typevar(ta) for ta in _inst.type_args):
						arg.expected_type_hint = _expected_pty
		for idx, arg in enumerate(expr.args):
			if isinstance(arg, H.HLambda):
				lambda_arg_indices.append(idx)
				arg.allow_capture_invoke = True
				fallback_params: list[TypeId] = []
				for p in arg.params:
					if getattr(p, "type", None) is None:
						fallback_params.append(ctx.unknown_ty)
						continue
					try:
						fallback_params.append(resolve_opaque_type(p.type, ctx.type_table, module_id=current_module_name))
					except Exception:
						fallback_params.append(ctx.unknown_ty)
				if getattr(arg, "ret_type", None) is not None:
					try:
						ret_ty = resolve_opaque_type(arg.ret_type, ctx.type_table, module_id=current_module_name)
					except Exception:
						ret_ty = ctx.unknown_ty
				else:
					ret_ty = ctx.unknown_ty
				arg_expected_type = ctx.type_table.ensure_function(fallback_params, ret_ty, can_throw=True)
				arg.expected_fn_inferred = True
				arg_types.append(type_expr(arg, expected_type=arg_expected_type, used_as_value=False))
			elif isinstance(arg, (H.HCall, getattr(H, "HInvoke", ()))):
				if isinstance(arg.fn, H.HQualifiedMember):
					arg.defer_infer_diag = True
				else:
					arg.defer_infer_diag = False
				arg_types.append(type_expr(arg, used_as_value=False))
			elif isinstance(arg, (H.HMapLiteral, H.HArrayLiteral)):
				arg.defer_infer_diag = True
				arg_types.append(type_expr(arg, used_as_value=False))
			else:
				arg_types.append(type_expr(arg, used_as_value=False))
		for idx, arg in enumerate(expr.args):
			if isinstance(arg, H.HLambda):
				ty = arg_types[idx]
				if ty is None or ty == ctx.unknown_ty:
					arg_types[idx] = type_expr(arg)
		def _wrap_explicit_capture_callbacks() -> bool:
			changed = False
			for idx, arg in enumerate(expr.args):
				arg_ty = arg_types[idx] if idx < len(arg_types) else None
				if not isinstance(arg, H.HLambda):
					if arg_ty is None:
						continue
					arg_def = ctx.type_table.get(arg_ty)
					if arg_def.kind is not TypeKind.FUNCTION or not arg_def.param_types:
						continue
				if isinstance(arg, H.HLambda):
					arity = len(arg.params)
				else:
					arity = len(arg_def.param_types) - 1
				cb_call = _implicit_callback_wrap(
					ctx,
					arg=arg,
					callback_arity=arity,
					is_throw=False,
				)
				expr.args[idx] = cb_call
				arg_types[idx] = type_expr(cb_call, used_as_value=False)
				changed = True
			return changed
		try:
			decl, sig_inst, inst_subst, require_error = _resolve_free_call_with_require_local(name=expr.fn.name, module_name=expr.fn.module_id, arg_types=arg_types, call_type_args=call_type_arg_ids, call_type_args_span=call_type_args_span, expected_type=expected_type)
		except ResolutionError as err:
			if expected_type is not None and lambda_arg_indices and str(err).startswith("cannot infer type arguments"):
				alt_arg_types = list(arg_types)
				for idx in lambda_arg_indices:
					if idx < len(alt_arg_types):
						alt_arg_types[idx] = ctx.unknown_ty
				try:
					decl, sig_inst, inst_subst, require_error = _resolve_free_call_with_require_local(name=expr.fn.name, module_name=expr.fn.module_id, arg_types=alt_arg_types, call_type_args=call_type_arg_ids, call_type_args_span=call_type_args_span, expected_type=expected_type)
					arg_types = alt_arg_types
				except ResolutionError:
					pass
				else:
					err = None
			if err is None:
				pass
			elif getattr(expr, "defer_infer_diag", False) and str(err).startswith("cannot infer type arguments"):
				fallback_params = [t if t is not None else ctx.unknown_ty for t in arg_types]
				record_call_info(expr, param_types=fallback_params, return_type=ctx.unknown_ty, can_throw=False, target=CallTarget.indirect(expr.node_id))
				return record_expr(expr, ctx.unknown_ty)
			elif _wrap_explicit_capture_callbacks():
				try:
					decl, sig_inst, inst_subst, require_error = _resolve_free_call_with_require_local(name=expr.fn.name, module_name=expr.fn.module_id, arg_types=arg_types, call_type_args=call_type_arg_ids, call_type_args_span=call_type_args_span, expected_type=expected_type)
				except ResolutionError as err2:
					if getattr(expr, "defer_infer_diag", False) and str(err2).startswith("cannot infer type arguments"):
						fallback_params = [t if t is not None else ctx.unknown_ty for t in arg_types]
						record_call_info(expr, param_types=fallback_params, return_type=ctx.unknown_ty, can_throw=False, target=CallTarget.indirect(expr.node_id))
						return record_expr(expr, ctx.unknown_ty)
					diagnostics.append(_tc_diag(message=str(err2), severity="error", span=getattr(expr, "loc", Span()), notes=list(getattr(err2, "notes", []) or []), code=getattr(err2, "code", None)))
					return record_expr(expr, ctx.unknown_ty)
			else:
				diagnostics.append(_tc_diag(message=str(err), severity="error", span=getattr(expr, "loc", Span()), notes=list(getattr(err, "notes", []) or []), code=getattr(err, "code", None)))
				return record_expr(expr, ctx.unknown_ty)
		if require_error is not None:
			diagnostics.append(_tc_diag(message=str(require_error), severity="error", span=getattr(expr, "loc", Span()), notes=list(getattr(require_error, "notes", []) or []), code=getattr(require_error, "code", None)))
		# Stage 3 direct `conc.arc<T=Interface>(...)` rejection.  The
		# stdlib `fn arc<T>(value: T) nothrow -> Arc<T>` body
		# constructs a thin `{buf}` `ArcBox<T>` — structurally
		# incompatible with the fat `{ctrl, data, vtable}` layout
		# specialization that fires for `Arc<I>` once
		# `STAGE3_FAT_ARC_ACTIVE` is on.  Let the thin construction
		# through and the result would be a value whose runtime
		# layout disagrees with the sink type.  Reject at call-site
		# resolution and direct users to the correct shape:
		# `arc(concrete).as_interface<type I>()`.
		if sig_inst is not None and decl.fn_id is not None:
			_fid = decl.fn_id
			if (getattr(_fid, "module", None) == "std.concurrent"
					and getattr(_fid, "name", None) == "arc"):
				_ret_ty = getattr(sig_inst, "result_type", None)
				if _ret_ty is not None and ctx.type_table.is_arc_interface_view_instance(_ret_ty):
					_inst = ctx.type_table.get_struct_instance(_ret_ty)
					_iface_ty = _inst.type_args[0] if (_inst is not None and _inst.type_args) else None
					_iface_def = ctx.type_table.get(_iface_ty) if _iface_ty is not None else None
					_iface_name = getattr(_iface_def, "name", None) or "<interface>"
					diagnostics.append(_tc_diag(
						message=(
							f"`conc.arc<T>(value)` cannot be called with T = interface '{_iface_name}'. "
							f"Use `conc.arc(concrete).as_interface<type {_iface_name}>()` instead — "
							f"the two-step form is the only construction path for a fat "
							f"`Arc<{_iface_name}>` handle."
						),
						code="E_ARC_OF_INTERFACE_DIRECT",
						severity="error",
						span=getattr(expr, "loc", Span()),
					))
					return record_expr(expr, ctx.unknown_ty)
		if ctx.record_call_resolution is not None:
			ctx.record_call_resolution(expr, decl)
		if sig_inst is not None:
			# Precompute which param indices have Fn*-trait bounds so TP4
			# can keep allow_capture_invoke=True only for those params.
			_fn_bounded_params: set[int] = set()
			if decl.fn_id is not None:
				_req_pre = _require_for_fn(decl.fn_id)
				if _req_pre is not None:
					_sig_pre = ctx.signatures_by_id.get(decl.fn_id) if ctx.signatures_by_id is not None else None
					_ptids_pre = list(getattr(_sig_pre, "param_type_ids", []) or []) if _sig_pre is not None else None
					_FN_TRAITS = {"Fn0", "Fn1", "Fn2", "FnThrow0", "FnThrow1", "FnThrow2"}
					if _ptids_pre is not None:
						for _atom in _extract_conjunctive_facts(_req_pre):
							if not isinstance(_atom, parser_ast.TraitIs):
								continue
							if getattr(_atom.trait, "name", None) not in _FN_TRAITS:
								continue
							_sn = _subject_name(_atom.subject)
							_sid = _atom.subject if isinstance(_atom.subject, TypeParamId) else None
							if _sn is None and _sid is None:
								continue
							for _pi, _tid in enumerate(_ptids_pre):
								_td = ctx.type_table.get(_tid)
								if _td.kind is not TypeKind.TYPEVAR:
									continue
								_tp_id = _td.type_param_id
								if _sid is not None and _tp_id == _sid:
									_fn_bounded_params.add(_pi)
									break
								if _sn is not None:
									if ctx.type_param_names and _tp_id in ctx.type_param_names and ctx.type_param_names[_tp_id] == _sn:
										_fn_bounded_params.add(_pi)
										break
									for _tp in list(getattr(_sig_pre, "type_params", []) or []):
										if _tp.id == _tp_id and _tp.name == _sn:
											_fn_bounded_params.add(_pi)
											break
			for idx, arg in enumerate(expr.args):
				if not isinstance(arg, H.HLambda):
					continue
				if idx >= len(sig_inst.param_types):
					continue
				param_ty = sig_inst.param_types[idx]
				param_def = ctx.type_table.get(param_ty)
				if param_def.kind is not TypeKind.FUNCTION:
					continue
				# For Fn-trait-bounded generic params with captures (any kind),
				# keep allow_capture_invoke=True. Borrowed captures are validated
				# by the borrow checker (SCOPED promotion). Copy/move captures
				# are auto-wrapped in callback_N() by TP5 (B2 path).
				_has_any_caps = bool(getattr(arg, "explicit_captures", None))
				if not (_has_any_caps and idx in _fn_bounded_params):
					arg.allow_capture_invoke = False
				arg.expected_fn_inferred = True
				arg.expected_type_from_require = param_ty
				arg_types[idx] = type_expr(arg, expected_type=param_ty, used_as_value=False)
			# Re-type explicit callbackN(lambda) args with the expected
			# Callback type so the lambda inside gets concrete param types.
			# Without this, callback2(|req, ctx| ...) inside add_route(...)
			# leaves the lambda params as Unknown because callback2 is generic
			# and the expected type wasn't available during initial arg typing.
			_CB_NAMES = frozenset({"callback0", "callback1", "callback2", "callback_throw0", "callback_throw1", "callback_throw2"})
			for idx, arg in enumerate(expr.args):
				if not isinstance(arg, H.HCall):
					continue
				if not isinstance(getattr(arg, "fn", None), H.HVar):
					continue
				if arg.fn.name not in _CB_NAMES:
					continue
				if idx >= len(sig_inst.param_types):
					continue
				param_ty = sig_inst.param_types[idx]
				param_def = ctx.type_table.get(param_ty)
				if param_def.kind is not TypeKind.INTERFACE:
					continue
				if param_def.name not in ("Callback0", "Callback1", "Callback2", "CallbackThrow0", "CallbackThrow1", "CallbackThrow2"):
					continue
				# The arg was already typed without expected_type. Re-type
				# with the resolved Callback type so the inner lambda gets
				# concrete param types from the interface's type args.
				prev_ty = arg_types[idx]
				if prev_ty is not None and prev_ty != ctx.unknown_ty:
					# Already successfully typed — skip.
					continue
				arg_types[idx] = type_expr(arg, expected_type=param_ty, used_as_value=False)
			_b2_wrapped_params: dict[int, TypeId] = {}  # param_idx -> new Callback type
			if decl.fn_id is not None and any(isinstance(a, H.HLambda) for a in expr.args):
				req_expr = _require_for_fn(decl.fn_id)
				if req_expr is not None:
					sig_local = ctx.signatures_by_id.get(decl.fn_id) if ctx.signatures_by_id is not None else None
					param_types_for_subject = list(getattr(sig_local, "param_type_ids", []) or []) if sig_local is not None else list(sig_inst.param_types)
					def _fn_trait_expected(trait_name: str) -> tuple[int, bool] | None:
						if trait_name == "Fn0":
							return (0, False)
						if trait_name == "Fn1":
							return (1, False)
						if trait_name == "Fn2":
							return (2, False)
						if trait_name == "FnThrow0":
							return (0, True)
						if trait_name == "FnThrow1":
							return (1, True)
						if trait_name == "FnThrow2":
							return (2, True)
						return None
					def _param_index_for_subject(subj: object) -> int | None:
						subj_name = _subject_name(subj)
						subj_id = subj if isinstance(subj, TypeParamId) else None
						for idx, tid in enumerate(param_types_for_subject):
							td_local = ctx.type_table.get(tid)
							if td_local.kind is not TypeKind.TYPEVAR:
								continue
							tp_id = td_local.type_param_id
							if subj_id is not None and tp_id == subj_id:
								return idx
							if subj_name is not None:
								if ctx.type_param_names and tp_id in ctx.type_param_names and ctx.type_param_names[tp_id] == subj_name:
									return idx
								for tp in list(getattr(sig_local, "type_params", []) or []):
									if tp.id == tp_id and tp.name == subj_name:
										return idx
						return None
					for atom in _extract_conjunctive_facts(req_expr):
						if not isinstance(atom, parser_ast.TraitIs):
							continue
						trait_name = getattr(atom.trait, "name", None)
						expect = _fn_trait_expected(trait_name) if trait_name is not None else None
						if expect is None:
							continue
						param_idx = _param_index_for_subject(atom.subject)
						if param_idx is None or param_idx >= len(expr.args):
							continue
						arg = expr.args[param_idx]
						if not isinstance(arg, H.HLambda):
							continue
						trait_args = list(getattr(atom.trait, "args", []) or [])
						arity, can_throw = expect
						if arity == 0:
							if len(trait_args) < 1:
								continue
							ret_ty = resolve_opaque_type(trait_args[0], ctx.type_table, module_id=decl.fn_id.module or current_module_name)
							param_tys: list[TypeId] = []
						elif arity == 1:
							if len(trait_args) < 2:
								continue
							param_tys = [resolve_opaque_type(trait_args[0], ctx.type_table, module_id=decl.fn_id.module or current_module_name)]
							ret_ty = resolve_opaque_type(trait_args[1], ctx.type_table, module_id=decl.fn_id.module or current_module_name)
						else:
							if len(trait_args) < 3:
								continue
							param_tys = [
								resolve_opaque_type(trait_args[0], ctx.type_table, module_id=decl.fn_id.module or current_module_name),
								resolve_opaque_type(trait_args[1], ctx.type_table, module_id=decl.fn_id.module or current_module_name),
							]
							ret_ty = resolve_opaque_type(trait_args[2], ctx.type_table, module_id=decl.fn_id.module or current_module_name)
						arg_expected_type = ctx.type_table.ensure_function(param_tys, ret_ty, can_throw=can_throw)
						# For Fn-trait-bounded params with any captures,
						# keep allow_capture_invoke=True. Borrowed captures validated
						# by borrow checker; copy/move auto-wrapped in callback_N below.
						_has_any_caps_tp5 = bool(getattr(arg, "explicit_captures", None))
						if not _has_any_caps_tp5:
							arg.allow_capture_invoke = False
						arg.expected_fn_inferred = True
						arg.expected_type_from_require = arg_expected_type
						arg_types[param_idx] = type_expr(arg, expected_type=arg_expected_type, used_as_value=False)
						# B2/B4: auto-wrap capturing lambdas in callback_N()
						# so F is instantiated as Callback (not fn ptr).
						# Includes borrowed captures — the borrow checker validates
						# escape levels; MIR callback env handles ref fields.
						if _has_any_caps_tp5:
							_cb_call = _implicit_callback_wrap(
								ctx,
								arg=arg,
								callback_arity=arity,
								is_throw=can_throw,
							)
							expr.args[param_idx] = _cb_call
							arg_types[param_idx] = type_expr(_cb_call, used_as_value=False)
							_b2_wrapped_params[param_idx] = arg_types[param_idx]
			for idx, arg in enumerate(expr.args):
				if isinstance(arg, H.HCall) and isinstance(arg.fn, H.HVar) and _is_std_core_module(arg.fn.module_id, module_ids_by_name, visibility_provenance) and arg.fn.name in ("callback0", "callback1", "callback2"):
					continue
				if idx >= len(sig_inst.param_types):
					continue
				param_ty = sig_inst.param_types[idx]
				inst = ctx.type_table.get_interface_instance(param_ty)
				base_id = inst.base_id if inst is not None else param_ty
				schema = ctx.type_table.interface_bases.get(base_id)
				if schema is None:
					continue
				if schema.name not in ("Callback0", "Callback1", "Callback2", "CallbackThrow0", "CallbackThrow1", "CallbackThrow2"):
					continue
				arg_ty = arg_types[idx] if idx < len(arg_types) else None
				if not isinstance(arg, H.HLambda):
					if arg_ty is None:
						continue
					arg_def = ctx.type_table.get(arg_ty)
					if arg_def.kind is not TypeKind.FUNCTION or not arg_def.param_types:
						continue
				if isinstance(arg, H.HLambda):
					arity = len(arg.params)
				else:
					arity = len(arg_def.param_types) - 1
				is_throw = schema.name in ("CallbackThrow0", "CallbackThrow1", "CallbackThrow2")
				cb_call = _implicit_callback_wrap(
					ctx,
					arg=arg,
					callback_arity=arity,
					is_throw=is_throw,
				)
				expr.args[idx] = cb_call
				arg_types[idx] = type_expr(cb_call, expected_type=param_ty, used_as_value=False)
		# B2: reconcile sig_inst param types with auto-wrapped callback args.
		# After TP5 wrapping, some arg_types may be Callback while sig_inst
		# still has the function pointer type. Update sig_inst to match.
		_sig_param_types = list(sig_inst.param_types)
		for _bi in range(min(len(_sig_param_types), len(arg_types))):
			if arg_types[_bi] is None:
				continue
			_arg_d = ctx.type_table.get(arg_types[_bi])
			_par_d = ctx.type_table.get(_sig_param_types[_bi])
			if _arg_d.kind is TypeKind.INTERFACE and _par_d.kind is TypeKind.FUNCTION:
				_sig_param_types[_bi] = arg_types[_bi]
		if tuple(_sig_param_types) != sig_inst.param_types:
			sig_inst = CallableSignature(param_types=tuple(_sig_param_types), result_type=sig_inst.result_type)
		updated_types, had_autoborrow_error = _apply_autoborrow_args(
			expr.args,
			arg_types,
			list(sig_inst.param_types),
			span=getattr(expr, "loc", Span()),
		)
		arg_types = list(updated_types)
		if had_autoborrow_error:
			return record_expr(expr, ctx.unknown_ty)
		if decl.fn_id is None:
			diagnostics.append(_tc_diag(message=f"internal: missing fn_id for function '{expr.fn.name}'", severity="error", span=getattr(expr, "loc", Span())))
			return record_expr(expr, ctx.unknown_ty)
		intent.arg_expected_types = _expected_arg_types_for_call(list(sig_inst.param_types), len(expr.args))
		_propagate_arg_expected_types(intent, arg_types)
		call_can_throw = True
		if signatures_by_id is not None:
			fn_sig = signatures_by_id.get(decl.fn_id)
			if fn_sig is not None and fn_sig.declared_can_throw is not None:
				call_can_throw = bool(fn_sig.declared_can_throw)
		intrinsic_kind = _intrinsic_kind_for_decl(decl, fn_sig if signatures_by_id is not None else None)
		if intrinsic_kind is not None:
			record_call_info(expr, param_types=list(sig_inst.param_types), return_type=sig_inst.result_type, can_throw=call_can_throw, target=CallTarget.intrinsic(intrinsic_kind))
			return record_expr(expr, sig_inst.result_type)
		record_call_info(expr, param_types=list(sig_inst.param_types), return_type=sig_inst.result_type, can_throw=call_can_throw, target=CallTarget.direct(decl.fn_id))
		# B2: update inst_subst args for wrapped callback params.
		# After TP5 wraps a capturing lambda in callback_N(), the type param
		# F should be Callback<A,R> not the function pointer type.
		if _b2_wrapped_params and inst_subst is not None and decl.fn_id is not None:
			_sig_for_tp = ctx.signatures_by_id.get(decl.fn_id) if ctx.signatures_by_id is not None else None
			if _sig_for_tp is not None:
				_ptids = list(getattr(_sig_for_tp, "param_type_ids", []) or [])
				_tps = list(getattr(_sig_for_tp, "type_params", []) or [])
				_new_args = list(inst_subst.args or [])
				_changed = False
				for _pi, _new_ty in _b2_wrapped_params.items():
					if _pi >= len(_ptids):
						continue
					_td = ctx.type_table.get(_ptids[_pi])
					if _td.kind is not TypeKind.TYPEVAR:
						continue
					_tp_id = _td.type_param_id
					for _ti, _tp in enumerate(_tps):
						if _tp.id == _tp_id and _ti < len(_new_args):
							_new_args[_ti] = _new_ty
							_changed = True
							break
				if _changed:
					inst_subst = Subst(owner=inst_subst.owner, args=_new_args)
		if ctx.record_instantiation is not None and inst_subst is not None and decl.fn_id is not None:
			inst_args = tuple(inst_subst.args or [])
			if inst_args and not any(ctx.type_table.has_typevar(t) for t in inst_args):
				csid = getattr(expr, "callsite_id", None)
				ctx.record_instantiation(callsite_id=csid, target_fn_id=decl.fn_id, impl_args=tuple(), fn_args=inst_args)
		return record_expr(expr, sig_inst.result_type)

	return record_expr(expr, ctx.unknown_ty)
