# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""ConstShare structural synthesis — Phase 1 / Phase 2 / Phase 3.

Scope (per `work/constshare-substrate/post-link-mandatory-design.md`):
  - structs only (no variants),
  - same-module nested composition via fixed-point iteration,
  - package serialization (auto-derived impls round-trip through
    `module_exports[mid]["impls"]` / `impl_headers` / `hir_funcs`
    / `signatures` like any hand-written impl).

Phase 1: non-generic structs with concrete fields.

Phase 2: same-build cross-module composition (no separate code
path; falls out of Phase 1 with the visibility-aware proof world).

Phase 3: generic structs that auto-derive iff the user's
declared `require` clause already proves every field qualifies.
No implicit constraint strengthening.  This slice handles
direct-typevar fields only (`value: T`); concrete-generic-with-
typevar-args fields (e.g. `ConstArc<T>`) defer.

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
from lang.driftc.checker import FnSignature, TypeParam
from lang.driftc.core.types_core import TypeId, TypeKind, TypeParamId
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
class _VariantArm:
	"""Phase 4 — single arm in a variant candidate.  Field type-ids
	are the concrete, instantiated payload types (variants are non-
	generic in this slice)."""
	name: str
	field_names: List[str]
	field_type_ids: List[TypeId]


@dataclass
class _Candidate:
	target_type_id: TypeId
	def_module: str
	struct_name: str  # variant name when `is_variant=True`
	field_names: List[str]
	field_type_ids: List[TypeId]
	loc: object  # parser source loc for synthesized AST
	# Phase 3 generic-struct support.  Empty for non-generic structs
	# (Phase 1 / Phase 2 candidates).  Populated for generic structs
	# that auto-derive via an explicit user require clause:
	#   - `type_params`: declared type-param names from the struct
	#     schema (e.g. ["T"]).
	#   - `require_atoms`: list of (subject_name, trait_key) tuples
	#     extracted from the struct's require clause.  Synthesized
	#     impl carries the same require verbatim — no implicit
	#     constraint strengthening.
	#   - `field_typeparam_names`: per-field, the typevar name when
	#     the field type IS a bare typevar reference (e.g., field
	#     `value: T` → "T"); None for non-typevar fields.  Phase 3
	#     handles direct-typevar fields only; concrete-generic-with-
	#     typevar-args fields are deferred.
	type_params: List[str] = None  # type: ignore[assignment]
	require_atoms: List[tuple] = None  # type: ignore[assignment]
	field_typeparam_names: List[Optional[str]] = None  # type: ignore[assignment]
	# Phase 4 variant support.  `is_variant=True` flips struct-side
	# fields (`field_names`, `field_type_ids`) to empty/unused and
	# uses `variant_arms` instead.  Non-generic variants only.
	is_variant: bool = False
	variant_arms: List[_VariantArm] = None  # type: ignore[assignment]

	def __post_init__(self):
		if self.type_params is None:
			self.type_params = []
		if self.require_atoms is None:
			self.require_atoms = []
		if self.field_typeparam_names is None:
			self.field_typeparam_names = [None] * len(self.field_names)
		if self.variant_arms is None:
			self.variant_arms = []


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
		# `source_modules` is the set of module names parsed in
		# this build (`modules.keys()` from main()).  On a CONSUMER
		# build, that set includes stdlib modules (parsed even when
		# loaded as deps for cross-module type resolution); we must
		# ALSO filter `std.*` / `lang.*` so synth never runs for
		# stdlib types whose typecheck context the consumer can't
		# fully reconstruct (e.g. trait-in-scope for the synthesized
		# body's variant-ctor / method dispatch).  Stdlib provides
		# its own hand-written ConstShare impls for the types that
		# need them.
		if mid.startswith("std.") or mid.startswith("lang.") or mid == "lang.core":
			continue
		if source_modules is not None and mid not in source_modules:
			continue
		fields = list(getattr(schema, "fields", []) or [])
		if not fields:
			continue
		if (mid, schema.name) in user_impl_targets:
			continue
		schema_type_params = list(getattr(schema, "type_params", []) or [])
		decl_loc = getattr(schema, "decl_loc", None)
		field_names = [f.name for f in fields]
		if schema_type_params:
			# Phase 3: generic struct with type_params.  Auto-derives
			# only when the user-declared `require` clause already
			# proves every field qualifies.  No implicit
			# constraint strengthening.
			#
			# `StructSchema` doesn't carry the require clause (frozen
			# dataclass; clause lives in `world.requires_by_struct`
			# keyed by the struct's TypeKey).  Look it up there.
			struct_local_pkg = (module_packages or {}).get(mid, package_id)
			schema_key = TypeKey(
				package_id=struct_local_pkg,
				module=mid,
				name=schema.name,
				args=(),
			)
			require_expr = linked_world.global_world.requires_by_struct.get(schema_key)
			if require_expr is None:
				continue  # Phase 3: generic without require → don't derive
			# Extract conjunctive atoms (T is X, T is Y, ...).
			#
			# `_resolve_trait_subjects` (`world.py:1057`) rewrites
			# struct-require subjects from `parser_ast.TypeNameRef` to
			# `TypeParamId(owner=__struct_<mod>::<Name>, index=i)` after
			# the trait-world build pass.  Either shape can land here
			# depending on whether atoms were extracted before or after
			# resolution; handle both.  TypeParamId.index → struct
			# type_params name.
			from lang.driftc.traits.enforce import _extract_conjunctive_facts
			from lang.driftc.traits.world import trait_key_from_expr
			from lang.driftc.core.types_core import TypeParamId
			atoms_raw = _extract_conjunctive_facts(require_expr)
			require_atoms: List[tuple] = []  # (subject_name, TraitKey)
			for atom in atoms_raw:
				subj = atom.subject
				subj_name: Optional[str] = None
				if isinstance(subj, parser_ast.TypeNameRef):
					subj_name = subj.name
				elif isinstance(subj, TypeParamId):
					if 0 <= subj.index < len(schema_type_params):
						subj_name = schema_type_params[subj.index]
				if subj_name is None or subj_name not in schema_type_params:
					continue
				trait_key = trait_key_from_expr(
					atom.trait,
					default_module=mid,
					default_package=package_id,
					module_packages=module_packages,
				)
				require_atoms.append((subj_name, trait_key))
			# Phase 3 (this slice): only handle direct-typevar fields
			# (the `value: T` shape).  More complex shapes
			# (`ConstArc<T>`, etc.) defer to a follow-up.
			#
			# Field templates use `GenericTypeExpr`, which spells type
			# parameters via `param_index` (NOT via `name == "T"`).
			# Map back to the typeparam name through the struct's
			# `type_params` list.
			field_typeparam_names: List[Optional[str]] = []
			any_non_typevar = False
			for f in fields:
				te = getattr(f, "type_expr", None)
				pidx = getattr(te, "param_index", None) if te is not None else None
				te_args = list(getattr(te, "args", []) or []) if te is not None else []
				if (
					pidx is not None
					and 0 <= pidx < len(schema_type_params)
					and not te_args
				):
					field_typeparam_names.append(schema_type_params[pidx])
				else:
					field_typeparam_names.append(None)
					any_non_typevar = True
			if any_non_typevar:
				# Phase 3 simple shape only — defer concrete-generic
				# fields that mix typevars (e.g. ConstArc<T>) to a
				# follow-up.
				continue
			candidates.append(_Candidate(
				target_type_id=tid,
				def_module=mid,
				struct_name=schema.name,
				field_names=field_names,
				field_type_ids=[],  # not used for typevar-only fields
				loc=decl_loc,
				type_params=schema_type_params,
				require_atoms=require_atoms,
				field_typeparam_names=field_typeparam_names,
			))
		else:
			# Phase 1 / Phase 2: non-generic struct.  Concrete field
			# TypeIds via the struct instance.
			inst = type_table.get_struct_instance(tid)
			field_type_ids: List[TypeId] = []
			if inst is not None and getattr(inst, "field_types", None):
				field_type_ids = list(inst.field_types)
			else:
				# No instance yet (or missing field types) — skip.
				continue
			if len(field_type_ids) != len(fields):
				continue
			candidates.append(_Candidate(
				target_type_id=tid,
				def_module=mid,
				struct_name=schema.name,
				field_names=field_names,
				field_type_ids=field_type_ids,
				loc=decl_loc,
			))

	# Phase 4: non-generic variant discovery.  Mirrors the struct
	# loop above but iterates `type_table.variant_schemas` and pulls
	# concrete arm payload types via `get_variant_instance`.
	# Generic variants (type_params non-empty) are deferred to a
	# follow-up — Phase 4 covers the non-generic path only.
	variant_schemas = getattr(type_table, "variant_schemas", None)
	if isinstance(variant_schemas, dict):
		for tid, schema in variant_schemas.items():
			mid = getattr(schema, "module_id", None)
			if not isinstance(mid, str) or not mid:
				continue
			# Same source_modules + stdlib filter as the struct
			# loop above (see comment there for rationale).  Phase 4
			# specifically benefits from the stdlib filter: many
			# stdlib variants (e.g. `IteratorOpId`) have zero-payload
			# arms that trivially qualify, but stdlib doesn't need
			# auto-derived ConstShare for them — and the synthesized
			# match body fails to typecheck on a consumer that
			# loaded stdlib as a dep with an impoverished trait
			# scope.
			if mid.startswith("std.") or mid.startswith("lang.") or mid == "lang.core":
				continue
			if source_modules is not None and mid not in source_modules:
				continue
			if (mid, schema.name) in user_impl_targets:
				continue
			if list(getattr(schema, "type_params", []) or []):
				# Phase 4 (this slice): non-generic variants only.
				continue
			arms_schema = list(getattr(schema, "arms", []) or [])
			if not arms_schema:
				continue
			# Tombstone constructors are internal-only — they're
			# rejected by the typechecker as match-arm patterns
			# (`E-MATCH-TOMBSTONE`), so the synthesized body must
			# not enumerate them.  Skip when reconstructing arms;
			# at runtime, a tombstone-tagged value never reaches
			# user code (the runtime drops/swaps it before any
			# `const_share()` call could observe it).
			tombstone_name = getattr(schema, "tombstone_ctor", None)
			vinst = type_table.get_variant_instance(tid)
			if vinst is None:
				continue
			vinst_arms = list(getattr(vinst, "arms", []) or [])
			if len(vinst_arms) != len(arms_schema):
				continue
			variant_arms: List[_VariantArm] = []
			ok = True
			for arm_schema, arm_inst in zip(arms_schema, vinst_arms):
				if tombstone_name is not None and arm_schema.name == tombstone_name:
					continue
				field_names_arm = [f.name for f in (getattr(arm_schema, "fields", []) or [])]
				field_type_ids_arm = list(getattr(arm_inst, "field_types", []) or [])
				if len(field_names_arm) != len(field_type_ids_arm):
					ok = False
					break
				variant_arms.append(_VariantArm(
					name=arm_schema.name,
					field_names=field_names_arm,
					field_type_ids=field_type_ids_arm,
				))
			if not ok or not variant_arms:
				continue
			candidates.append(_Candidate(
				target_type_id=tid,
				def_module=mid,
				struct_name=schema.name,
				field_names=[],
				field_type_ids=[],
				loc=getattr(schema, "decl_loc", None),
				is_variant=True,
				variant_arms=variant_arms,
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
			# Phase 4: variants take a per-arm qualifier; structs take
			# the flat per-field qualifier.  Both return None on the
			# first non-qualifying field.
			variant_arm_paths: Optional[List[List[str]]] = None
			field_paths: Optional[List[str]] = None
			if cand.is_variant:
				variant_arm_paths = _qualify_variant_arms(
					cand=cand,
					proof_world=proof_world,
					env=env,
					type_table=type_table,
					cs_key=cs_key,
					copy_key=copy_key,
					frozen_key=frozen_key,
				)
				if variant_arm_paths is None:
					continue
			else:
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
				variant_arm_paths=variant_arm_paths,
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


def _qualify_one_field(
	field_ty_id: TypeId,
	*,
	proof_world: TraitWorld,
	env: TraitEnv,
	type_table,
	cs_key: TraitKey,
	copy_key: TraitKey,
	frozen_key: TraitKey,
) -> Optional[str]:
	"""Single-field qualification helper, shared by struct and
	variant paths.  Returns 'const_share' / 'copy_frozen' / None
	(blocked).  Typevar fields conservatively reject (Phase 1
	contract; generic struct path handles typevars separately
	via `_qualify_fields_generic`)."""
	if type_table.has_typevar(field_ty_id):
		return None
	field_ty_key = type_key_from_typeid(type_table, field_ty_id)
	cs = prove_is(proof_world, env, {}, field_ty_key, cs_key)
	if cs.status is ProofStatus.PROVED:
		return "const_share"
	cp = prove_is(proof_world, env, {}, field_ty_key, copy_key)
	fz = prove_is(proof_world, env, {}, field_ty_key, frozen_key)
	if cp.status is ProofStatus.PROVED and fz.status is ProofStatus.PROVED:
		return "copy_frozen"
	return None


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
	if cand.type_params:
		return _qualify_fields_generic(
			cand=cand,
			cs_key=cs_key,
			copy_key=copy_key,
			frozen_key=frozen_key,
		)
	field_paths: List[str] = []
	for field_ty_id in cand.field_type_ids:
		path = _qualify_one_field(
			field_ty_id,
			proof_world=proof_world,
			env=env,
			type_table=type_table,
			cs_key=cs_key,
			copy_key=copy_key,
			frozen_key=frozen_key,
		)
		if path is None:
			return None
		field_paths.append(path)
	return field_paths


def _qualify_variant_arms(
	*,
	cand: _Candidate,
	proof_world: TraitWorld,
	env: TraitEnv,
	type_table,
	cs_key: TraitKey,
	copy_key: TraitKey,
	frozen_key: TraitKey,
) -> Optional[List[List[str]]]:
	"""Phase 4 variant qualifier: returns per-arm per-field paths,
	or None if ANY arm carries a non-qualifying field.  Single
	non-qualifying field anywhere in the variant blocks the whole
	derivation (matches the same all-or-nothing contract structs
	use)."""
	per_arm_paths: List[List[str]] = []
	for arm in cand.variant_arms:
		arm_paths: List[str] = []
		for field_ty_id in arm.field_type_ids:
			path = _qualify_one_field(
				field_ty_id,
				proof_world=proof_world,
				env=env,
				type_table=type_table,
				cs_key=cs_key,
				copy_key=copy_key,
				frozen_key=frozen_key,
			)
			if path is None:
				return None
			arm_paths.append(path)
		per_arm_paths.append(arm_paths)
	return per_arm_paths


def _qualify_fields_generic(
	*,
	cand: _Candidate,
	cs_key: TraitKey,
	copy_key: TraitKey,
	frozen_key: TraitKey,
) -> Optional[List[str]]:
	"""Phase 3 generic-struct qualification.

	For each field, look up the typevar-name → require-atom set
	from the struct's declared `require` clause.  A field
	qualifies if its typevar carries `T is ConstShare` (path 1)
	or BOTH `T is Copy` AND `T is Frozen` (path 2) in the
	require clause.  No implicit constraint strengthening — if
	the require clause provides only Frozen or only Copy, the
	field does NOT qualify.

	Trait identity uses FULL `TraitKey` (package_id + module +
	name), not the `(module, name)` pair.  Two distinct packages
	may both export a trait whose `module.name` collides; treating
	them as the same atom would let a require clause naming the
	wrong package's trait spoof qualification.  Mirrors the
	tightened identity discipline used elsewhere in trait
	resolution.
	"""
	# Build per-typeparam TraitKey set keyed by typeparam name.
	tp_traits: dict[str, set[TraitKey]] = {tp: set() for tp in cand.type_params}
	for subj_name, trait_key in cand.require_atoms:
		if subj_name in tp_traits:
			tp_traits[subj_name].add(trait_key)
	field_paths: List[str] = []
	for tp_name in cand.field_typeparam_names:
		if tp_name is None:
			# Phase 3 (this slice) handles direct-typevar fields
			# only.  Concrete-generic-with-typevar-args fields
			# (e.g. `ConstArc<T>`) are out of scope for this slice.
			return None
		traits = tp_traits.get(tp_name, set())
		if cs_key in traits:
			field_paths.append("const_share")
			continue
		if copy_key in traits and frozen_key in traits:
			field_paths.append("copy_frozen")
			continue
		# Insufficient require — no implicit strengthening.
		return None
	return field_paths


def _build_require_expr(
	require_atoms: List[tuple],
	loc: object,
) -> Optional[parser_ast.TraitExpr]:
	"""Reconstruct a `TraitExpr` from the candidate's flattened
	conjunctive atoms.  Returns None if the atom list is empty
	(non-generic candidates pass an empty list).

	Each atom `(subject_name, TraitKey)` becomes a `TraitIs(
	TypeNameRef(subject_name), TypeExpr(name=trait, module_id=
	trait_module))`.  Multiple atoms chain left-associatively into
	a `TraitAnd` tree — the same shape `parser/parser.py:1346`
	produces for source-level `T is A, T is B` clauses.

	No implicit constraint strengthening: this is a verbatim copy
	of the user-declared require atoms (Phase 3 contract).
	"""
	if not require_atoms:
		return None
	atoms: List[parser_ast.TraitExpr] = []
	for subj_name, trait_key in require_atoms:
		subject_ref = parser_ast.TypeNameRef(loc=loc, name=subj_name)
		trait_te = parser_ast.TypeExpr(
			name=trait_key.name,
			loc=loc,
			module_id=trait_key.module,
		)
		atoms.append(parser_ast.TraitIs(loc=loc, subject=subject_ref, trait=trait_te))
	combined = atoms[0]
	for atom in atoms[1:]:
		combined = parser_ast.TraitAnd(loc=loc, left=combined, right=atom)
	return combined


def _build_synthesis_artifact(
	*,
	cand: _Candidate,
	field_paths: Optional[List[str]],
	variant_arm_paths: Optional[List[List[str]]] = None,
	type_table,
	cs_key: TraitKey,
	fn_id_ordinals: dict,
	module_packages: Mapping[str, str],
) -> _SynthResult:
	"""Build the synthesized HIR + FnSignature + ImplMeta for one
	candidate.

	Either `field_paths` (struct path) or `variant_arm_paths`
	(Phase 4 variant path) is non-None — selected by
	`cand.is_variant`."""
	def_module = cand.def_module
	target_type_id = cand.target_type_id
	target_name = cand.struct_name

	# Symbol name follows the same shape user-written impl methods
	# use: `{target}::{trait}::{fn_name}`.
	symbol_name = f"{target_name}::{_CONST_SHARE_TRAIT_NAME}::const_share"
	ordinal = fn_id_ordinals.get((def_module, symbol_name), 0)
	fn_id_ordinals[(def_module, symbol_name)] = ordinal + 1
	fn_id = FunctionId(module=def_module, name=symbol_name, ordinal=ordinal)

	# Synthesized signature: `fn const_share(self: &Self) nothrow -> Self`.
	#
	# For NON-GENERIC structs (Phase 1 / Phase 2): `self_type` is
	# the concrete base TypeId, and `param_type_ids=[Ref<Box>]`.
	#
	# For GENERIC structs (Phase 3): `self_type` is a STRUCT
	# TEMPLATE `Box<T_typevar>` so the call-resolver's receiver
	# compatibility check can match an actual `Box<X>` receiver
	# under a typevar binding (`_match_impl_type_args` /
	# `_bind_impl_type_params`).  Mirrors what
	# `lang/driftc/type_resolver.py:208-211` does for user-written
	# `implement<T> Box<T>`: `impl_target_type_args=[typevar_T_id]`,
	# and the param type expr resolves Self→Box<T> with T as a
	# fresh impl-scoped typevar.
	#
	# `FnSignature.impl_type_params` carries `TypeParam` objects
	# whose `TypeParamId(owner=fn_id, index=i)` is the OWNER for
	# the typevar TypeIds we materialize here, so substitution at
	# call-resolution time keys correctly.
	impl_type_param_objs: List[TypeParam] = [
		TypeParam(id=TypeParamId(owner=fn_id, index=i), name=tp_name)
		for i, tp_name in enumerate(cand.type_params)
	]
	if cand.type_params:
		impl_typevar_tids: List[TypeId] = [
			type_table.ensure_typevar(tp.id, name=tp.name)
			for tp in impl_type_param_objs
		]
		self_type = type_table.ensure_struct_template(target_type_id, impl_typevar_tids)
		impl_target_type_args: Optional[List[TypeId]] = list(impl_typevar_tids)
	else:
		self_type = target_type_id
		impl_target_type_args = None
	self_ref_ty = type_table.ensure_ref(self_type)

	# Phase 3: producer-side `compile_stubbed_funcs` /
	# `TemplateHIR-v1` (`driftc.py:3298`) requires the legacy raw
	# `param_types` / `return_type` TypeExpr fields on any signature
	# carrying `type_params` or `impl_type_params`.  User-source
	# methods get them from `type_resolver.py:230-231`; for synth
	# we reconstruct minimal parser_ast TypeExprs that resolve back
	# to the same TypeIds when the consumer's TypeChecker re-runs
	# `resolve_opaque_type` over them.  Non-generic signatures
	# don't need raw TypeExprs (TypeId is authoritative there).
	raw_params: Optional[List[parser_ast.TypeExpr]] = None
	raw_ret: Optional[parser_ast.TypeExpr] = None
	if cand.type_params:
		ploc = cand.loc
		# `Box<T_n, ...>` — typevar arg names come from impl type params.
		target_args_te: List[parser_ast.TypeExpr] = [
			parser_ast.TypeExpr(name=tp_name, loc=ploc)
			for tp_name in cand.type_params
		]
		target_te = parser_ast.TypeExpr(
			name=target_name,
			args=target_args_te,
			loc=ploc,
			module_id=def_module,
		)
		# `&Box<T>` for self.
		self_te = parser_ast.TypeExpr(
			name="&",
			args=[target_te],
			loc=ploc,
		)
		raw_params = [self_te]
		# Return is a fresh `Box<T, ...>` TypeExpr (don't share the
		# self's nested node — keeps the AST tree-shaped, not DAG-
		# shaped, matching parser output).
		target_args_te_ret: List[parser_ast.TypeExpr] = [
			parser_ast.TypeExpr(name=tp_name, loc=ploc)
			for tp_name in cand.type_params
		]
		raw_ret = parser_ast.TypeExpr(
			name=target_name,
			args=target_args_te_ret,
			loc=ploc,
			module_id=def_module,
		)

	signature = FnSignature(
		name=function_symbol(fn_id),
		module=def_module,
		method_name="const_share",
		param_names=["self"],
		param_type_ids=[self_ref_ty],
		param_mutable=[False],
		return_type_id=self_type,
		declared_can_throw=False,
		is_method=True,
		self_mode="ref",
		is_pub=True,
		impl_target_type_id=target_type_id,
		impl_target_type_args=impl_target_type_args,
		impl_type_params=impl_type_param_objs,
		# Legacy/raw parser_ast TypeExpr fields — required by
		# producer-side TemplateHIR-v1 for generic signatures.
		param_types=raw_params,
		return_type=raw_ret,
	)

	# Synthesized HIR body.  Variant candidates produce a `match
	# self { ... }` body; struct candidates produce a single
	# constructor call.
	if cand.is_variant:
		hir = _build_const_share_hir_variant(
			cand=cand,
			variant_arm_paths=variant_arm_paths or [],
			def_module=def_module,
		)
	else:
		hir = _build_const_share_hir(
			cand=cand,
			field_paths=field_paths or [],
			def_module=def_module,
		)

	# ImplMeta for serialization + impl_index.
	loc = cand.loc
	# For generic structs: target expr is `Box<T, U, ...>` —
	# bare type names as args (resolved by downstream phases).
	target_args: List[parser_ast.TypeExpr] = []
	for tp in cand.type_params:
		target_args.append(parser_ast.TypeExpr(name=tp, loc=loc))
	target_expr = parser_ast.TypeExpr(
		name=target_name,
		args=target_args,
		loc=loc,
		module_id=def_module,
	)
	trait_expr = parser_ast.TypeExpr(
		name=_CONST_SHARE_TRAIT_NAME,
		loc=loc,
		module_id=_CONST_SHARE_TRAIT_MODULE,
	)
	# Reconstruct the require TraitExpr from atoms (verbatim copy
	# of the user-declared require clause; Phase 3 forbids
	# implicit strengthening).
	require_expr = _build_require_expr(cand.require_atoms, loc)
	# For generic structs, ImplMeta.target_type_id is the TEMPLATE
	# (`Box<T_typevar>`), NOT the BASE (`Box`).  This mirrors what
	# `parser/__init__.py:4607-4612` does for user-source impls
	# (`resolve_opaque_type(Box<T>, type_params={T: typevar_id})`)
	# and is required by `validate_trait_impls` (`type_checker.py:1454`)
	# which binds `Self -> impl.target_type_id` when expanding the
	# trait method's expected param/return type.  If we passed the
	# base, the validator computes `expects Ref<Box>` while the
	# synthesized signature has `Ref<Box<T>>`, and rejects with
	# `E_TRAIT_METHOD_PARAM_MISMATCH`.
	impl_target_id_for_meta = self_type
	impl_meta = ImplMeta(
		impl_id=-1,  # set by caller / package emit
		def_module=def_module,
		target_type_id=impl_target_id_for_meta,
		trait_key=cs_key,
		trait_expr=trait_expr,
		trait_args=[],
		require_expr=require_expr,
		target_expr=target_expr,
		impl_type_params=list(cand.type_params),
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
	#
	# Phase 3: for generic structs (`type_params` non-empty) the
	# constructor inference can't bind T from kwargs alone when the
	# field is a typevar (`value: T`) — the field type is itself
	# `T`, so kwarg-driven inference loops.  Emit explicit
	# `type_args=[TypeExpr(name=Tk), ...]` so the typechecker
	# pins the instantiation up front, mirroring user source
	# `Box<type T>(value = ...)`.  For non-generic structs the
	# field types pin the inference and `type_args` stays None.
	struct_name_var = H.HVar(name=cand.struct_name, loc=span)
	ctor_type_args: Optional[List[parser_ast.TypeExpr]] = None
	if cand.type_params:
		ctor_type_args = [
			parser_ast.TypeExpr(name=tp, loc=loc)
			for tp in cand.type_params
		]
	ctor = H.HCall(
		fn=struct_name_var,
		args=[],
		kwargs=kwargs,
		type_args=ctor_type_args,
		origin="const_share_synth_ctor",
		loc=span,
	)

	return_stmt = H.HReturn(value=ctor, loc=span)
	return H.HBlock(statements=[return_stmt])


def _build_const_share_hir_variant(
	*,
	cand: _Candidate,
	variant_arm_paths: List[List[str]],
	def_module: str,
) -> H.HBlock:
	"""Phase 4 — synthesized HIR body for a variant `const_share`.

	Body shape (one arm per variant case):

	    return match self {
	        V::A => { V::A() },
	        V::B(b0) => { V::B(b0.const_share()) },
	        V::C(c0, c1) => { V::C(c0, c1) },        // copy_frozen path
	        ...
	    };

	Per-arm details:
	  - `arm.ctor` is the bare ctor name (e.g. "B", not "V::B");
	  - `arm.ctor_base` carries the variant TypeExpr so the
	    typechecker knows which variant the ctor name belongs to,
	    matching what user-source `match` arms produce in stage1
	    (`ast_to_hir.py:1184`);
	  - `pattern_arg_form="positional"` and
	    `binder_field_indices=range(N)` for payload arms (zero-
	    payload arms use "paren" with empty binders);
	  - `block` stays empty — the synthesized arm produces its
	    value via `result`, the same shape stage1 normalizer
	    expects for value-form match arms.

	Reconstruction at the value site uses
	`HCall(HQualifiedMember(VariantTypeExpr, ctor_name), args=[
	per-binder transformation])` — the same form a user-written
	`V::B(...)` constructor lowers to.
	"""
	loc = cand.loc
	span = Span.from_loc(loc)

	# Variant base type-expr — referenced by `ctor_base` on each
	# arm AND by the result-side ctor `HQualifiedMember`.  Module-id
	# is set so the typechecker can resolve the variant's owning
	# module deterministically (no name shadowing across modules).
	variant_te = parser_ast.TypeExpr(
		name=cand.struct_name,
		loc=loc,
		module_id=def_module,
	)

	self_var = H.HVar(name="self", loc=span)
	arms: List[H.HMatchArm] = []
	# Per-arm binder localization counter -- mirrors
	# `lang/driftc/stage1/ast_to_hir.py::lower_match_expr`'s
	# `self._match_binder_counter` so each binder gets a globally-
	# unique internal name within this synthesized fn.  Required:
	# user-source match arms with the same binder name in two arms
	# (e.g. `MissingKey(key)` + `BadType(key, expected)`) get this
	# rename in `ast_to_hir`; const_share synth bypasses that pass
	# and must replicate it, otherwise the typechecker assigns
	# different binding_ids to the two `key` binders and MIR
	# lowering ends up with an asymmetry between the pattern-binding
	# store (uses raw `bname`) and the result-expression read (uses
	# `_canonical_local`, which suffixes to `key__b<N>` on the
	# second arm).  SSA multi-block rename then fails with "load
	# before store for local 'key__b<N>'" -- the sgw-stub
	# 2026-05-17 `variant_repro.drift` regression.  Counter is
	# local because the names just need to be unique within this
	# synthesized fn; no need to share across compilations.
	#
	# The `__match_binder_*` shape is load-bearing: `_canonical_local`
	# at `lang/driftc/stage2/hir_to_mir.py:1123-1125` recognizes this
	# prefix and skips the binding-id-based suffix path entirely --
	# the same special-case that keeps user-source binders safe.
	# Using any other prefix would re-hit the suffix bug.
	binder_counter = 0
	for arm_data, arm_paths in zip(cand.variant_arms, variant_arm_paths):
		# Binder names: localize per-arm.  Each binder gets a fresh
		# globally-unique internal name; the original schema field
		# name is retained as the suffix for debug readability
		# (so `__match_binder_3_key` is easy to trace back to the
		# `BadType(key, ...)` arm in any IR dump).
		orig_binders: List[str] = list(arm_data.field_names)
		internal_binders: List[str] = []
		for orig in orig_binders:
			internal_binders.append(f"__match_binder_{binder_counter}_{orig}")
			binder_counter += 1
		# Per-binder result-side value: `binder.const_share()` for
		# ConstShare-path fields, plain `HVar(binder)` for
		# Copy+Frozen-path fields (HIR→MIR's borrowed-Copy auto-
		# copy machinery handles the duplication at lowering time,
		# the same way `self.f` reads through a borrowed match
		# arm in user source).  HVar references use the localized
		# internal name to match the binder declaration.
		ctor_args: List[H.HExpr] = []
		for binder_name, path in zip(internal_binders, arm_paths):
			binder_var: H.HExpr = H.HVar(name=binder_name, loc=span)
			if path == "const_share":
				ctor_args.append(H.HMethodCall(
					receiver=binder_var,
					method_name="const_share",
					args=[],
					origin="const_share_synth_variant_arm",
					loc=span,
				))
			else:  # "copy_frozen"
				ctor_args.append(binder_var)
		# Reconstruct: `V::Ctor(arg0, arg1, ...)` — qualified member
		# call mirrors user-source variant construction.
		ctor_hir = H.HCall(
			fn=H.HQualifiedMember(
				base_type_expr=variant_te,
				member=arm_data.name,
				loc=span,
			),
			args=ctor_args,
			kwargs=[],
			origin="const_share_synth_variant_ctor",
			loc=span,
		)
		# Pattern arg form: zero-payload arms use "paren" with empty
		# binders to match `V::A()` ctor shape; payload arms use
		# positional matching.
		if not internal_binders:
			pattern_arg_form = "paren"
			binder_field_indices: List[int] = []
		else:
			pattern_arg_form = "positional"
			binder_field_indices = list(range(len(internal_binders)))
		arms.append(H.HMatchArm(
			ctor=arm_data.name,
			ctor_base=variant_te,
			binders=internal_binders,
			binder_is_mutable=None,
			binder_fields=None,
			pattern_arg_form=pattern_arg_form,
			binder_field_indices=binder_field_indices,
			block=H.HBlock(statements=[]),
			result=ctor_hir,
			loc=span,
		))

	match_expr = H.HMatchExpr(
		scrutinee=self_var,
		arms=arms,
		loc=span,
	)
	return_stmt = H.HReturn(value=match_expr, loc=span)
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
	#
	# For generic structs the impl target needs typevar placeholders in
	# `args` so the trait solver's `_bind_impl_type_params` can match
	# `Box<T>` (template) against `Box<ConcreteX>` (actual subject) and
	# bind `T → ConcreteX`.  Each placeholder is a bare TypeKey with
	# just a `name`; module / package_id stay None — `_bind_impl_type_params`
	# treats `template.name in params and not template.args` as the
	# typevar slot, ignoring module/package on the placeholder side.
	target_args: Tuple[TypeKey, ...] = tuple(
		TypeKey(package_id=None, module=None, name=tp, args=())
		for tp in cand.type_params
	)
	target_key = TypeKey(
		package_id=(module_packages or {}).get(def_module, package_id),
		module=def_module,
		name=cand.struct_name,
		args=target_args,
	)
	target_head = target_key.head()
	# Phase 3: generic structs carry both `type_params` (the impl's
	# universally-quantified type-param names) and `require` (a
	# verbatim copy of the user-declared require clause).  The
	# trait solver consults `impl.require` when proving an
	# instantiation: the require atoms become assumed-true premises
	# in the proof environment for the bound type-params.
	#
	# `synth.impl_meta.require_expr` already holds the reconstructed
	# `TraitExpr` (built by `_build_require_expr` from
	# `cand.require_atoms`).  Reusing it keeps a single source of
	# truth between ImplMeta (for serialization / impl_index) and
	# ImplDef (for the trait world).
	impl_def = ImplDef(
		trait=cs_key,
		trait_args=(),
		target=target_key,
		target_head=target_head,
		methods=[],  # parser_ast.FunctionDef list — empty (we don't keep one)
		require=synth.impl_meta.require_expr,
		type_params=list(cand.type_params),
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
		# Phase 3: register the impl-method's require clause so the
		# type-checker / call-resolver can dispatch typevar-method
		# calls inside the synthesized body (`self.value.const_share()`
		# resolves only when `T is ConstShare` is a known fact via
		# `requires_by_fn[fn_id]`).  Mirrors what
		# `world.py:646` does for source-declared impl-method
		# requires.  Generic structs only — non-generic synthesis
		# carries no require atoms and skips this branch.
		if synth.impl_meta.require_expr is not None:
			world.requires_by_fn[fn_id] = synth.impl_meta.require_expr

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
			CallableTemplateSignature,
			SelfMode,
			Visibility,
		)
		next_id = next_callable_id_box[0]
		mod_id = module_ids.setdefault(def_module, len(module_ids))
		# Phase 3: generic structs get a `template_signature` and
		# `template_impl_type_params` so the call resolver knows
		# this method is generic and how to bind the impl's type-
		# params from the receiver's actual type-args.  For non-
		# generic structs (Phase 1 / Phase 2), the bulk-registration
		# path leaves these empty and we follow suit.
		is_generic_method = bool(cand.type_params)
		template_sig: CallableTemplateSignature | None = None
		if is_generic_method:
			template_sig = CallableTemplateSignature(
				param_types=tuple(synth.signature.param_type_ids),
				result_type=synth.signature.return_type_id,
			)
		callable_registry.register_inherent_method(
			callable_id=next_id,
			name=synth.signature.method_name or "const_share",
			module_id=mod_id,
			visibility=Visibility.public(),
			signature=CallableSignature(
				param_types=tuple(synth.signature.param_type_ids),
				result_type=synth.signature.return_type_id,
			),
			template_signature=template_sig,
			template_type_params=(),
			template_impl_type_params=tuple(cand.type_params),
			fn_id=synth.fn_id,
			impl_id=next_id,
			impl_target_type_id=cand.target_type_id,
			self_mode=SelfMode.SELF_BY_REF,
			is_generic=is_generic_method,
		)
		next_callable_id_box[0] = next_id + 1


def resolve_const_share_trait_key(linked_world) -> Optional[TraitKey]:
	"""The canonical `std.core.shareable.ConstShare` TraitKey in this linked world,
	or None if the trait is not in scope (consumer never imported shareable, so no
	ConstShare impls exist to hydrate)."""
	if linked_world is None:
		return None
	gw = getattr(linked_world, "global_world", None)
	if gw is None:
		return None
	return _resolve_trait_key(gw, module=_CONST_SHARE_TRAIT_MODULE, name=_CONST_SHARE_TRAIT_NAME)


def hydrate_const_share_registry(
	*,
	module_exports,
	signatures_by_id,
	callable_registry,
	module_ids,
	next_callable_id_box,
	const_share_trait_key,
) -> int:
	"""Register synthesized ConstShare `const_share` methods that already exist in
	`module_exports` (semantic synthesis ran) but are MISSING from
	`callable_registry`.

	`const_share_trait_key` is the RESOLVED canonical `std.core.shareable.ConstShare`
	TraitKey (see `resolve_const_share_trait_key`); impls are matched by full
	TraitKey equality against it, so a same-named trait from another module/package
	is never hydrated.

	This is the registry-only counterpart of `synthesize_const_share_phase1`: it
	does NOT re-derive impls or re-append semantic state (the synthesizer excludes
	already-covered targets, so re-running it would register nothing).  It exists
	so a PREPARED TypeTable reused with a FRESH pipeline-owned registry still gets
	its synthesized methods registered — the method-resolution dispatch consults
	the registry, so without this the synthesized `const_share` would resolve to
	Unknown.  Returns the number of methods hydrated.
	"""
	if callable_registry is None or not module_exports:
		return 0
	if const_share_trait_key is None:
		# ConstShare trait not in scope → no synthesized impls exist to hydrate.
		return 0
	from lang.driftc.method_registry import (
		CallableSignature,
		CallableTemplateSignature,
		SelfMode,
		Visibility,
	)
	hydrated = 0
	for mod_name, exp in module_exports.items():
		if not isinstance(exp, dict):
			continue
		for impl in (exp.get("impls") or []):
			tk = getattr(impl, "trait_key", None)
			# Full canonical identity: match the resolved ConstShare TraitKey by
			# equality (package + module + name) — a same-named trait from another
			# module/package must NOT be hydrated as ConstShare.
			if tk is None or tk != const_share_trait_key:
				continue
			impl_tps = list(getattr(impl, "impl_type_params", []) or [])
			target_tid = getattr(impl, "target_type_id", None)
			def_module = getattr(impl, "def_module", mod_name)
			for m in (getattr(impl, "methods", []) or []):
				if getattr(m, "name", None) != "const_share":
					continue
				fn_id = getattr(m, "fn_id", None)
				if fn_id is None:
					continue
				if callable_registry.get_by_fn_id(fn_id) is not None:
					continue  # already registered (hand-written or already hydrated)
				sig = signatures_by_id.get(fn_id)
				if sig is None or sig.param_type_ids is None or sig.return_type_id is None:
					# A synthesized ConstShare method with no usable signature is an
					# internal invariant violation (synthesis registers the signature
					# alongside the impl) — never silently mark the registry hydrated
					# while skipping it.
					raise AssertionError(
						f"ConstShare registry hydration invariant: synthesized const_share method {fn_id} "
						f"has no usable signature in signatures_by_id (sig={sig!r})"
					)
				next_id = next_callable_id_box[0]
				mod_id = module_ids.setdefault(def_module, len(module_ids))
				is_generic = bool(impl_tps)
				tmpl = (
					CallableTemplateSignature(
						param_types=tuple(sig.param_type_ids),
						result_type=sig.return_type_id,
					)
					if is_generic
					else None
				)
				callable_registry.register_inherent_method(
					callable_id=next_id,
					name="const_share",
					module_id=mod_id,
					visibility=Visibility.public(),
					signature=CallableSignature(
						param_types=tuple(sig.param_type_ids),
						result_type=sig.return_type_id,
					),
					template_signature=tmpl,
					template_type_params=(),
					template_impl_type_params=tuple(impl_tps),
					fn_id=fn_id,
					impl_id=next_id,
					impl_target_type_id=target_tid,
					self_mode=SelfMode.SELF_BY_REF,
					is_generic=is_generic,
				)
				next_callable_id_box[0] = next_id + 1
				hydrated += 1
	return hydrated
