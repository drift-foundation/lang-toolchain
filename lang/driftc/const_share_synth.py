# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""ConstShare structural synthesis — Phase 1.

Phase 1 scope (per `work/constshare-substrate/post-link-mandatory-design.md`):
  - structs only (no variants),
  - concrete fields only (no generic typevar fields),
  - same-module nested composition (fixed-point iteration),
  - package serialization included.

Runs at driver level (not parser-local).  Inserted into the main
flow between `_build_linked_world` and the `_pre_typecheck_hirs`
snapshot (`driftc.py`), so synthesized HIR + signatures + impl
metadata all flow into the package emission path.

**Single source of trait truth: `linked_world.visible_world(M)`**
for a struct defined in module M.  Not `linked_world.global_world`
directly — that would let synthesis cross visibility boundaries
the user's source code couldn't have written.

**Single mutation entry point: `register_synthesized_const_share_impl`**.
All driver-state updates go through one helper.  No scattered
writes.

**Per-iteration registration**: as soon as a candidate qualifies,
its synthesized impl is registered so subsequent iterations
naturally see it (real impls, not phantom proof model).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional, Tuple

from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.checker import FnSignature
from lang.driftc.core.types_core import TypeId, TypeKind
from lang.driftc.impl_index import ImplMeta, ImplMethodMeta
from lang.driftc.parser import ast as parser_ast
from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.core.span import Span
from lang.driftc.traits.linked_world import LinkedWorld
from lang.driftc.traits.solver import (
	Env as TraitEnv,
	ProofStatus,
	prove_is,
)
from lang.driftc.traits.world import (
	ImplDef,
	TraitKey,
	TraitWorld,
	TypeKey,
	type_key_from_typeid,
)


_CONST_SHARE_TRAIT_NAME = "ConstShare"
_CONST_SHARE_TRAIT_MODULE = "std.core.shareable"
_COPY_TRAIT_NAME = "Copy"
_COPY_TRAIT_MODULE = "std.core.copy"
_FROZEN_TRAIT_NAME = "Frozen"
_FROZEN_TRAIT_MODULE = "std.core.shareable"

# Defensive cap on the discovery fixed-point iteration count.  Each
# successful iteration adds at least one candidate to `derived`, so
# the natural bound is len(candidates).  This guard catches a buggy
# qualifier that returns None even after registration (which would
# otherwise loop forever).
_MAX_FIXEDPOINT_ITERS = 100


@dataclass
class _Candidate:
	target_type_id: TypeId
	def_module: str
	struct_name: str
	field_names: List[str]
	field_type_ids: List[TypeId]
	loc: object  # parser source loc for synthesized AST


@dataclass
class _SynthResult:
	fn_id: FunctionId
	signature: FnSignature
	hir: H.HBlock
	field_paths: List[str]
	impl_meta: ImplMeta


def _resolve_trait_key(world: TraitWorld, *, module: str, name: str) -> Optional[TraitKey]:
	for k in getattr(world, "traits", {}).keys():
		if k.module == module and k.name == name:
			return k
	return None


