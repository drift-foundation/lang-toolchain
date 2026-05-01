# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Set, Tuple, Mapping

from lang.driftc.parser import ast as parser_ast
from lang.driftc.core.types_core import TypeTable, TypeKind
from .world import TraitWorld, TraitKey, TypeKey, ImplDef, trait_key_from_expr, type_key_from_expr, type_key_from_typeid


class ProofStatus(Enum):
	PROVED = auto()
	REFUTED = auto()
	UNKNOWN = auto()
	AMBIGUOUS = auto()


class ProofFailureReason(Enum):
	NO_IMPL = auto()
	AMBIGUOUS_IMPL = auto()
	UNKNOWN = auto()


class ObligationOriginKind(Enum):
	CALL_SITE = auto()
	METHOD_CALL = auto()
	CANDIDATE_IMPL = auto()
	CALLEE_REQUIRE = auto()
	IMPL_REQUIRE = auto()


@dataclass(frozen=True)
class ObligationOrigin:
	kind: ObligationOriginKind
	label: Optional[str] = None
	span: Optional[object] = None


@dataclass(frozen=True)
class Obligation:
	subject: TypeKey
	trait: TraitKey
	origin: ObligationOrigin
	trait_args: Tuple[TypeKey, ...] = ()
	span: Optional[object] = None
	notes: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProofFailure:
	obligation: Obligation
	reason: ProofFailureReason
	impl_ids: Tuple[int, ...] = ()
	details: Tuple[str, ...] = ()
	message_override: str | None = None


@dataclass
class ProofResult:
	status: ProofStatus
	reasons: List[str] = field(default_factory=list)
	used_impls: List[int] = field(default_factory=list)


@dataclass
class Env:
	assumed_true: Set[Tuple[object, TraitKey]] = field(default_factory=set)
	assumed_false: Set[Tuple[object, TraitKey]] = field(default_factory=set)
	default_module: Optional[str] = None
	default_package: Optional[str] = None
	module_packages: Mapping[str, str] = field(default_factory=dict)
	type_table: TypeTable | None = None


CacheKey = Tuple[object, str, Optional[TypeKey], Tuple[TypeKey, ...]]


def _type_key_str(key: TypeKey) -> str:
	pkg = key.package_id
	module = key.module
	base = f"{module}.{key.name}" if module else key.name
	if pkg:
		base = f"{pkg}::{base}"
	if not key.args:
		return base
	args = ", ".join(_type_key_str(a) for a in key.args)
	return f"{base}<{args}>"


def _trait_key_str(key: TraitKey) -> str:
	base = f"{key.module}.{key.name}" if key.module else key.name
	if key.package_id:
		return f"{key.package_id}::{base}"
	return base


def _impl_sort_key(impl: ImplDef) -> Tuple[str, str, int, int]:
	trait_str = _trait_key_str(impl.trait)
	target_str = _type_key_str(impl.target)
	loc = getattr(impl.loc, "loc", None) if hasattr(impl.loc, "loc") else impl.loc
	line = getattr(loc, "line", 0) or 0
	col = getattr(loc, "column", 0) or 0
	return (trait_str, target_str, line, col)


