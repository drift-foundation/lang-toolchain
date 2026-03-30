# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
SemanticWorld — unified semantic store for a compiler session.

This is a thin wrapper that holds references to the existing separate
stores (TypeTable, CallableRegistry, module exports, signatures, etc.)
and provides a single object for phase boundaries to accept.

Design intent:
  - Contract unification first, storage unification later.
  - Phase boundaries (parser, checker, lowerer, codegen) accept one
    world-shaped object instead of passing long bundles of type_table +
    module_exports + signatures + callable_registry + ...
  - The world carries lifecycle state so invariants can be asserted:
    package ingress done, source ingress done, freeze/ready.

Not yet:
  - Merging TypeTable and CallableRegistry internals.
  - Moving signatures or exports into the world's own storage.
  - Replacing all callers (incremental migration).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, Mapping, Optional

if TYPE_CHECKING:
	from pathlib import Path

	from lang.driftc.core.function_id import FunctionId
	from lang.driftc.core.types_core import TypeTable
	from lang.driftc.method_registry import CallableRegistry


class WorldPhase(Enum):
	"""Lifecycle phases of the semantic world."""
	EMPTY = auto()               # Created, nothing ingressed yet
	PACKAGE_INGRESS = auto()     # Package types/aliases/schemas being loaded
	PACKAGES_READY = auto()      # All package data ingressed, ready for source parsing
	SOURCE_INGRESS = auto()      # Parser adding source types/signatures
	READY = auto()               # All ingress complete, checking/lowering may proceed
	FROZEN = auto()              # No further mutations allowed