def synthesize_const_share_phase1(
	*,
	linked_world: LinkedWorld,
	type_table,
	module_exports: dict | None,
	signatures_by_id: dict,
	normalized_hirs_by_id: dict,
	func_hirs_by_id: dict,
	fn_ids_by_name: dict,
	module_ids: dict,
	visible_module_names_by_name: dict | None,
	package_id: Optional[str],
	module_packages: Mapping[str, str],
	callable_registry=None,
	next_callable_id_box: Optional[list] = None,
	source_modules: Optional[set] = None,
) -> int:
	"""Driver-level entry point.  Discovers structs that auto-derive
	ConstShare under the v1 composition rule and registers
	synthesized impls.  Returns the number of impls synthesized.

	Per-iteration registration via the single helper is the
	authoritative model — see the design doc.
	"""
	# Resolve the three trait keys we'll query against.  ConstShare's
	# trait must exist for any synthesis to be possible; if not,
	# return 0 (consumer that didn't import shareable).
	cs_key = _resolve_trait_key(
		linked_world.global_world,
		module=_CONST_SHARE_TRAIT_MODULE,
		name=_CONST_SHARE_TRAIT_NAME,
	)
	if cs_key is None:
		return 0
	copy_key = _resolve_trait_key(
		linked_world.global_world,
		module=_COPY_TRAIT_MODULE,
		name=_COPY_TRAIT_NAME,
	)
	frozen_key = _resolve_trait_key(
		linked_world.global_world,
		module=_FROZEN_TRAIT_MODULE,
		name=_FROZEN_TRAIT_NAME,
	)
	if copy_key is None or frozen_key is None:
		return 0

	# Build the candidate list from the type table's registered
	# struct bases.  Iterate all struct schemas, filter to:
	#   - non-generic (no type_params),
	#   - non-empty fields,
	#   - module declared (def_module known),
	#   - not already covered by a hand-written ConstShare impl.
	#
	# For each, resolve concrete field TypeIds via the struct
	# instance (base_id == instance_id for non-generic structs).
	candidates: List[_Candidate] = []
	user_impl_targets: set[Tuple[str, str]] = _existing_const_share_targets(
		linked_world,
		cs_key,
	)
	struct_bases = getattr(type_table, "struct_bases", None)
	if not isinstance(struct_bases, dict):
		return 0
	for tid, schema in struct_bases.items():
		mid = getattr(schema, "module_id", None)
		if not isinstance(mid, str) or not mid:
			continue
		# Phase 1: only synthesize for structs declared in the
		# CURRENT BUILD's source modules.  Structs from dep
		# packages (loaded from .dmp) already have their own
		# producer-side synthesis; re-synthesizing here would
		# pollute `module_exports[that_module]["impls"]` and
		# cause cross-package "module provided by multiple
		# packages" conflicts at consumer load time.
		#
		# `source_modules` is the set of module names being
		# compiled in this build (from `modules.keys()` in
		# main()).  Empty / None → fall back to skipping
		# stdlib + lang only (used by the test-helper paths
		# that don't pass source_modules through).
		if source_modules is not None:
			if mid not in source_modules:
				continue
		else:
			if mid.startswith("std.") or mid.startswith("lang.") or mid == "lang.core":
				continue
		if list(getattr(schema, "type_params", []) or []):
			continue  # Phase 1: skip generics
		fields = list(getattr(schema, "fields", []) or [])
		if not fields:
			continue
		if (mid, schema.name) in user_impl_targets:
			continue
		# Concrete field TypeIds via the struct instance.
		inst = type_table.get_struct_instance(tid)
		field_type_ids: List[TypeId] = []
		if inst is not None and getattr(inst, "field_types", None):
			field_type_ids = list(inst.field_types)
		else:
			# No instance yet (or missing field types) — skip.
			continue
		if len(field_type_ids) != len(fields):
			continue
		field_names = [f.name for f in fields]
		decl_loc = getattr(schema, "decl_loc", None)
		candidates.append(_Candidate(
			target_type_id=tid,
			def_module=mid,
			struct_name=schema.name,
			field_names=field_names,
			field_type_ids=field_type_ids,
			loc=decl_loc,
		))

	if not candidates:
		return 0

	# Per-iteration fixed-point.
	derived: set[TypeId] = set()
	fn_id_ordinals: dict[Tuple[str, str], int] = {}  # (module, symbol_name) -> next ordinal
	synthesized = 0
	iteration = 0
	changed = True
	while changed:
		iteration += 1
		if iteration > _MAX_FIXEDPOINT_ITERS:
			raise AssertionError(
				"ConstShare structural synthesis exceeded fixed-point bound; "
				f"derived={len(derived)} candidates={len(candidates)}"
			)
		changed = False
		for cand in candidates:
			if cand.target_type_id in derived:
				continue
			# Visibility-aware proof world.
			visible = _visible_modules_for(visible_module_names_by_name, cand.def_module)
			proof_world = linked_world.visible_world(visible)
			env = TraitEnv(
				default_module=cand.def_module,
				default_package=package_id,
				module_packages=dict(module_packages),
				type_table=type_table,
			)
			field_paths = _qualify_fields(
				cand=cand,
				proof_world=proof_world,
				env=env,
				type_table=type_table,
				cs_key=cs_key,
				copy_key=copy_key,
				frozen_key=frozen_key,
			)
			if field_paths is None:
				continue
			# Qualified — synthesize and register.
			synth = _build_synthesis_artifact(
				cand=cand,
				field_paths=field_paths,
				type_table=type_table,
				cs_key=cs_key,
				fn_id_ordinals=fn_id_ordinals,
				module_packages=module_packages,
			)
			register_synthesized_const_share_impl(
				cand=cand,
				synth=synth,
				cs_key=cs_key,
				linked_world=linked_world,
				signatures_by_id=signatures_by_id,
				normalized_hirs_by_id=normalized_hirs_by_id,
				func_hirs_by_id=func_hirs_by_id,
				fn_ids_by_name=fn_ids_by_name,
				module_exports=module_exports,
				type_table=type_table,
				module_ids=module_ids,
				package_id=package_id,
				module_packages=module_packages,
				callable_registry=callable_registry,
				next_callable_id_box=next_callable_id_box,
			)
			derived.add(cand.target_type_id)
			synthesized += 1
			changed = True
	return synthesized