def prove_expr(
	world: TraitWorld,
	env: Env,
	subst: Dict[object, TypeKey],
	expr: parser_ast.TraitExpr,
	*,
	_cache: Optional[Dict[CacheKey, ProofResult]] = None,
	_in_progress: Optional[Set[Tuple[str, TraitKey, Optional[TypeKey]]]] = None,
) -> ProofResult:
	if isinstance(expr, parser_ast.TraitIs):
		trait_key = trait_key_from_expr(
			expr.trait,
			default_module=env.default_module,
			default_package=env.default_package,
			module_packages=env.module_packages,
			type_param_subst=subst,
		)
		trait_args = tuple(
			_resolve_trait_arg(
				a,
				subst,
				default_module=env.default_module,
				default_package=env.default_package,
				module_packages=env.module_packages,
			)
			for a in (getattr(expr.trait, "args", []) or [])
		)
		return prove_is(
			world,
			env,
			subst,
			expr.subject,
			trait_key,
			trait_args=trait_args,
			_cache=_cache,
			_in_progress=_in_progress,
		)
	if isinstance(expr, parser_ast.TraitAnd):
		left = prove_expr(world, env, subst, expr.left, _cache=_cache, _in_progress=_in_progress)
		if left.status is ProofStatus.REFUTED:
			return left
		right = prove_expr(world, env, subst, expr.right, _cache=_cache, _in_progress=_in_progress)
		if right.status is ProofStatus.REFUTED:
			return right
		if left.status is ProofStatus.AMBIGUOUS or right.status is ProofStatus.AMBIGUOUS:
			return ProofResult(status=ProofStatus.AMBIGUOUS, reasons=left.reasons + right.reasons)
		if left.status is ProofStatus.UNKNOWN or right.status is ProofStatus.UNKNOWN:
			return ProofResult(status=ProofStatus.UNKNOWN, reasons=left.reasons + right.reasons)
		return ProofResult(status=ProofStatus.PROVED, reasons=left.reasons + right.reasons, used_impls=left.used_impls + right.used_impls)
	if isinstance(expr, parser_ast.TraitOr):
		left = prove_expr(world, env, subst, expr.left, _cache=_cache, _in_progress=_in_progress)
		right = prove_expr(world, env, subst, expr.right, _cache=_cache, _in_progress=_in_progress)
		if left.status is ProofStatus.PROVED and right.status is ProofStatus.PROVED:
			return ProofResult(status=ProofStatus.PROVED, reasons=left.reasons + right.reasons, used_impls=left.used_impls + right.used_impls)
		if left.status is ProofStatus.PROVED:
			return left
		if right.status is ProofStatus.PROVED:
			return right
		if left.status is ProofStatus.REFUTED and right.status is ProofStatus.REFUTED:
			return ProofResult(status=ProofStatus.REFUTED, reasons=left.reasons + right.reasons)
		if left.status is ProofStatus.AMBIGUOUS or right.status is ProofStatus.AMBIGUOUS:
			return ProofResult(status=ProofStatus.AMBIGUOUS, reasons=left.reasons + right.reasons)
		return ProofResult(status=ProofStatus.UNKNOWN, reasons=left.reasons + right.reasons)
	if isinstance(expr, parser_ast.TraitNot):
		return deny_expr(world, env, subst, expr.expr, _cache=_cache, _in_progress=_in_progress)
	return ProofResult(status=ProofStatus.UNKNOWN, reasons=["unsupported trait expression"])


