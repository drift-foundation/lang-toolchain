#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Method/function registry for v1 resolution.

This stores callable declarations (free functions + methods) with enough
metadata for the resolver to pick a concrete overload. The registry itself
does not perform overload resolution; it only returns candidate sets filtered
by name/type/module/visibility. The type checker/resolver will apply the
type-based rules from drift-method-resolution-v1 to select a single callable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, Iterable, List, Optional, Tuple

from lang2.core.types_core import TypeId

# Opaque identifiers; use stable ints (or real symbol IDs) to keep hashing reliable.
ModuleId = int
ImplId = int
TraitId = int
CallableId = int


class SelfMode(Enum):
	SELF_BY_VALUE = auto()
	SELF_BY_REF = auto()
	SELF_BY_REF_MUT = auto()


class CallableKind(Enum):
	FREE_FUNCTION = auto()
	METHOD_INHERENT = auto()
	METHOD_TRAIT = auto()


@dataclass(frozen=True)
class Visibility:
	"""Minimal v1 visibility; extend as needed."""

	is_public: bool

	@staticmethod
	def public() -> "Visibility":
		return Visibility(is_public=True)

	@staticmethod
	def private() -> "Visibility":
		return Visibility(is_public=False)


@dataclass(frozen=True)
class CallableSignature:
	"""Concrete, fully-typed signature (param/result types)."""

	param_types: Tuple[TypeId, ...]
	result_type: TypeId
	# v1 assumes param/result types are fully concrete. If a declaration is generic
	# and not yet monomorphized, the resolver should treat it as not viable unless
	# all type parameters are explicitly substituted.


@dataclass(frozen=True)
class CallableDecl:
	"""Registry entry for a free function or method."""

	callable_id: CallableId
	name: str
	kind: CallableKind
	module_id: ModuleId
	visibility: Visibility
	signature: CallableSignature

	impl_id: Optional[ImplId] = None
	impl_target_type_id: Optional[TypeId] = None  # nominal type T in impl T { ... }
	self_mode: Optional[SelfMode] = None
	trait_id: Optional[TraitId] = None
	is_generic: bool = False