def _existing_const_share_targets(
	linked_world: LinkedWorld,
	cs_key: TraitKey,
) -> set[Tuple[str, str]]:
	"""Returns the set of (module, name) for types that already have a
	hand-written ConstShare impl in the linked world.  Synthesis must
	NOT produce a duplicate for these."""
	out: set[Tuple[str, str]] = set()
	for trait_world in linked_world.trait_worlds.values():
		for impl in trait_world.impls:
			if impl.trait != cs_key:
				continue
			tgt = impl.target
			if tgt.module is not None:
				out.add((tgt.module, tgt.name))
	return out


def _visible_modules_for(
	visible_module_names_by_name: dict | None,
	def_module: str,
) -> set[str]:
	if visible_module_names_by_name is None:
		return {def_module}
	got = visible_module_names_by_name.get(def_module)
	if got is None:
		return {def_module}
	if isinstance(got, set):
		return got
	return set(got)


def _qualify_fields(
	*,
	cand: _Candidate,
	proof_world: TraitWorld,
	env: TraitEnv,
	type_table,
	cs_key: TraitKey,
	copy_key: TraitKey,
	frozen_key: TraitKey,
) -> Optional[List[str]]:
	"""Returns a list of paths ('const_share' or 'copy_frozen') per
	field, or None if any field blocks (or has UNKNOWN status,
	conservative)."""
	field_paths: List[str] = []
	for field_ty_id in cand.field_type_ids:
		# Phase 1: reject typevars (generics deferred to phase 3).
		if type_table.has_typevar(field_ty_id):
			return None
		field_ty_key = type_key_from_typeid(type_table, field_ty_id)
		# Path 1: ConstShare direct.
		cs = prove_is(proof_world, env, {}, field_ty_key, cs_key)
		if cs.status is ProofStatus.PROVED:
			field_paths.append("const_share")
			continue
		# Path 2: Copy + Frozen.
		cp = prove_is(proof_world, env, {}, field_ty_key, copy_key)
		fz = prove_is(proof_world, env, {}, field_ty_key, frozen_key)
		if cp.status is ProofStatus.PROVED and fz.status is ProofStatus.PROVED:
			field_paths.append("copy_frozen")
			continue
		# Field doesn't qualify (REFUTED, UNKNOWN, or AMBIGUOUS).
		return None
	return field_paths