@dataclass
class SemanticWorld:
	"""Unified semantic store for a single compiler session.

	Holds references to the existing separate stores.  Phase boundaries
	accept this object instead of passing each store individually.
	"""

	# ── Core stores (references, not owned copies) ──────────────────
	# type_table may be None initially for source-only builds; the parser
	# creates it with the correct word_bits configuration.
	type_table: Optional["TypeTable"] = None
	callable_registry: Optional["CallableRegistry"] = None

	# ── Signature maps ──────────────────────────────────────────────
	# Base signatures from source parsing.
	base_signatures: Optional[Mapping["FunctionId", Any]] = None
	# Derived signatures (wrappers, instantiations).
	derived_signatures: Optional[Dict["FunctionId", Any]] = None
	# External signatures reconstructed from package payloads.
	external_signatures: Optional[Dict["FunctionId", Any]] = None

	# ── Module metadata ─────────────────────────────────────────────
	# Per-module export sets (values, types, consts, traits, reexports).
	module_exports: Optional[Dict[str, Dict[str, Any]]] = None
	# Module dependency graph.
	module_deps: Optional[Dict[str, set]] = None

	# ── Package metadata ────────────────────────────────────────────
	# Per-package TypeId remap tables (package path -> {pkg_tid -> host_tid}).
	pkg_typeid_maps: Dict["Path", Dict[int, int]] = field(default_factory=dict)
	# Per-package TypeId universes for boundary checks.
	pkg_tid_universes: Dict["Path", FrozenSet[int]] = field(default_factory=dict)

	# ── Trait/impl indexes ──────────────────────────────────────────
	external_trait_defs: list = field(default_factory=list)
	external_impl_metas: list = field(default_factory=list)
	external_missing_traits: set = field(default_factory=set)

	# ── Analysis overlay (preparatory) ──────────────────────────────
	# Phase-owned derived metadata that augments canonical signatures.
	# Keyed by FunctionId, holds analysis results like non-retaining
	# param annotations and escape metadata.
	#
	# NOTE: the driver still uses a merge-and-mutate pattern for
	# analyze_non_retaining_params / _apply_stdlib_escape_annotations.
	# This overlay is infrastructure for migrating those consumers to
	# write here instead of mutating signature dicts.  Until that
	# migration is complete, the overlay coexists with the old path.
	signature_annotations: Dict[Any, Dict[str, Any]] = field(default_factory=dict)

	# ── Lifecycle ───────────────────────────────────────────────────
	phase: WorldPhase = WorldPhase.EMPTY

	def advance_to(self, target: WorldPhase) -> None:
		"""Advance the world to a new lifecycle phase.

		Phases must advance monotonically.  Skipping phases is allowed
		(e.g. EMPTY -> READY for source-only builds without packages).
		"""
		if target.value < self.phase.value:
			raise RuntimeError(
				f"cannot move world backward: {self.phase.name} -> {target.name}"
			)
		self.phase = target

	def assert_ready(self) -> None:
		"""Assert that all ingress is complete and the world is ready
		for type checking / MIR lowering / codegen."""
		if self.phase.value < WorldPhase.READY.value:
			raise RuntimeError(
				f"world is not ready for checking (phase={self.phase.name}); "
				f"complete package and source ingress first"
			)

	def assert_packages_ready(self) -> None:
		"""Assert that package ingress is complete."""
		if self.phase.value < WorldPhase.PACKAGES_READY.value:
			raise RuntimeError(
				f"package ingress not complete (phase={self.phase.name})"
			)

	@property
	def is_frozen(self) -> bool:
		return self.phase is WorldPhase.FROZEN

	def freeze(self) -> None:
		"""Freeze the world: no further declaration mutations allowed.

		After freeze:
		  - No new nominal type declarations (declare_struct, declare_variant, etc.)
		  - No new type alias definitions
		  - No new callable registrations
		  - No new module export mutations

		Still allowed after freeze:
		  - Structural type interning (ensure_ref, ensure_function, new_array, etc.)
		  - Expression type annotations (checker output)
		  - MIR emission, codegen
		"""
		self.advance_to(WorldPhase.FROZEN)
		# Install declaration guards on the TypeTable.
		if self.type_table is not None:
			self.type_table._frozen = True
		# Install declaration guards on the CallableRegistry.
		if self.callable_registry is not None:
			self.callable_registry._frozen = True

	# ── Query helpers ───────────────────────────────────────────────
	# These provide a single read path for data that spans multiple
	# internal stores, so callers don't need to know about the
	# base/derived/external signature split or other internal details.

	def get_signature(self, fn_id: "FunctionId") -> Any:
		"""Look up a function signature across all signature maps.

		Search order: derived (wrappers/instantiations) → base (source) → external (packages).
		Returns None if the function is not found.
		"""
		if self.derived_signatures is not None:
			sig = self.derived_signatures.get(fn_id)
			if sig is not None:
				return sig
		if self.base_signatures is not None:
			sig = self.base_signatures.get(fn_id)
			if sig is not None:
				return sig
		if self.external_signatures is not None:
			return self.external_signatures.get(fn_id)
		return None

	def all_signatures(self) -> Mapping["FunctionId", Any]:
		"""Return a merged view of all signatures (derived > base > external)."""
		from collections import ChainMap
		maps: list[Mapping] = []
		if self.derived_signatures is not None:
			maps.append(self.derived_signatures)
		if self.base_signatures is not None:
			maps.append(self.base_signatures)
		if self.external_signatures is not None:
			maps.append(self.external_signatures)
		return ChainMap(*maps) if maps else {}

	def get_module_exports(self, module_id: str) -> Optional[Dict[str, Any]]:
		"""Look up exports for a module."""
		if self.module_exports is not None:
			return self.module_exports.get(module_id)
		return None

	def get_pkg_typeid_map(self, pkg_path: "Path") -> Optional[Dict[int, int]]:
		"""Look up the TypeId remap table for a loaded package."""
		return self.pkg_typeid_maps.get(pkg_path)

	@property
	def package_id(self) -> Optional[str]:
		"""The current compilation's package identity."""
		if self.type_table is not None:
			return getattr(self.type_table, "package_id", None)
		return None

	@property
	def module_packages(self) -> Dict[str, str]:
		"""Module → package ownership mapping."""
		if self.type_table is not None:
			return getattr(self.type_table, "module_packages", {})
		return {}

	def annotate_signature(self, fn_id: "FunctionId", key: str, value: Any) -> None:
		"""Attach an analysis annotation to a function signature.

		Annotations are phase-owned derived metadata (e.g. non-retaining
		param flags, escape analysis results).  They augment but never
		replace the canonical signature in base/derived/external stores.
		"""
		self.signature_annotations.setdefault(fn_id, {})[key] = value

	def get_signature_annotation(self, fn_id: "FunctionId", key: str) -> Any:
		"""Look up an analysis annotation for a function signature."""
		entry = self.signature_annotations.get(fn_id)
		if entry is not None:
			return entry.get(key)
		return None

	def effective_param_escape_level(self, fn_id: "FunctionId", param_index: int) -> Any:
		"""Get the effective escape level for a function parameter.

		The overlay is the sole authority for escape-level metadata.
		Returns EscapeLevel.THREAD as the default if no annotation exists.
		"""
		from lang.driftc.borrow_checker import EscapeLevel
		pel = self.get_signature_annotation(fn_id, "param_escape_level")
		if pel is not None and param_index < len(pel):
			lvl = pel[param_index]
			if lvl is not None:
				return lvl
		return EscapeLevel.THREAD
