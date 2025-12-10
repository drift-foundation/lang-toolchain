# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-08
"""
Minimal type core shared by the checker and TypeEnv.

TypeIds are opaque ints indexing into a TypeTable. TypeKind keeps the universe
small and extensible; TypeDef carries kind/name/params for inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, List


TypeId = int  # opaque handle into the TypeTable


class TypeKind(Enum):
	"""Kinds of types understood by the minimal type core."""

	SCALAR = auto()
	ERROR = auto()
	FNRESULT = auto()
	FUNCTION = auto()
	ARRAY = auto()
	REF = auto()
	UNKNOWN = auto()


@dataclass(frozen=True)
class TypeDef:
	"""Definition of a type stored in the TypeTable."""

	kind: TypeKind
	name: str
	param_types: List[TypeId]
	ref_mut: bool | None = None  # only meaningful for TypeKind.REF


class TypeTable:
	"""
	Simple type table that owns TypeIds.

	This is intentionally tiny: enough to represent scalars, Error, FnResult,
	and function types. It can be extended as the checker grows.
	"""

	def __init__(self) -> None:
		self._defs: Dict[TypeId, TypeDef] = {}
		self._next_id: TypeId = 1  # reserve 0 for "invalid"
		# Seed well-known scalars if callers stash them here.
		self._uint_type: TypeId | None = None  # type: ignore[var-annotated]
		self._int_type: TypeId | None = None  # type: ignore[var-annotated]
		self._bool_type: TypeId | None = None  # type: ignore[var-annotated]
		self._string_type: TypeId | None = None  # type: ignore[var-annotated]

	def new_scalar(self, name: str) -> TypeId:
		"""Register a scalar type (e.g., Int, Bool) and return its TypeId."""
		return self._add(TypeKind.SCALAR, name, [])

	def ensure_uint(self) -> TypeId:
		"""Return a stable Uint TypeId, creating it once."""
		if getattr(self, "_uint_type", None) is None:
			self._uint_type = self.new_scalar("Uint")  # type: ignore[attr-defined]
		return self._uint_type  # type: ignore[attr-defined]

	def ensure_int(self) -> TypeId:
		"""Return a stable Int TypeId, creating it once."""
		if getattr(self, "_int_type", None) is None:
			self._int_type = self.new_scalar("Int")  # type: ignore[attr-defined]
		return self._int_type  # type: ignore[attr-defined]

	def ensure_bool(self) -> TypeId:
		"""Return a stable Bool TypeId, creating it once."""
		if getattr(self, "_bool_type", None) is None:
			self._bool_type = self.new_scalar("Bool")  # type: ignore[attr-defined]
		return self._bool_type  # type: ignore[attr-defined]

	def ensure_string(self) -> TypeId:
		"""Return a stable String TypeId, creating it once."""
		if getattr(self, "_string_type", None) is None:
			self._string_type = self.new_scalar("String")  # type: ignore[attr-defined]
		return self._string_type  # type: ignore[attr-defined]

	def ensure_ref(self, inner: TypeId) -> TypeId:
		"""Return a stable shared reference TypeId to `inner`, creating it once."""
		if not hasattr(self, "_ref_cache"):
			self._ref_cache = {}  # type: ignore[attr-defined]
		key = ("ref", inner)
		cache = getattr(self, "_ref_cache")  # type: ignore[attr-defined]
		if key not in cache:
			cache[key] = self.new_ref(inner, is_mut=False)
		return cache[key]

	def ensure_ref_mut(self, inner: TypeId) -> TypeId:
		"""Return a stable mutable reference TypeId to `inner`, creating it once."""
		if not hasattr(self, "_ref_cache"):
			self._ref_cache = {}  # type: ignore[attr-defined]
		key = ("ref_mut", inner)
		cache = getattr(self, "_ref_cache")  # type: ignore[attr-defined]
		if key not in cache:
			cache[key] = self.new_ref(inner, is_mut=True)
		return cache[key]

	def ensure_unknown(self) -> TypeId:
		"""Return a stable Unknown TypeId, creating it once."""
		if getattr(self, "_unknown_type", None) is None:
			self._unknown_type = self.new_unknown("Unknown")  # type: ignore[attr-defined]
		return self._unknown_type  # type: ignore[attr-defined]

	def new_error(self, name: str = "Error") -> TypeId:
		"""Register the canonical error/event type."""
		return self._add(TypeKind.ERROR, name, [])

	def new_fnresult(self, ok: TypeId, err: TypeId) -> TypeId:
		"""Register a FnResult<ok, err> type."""
		return self._add(TypeKind.FNRESULT, "FnResult", [ok, err])

	def new_function(self, name: str, param_types: List[TypeId], return_type: TypeId) -> TypeId:
		"""Register a function type (name + params + return)."""
		return self._add(TypeKind.FUNCTION, name, [*param_types, return_type])

	def new_array(self, elem: TypeId) -> TypeId:
		"""Register an Array<elem> type."""
		# Reuse existing Array<elem> if present to keep TypeIds stable.
		for ty_id, ty_def in self._defs.items():
			if ty_def.kind is TypeKind.ARRAY and ty_def.param_types and ty_def.param_types[0] == elem:
				return ty_id
		return self._add(TypeKind.ARRAY, "Array", [elem])

	def new_ref(self, inner: TypeId, is_mut: bool) -> TypeId:
		"""Register a reference type to `inner` (mutable vs shared encoded in ref_mut/name)."""
		name = "RefMut" if is_mut else "Ref"
		for ty_id, ty_def in self._defs.items():
			if ty_def.kind is TypeKind.REF and ty_def.param_types and ty_def.param_types[0] == inner and ty_def.ref_mut == is_mut:
				return ty_id
		return self._add(TypeKind.REF, name, [inner], ref_mut=is_mut)

	def new_unknown(self, name: str = "Unknown") -> TypeId:
		"""Register an unknown type (debug/fallback)."""
		return self._add(TypeKind.UNKNOWN, name, [])

	def _add(self, kind: TypeKind, name: str, params: List[TypeId], ref_mut: bool | None = None) -> TypeId:
		ty_id = self._next_id
		self._next_id += 1
		self._defs[ty_id] = TypeDef(kind=kind, name=name, param_types=list(params), ref_mut=ref_mut if kind is TypeKind.REF else None)
		return ty_id

	def get(self, ty: TypeId) -> TypeDef:
		"""Fetch the TypeDef for a given TypeId."""
		return self._defs[ty]


__all__ = ["TypeId", "TypeKind", "TypeDef", "TypeTable"]