def _build_synthesis_artifact(
	*,
	cand: _Candidate,
	field_paths: List[str],
	type_table,
	cs_key: TraitKey,
	fn_id_ordinals: dict,
	module_packages: Mapping[str, str],
) -> _SynthResult:
	"""Build the synthesized HIR + FnSignature + ImplMeta for one
	candidate."""
	def_module = cand.def_module
	target_type_id = cand.target_type_id
	target_name = cand.struct_name

	# Symbol name follows the same shape user-written impl methods
	# use: `{target}::{trait}::{fn_name}`.
	symbol_name = f"{target_name}::{_CONST_SHARE_TRAIT_NAME}::const_share"
	ordinal = fn_id_ordinals.get((def_module, symbol_name), 0)
	fn_id_ordinals[(def_module, symbol_name)] = ordinal + 1
	fn_id = FunctionId(module=def_module, name=symbol_name, ordinal=ordinal)

	# Synthesized signature: `fn const_share(self: &Self) nothrow -> Self`
	self_ref_ty = type_table.ensure_ref(target_type_id)
	signature = FnSignature(
		name=function_symbol(fn_id),
		module=def_module,
		method_name="const_share",
		param_names=["self"],
		param_type_ids=[self_ref_ty],
		param_mutable=[False],
		return_type_id=target_type_id,
		declared_can_throw=False,
		is_method=True,
		self_mode="ref",
		is_pub=True,
		impl_target_type_id=target_type_id,
	)

	# Synthesized HIR body.
	hir = _build_const_share_hir(
		cand=cand,
		field_paths=field_paths,
		def_module=def_module,
	)

	# ImplMeta for serialization + impl_index.
	loc = cand.loc
	target_expr = parser_ast.TypeExpr(
		name=target_name,
		loc=loc,
		module_id=def_module,
	)
	trait_expr = parser_ast.TypeExpr(
		name=_CONST_SHARE_TRAIT_NAME,
		loc=loc,
		module_id=_CONST_SHARE_TRAIT_MODULE,
	)
	impl_meta = ImplMeta(
		impl_id=-1,  # set by caller / package emit
		def_module=def_module,
		target_type_id=target_type_id,
		trait_key=cs_key,
		trait_expr=trait_expr,
		trait_args=[],
		require_expr=None,
		target_expr=target_expr,
		impl_type_params=[],
		methods=[
			ImplMethodMeta(
				fn_id=fn_id,
				name="const_share",
				is_pub=True,
				fn_symbol=function_symbol(fn_id),
				loc=Span.from_loc(loc),
			),
		],
		loc=Span.from_loc(loc),
	)

	return _SynthResult(
		fn_id=fn_id,
		signature=signature,
		hir=hir,
		field_paths=field_paths,
		impl_meta=impl_meta,
	)


def _build_const_share_hir(
	*,
	cand: _Candidate,
	field_paths: List[str],
	def_module: str,
) -> H.HBlock:
	"""Construct the synthesized HIR body for `const_share`.

	Body shape:
	  `return Self(f1=self.f1.const_share(), f2=self.f2, ...)`

	For ConstShare-path fields: `HCall(HQualifiedMember(ConstShare,
	"const_share"), [HBorrow(HPlaceExpr(HField(self, fi)))])`.
	For Copy+Frozen-path fields: `HField(self, fi)` directly.

	Struct construction `Self(f=v)` is `HCall(fn=HVar("Self"),
	args=[], kwargs=[HKwArg("f", v)])` — same shape user-written
	struct constructors lower to.
	"""
	loc = cand.loc
	span = Span.from_loc(loc)

	# `self` HVar (the param).
	self_var = H.HVar(name="self", loc=span)

	kwargs: List[H.HKwArg] = []
	for fname, path in zip(cand.field_names, field_paths):
		# `self.f` — field access.  HField has no `loc` field.
		field_attr = H.HField(subject=self_var, name=fname)
		if path == "const_share":
			# `self.f.const_share()` — method call on the
			# borrowed field.  Drift's method resolution
			# auto-borrows the receiver and dispatches via the
			# trait's impl on the field type (e.g. ConstArc<U>).
			# Same shape user-written `x.method()` lowers to.
			value: H.HExpr = H.HMethodCall(
				receiver=field_attr,
				method_name="const_share",
				args=[],
				origin="const_share_synth",
				loc=span,
			)
		else:  # "copy_frozen"
			# Direct field read; existing borrowed-Copy auto-copy
			# machinery handles the deref + duplicate at lowering.
			value = field_attr
		kwargs.append(H.HKwArg(name=fname, value=value, loc=span))

	# Struct construction: HCall(HVar(<struct_name>), args=[], kwargs=...).
	# Same shape user-written `Holder(handle = ...)` lowers to.
	struct_name_var = H.HVar(name=cand.struct_name, loc=span)
	ctor = H.HCall(
		fn=struct_name_var,
		args=[],
		kwargs=kwargs,
		origin="const_share_synth_ctor",
		loc=span,
	)

	return_stmt = H.HReturn(value=ctor, loc=span)
	return H.HBlock(statements=[return_stmt])


# ── Single-helper API ────────────────────────────────────────────