def deny_expr(
	world: TraitWorld,
	env: Env,
	subst: Dict[object, TypeKey],
	expr: parser_ast.TraitExpr,
	*,
	_cache: Optional[Dict[CacheKey, ProofResult]] = None,
	_in_progress: Optional[Set[Tuple[str, TraitKey, Optional[TypeKey]]]] = None,
) -> ProofResult:
	if isinstance(expr, parser_ast.TraitIs):
		trait_key = trait_key_from_expr(
			expr.trait,
			default_module=env.default_module,
			default_package=env.default_package,
			module_packages=env.module_packages,
			type_param_subst=subst,
		)
		trait_args = tuple(
			_resolve_trait_arg(
				a,
				subst,
				default_module=env.default_module,
				default_package=env.default_package,
				module_packages=env.module_packages,
			)
			for a in (getattr(expr.trait, "args", []) or [])
		)
		res = prove_is(
			world,
			env,
			subst,
			expr.subject,
			trait_key,
			trait_args=trait_args,
			_cache=_cache,
			_in_progress=_in_progress,
		)
		if res.status is ProofStatus.PROVED:
			return ProofResult(status=ProofStatus.REFUTED, reasons=res.reasons)
		if res.status is ProofStatus.REFUTED:
			return ProofResult(status=ProofStatus.PROVED, reasons=res.reasons)
		if res.status is ProofStatus.AMBIGUOUS:
			return ProofResult(status=ProofStatus.AMBIGUOUS, reasons=res.reasons)
		return ProofResult(status=ProofStatus.UNKNOWN, reasons=res.reasons)
	if isinstance(expr, parser_ast.TraitAnd):
		left = deny_expr(world, env, subst, expr.left, _cache=_cache, _in_progress=_in_progress)
		right = deny_expr(world, env, subst, expr.right, _cache=_cache, _in_progress=_in_progress)
		if left.status is ProofStatus.PROVED or right.status is ProofStatus.PROVED:
			return ProofResult(status=ProofStatus.PROVED, reasons=left.reasons + right.reasons)
		if left.status is ProofStatus.AMBIGUOUS or right.status is ProofStatus.AMBIGUOUS:
			return ProofResult(status=ProofStatus.AMBIGUOUS, reasons=left.reasons + right.reasons)
		if left.status is ProofStatus.REFUTED and right.status is ProofStatus.REFUTED:
			return ProofResult(status=ProofStatus.REFUTED, reasons=left.reasons + right.reasons)
		return ProofResult(status=ProofStatus.UNKNOWN, reasons=left.reasons + right.reasons)
	if isinstance(expr, parser_ast.TraitOr):
		left = deny_expr(world, env, subst, expr.left, _cache=_cache, _in_progress=_in_progress)
		if left.status is ProofStatus.REFUTED:
			return left
		right = deny_expr(world, env, subst, expr.right, _cache=_cache, _in_progress=_in_progress)
		if right.status is ProofStatus.REFUTED:
			return right
		if left.status is ProofStatus.AMBIGUOUS or right.status is ProofStatus.AMBIGUOUS:
			return ProofResult(status=ProofStatus.AMBIGUOUS, reasons=left.reasons + right.reasons)
		if left.status is ProofStatus.UNKNOWN or right.status is ProofStatus.UNKNOWN:
			return ProofResult(status=ProofStatus.UNKNOWN, reasons=left.reasons + right.reasons)
		return ProofResult(status=ProofStatus.PROVED, reasons=left.reasons + right.reasons)
	if isinstance(expr, parser_ast.TraitNot):
		return prove_expr(world, env, subst, expr.expr, _cache=_cache, _in_progress=_in_progress)
	return ProofResult(status=ProofStatus.UNKNOWN, reasons=["unsupported trait expression"])


def _subject_key(subject: object) -> object:
	if isinstance(subject, parser_ast.SelfRef):
		return "Self"
	if isinstance(subject, parser_ast.TypeNameRef):
		return subject.name
	if isinstance(subject, str):
		return subject
	return subject


def _type_expr_from_key(key: TypeKey) -> parser_ast.TypeExpr:
	return parser_ast.TypeExpr(
		name=key.name,
		args=[_type_expr_from_key(a) for a in key.args],
		module_id=key.module,
	)


def _resolve_trait_arg(
	arg: parser_ast.TypeExpr,
	subst: Dict[object, TypeKey],
	*,
	default_module: Optional[str],
	default_package: Optional[str],
	module_packages: Mapping[str, str],
) -> TypeKey:
	if not getattr(arg, "args", None):
		subj = subst.get(arg.name)
		if isinstance(subj, TypeKey):
			return subj
		if arg.name == "Self":
			subj = subst.get("Self")
			if isinstance(subj, TypeKey):
				return subj
	key = type_key_from_expr(
		arg,
		default_module=default_module,
		default_package=default_package,
		module_packages=module_packages,
	)
	if not getattr(arg, "args", None):
		return key
	args = tuple(
		_resolve_trait_arg(
			a,
			subst,
			default_module=default_module,
			default_package=default_package,
			module_packages=module_packages,
		)
		for a in (getattr(arg, "args", []) or [])
	)
	if args == key.args:
		return key
	return TypeKey(package_id=key.package_id, module=key.module, name=key.name, args=args)


def _bind_impl_type_params(
	template: TypeKey,
	actual: TypeKey,
	params: set[str],
	out: dict[str, TypeKey],
) -> bool:
	if template.name in params and not template.args:
		cur = out.get(template.name)
		if cur is None:
			out[template.name] = actual
			return True
		return cur == actual
	if template.name != actual.name or template.module != actual.module or template.package_id != actual.package_id:
		return False
	if len(template.args) != len(actual.args):
		return False
	return all(_bind_impl_type_params(t, a, params, out) for t, a in zip(template.args, actual.args))


