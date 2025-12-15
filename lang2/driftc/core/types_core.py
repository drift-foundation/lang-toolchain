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
	STRUCT = auto()
	ERROR = auto()
	DIAGNOSTICVALUE = auto()
	OPTIONAL = auto()
	VOID = auto()
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
	field_names: List[str] | None = None  # only meaningful for TypeKind.STRUCT


class TypeTable:
	"""
	Simple type table that owns TypeIds.

	This is intentionally tiny: enough to represent scalars, Error, FnResult,
	and function types. It can be extended as the checker grows.
	"""

	def __init__(self) -> None:
		self._defs: Dict[TypeId, TypeDef] = {}
		self._next_id: TypeId = 1  # reserve 0 for "invalid"
		# Nominal name → TypeId mapping. This ensures repeated references to the
		# same user-defined type resolve to a single TypeId (critical for long-term
		# correctness once structs/enums/modules are enabled).
		self._named: Dict[str, TypeId] = {}
		# Seed well-known scalars if callers stash them here.
		self._uint_type: TypeId | None = None  # type: ignore[var-annotated]
		self._int_type: TypeId | None = None  # type: ignore[var-annotated]
		self._float_type: TypeId | None = None  # type: ignore[var-annotated]
		self._bool_type: TypeId | None = None  # type: ignore[var-annotated]
		self._string_type: TypeId | None = None  # type: ignore[var-annotated]
		self._void_type: TypeId | None = None  # type: ignore[var-annotated]
		self._error_type: TypeId | None = None  # type: ignore[var-annotated]
		self._dv_type: TypeId | None = None  # type: ignore[var-annotated]
		# Exception schemas keyed by canonical event FQN strings. Values are
		# (canonical_fqn, [declared_field_names]) so later stages can:
		# - resolve constructor-call args (positional/keyword) to declared fields
		# - enforce exact coverage (no missing/unknown/duplicates)
		# - attach attrs deterministically in lowering.
		self.exception_schemas: dict[str, tuple[str, list[str]]] = {}
		# Struct schemas keyed by nominal type name. Values are (name, [field_names]).
		# Field types live in the STRUCT TypeDef itself.
		self.struct_schemas: dict[str, tuple[str, list[str]]] = {}

	def new_scalar(self, name: str) -> TypeId:
		"""Register a scalar type (e.g., Int, Bool) and return its TypeId."""
		return self._add(TypeKind.SCALAR, name, [])

	def ensure_named(self, name: str) -> TypeId:
		"""
		Return a stable TypeId for a nominal name.

		This is used for user-defined type names that appear in annotations.
		If the name has not been declared yet, we conservatively create a scalar
		nominal type. Later, a richer kind (STRUCT/ENUM) may be declared under
		that name; callers should prefer explicit `declare_struct` for structs.
		"""
		if name in self._named:
			return self._named[name]
		return self._add(TypeKind.SCALAR, name, [])

	def declare_struct(self, name: str, field_names: List[str]) -> TypeId:
		"""
		Declare a struct nominal type with placeholder field types.

		This supports recursive type references by first declaring all struct
		names, then filling field types in a second pass via `define_struct_fields`.
		"""
		if name in self._named:
			ty_id = self._named[name]
			td = self.get(ty_id)
			if td.kind is TypeKind.STRUCT:
				return ty_id
			raise ValueError(f"type name '{name}' already defined as {td.kind}")
		unknown = self.ensure_unknown()
		placeholder = [unknown for _ in field_names]
		ty_id = self._add(TypeKind.STRUCT, name, placeholder, field_names=list(field_names))
		self.struct_schemas[name] = (name, list(field_names))
		return ty_id

	def define_struct_fields(self, struct_id: TypeId, field_types: List[TypeId]) -> None:
		"""Fill in the field TypeIds for a declared struct."""
		td = self.get(struct_id)
		if td.kind is not TypeKind.STRUCT or td.field_names is None:
			raise ValueError("define_struct_fields requires a STRUCT TypeId")
		if len(field_types) != len(td.field_names):
			raise ValueError("field_types length mismatch for struct definition")
		self._defs[struct_id] = TypeDef(
			kind=TypeKind.STRUCT,
			name=td.name,
			param_types=list(field_types),
			ref_mut=None,
			field_names=list(td.field_names),
		)

	def struct_field(self, struct_id: TypeId, field_name: str) -> tuple[int, TypeId] | None:
		"""Return (field_index, field_type_id) for a struct field, or None."""
		td = self.get(struct_id)
		if td.kind is not TypeKind.STRUCT or td.field_names is None:
			return None
		try:
			idx = td.field_names.index(field_name)
		except ValueError:
			return None
		if idx >= len(td.param_types):
			return None
		return idx, td.param_types[idx]

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

	def ensure_float(self) -> TypeId:
		"""
		Return a stable Float TypeId, creating it once.

		In lang2 v1, `Float` is IEEE-754 double precision and maps to C `double`
		and LLVM `double`.
		"""
		if getattr(self, "_float_type", None) is None:
			self._float_type = self.new_scalar("Float")  # type: ignore[attr-defined]
		return self._float_type  # type: ignore[attr-defined]

	def ensure_error(self) -> TypeId:
		"""
		Return the canonical Error TypeId, creating it once.

		Error is modeled as a builtin event type; callers should prefer this
		over minting duplicate TypeIds for the same logical error type.
		"""
		if getattr(self, "_error_type", None) is None:
			self._error_type = self.new_error("Error")  # type: ignore[attr-defined]
		return self._error_type  # type: ignore[attr-defined]

	def ensure_void(self) -> TypeId:
		"""
		Return a stable Void TypeId, creating it once.

		Void represents “no value” and is distinct from scalar/unit types.
		"""
		if getattr(self, "_void_type", None) is None:
			self._void_type = self._add(TypeKind.VOID, "Void", [])  # type: ignore[attr-defined]
		return self._void_type  # type: ignore[attr-defined]

	def ensure_diagnostic_value(self) -> TypeId:
		"""Return the canonical DiagnosticValue TypeId, creating it once."""
		if getattr(self, "_dv_type", None) is None:
			self._dv_type = self._add(TypeKind.DIAGNOSTICVALUE, "DiagnosticValue", [])  # type: ignore[attr-defined]
		return self._dv_type  # type: ignore[attr-defined]

	def new_optional(self, inner: TypeId) -> TypeId:
		"""Register Optional<inner> (cached for stability)."""
		if not hasattr(self, "_optional_cache"):
			self._optional_cache = {}  # type: ignore[attr-defined]
		cache = getattr(self, "_optional_cache")  # type: ignore[attr-defined]
		if inner in cache:
			return cache[inner]
		opt_id = self._add(TypeKind.OPTIONAL, "Optional", [inner])
		cache[inner] = opt_id
		return opt_id

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

	def ensure_fnresult(self, ok: TypeId, err: TypeId) -> TypeId:
		"""
		Return a stable shared FnResult<ok, err> TypeId, creating it once.

		FnResult is used as an internal ABI carrier in lang2. Keeping a stable,
		reused TypeId for a given (ok, err) pair makes type comparisons and
		diagnostics less fragile across passes.
		"""
		if not hasattr(self, "_fnresult_cache"):
			self._fnresult_cache = {}  # type: ignore[attr-defined]
		key = (ok, err)
		cache = getattr(self, "_fnresult_cache")  # type: ignore[attr-defined]
		if key not in cache:
			cache[key] = self.new_fnresult(ok, err)
		return cache[key]

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

	def _add(
		self,
		kind: TypeKind,
		name: str,
		params: List[TypeId],
		ref_mut: bool | None = None,
		field_names: List[str] | None = None,
	) -> TypeId:
		ty_id = self._next_id
		self._next_id += 1
		self._defs[ty_id] = TypeDef(
			kind=kind,
			name=name,
			param_types=list(params),
			ref_mut=ref_mut if kind is TypeKind.REF else None,
			field_names=list(field_names) if field_names is not None else None,
		)
		if kind in (TypeKind.SCALAR, TypeKind.STRUCT):
			self._named.setdefault(name, ty_id)
		return ty_id

	def get(self, ty: TypeId) -> TypeDef:
		"""Fetch the TypeDef for a given TypeId."""
		return self._defs[ty]

	def is_void(self, ty: TypeId) -> bool:
		"""Return True when the TypeId refers to the canonical Void type."""
		return self.get(ty).kind is TypeKind.VOID


__all__ = ["TypeId", "TypeKind", "TypeDef", "TypeTable"]
