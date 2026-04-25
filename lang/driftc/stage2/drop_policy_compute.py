# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Standalone computation of the canonical Phase-1 `DropPolicy` for a type.

Mirrors `HIRToMIR._drop_policy` exactly — same five axes, same shortcut
rules, same DV transitive walk.  Lives here so passes that need the
canonical policy (Phase 3C drop-flag insertion; future 3B consumer
swaps; etc.) can call it without instantiating a full `HIRToMIR`
instance, which has heavy initialisation side effects (emits a ConstString
into its builder, etc.).

The two implementations MUST stay in sync.  When `HIRToMIR._drop_policy`
changes, this function changes too.  A contract test
(`test_drop_policy_standalone_matches_hir_to_mir.py`) pins this for every
canonical type in the `DropPolicy` contract test suite.
"""

from __future__ import annotations

from typing import Set

from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable

from .hir_to_mir import DropPolicy


def compute_drop_policy(type_table: TypeTable, ty: TypeId) -> DropPolicy:
	"""Canonical Phase-1 DropPolicy for `ty` against `type_table`.

	Identical semantics to `HIRToMIR._drop_policy`; see the docstring
	on the `DropPolicy` dataclass for the contract.
	"""
	unknown_ty = type_table.ensure_unknown()
	if ty == unknown_ty:
		return DropPolicy(
			needs_drop=False,
			is_bitcopy=False,
			is_cheap_copy=False,
			is_destructible=False,
			has_structural_drop=False,
		)
	try:
		is_bitcopy = bool(type_table.is_bitcopy(ty))
	except Exception:
		is_bitcopy = False
	try:
		copy_status = type_table.copy_status(ty)
	except Exception:
		copy_status = None
	try:
		raw_has_drop = bool(type_table.has_drop(ty))
	except Exception:
		raw_has_drop = False
	try:
		raw_is_destructible = bool(type_table.is_destructible(ty))
	except Exception:
		raw_is_destructible = False
	contains_dv = _contains_dv_transitive(type_table, ty, set())
	# needs_drop: driven by destruction reality.  A type that has
	# `has_drop=True` (refcount release, user destructor, structural
	# drop) MUST be dropped at scope exit, regardless of whether it
	# also has `copy_status=True`.  The pre-fix shortcut
	# (`elif copy_status is True: needs_drop=False`) treated `Copy`
	# as "no drop needed" — wrong for refcounted scalars like
	# `String` (`copy_status=True` AND `has_drop=True`) and for
	# variants/structs with `String`/`Array<…>` fields.  Pinned by
	# `lang/tests/driver/test_drop_policy_copy_short_circuit_bug.py`.
	needs_drop = bool(contains_dv or raw_has_drop or raw_is_destructible)
	# is_cheap_copy: decoupled from `needs_drop`.  A type is "cheap
	# copy" when its Copy semantics can be implemented with a single
	# bitcopy or a single retain — POD bitcopy types, refcounted
	# SCALAR types (String — one retain), and Copy structural types
	# whose payload has no drop work.  Structural-with-drop (e.g.
	# `Optional<String>`, `Array<String>`) requires per-field
	# traversal and is NOT cheap.
	td_for_kind = type_table.get(ty)
	is_scalar_kind = td_for_kind.kind is TypeKind.SCALAR
	has_structural_drop = contains_dv or raw_has_drop
	is_cheap_copy = (copy_status is True) and (
		is_bitcopy or is_scalar_kind or not has_structural_drop
	)
	is_destructible = raw_is_destructible
	return DropPolicy(
		needs_drop=needs_drop,
		is_bitcopy=is_bitcopy,
		is_cheap_copy=is_cheap_copy,
		is_destructible=is_destructible,
		has_structural_drop=has_structural_drop,
	)


def _contains_dv_transitive(type_table: TypeTable, ty: TypeId, visited: Set[TypeId]) -> bool:
	if ty in visited:
		return False
	visited.add(ty)
	td = type_table.get(ty)
	if td.kind is TypeKind.DIAGNOSTICVALUE:
		return True
	if td.kind is TypeKind.STRUCT:
		inst = type_table.get_struct_instance(ty)
		if inst is not None:
			for ft in inst.field_types:
				if _contains_dv_transitive(type_table, ft, visited):
					return True
	if td.kind is TypeKind.VARIANT:
		inst = type_table.get_variant_instance(ty)
		if inst is not None:
			for arm in inst.arms:
				for ft in arm.field_types:
					if _contains_dv_transitive(type_table, ft, visited):
						return True
	if td.param_types:
		for pt in td.param_types:
			if _contains_dv_transitive(type_table, pt, visited):
				return True
	return False