def prove_is(
	world: TraitWorld,
	env: Env,
	subst: Dict[object, TypeKey],
	subject: object,
	trait_key: TraitKey,
	*,
	trait_args: Tuple[TypeKey, ...] = (),
	_cache: Optional[Dict[CacheKey, ProofResult]] = None,
	_in_progress: Optional[Set[Tuple[str, TraitKey, Optional[TypeKey]]]] = None,
) -> ProofResult:
	cache = _cache if _cache is not None else {}
	in_progress = _in_progress if _in_progress is not None else set()
	subj_key = _subject_key(subject)
	subject_ty = subst.get(subj_key)
	if subject_ty is None and isinstance(subject, TypeKey):
		subject_ty = subject
	cache_key: CacheKey = (subj_key, _trait_key_str(trait_key), subject_ty, trait_args)
	if cache_key in cache:
		return cache[cache_key]
	if (subj_key, trait_key) in env.assumed_true:
		res = ProofResult(status=ProofStatus.PROVED, reasons=["assumed true"])
		cache[cache_key] = res
		return res
	if subject_ty is not None and (subject_ty, trait_key) in env.assumed_true:
		res = ProofResult(status=ProofStatus.PROVED, reasons=["assumed true"])
		cache[cache_key] = res
		return res
	if (subj_key, trait_key) in env.assumed_false:
		res = ProofResult(status=ProofStatus.REFUTED, reasons=["assumed false"])
		cache[cache_key] = res
		return res
	if subject_ty is not None and (subject_ty, trait_key) in env.assumed_false:
		res = ProofResult(status=ProofStatus.REFUTED, reasons=["assumed false"])
		cache[cache_key] = res
		return res
	if subject_ty is None:
		res = ProofResult(status=ProofStatus.UNKNOWN, reasons=["unknown subject type"])
		cache[cache_key] = res
		return res
	from lang.driftc.checker.call_resolver import _CALLBACK_FN_TRAIT_NAMES, _CALLBACK_FN_PAIRS
	if trait_key.name in _CALLBACK_FN_TRAIT_NAMES and subject_ty.name == "fn":
		if trait_key.module in (None, "std.core", "lang.core") or str(trait_key.module).endswith("std.core") or str(trait_key.module).endswith("lang.core"):
			if subject_ty.fn_throws is True:
				res = ProofResult(status=ProofStatus.REFUTED, reasons=["fn can throw"])
				cache[cache_key] = res
				return res
			res = ProofResult(status=ProofStatus.PROVED, reasons=["fn pointer impl"])
			cache[cache_key] = res
			return res
	if (subject_ty.name, trait_key.name) in _CALLBACK_FN_PAIRS:
		_mod_ok = lambda m: m in (None, "std.core", "lang.core") or str(m).endswith("std.core") or str(m).endswith("lang.core")
		if _mod_ok(trait_key.module) and _mod_ok(subject_ty.module):
			if not trait_args or not subject_ty.args or subject_ty.args == trait_args:
				res = ProofResult(status=ProofStatus.PROVED, reasons=["callback interface impl"])
				cache[cache_key] = res
				return res
	if trait_key not in world.traits:
		# Interface-aware path: `require T is I` where I is an interface
		# is proved by an `implement I for T` entry in the interface-
		# impl index.  This is a generic-constraint-only query — we
		# only need to know the impl exists; method-level machinery
		# (vtable emission, coherence checks) is handled elsewhere by
		# the interface-type-table path.
		#
		# Phase 1 deliberately proves ONLY for non-generic,
		# non-conditional impls whose `target` equals the subject
		# exactly.  Head-match-alone is unsound (a `Box<Int>` impl
		# would otherwise satisfy `require T is I` for `T = Box<String>`
		# and route dispatch through a vtable built against a
		# different type instantiation).  Generic / conditional impls
		# (`implement<T> I for Box<T>` or with a `require` clause) are
		# refuted here because there is no impl-applicability solver
		# for interfaces yet — a later phase will extend this path to
		# bind impl type params and recursively prove the impl's
		# require clause.  Until then, deferring is the safe default.
		if trait_key in getattr(world, "interfaces", {}):
			head = subject_ty.head()
			iface_impls = getattr(world, "interface_impls_by_iface_target", {}).get((trait_key, head), [])
			saw_generic = False
			saw_conditional = False
			for ref in iface_impls:
				if getattr(ref, "type_params", ()):
					saw_generic = True
					continue
				if getattr(ref, "require_expr", None) is not None:
					saw_conditional = True
					continue
				if ref.target == subject_ty:
					res = ProofResult(status=ProofStatus.PROVED, reasons=["interface impl"])
					cache[cache_key] = res
					return res
			reasons: List[str] = []
			if saw_generic:
				reasons.append("generic interface impl not yet supported for require proving")
			if saw_conditional:
				reasons.append("conditional interface impl not yet supported for require proving")
			if not reasons:
				reasons.append("no interface impl")
			res = ProofResult(status=ProofStatus.REFUTED, reasons=reasons)
			cache[cache_key] = res
			return res
		res = ProofResult(status=ProofStatus.REFUTED, reasons=["unknown trait"])
		cache[cache_key] = res
		return res

	cycle_key = (subj_key, trait_key, subject_ty)
	if cycle_key in in_progress:
		res = ProofResult(status=ProofStatus.UNKNOWN, reasons=["cycle in trait requirements"])
		cache[cache_key] = res
		return res
	in_progress.add(cycle_key)
	try:
		head = subject_ty.head()
		candidates = world.impls_by_trait_target.get((trait_key, head), [])
		ordered = [(impl_id, world.impls[impl_id]) for impl_id in candidates]
		ordered.sort(key=lambda item: (_impl_sort_key(item[1]), item[0]))

		applicable: List[int] = []
		reasons: List[str] = []
		saw_unknown_req = False
		saw_ambiguous_req = False
		for impl_id, impl in ordered:
			bindings: dict[str, TypeKey] = {}
			if trait_args or impl.trait_args:
				if len(trait_args) != len(impl.trait_args):
					continue
				params = set(getattr(impl, "type_params", []) or [])
				ok = True
				for template_arg, actual_arg in zip(impl.trait_args, trait_args):
					if template_arg == actual_arg:
						continue
					if not params or not _bind_impl_type_params(template_arg, actual_arg, params, bindings):
						ok = False
						break
				if not ok:
					continue
			if impl.target != subject_ty:
				params = set(getattr(impl, "type_params", []) or [])
				if not params or not _bind_impl_type_params(impl.target, subject_ty, params, bindings):
					continue
			impl_subst = subst
			if bindings:
				impl_subst = dict(subst)
				impl_subst.update(bindings)
			if impl.require is not None:
				req = prove_expr(world, env, impl_subst, impl.require, _cache=cache, _in_progress=in_progress)
				if req.status is ProofStatus.PROVED:
					applicable.append(impl_id)
					continue
				if req.status is ProofStatus.AMBIGUOUS:
					saw_ambiguous_req = True
					reasons.append("ambiguous impl requirement")
					continue
				if req.status is ProofStatus.UNKNOWN:
					saw_unknown_req = True
					reasons.append("impl requirement unknown")
					continue
				reasons.append("impl requirement refuted")
				continue
			applicable.append(impl_id)

		if len(applicable) == 0:
			if saw_ambiguous_req:
				res = ProofResult(status=ProofStatus.AMBIGUOUS, reasons=reasons or ["ambiguous impl requirements"])
			elif saw_unknown_req:
				res = ProofResult(status=ProofStatus.UNKNOWN, reasons=reasons or ["impl requirement unknown"])
			else:
				res = ProofResult(status=ProofStatus.REFUTED, reasons=reasons or ["no applicable impls"])
		elif len(applicable) == 1:
			res = ProofResult(status=ProofStatus.PROVED, used_impls=applicable)
		else:
			res = ProofResult(status=ProofStatus.AMBIGUOUS, reasons=["multiple applicable impls"], used_impls=applicable)
		req_expr = world.traits.get(trait_key).require if trait_key in world.traits else None
		if req_expr is not None:
			req_subst = dict(subst)
			req_subst["Self"] = subject_ty
			trait_def = world.traits.get(trait_key)
			trait_params = list(getattr(trait_def, "type_params", []) or []) if trait_def is not None else []
			for name, arg in zip(trait_params, trait_args):
				req_subst[name] = arg
			req_env = Env(
				assumed_true=set(env.assumed_true),
				assumed_false=set(env.assumed_false),
				default_module=trait_key.module,
				default_package=trait_key.package_id,
				module_packages=env.module_packages,
				type_table=env.type_table,
			)
			req_res = prove_expr(world, req_env, req_subst, req_expr, _cache=cache, _in_progress=in_progress)
			if req_res.status is not ProofStatus.PROVED:
				combined_reasons = list(res.reasons) + list(req_res.reasons)
				def _combine_status(left: ProofStatus, right: ProofStatus) -> ProofStatus:
					if left is ProofStatus.REFUTED or right is ProofStatus.REFUTED:
						return ProofStatus.REFUTED
					if left is ProofStatus.AMBIGUOUS or right is ProofStatus.AMBIGUOUS:
						return ProofStatus.AMBIGUOUS
					if left is ProofStatus.UNKNOWN or right is ProofStatus.UNKNOWN:
						return ProofStatus.UNKNOWN
					return ProofStatus.PROVED
				res = ProofResult(
					status=_combine_status(res.status, req_res.status),
					reasons=combined_reasons,
					used_impls=res.used_impls,
				)
		# Frozen structural-derive shortcut.  When the regular impl
		# lookup didn't prove `T: shareable.Frozen` for a struct/
		# variant subject, fall back to structural derivation: the
		# type is Frozen iff every owned field's type is itself
		# proved Frozen.  Per the substrate plan
		# `work/constshare-substrate/phase1a-dispositions.md` §3,
		# this is path (a) — a prover-level shortcut, no synthesized
		# ImplDef records.
		#
		# Constraints honored:
		#   - Only auto-prove for STRUCT / VARIANT.
		#   - Every field must be PROVED (UNKNOWN / AMBIGUOUS /
		#     REFUTED on any field → don't promote to PROVED).
		#   - Cycle handling: prove_is's existing in-progress set
		#     covers self-recursion conservatively (returns UNKNOWN
		#     on cycle), which propagates here as "field not PROVED"
		#     and blocks the structural promotion.
		#   - References (&T / &mut T) lack explicit Frozen impls in
		#     stdlib; fall through here as REFUTED-on-field.
		#   - Mutable backings (Mutex / Arc / Atomic / Array /
		#     HashMap) similarly lack explicit impls and so block the
		#     containing struct's promotion via the field check.
		if (
			res.status is ProofStatus.REFUTED
			and trait_key.module == "std.core.shareable"
			and trait_key.name == "Frozen"
			and env.type_table is not None
		):
			structural = _frozen_structural_status(
				world, env, subst, subject_ty, trait_key,
				cache, in_progress,
			)
			if structural is ProofStatus.PROVED:
				res = ProofResult(
					status=ProofStatus.PROVED,
					reasons=["frozen structural auto-derive (all fields Frozen)"],
				)
		cache[cache_key] = res
		return res
	finally:
		in_progress.remove(cycle_key)