class CallableRegistry:
	"""
	Store callable declarations and provide fast candidate retrieval.

	This does not resolve overloads; it only buckets by name/type/module and
	returns candidates. The resolver applies type/auto-borrow rules to choose
	the winner.

	Short-term (single-file driver):
	  - We populate this registry from parser/type-resolver signatures for one
	    compilation unit. Module ids come from the source module name.

	Long-term (multi-file merge/link with .do artifacts):
	  - Each per-file build emits a Drift Object (.do) with:
	      * Module name/id
	      * CallableDecl metadata (free + methods) including impl_target/self_mode
	      * Visibility
	    The link/merge phase will read those .do metas, assign stable module ids
	    across files, detect collisions, and repopulate a merged CallableRegistry
	    for resolution/codegen. No code changes are needed here; the caller simply
	    feeds the merged registry into the resolver/type checker.
	"""

	def __init__(self) -> None:
		self._free_by_name: Dict[str, List[CallableDecl]] = {}
		# Methods bucketed per module: module_id -> {(impl_target_type_id, name) -> [CallableDecl]}
		self._methods_by_module: Dict[ModuleId, Dict[Tuple[TypeId, str], List[CallableDecl]]] = {}
		self._by_id: Dict[CallableId, CallableDecl] = {}

	def register_free_function(
		self,
		*,
		callable_id: CallableId,
		name: str,
		module_id: ModuleId,
		visibility: Visibility,
		signature: CallableSignature,
		is_generic: bool = False,
	) -> None:
		if callable_id in self._by_id:
			raise ValueError(f"duplicate callable_id {callable_id}")
		decl = CallableDecl(
			callable_id=callable_id,
			name=name,
			kind=CallableKind.FREE_FUNCTION,
			module_id=module_id,
			visibility=visibility,
			signature=signature,
			is_generic=is_generic,
		)
		self._by_id[callable_id] = decl
		self._free_by_name.setdefault(name, []).append(decl)

	def register_inherent_method(
		self,
		*,
		callable_id: CallableId,
		name: str,
		module_id: ModuleId,
		visibility: Visibility,
		signature: CallableSignature,
		impl_id: ImplId,
		impl_target_type_id: TypeId,
		self_mode: SelfMode,
		is_generic: bool = False,
	) -> None:
		if callable_id in self._by_id:
			raise ValueError(f"duplicate callable_id {callable_id}")
		decl = CallableDecl(
			callable_id=callable_id,
			name=name,
			kind=CallableKind.METHOD_INHERENT,
			module_id=module_id,
			visibility=visibility,
			signature=signature,
			impl_id=impl_id,
			impl_target_type_id=impl_target_type_id,
			self_mode=self_mode,
			is_generic=is_generic,
		)
		self._by_id[callable_id] = decl
		bucket = self._methods_by_module.setdefault(module_id, {})
		bucket.setdefault((impl_target_type_id, name), []).append(decl)

	def register_trait_method(
		self,
		*,
		callable_id: CallableId,
		name: str,
		module_id: ModuleId,
		visibility: Visibility,
		signature: CallableSignature,
		impl_id: ImplId,
		impl_target_type_id: TypeId,
		trait_id: TraitId,
		self_mode: SelfMode,
		is_generic: bool = False,
	) -> None:
		if callable_id in self._by_id:
			raise ValueError(f"duplicate callable_id {callable_id}")
		decl = CallableDecl(
			callable_id=callable_id,
			name=name,
			kind=CallableKind.METHOD_TRAIT,
			module_id=module_id,
			visibility=visibility,
			signature=signature,
			impl_id=impl_id,
			impl_target_type_id=impl_target_type_id,
			trait_id=trait_id,
			self_mode=self_mode,
			is_generic=is_generic,
		)
		self._by_id[callable_id] = decl
		bucket = self._methods_by_module.setdefault(module_id, {})
		bucket.setdefault((impl_target_type_id, name), []).append(decl)

	def get_free_candidates(
		self,
		*,
		name: str,
		visible_modules: Iterable[ModuleId],
		include_private_in: Optional[ModuleId] = None,
	) -> List[CallableDecl]:
		all_candidates = self._free_by_name.get(name, [])
		if not all_candidates:
			return []
		visible_modules_set = set(visible_modules)
		result: List[CallableDecl] = []
		for decl in all_candidates:
			if decl.visibility.is_public and decl.module_id in visible_modules_set:
				result.append(decl)
			elif include_private_in is not None and decl.module_id == include_private_in:
				result.append(decl)
		return result

	def get_method_candidates(
		self,
		*,
		receiver_nominal_type_id: TypeId,
		name: str,
		visible_modules: Iterable[ModuleId],
		include_private_in: Optional[ModuleId] = None,
	) -> List[CallableDecl]:
		visible_modules_set = set(visible_modules)
		result: List[CallableDecl] = []
		for mod_id, bucket in self._methods_by_module.items():
			is_visible_module = mod_id in visible_modules_set
			is_private_ok = include_private_in is not None and mod_id == include_private_in
			if not (is_visible_module or is_private_ok):
				continue
			cands = bucket.get((receiver_nominal_type_id, name), [])
			if not cands:
				continue
			for decl in cands:
				if decl.visibility.is_public and is_visible_module:
					result.append(decl)
				elif is_private_ok and decl.module_id == include_private_in:
					result.append(decl)
		return result

	def get_by_id(self, callable_id: CallableId) -> CallableDecl:
		return self._by_id[callable_id]


__all__ = [
	"CallableRegistry",
	"CallableDecl",
	"CallableSignature",
	"CallableKind",
	"SelfMode",
	"Visibility",
	"CallableId",
	"ModuleId",
	"ImplId",
	"TraitId",
]