def register_synthesized_const_share_impl(
	*,
	cand: _Candidate,
	synth: _SynthResult,
	cs_key: TraitKey,
	linked_world: LinkedWorld,
	signatures_by_id: dict,
	normalized_hirs_by_id: dict,
	func_hirs_by_id: dict,
	fn_ids_by_name: dict,
	module_exports: dict | None,
	type_table,
	module_ids: dict,
	package_id: Optional[str],
	module_packages: Mapping[str, str],
	callable_registry=None,
	next_callable_id_box: Optional[list] = None,
) -> None:
	"""SOLE entry point for multi-table synthesized-impl
	registration.  Mutations are NOT atomic in the database
	sense — each table is written sequentially in the order
	below, and a failure mid-sequence leaves earlier tables
	updated.  In practice this isn't a problem because the
	called helpers (`dict.setdefault`, `list.append`,
	`callable_registry.register_inherent_method`) only fail on
	truly broken inputs (e.g. None for a required key) which a
	caller-side bug catches before this point.  If a future
	change introduces a fallible path mid-sequence, prevalidate
	all inputs before any write.

	Tables updated (per
	`work/constshare-substrate/post-link-investigation-results.md`):
	  - signatures_by_id
	  - normalized_hirs_by_id
	  - func_hirs_by_id (defensive)
	  - fn_ids_by_name
	  - linked_world.global_world (4 indices)
	  - linked_world.trait_worlds[def_mid] (4 indices)
	  - module_exports[def_mid]["impls"]
	"""
	def_module = cand.def_module
	target_type_id = cand.target_type_id
	fn_id = synth.fn_id

	# 1. signatures.
	signatures_by_id[fn_id] = synth.signature
	# 2. normalized HIR (consumed by type-check + HIR→MIR).
	normalized_hirs_by_id[fn_id] = synth.hir
	# 3. func_hirs (defensive sync).
	func_hirs_by_id[fn_id] = synth.hir
	# 4. fn_ids_by_name.
	fn_ids_by_name.setdefault(function_symbol(fn_id), []).append(fn_id)

	# 5/6. Build ImplDef and register into linked_world.global_world AND
	#       linked_world.trait_worlds[def_module].
	target_key = TypeKey(
		package_id=(module_packages or {}).get(def_module, package_id),
		module=def_module,
		name=cand.struct_name,
		args=(),
	)
	target_head = target_key.head()
	impl_def = ImplDef(
		trait=cs_key,
		trait_args=(),
		target=target_key,
		target_head=target_head,
		methods=[],  # parser_ast.FunctionDef list — empty (we don't keep one)
		require=None,
		type_params=[],
		loc=synth.impl_meta.loc,
	)

	for world in (linked_world.global_world, linked_world.trait_worlds.get(def_module)):
		if world is None:
			continue
		impl_id = len(world.impls)
		world.impls.append(impl_def)
		world.impls_by_trait.setdefault(cs_key, []).append(impl_id)
		world.impls_by_target_head.setdefault(target_head, []).append(impl_id)
		world.impls_by_trait_target.setdefault((cs_key, target_head), []).append(impl_id)

	# 7. module_exports[def_module]["impls"].  Used by:
	#    - global_impl_index = GlobalImplIndex.from_module_exports(...)
	#    - global_trait_impl_index = GlobalTraitImplIndex.from_module_exports(...)
	#    - package emission's _encode_impl_headers_for_module
	if isinstance(module_exports, dict):
		mexp = module_exports.setdefault(def_module, {})
		impls_list = mexp.setdefault("impls", [])
		impls_list.append(synth.impl_meta)

	# 8. callable_registry — method dispatch consults this to find
	#    inherent + trait impl methods on a receiver.  The synthesizer
	#    runs AFTER `_register_signatures_in_callable_registry` (the
	#    initial bulk registration), so we must register synthesized
	#    methods directly via the same `register_inherent_method`
	#    entry point user-written impl methods use.
	if callable_registry is not None and next_callable_id_box is not None:
		from lang.driftc.method_registry import (
			CallableSignature,
			SelfMode,
			Visibility,
		)
		next_id = next_callable_id_box[0]
		mod_id = module_ids.setdefault(def_module, len(module_ids))
		callable_registry.register_inherent_method(
			callable_id=next_id,
			name=synth.signature.method_name or "const_share",
			module_id=mod_id,
			visibility=Visibility.public(),
			signature=CallableSignature(
				param_types=tuple(synth.signature.param_type_ids),
				result_type=synth.signature.return_type_id,
			),
			template_signature=None,
			template_type_params=(),
			template_impl_type_params=(),
			fn_id=synth.fn_id,
			impl_id=next_id,
			impl_target_type_id=cand.target_type_id,
			self_mode=SelfMode.SELF_BY_REF,
			is_generic=False,
		)
		next_callable_id_box[0] = next_id + 1