def _frozen_structural_status(
	world: TraitWorld,
	env: Env,
	subst: Dict[object, TypeKey],
	subject_ty: TypeKey,
	frozen_key: TraitKey,
	cache: Dict[CacheKey, ProofResult],
	in_progress: Set[Tuple[str, TraitKey, Optional[TypeKey]]],
) -> Optional[ProofStatus]:
	"""Structural Frozen derivation for STRUCT / VARIANT subjects.

	Returns:
	  - PROVED if subject_ty is a struct/variant whose every owned
	    field type proves Frozen (recursively).
	  - REFUTED if any field provably does NOT prove Frozen.
	  - UNKNOWN if the lookup is inconclusive (no matching tid found,
	    cycle in recursion, etc.) — caller leaves the prior REFUTED
	    in place.
	  - None if subject is not a struct/variant.
	"""
	type_table = env.type_table
	if type_table is None:
		return None
	tid = _find_typeid_for_typekey(type_table, subject_ty)
	if tid is None:
		return ProofStatus.UNKNOWN
	td = type_table.get(tid)
	if td.kind is TypeKind.STRUCT:
		inst = type_table.get_struct_instance(tid)
		if inst is None:
			return ProofStatus.UNKNOWN
		field_types = list(inst.field_types)
	elif td.kind is TypeKind.VARIANT:
		inst = type_table.get_variant_instance(tid)
		if inst is None:
			return ProofStatus.UNKNOWN
		field_types = []
		for arm in inst.arms:
			field_types.extend(arm.field_types)
	else:
		return None
	# Empty struct / payload-less variant: no observation surface,
	# trivially Frozen.
	if not field_types:
		return ProofStatus.PROVED
	for ftid in field_types:
		fkey = type_key_from_typeid(type_table, ftid)
		sub = prove_is(
			world, env, subst, fkey, frozen_key,
			_cache=cache, _in_progress=in_progress,
		)
		if sub.status is not ProofStatus.PROVED:
			return ProofStatus.REFUTED if sub.status is ProofStatus.REFUTED else ProofStatus.UNKNOWN
	return ProofStatus.PROVED


