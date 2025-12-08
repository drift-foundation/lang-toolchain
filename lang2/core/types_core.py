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
	UNKNOWN = auto()


@dataclass(frozen=True)
class TypeDef:
	"""Definition of a type stored in the TypeTable."""

	kind: TypeKind
	name: str
	param_types: List[TypeId]


class TypeTable:
	"""
	Simple type table that owns TypeIds.

	This is intentionally tiny: enough to represent scalars, Error, FnResult,
	and function types. It can be extended as the checker grows.
	"""

	def __init__(self) -> None:
		self._defs: Dict[TypeId, TypeDef] = {}
		self._next_id: TypeId = 1  # reserve 0 for "invalid"

	def new_scalar(self, name: str) -> TypeId:
		"""Register a scalar type (e.g., Int, Bool) and return its TypeId."""
		return self._add(TypeKind.SCALAR, name, [])

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
		return self._add(TypeKind.ARRAY, "Array", [elem])

	def new_unknown(self, name: str = "Unknown") -> TypeId:
		"""Register an unknown type (debug/fallback)."""
		return self._add(TypeKind.UNKNOWN, name, [])

	def _add(self, kind: TypeKind, name: str, params: List[TypeId]) -> TypeId:
		ty_id = self._next_id
		self._next_id += 1
		self._defs[ty_id] = TypeDef(kind=kind, name=name, param_types=list(params))
		return ty_id

	def get(self, ty: TypeId) -> TypeDef:
		"""Fetch the TypeDef for a given TypeId."""
		return self._defs[ty]


__all__ = ["TypeId", "TypeKind", "TypeDef", "TypeTable"]