def _find_typeid_for_typekey(type_table: TypeTable, key: TypeKey) -> Optional[int]:
	"""Locate a TypeId in the type_table whose canonical TypeKey
	equals `key`.  Cached per-key on the type_table.

	Linear scan over the type_table's tids on first lookup; cached
	thereafter.  Used by the Frozen structural shortcut to navigate
	from a prover-side TypeKey back to the type_table's struct/
	variant instance for field walking.
	"""
	cache_attr = "_frozen_typekey_to_tid_cache"
	cache: dict = getattr(type_table, cache_attr, None)
	if cache is None:
		cache = {}
		setattr(type_table, cache_attr, cache)
	cached = cache.get(key)
	if cached is not None:
		return cached if cached >= 0 else None
	types_by_id = getattr(type_table, "types_by_id", None)
	tid_iter: list[int]
	if isinstance(types_by_id, dict):
		tid_iter = list(types_by_id.keys())
	else:
		struct_bases = getattr(type_table, "struct_bases", {}) or {}
		variant_schemas = getattr(type_table, "variant_schemas", {}) or {}
		tid_iter = list(struct_bases.keys()) + list(variant_schemas.keys())
	for tid in tid_iter:
		td = type_table.get(tid)
		if td.kind not in (TypeKind.STRUCT, TypeKind.VARIANT):
			continue
		try:
			other_key = type_key_from_typeid(type_table, tid)
		except Exception:
			continue
		if other_key == key:
			cache[key] = tid
			return tid
	cache[key] = -1
	return None


def prove_obligation(
	world: TraitWorld,
	env: Env,
	obligation: Obligation,
) -> ProofFailure | None:
	res = prove_is(
		world,
		env,
		{},
		obligation.subject,
		obligation.trait,
		trait_args=obligation.trait_args,
	)
	if res.status is ProofStatus.PROVED:
		return None
	if res.status is ProofStatus.AMBIGUOUS:
		return ProofFailure(
			obligation=obligation,
			reason=ProofFailureReason.AMBIGUOUS_IMPL,
			impl_ids=tuple(res.used_impls),
			details=tuple(res.reasons),
		)
	if res.status is ProofStatus.UNKNOWN:
		return ProofFailure(
			obligation=obligation,
			reason=ProofFailureReason.UNKNOWN,
			impl_ids=tuple(res.used_impls),
			details=tuple(res.reasons),
		)
	return ProofFailure(
		obligation=obligation,
		reason=ProofFailureReason.NO_IMPL,
		impl_ids=tuple(res.used_impls),
		details=tuple(res.reasons),
	)


__all__ = [
	"Env",
	"ProofFailure",
	"ProofFailureReason",
	"ProofResult",
	"ProofStatus",
	"Obligation",
	"ObligationOrigin",
	"ObligationOriginKind",
	"prove_expr",
	"prove_is",
	"deny_expr",
	"prove_obligation",
	"CacheKey",
]
