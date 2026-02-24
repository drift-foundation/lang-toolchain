# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-08
"""
Minimal type core shared by the checker and TypeEnv.

TypeIds are opaque ints indexing into a TypeTable. TypeKind keeps the universe
small and extensible; TypeDef carries kind/name/params for inspection.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto
import sys
from typing import Dict, Iterable, List

from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.span import Span
from lang.driftc.core.source_manager import SourceManager
from lang.driftc import debug as drift_debug


TypeId = int  # opaque handle into the TypeTable


class TypeKind(Enum):
	"""Kinds of types understood by the minimal type core."""

	SCALAR = auto()
	FORWARD_NOMINAL = auto()
	STRUCT = auto()
	INTERFACE = auto()
	TYPEVAR = auto()
	ERROR = auto()
	DIAGNOSTICVALUE = auto()
	VOID = auto()
	FNRESULT = auto()
	FUNCTION = auto()
	ARRAY = auto()
	REF = auto()
	RAW_PTR = auto()
	VARIANT = auto()
	UNKNOWN = auto()


@dataclass(frozen=True)
class TypeParamId:
	"""Stable identity for a type parameter within a function/signature."""

	owner: FunctionId
	index: int


@dataclass(frozen=True)
class NominalKey:
	"""
	Canonical identity for a nominal (named) type.

	For production correctness, user-defined nominal types are module-scoped.
	That means `struct Point` declared in module `a.geom` is a different type
	from `struct Point` declared in module `b.geom`, even though both share the
	short name `Point`.

	Builtins use `module_id=None`/`package_id=None` and are toolchain-owned.
	"""

	package_id: str | None
	module_id: str | None
	name: str
	kind: TypeKind


@dataclass(frozen=True)
class TypeDef:
	"""Definition of a type stored in the TypeTable."""

	kind: TypeKind
	name: str
	param_types: List[TypeId]
	type_param_id: TypeParamId | None = None
	# Module id for nominal types (STRUCT/VARIANT and any other module-scoped
	# named types). Builtins use module_id=None.
	module_id: str | None = None
	ref_mut: bool | None = None  # only meaningful for TypeKind.REF
	fn_throws: bool = True  # only meaningful for TypeKind.FUNCTION
	field_names: List[str] | None = None  # only meaningful for TypeKind.STRUCT

	def fn_throws_raw(self) -> bool | None:
		"""Return the raw throw marker for serialization/debugging."""
		if self.kind is not TypeKind.FUNCTION:
			return None
		return bool(self.fn_throws)

	def can_throw(self) -> bool:
		"""Return True if the function type can throw."""
		if self.kind is not TypeKind.FUNCTION:
			return False
		return bool(self.fn_throws)

	def with_can_throw(self, can_throw: bool) -> "TypeDef":
		"""Return a copy with the function throw-mode updated."""
		if self.kind is not TypeKind.FUNCTION:
			return self
		return replace(self, fn_throws=bool(can_throw))


@dataclass(frozen=True)
class StructFieldSchema:
	"""Single declared field in a struct schema."""

	name: str
	type_expr: GenericTypeExpr
	is_pub: bool = False


@dataclass(frozen=True)
class StructSchema:
	"""
	Definition-time schema for a struct (generic or non-generic).

	Field types are stored as GenericTypeExpr templates and instantiated with
	concrete type arguments to create StructInstance entries.
	"""

	module_id: str
	name: str
	type_params: list[str]
	fields: list[StructFieldSchema]


@dataclass(frozen=True)
class StructInstance:
	"""Concrete (monomorphized) view of a struct type."""

	base_id: TypeId
	type_args: list[TypeId]
	field_names: list[str]
	field_types: list[TypeId]
	fields_by_name: dict[str, int]


@dataclass(frozen=True)
class InterfaceSchema:
	"""Definition-time schema for an interface (generic or non-generic)."""

	module_id: str
	name: str
	type_params: list[str]
	parents: list[GenericTypeExpr]
	parent_base_ids: list[TypeId]
	methods: list["InterfaceMethodSchema"]


@dataclass(frozen=True)
class InterfaceInstance:
	"""Concrete (monomorphized) view of an interface type."""

	base_id: TypeId
	type_args: list[TypeId]


@dataclass(frozen=True)
class InterfaceParamSchema:
	"""Single parameter in an interface method schema."""

	name: str
	type_expr: GenericTypeExpr


@dataclass(frozen=True)
class InterfaceMethodSchema:
	"""Definition-time signature for an interface method."""

	name: str
	params: list[InterfaceParamSchema]
	return_type: GenericTypeExpr
	type_params: list[str]
	declared_nothrow: bool = False
	is_unsafe: bool = False


@dataclass(frozen=True)
class VariantFieldSchema:
	"""A single declared field in a variant constructor."""

	name: str
	type_expr: GenericTypeExpr


@dataclass(frozen=True)
class VariantArmSchema:
	"""A single constructor arm in a variant schema."""

	name: str
	fields: list[VariantFieldSchema]


@dataclass(frozen=True)
class VariantSchema:
	"""
	Definition-time schema for a variant (generic or non-generic).

	The schema is stored on the *base* variant type (declared name). Concrete
	instantiations are created via `TypeTable.ensure_variant_instantiated(...)` which
	evaluates field `GenericTypeExpr`s into concrete `TypeId`s.
	"""

	module_id: str
	name: str
	type_params: list[str]
	arms: list[VariantArmSchema]
	tombstone_ctor: str | None = None


@dataclass(frozen=True)
class VariantArmInstance:
	"""A concrete constructor arm for an instantiated variant type."""

	tag: int
	name: str
	field_names: list[str]
	field_types: list[TypeId]


@dataclass(frozen=True)
class VariantInstance:
	"""
	Concrete (monomorphized) view of a variant type.

	This is internal compiler data. The language ABI for variants is not frozen
	yet, but lowering/codegen treat this as the authoritative description of:
	- constructor tags,
	- field names and types per constructor.
	"""

	base_id: TypeId
	type_args: list[TypeId]
	arms: list[VariantArmInstance]
	arms_by_name: dict[str, VariantArmInstance]
	internal_tombstone_ctor: str | None = None
	internal_tombstone_tag: int | None = None


@dataclass(frozen=True)
class TypeProvenanceEntry:
	phase: str
	kind: str
	span: Span | None = None
	note: str | None = None
	order: int = 0


class TypeTable:
	"""
	Simple type table that owns TypeIds.

	This is intentionally tiny: enough to represent scalars, Error, FnResult,
	and function types. It can be extended as the checker grows.
	"""

	def __init__(self) -> None:
		self._defs: Dict[TypeId, TypeDef] = {}
		self._next_id: TypeId = 1  # reserve 0 for "invalid"
		# Package identity for module-scoped type keys.
		self.package_id: str | None = None
		self.module_packages: dict[str, str] = {}
		# Nominal key → TypeId mapping. This ensures repeated references to the
		# same module-scoped user-defined type resolve to a single TypeId.
		self._nominal: Dict[NominalKey, TypeId] = {}
		# Seed well-known scalars if callers stash them here.
		self._uint_type: TypeId | None = None  # type: ignore[var-annotated]
		self._int_type: TypeId | None = None  # type: ignore[var-annotated]
		self._float_type: TypeId | None = None  # type: ignore[var-annotated]
		self._bool_type: TypeId | None = None  # type: ignore[var-annotated]
		self._string_type: TypeId | None = None  # type: ignore[var-annotated]
		self._void_type: TypeId | None = None  # type: ignore[var-annotated]
		self._error_type: TypeId | None = None  # type: ignore[var-annotated]
		self._dv_type: TypeId | None = None  # type: ignore[var-annotated]
		self.source_manager: SourceManager | None = None
		# Exception schemas keyed by canonical event FQN strings. Values are
		# (canonical_fqn, [declared_field_names]) so later stages can:
		# - resolve constructor-call args (positional/keyword) to declared fields
		# - enforce exact coverage (no missing/unknown/duplicates)
		# - attach attrs deterministically in lowering.
		self.exception_schemas: dict[str, tuple[str, list[str]]] = {}
		# Struct schemas keyed by nominal identity. Values are (name, [field_names]).
		# Field types live in the STRUCT TypeDef itself.
		self.struct_schemas: dict[NominalKey, tuple[str, list[str]]] = {}
		# Struct base schemas keyed by the base TypeId (declared name).
		self.struct_bases: dict[TypeId, StructSchema] = {}
		# Struct type parameter ids keyed by the base TypeId (declared name).
		self.struct_type_param_ids: dict[TypeId, list[TypeParamId]] = {}
		# Concrete instantiations keyed by the instantiated TypeId.
		self.struct_instances: dict[TypeId, StructInstance] = {}
		# Interface base schemas keyed by the base TypeId (declared name).
		self.interface_bases: dict[TypeId, InterfaceSchema] = {}
		# Interface type parameter ids keyed by the base TypeId (declared name).
		self.interface_type_param_ids: dict[TypeId, list[TypeParamId]] = {}
		# Concrete instantiations keyed by the instantiated TypeId.
		self.interface_instances: dict[TypeId, InterfaceInstance] = {}
		# Variant schemas keyed by the *base* TypeId (declared name).
		self.variant_schemas: dict[TypeId, VariantSchema] = {}
		# Concrete instantiations keyed by the instantiated TypeId.
		self.variant_instances: dict[TypeId, VariantInstance] = {}
		# Instantiation cache: (base_id, args...) -> instantiated TypeId.
		self._instantiation_cache: dict[tuple[TypeId, tuple[TypeId, ...]], TypeId] = {}
		# Variant template cache: (base_id, args...) -> template TypeId.
		self._variant_template_cache: dict[tuple[TypeId, tuple[TypeId, ...]], TypeId] = {}
		# Struct instantiation cache: (base_id, args...) -> instantiated TypeId.
		self._struct_instantiation_cache: dict[tuple[TypeId, tuple[TypeId, ...]], TypeId] = {}
		# Struct template cache: (base_id, args...) -> template TypeId.
		self._struct_template_cache: dict[tuple[TypeId, tuple[TypeId, ...]], TypeId] = {}
		# Interface instantiation cache: (base_id, args...) -> instantiated TypeId.
		self._interface_instantiation_cache: dict[tuple[TypeId, tuple[TypeId, ...]], TypeId] = {}
		# Interface template cache: (base_id, args...) -> template TypeId.
		self._interface_template_cache: dict[tuple[TypeId, tuple[TypeId, ...]], TypeId] = {}
		# Interface linearization cache: base_id -> linearized base ids.
		self._interface_linearization_cache: dict[TypeId, list[TypeId]] = {}
		# Interface segment offset cache: base_id -> base_id -> slot offset.
		self._interface_segment_offsets_cache: dict[TypeId, dict[TypeId, int]] = {}
		# Type parameter cache: TypeParamId -> TypeId.
		self._typevar_cache: dict[TypeParamId, TypeId] = {}
		# Array cache: elem TypeId -> Array<elem> TypeId.
		self._array_cache: dict[TypeId, TypeId] = {}
		# Array base id for trait impl matching (Array<__T>).
		self._array_base_id: TypeId | None = None
		# Cache for drop-needs checks.
		self._needs_drop_cache: dict[TypeId, bool] = {}
		# Default Destructible query fallback so early instantiations don't error.
		self.set_destructible_query(lambda _tid: None, allow_fallback=True)
		# Compile-time constants keyed by their fully-qualified symbol name.
		#
		# MVP: constants are literal values embedded into IR at each use site; there
		# is no runtime global storage. We still need a central table so:
		# - type checking can resolve `Name` references to constants,
		# - cross-module/package imports can supply constant values without source,
		# - package emission can encode exported consts as part of the interface.
		#
		# Key format follows the callable naming scheme: "<module_id>::<name>".
		# Values store the TypeId (so const refs can be typed) and the python value.
		self.consts: dict[str, tuple[TypeId, object]] = {}
		# Type aliases keyed by (module_id, name).
		#
		# Values are (type_params, target_type_expr, loc).
		self.type_aliases: dict[tuple[str | None, str], tuple[list[str], object, object | None]] = {}
		# Optional type provenance side table (enabled via debug flag).
		self._type_provenance_enabled: bool = False
		self._type_provenance_counter: int = 0
		self._type_provenance: dict[TypeId, list[TypeProvenanceEntry]] = {}

	def set_source_manager(self, source_manager: SourceManager | None) -> None:
		self.source_manager = source_manager

	def source_slice_from_span(self, span: Span) -> str | None:
		sm = self.source_manager
		if sm is None:
			return None
		start_pos = span.start_pos
		end_pos = span.end_pos
		if start_pos is None or end_pos is None:
			return None
		file_id = span.file_id
		if file_id is None and span.file is not None:
			file_id = sm.file_id_for_path(span.file)
		if file_id is None:
			return None
		return sm.slice(file_id, start_pos, end_pos)

	def enable_type_provenance(self) -> None:
		self._type_provenance_enabled = True

	def type_provenance_enabled(self) -> bool:
		return bool(self._type_provenance_enabled)

	def record_type_provenance(
		self,
		ty_id: TypeId | None,
		*,
		phase: str,
		kind: str,
		span: Span | None = None,
		note: str | None = None,
	) -> None:
		if not self._type_provenance_enabled:
			return
		if ty_id is None or not isinstance(ty_id, int) or ty_id <= 0:
			return
		self._type_provenance_counter += 1
		entry = TypeProvenanceEntry(
			phase=phase,
			kind=kind,
			span=span,
			note=note,
			order=self._type_provenance_counter,
		)
		self._type_provenance.setdefault(ty_id, []).append(entry)

	def provenance_for(self, ty_id: TypeId) -> list[TypeProvenanceEntry]:
		return list(self._type_provenance.get(ty_id, []))

	def audit_type_provenance(
		self,
		*,
		required: Iterable[TypeId],
	) -> tuple[list[TypeId], dict[str, int], dict[str, int]]:
		"""
		Return (missing, phase_counts, kind_counts) for required TypeIds.
		"""
		missing: list[TypeId] = []
		phase_counts: dict[str, int] = {}
		kind_counts: dict[str, int] = {}
		for tid in required:
			if tid is None or not isinstance(tid, int) or tid <= 0:
				continue
			entries = self._type_provenance.get(tid)
			if not entries:
				missing.append(tid)
				continue
			for entry in entries:
				phase_counts[entry.phase] = phase_counts.get(entry.phase, 0) + 1
				kind_counts[entry.kind] = kind_counts.get(entry.kind, 0) + 1
		return missing, phase_counts, kind_counts

	def define_const(self, *, module_id: str, name: str, type_id: TypeId, value: object) -> None:
		"""
		Define a compile-time constant.

		Constants are module-scoped; the fully-qualified symbol is derived from
		`module_id` and `name`.
		"""
		sym = f"{module_id}::{name}"
		self.consts[sym] = (type_id, value)

	def lookup_const(self, sym: str) -> tuple[TypeId, object] | None:
		"""Return (TypeId, value) for a fully-qualified const symbol."""
		return self.consts.get(sym)

	def define_type_alias(self, *, module_id: str | None, name: str, type_params: list[str], target: object, loc: object | None = None) -> None:
		"""Define a module-scoped type alias."""
		self.type_aliases[(module_id, name)] = (list(type_params), target, loc)

	def lookup_type_alias(self, *, module_id: str | None, name: str) -> tuple[list[str], object, object | None] | None:
		"""Return (type_params, target_type_expr, loc) for a module-scoped alias."""
		return self.type_aliases.get((module_id, name))

	def find_unique_type_alias_by_name(self, *, name: str) -> tuple[str | None, list[str], object, object | None] | None:
		"""Return a unique type alias match by name across modules, or None if ambiguous/missing."""
		found: tuple[str | None, list[str], object, object | None] | None = None
		for (mod, alias_name), payload in self.type_aliases.items():
			if alias_name != name:
				continue
			if found is not None and found[0] != mod:
				return None
			type_params, target, loc = payload
			found = (mod, list(type_params), target, loc)
		return found

	def _package_for_module(self, module_id: str | None) -> str | None:
		if module_id is None:
			return None
		return self.module_packages.get(module_id, self.package_id)

	def new_scalar(self, name: str) -> TypeId:
		"""Register a scalar type (e.g., Int, Bool) and return its TypeId."""
		return self._add(TypeKind.SCALAR, name, [])

	def ensure_named(self, name: str, *, module_id: str | None = None) -> TypeId:
		"""
		Return a stable TypeId for a forward-declared nominal name.

		This is used for user-defined type names that appear in annotations.
		If the name has not been declared yet, we create a forward nominal and
		expect a later declaration to upgrade it to STRUCT/VARIANT.
		"""
		for kind in (TypeKind.STRUCT, TypeKind.VARIANT, TypeKind.INTERFACE, TypeKind.FORWARD_NOMINAL, TypeKind.SCALAR):
			key = NominalKey(package_id=self._package_for_module(module_id), module_id=module_id, name=name, kind=kind)
			prev = self._nominal.get(key)
			if prev is not None:
				return prev
		return self._add(TypeKind.FORWARD_NOMINAL, name, [], module_id=module_id, register_named=True)

	def get_forward_nominal(self, *, module_id: str | None, name: str) -> TypeId | None:
		"""Return a forward nominal TypeId by identity, or None if not present."""
		return self._nominal.get(
			NominalKey(package_id=self._package_for_module(module_id), module_id=module_id, name=name, kind=TypeKind.FORWARD_NOMINAL)
		)

	def get_nominal(self, *, kind: TypeKind, module_id: str | None, name: str) -> TypeId | None:
		"""Return a nominal TypeId by identity, or None if not present."""
		key = NominalKey(package_id=self._package_for_module(module_id), module_id=module_id, name=name, kind=kind)
		return self._nominal.get(key)

	def require_nominal(self, *, kind: TypeKind, module_id: str | None, name: str) -> TypeId:
		"""Return a nominal TypeId by identity, raising if missing."""
		ty = self.get_nominal(kind=kind, module_id=module_id, name=name)
		if ty is None:
			raise ValueError(f"unknown nominal type {module_id or '<builtin>'}:{name} ({kind.name})")
		return ty

	def find_unique_nominal_by_name(self, *, kind: TypeKind, name: str) -> TypeId | None:
		"""
		Return the unique nominal TypeId matching (kind,name) across all modules.

		This is a convenience for MVP call sites that do not yet thread module
		context explicitly (e.g. local struct constructor typing in single-module
		builds). If more than one module defines the same nominal name, this
		returns None so callers can produce a clear “ambiguous; qualify it” error.
		"""
		found: TypeId | None = None
		for key, tid in self._nominal.items():
			if key.kind is kind and key.name == name:
				if found is not None and found != tid:
					return None
				found = tid
		return found

	def declare_struct(self, module_id: str, name: str, field_names: List[str], type_params: list[str] | None = None) -> TypeId:
		"""
		Declare a struct nominal type with placeholder field types.

		This supports recursive type references by first declaring all struct
		names, then filling field types in a second pass via `define_struct_fields`.
		"""
		type_params = list(type_params or [])
		key = NominalKey(package_id=self._package_for_module(module_id), module_id=module_id, name=name, kind=TypeKind.STRUCT)
		forward_key = NominalKey(
			package_id=self._package_for_module(module_id),
			module_id=module_id,
			name=name,
			kind=TypeKind.FORWARD_NOMINAL,
		)
		if forward_key in self._nominal:
			ty_id = self._nominal.pop(forward_key)
			td = self.get(ty_id)
			if td.kind is not TypeKind.FORWARD_NOMINAL:
				raise ValueError(f"type name '{name}' already defined as {td.kind}")
			unknown = self.ensure_unknown()
			placeholder = [unknown for _ in field_names]
			self._defs[ty_id] = TypeDef(
				kind=TypeKind.STRUCT,
				name=name,
				param_types=placeholder,
				module_id=module_id,
				field_names=list(field_names),
			)
			self._nominal[key] = ty_id
			self.struct_schemas[key] = (name, list(field_names))
			self.struct_bases[ty_id] = StructSchema(
				module_id=module_id,
				name=name,
				type_params=type_params,
				fields=[],
			)
			if ty_id not in self.struct_type_param_ids:
				owner = FunctionId(module="lang.__internal", name=f"__struct_{module_id}::{name}", ordinal=0)
				self.struct_type_param_ids[ty_id] = [
					TypeParamId(owner=owner, index=idx) for idx, _name in enumerate(type_params)
				]
			return ty_id
		if key in self._nominal:
			ty_id = self._nominal[key]
			td = self.get(ty_id)
			if td.kind is TypeKind.STRUCT:
				schema = self.struct_schemas.get(key)
				if schema is not None:
					_decl_name, decl_fields = schema
					if list(field_names) != list(decl_fields):
						raise ValueError(
							f"struct '{module_id}::{name}' field list mismatch: "
							f"{decl_fields} vs {field_names}"
						)
				base = self.struct_bases.get(ty_id)
				if base is not None and list(base.type_params) != list(type_params):
					raise ValueError(
						f"struct '{module_id}::{name}' type params mismatch: "
						f"{base.type_params} vs {type_params}"
					)
				return ty_id
			raise ValueError(f"type name '{name}' already defined as {td.kind}")
		unknown = self.ensure_unknown()
		placeholder = [unknown for _ in field_names]
		ty_id = self._add(TypeKind.STRUCT, name, placeholder, field_names=list(field_names), module_id=module_id)
		self.struct_schemas[key] = (name, list(field_names))
		self.struct_bases[ty_id] = StructSchema(
			module_id=module_id,
			name=name,
			type_params=type_params,
			fields=[],
		)
		if ty_id not in self.struct_type_param_ids:
			owner = FunctionId(module="lang.__internal", name=f"__struct_{module_id}::{name}", ordinal=0)
			self.struct_type_param_ids[ty_id] = [
				TypeParamId(owner=owner, index=idx) for idx, _name in enumerate(type_params)
			]
		return ty_id

	def declare_interface(self, module_id: str, name: str, type_params: list[str] | None = None) -> TypeId:
		"""Declare an interface nominal type."""
		type_params = list(type_params or [])
		key = NominalKey(package_id=self._package_for_module(module_id), module_id=module_id, name=name, kind=TypeKind.INTERFACE)
		forward_key = NominalKey(
			package_id=self._package_for_module(module_id),
			module_id=module_id,
			name=name,
			kind=TypeKind.FORWARD_NOMINAL,
		)
		if forward_key in self._nominal:
			ty_id = self._nominal.pop(forward_key)
			td = self.get(ty_id)
			if td.kind is not TypeKind.FORWARD_NOMINAL:
				raise ValueError(f"type name '{name}' already defined as {td.kind}")
			self._defs[ty_id] = TypeDef(
				kind=TypeKind.INTERFACE,
				name=name,
				param_types=[],
				module_id=module_id,
			)
			self._nominal[key] = ty_id
			self.interface_bases[ty_id] = InterfaceSchema(
				module_id=module_id,
				name=name,
				type_params=type_params,
				parents=[],
				parent_base_ids=[],
				methods=[],
			)
			if ty_id not in self.interface_type_param_ids:
				owner = FunctionId(module="lang.__internal", name=f"__interface_{module_id}::{name}", ordinal=0)
				self.interface_type_param_ids[ty_id] = [
					TypeParamId(owner=owner, index=idx) for idx, _name in enumerate(type_params)
				]
			return ty_id
		if key in self._nominal:
			ty_id = self._nominal[key]
			td = self.get(ty_id)
			if td.kind is TypeKind.INTERFACE:
				base = self.interface_bases.get(ty_id)
				if base is not None and list(base.type_params) != list(type_params):
					raise ValueError(
						f"interface '{module_id}::{name}' type params mismatch: "
						f"{base.type_params} vs {type_params}"
					)
				return ty_id
			raise ValueError(f"type name '{name}' already defined as {td.kind}")
		ty_id = self._add(TypeKind.INTERFACE, name, [], register_named=True, module_id=module_id)
		self.interface_bases[ty_id] = InterfaceSchema(
			module_id=module_id,
			name=name,
			type_params=type_params,
			parents=[],
			parent_base_ids=[],
			methods=[],
		)
		if ty_id not in self.interface_type_param_ids:
			owner = FunctionId(module="lang.__internal", name=f"__interface_{module_id}::{name}", ordinal=0)
			self.interface_type_param_ids[ty_id] = [
				TypeParamId(owner=owner, index=idx) for idx, _name in enumerate(type_params)
			]
		return ty_id

	def declare_variant(
		self,
		module_id: str,
		name: str,
		type_params: list[str],
		arms: list[VariantArmSchema],
		*,
		tombstone_ctor: str | None = None,
	) -> TypeId:
		"""
		Declare a variant nominal type (generic or non-generic).

		The returned `TypeId` is the *base* type for the declared name.
		Concrete instantiations are created via `ensure_variant_instantiated`.

		For non-generic variants (`type_params == []`), the base type is also a
		concrete instantiation with zero type arguments, and is available via
		`get_variant_instance(base_id)`.
		"""
		if tombstone_ctor is not None:
			tombstone_arm = next((arm for arm in arms if arm.name == tombstone_ctor), None)
			if tombstone_arm is None:
				raise ValueError(f"tombstone_ctor '{tombstone_ctor}' missing in variant '{name}'")
			if tombstone_arm.fields:
				raise ValueError(
					f"tombstone_ctor '{tombstone_ctor}' for variant '{name}' must have no payload"
				)
		key = NominalKey(package_id=self._package_for_module(module_id), module_id=module_id, name=name, kind=TypeKind.VARIANT)
		forward_key = NominalKey(
			package_id=self._package_for_module(module_id),
			module_id=module_id,
			name=name,
			kind=TypeKind.FORWARD_NOMINAL,
		)
		if forward_key in self._nominal:
			base_id = self._nominal.pop(forward_key)
			td = self.get(base_id)
			if td.kind is not TypeKind.FORWARD_NOMINAL:
				raise ValueError(f"type name '{name}' already defined as {td.kind}")
			self._defs[base_id] = TypeDef(
				kind=TypeKind.VARIANT,
				name=name,
				param_types=[],
				module_id=module_id,
			)
			self._nominal[key] = base_id
			self.variant_schemas[base_id] = VariantSchema(
				module_id=module_id,
				name=name,
				type_params=list(type_params),
				arms=list(arms),
				tombstone_ctor=tombstone_ctor,
			)
			return base_id
		if key in self._nominal:
			ty_id = self._nominal[key]
			td = self.get(ty_id)
			if td.kind is TypeKind.VARIANT:
				schema = self.variant_schemas.get(ty_id)
				if schema is not None:
					if (
						list(schema.type_params) != list(type_params)
						or list(schema.arms) != list(arms)
						or schema.tombstone_ctor != tombstone_ctor
					):
						raise ValueError(
							f"variant '{module_id}::{name}' schema mismatch: "
							f"params {schema.type_params} vs {type_params}, arms {schema.arms} vs {arms}"
						)
				return ty_id
			raise ValueError(f"type name '{name}' already defined as {td.kind}")
		# Base variant type. Note: base is named; instantiations are not.
		base_id = self._add(TypeKind.VARIANT, name, [], register_named=True, module_id=module_id)
		self.variant_schemas[base_id] = VariantSchema(
			module_id=module_id,
			name=name,
			type_params=list(type_params),
			arms=list(arms),
			tombstone_ctor=tombstone_ctor,
		)
		return base_id

	def declare_scalar(self, module_id: str, name: str) -> TypeId:
		"""Declare a module-scoped scalar nominal."""
		key = NominalKey(package_id=self._package_for_module(module_id), module_id=module_id, name=name, kind=TypeKind.SCALAR)
		forward_key = NominalKey(
			package_id=self._package_for_module(module_id),
			module_id=module_id,
			name=name,
			kind=TypeKind.FORWARD_NOMINAL,
		)
		if forward_key in self._nominal:
			ty_id = self._nominal.pop(forward_key)
			td = self.get(ty_id)
			if td.kind is not TypeKind.FORWARD_NOMINAL:
				raise ValueError(f"type name '{name}' already defined as {td.kind}")
			self._defs[ty_id] = TypeDef(kind=TypeKind.SCALAR, name=name, param_types=[], module_id=module_id)
			self._nominal[key] = ty_id
			return ty_id
		if key in self._nominal:
			ty_id = self._nominal[key]
			td = self.get(ty_id)
			if td.kind is TypeKind.SCALAR:
				return ty_id
			raise ValueError(f"type name '{name}' already defined as {td.kind}")
		return self._add(TypeKind.SCALAR, name, [], module_id=module_id)

	def define_struct_schema_fields(self, struct_id: TypeId, fields: list[StructFieldSchema]) -> None:
		"""Define struct schema fields (template types) for a declared struct base."""
		schema = self.struct_bases.get(struct_id)
		if schema is None:
			raise ValueError("define_struct_schema_fields requires a declared struct base TypeId")
		td = self.get(struct_id)
		if td.field_names is None:
			raise ValueError("define_struct_schema_fields requires struct field names")
		field_names = [f.name for f in fields]
		if len(field_names) != len(td.field_names) or field_names != list(td.field_names):
			raise ValueError("define_struct_schema_fields field names mismatch")
		self.struct_bases[struct_id] = StructSchema(
			module_id=schema.module_id,
			name=schema.name,
			type_params=list(schema.type_params),
			fields=list(fields),
		)

	def get_struct_schema(self, ty: TypeId) -> StructSchema | None:
		"""Return the struct schema for a base or instantiated struct TypeId."""
		td = self.get(ty)
		if td.kind is not TypeKind.STRUCT:
			return None
		if ty in self.struct_bases:
			return self.struct_bases[ty]
		inst = self.struct_instances.get(ty)
		if inst is not None:
			return self.struct_bases.get(inst.base_id)
		return None

	def get_struct_type_param_ids(self, base_id: TypeId) -> list[TypeParamId] | None:
		"""Return struct type parameter ids for a base TypeId, if known."""
		return self.struct_type_param_ids.get(base_id)

	def get_struct_base(self, *, module_id: str, name: str) -> TypeId | None:
		"""Return the base TypeId for a declared struct in `module_id`, if present."""
		tid = self.get_nominal(kind=TypeKind.STRUCT, module_id=module_id, name=name)
		if tid is None:
			return None
		return tid if tid in self.struct_bases else None

	def get_struct_instance(self, ty: TypeId) -> StructInstance | None:
		"""Return the concrete struct instance for a struct TypeId, if available."""
		return self.struct_instances.get(ty)

	def get_interface_schema(self, ty: TypeId) -> InterfaceSchema | None:
		"""Return the interface schema for a base or instantiated interface TypeId."""
		td = self.get(ty)
		if td.kind is not TypeKind.INTERFACE:
			return None
		if ty in self.interface_bases:
			return self.interface_bases[ty]
		inst = self.interface_instances.get(ty)
		if inst is not None:
			return self.interface_bases.get(inst.base_id)
		return None

	def define_interface_schema_methods(
		self,
		interface_id: TypeId,
		methods: list[InterfaceMethodSchema],
		*,
		parents: list[GenericTypeExpr] | None = None,
		parent_base_ids: list[TypeId] | None = None,
	) -> None:
		"""Define interface method schemas for a declared interface base."""
		schema = self.interface_bases.get(interface_id)
		if schema is None:
			raise ValueError("define_interface_schema_methods requires a declared interface base TypeId")
		existing = list(schema.methods)
		if existing:
			if list(existing) != list(methods):
				raise ValueError(
					f"interface '{schema.module_id}::{schema.name}' method schema mismatch"
				)
			if parents is not None and list(getattr(schema, "parents", []) or []) != list(parents):
				raise ValueError(
					f"interface '{schema.module_id}::{schema.name}' parent schema mismatch"
				)
			return
		seen: set[str] = set()
		for m in methods:
			if m.name in seen:
				raise ValueError(f"interface '{schema.module_id}::{schema.name}' has duplicate method '{m.name}'")
			seen.add(m.name)
		self.interface_bases[interface_id] = InterfaceSchema(
			module_id=schema.module_id,
			name=schema.name,
			type_params=list(schema.type_params),
			parents=list(parents or []),
			parent_base_ids=list(parent_base_ids or []),
			methods=list(methods),
		)
		# Reject ambiguous method names across parents (and parent vs child).
		base_schema = self.interface_bases.get(interface_id)
		if base_schema is not None:
			seen: dict[str, TypeId] = {}
			linear = self.interface_linearization(interface_id)
			for tid in linear:
				parent_schema = self.interface_bases.get(tid)
				for m in list(getattr(parent_schema, "methods", []) or []):
					prev = seen.get(m.name)
					if prev is None:
						seen[m.name] = tid
						continue
					if prev == tid:
						continue
					prev_schema = self.interface_bases.get(prev)
					prev_name = f"{prev_schema.module_id}::{prev_schema.name}" if prev_schema else str(prev)
					cur_name = f"{parent_schema.module_id}::{parent_schema.name}" if parent_schema else str(tid)
					raise ValueError(
						f"interface '{base_schema.module_id}::{base_schema.name}' inherits duplicate method '{m.name}' from '{prev_name}' and '{cur_name}'"
					)
		# Parent/method changes invalidate cached linearization/offsets.
		self._interface_linearization_cache.pop(interface_id, None)
		self._interface_segment_offsets_cache.pop(interface_id, None)

	def interface_linearization(self, base_id: TypeId) -> list[TypeId]:
		"""
		Return deterministic linearization for an interface base.

		Linearization rule:
		- [base_id] + topological order of unique ancestors
		- primary tie-break: first-seen order from declared parent traversal
		- secondary tie-break: fully-qualified interface name
		"""
		cached = self._interface_linearization_cache.get(base_id)
		if cached is not None:
			return list(cached)
		if base_id not in self.interface_bases:
			raise ValueError("interface_linearization requires an interface base id")
		ancestors: set[TypeId] = set()
		first_seen: dict[TypeId, int] = {}
		visiting: set[TypeId] = set()
		visited: set[TypeId] = set()

		def _walk(cur: TypeId) -> None:
			if cur in visiting:
				raise ValueError("interface inheritance cycle detected")
			if cur in visited:
				return
			visiting.add(cur)
			schema = self.interface_bases.get(cur)
			for parent_id in list(getattr(schema, "parent_base_ids", []) or []):
				if parent_id not in first_seen:
					first_seen[parent_id] = len(first_seen)
				ancestors.add(parent_id)
				_walk(parent_id)
			visiting.remove(cur)
			visited.add(cur)

		_walk(base_id)
		if not ancestors:
			out = [base_id]
			self._interface_linearization_cache[base_id] = list(out)
			return list(out)

		def _iface_key(tid: TypeId) -> tuple[int, str]:
			order = first_seen.get(tid, 1 << 30)
			schema = self.interface_bases.get(tid)
			if schema is None:
				return (order, "")
			mod = schema.module_id or ""
			return (order, f"{mod}::{schema.name}")

		# Build child->parent edges within ancestor set.
		edges: dict[TypeId, set[TypeId]] = {tid: set() for tid in ancestors}
		indegree: dict[TypeId, int] = {tid: 0 for tid in ancestors}
		for child in list(ancestors):
			schema = self.interface_bases.get(child)
			for parent in list(getattr(schema, "parent_base_ids", []) or []):
				if parent not in ancestors:
					continue
				if parent in edges[child]:
					continue
				edges[child].add(parent)
				indegree[parent] += 1

		ready = [tid for tid, deg in indegree.items() if deg == 0]
		ready.sort(key=_iface_key)
		order: list[TypeId] = []
		while ready:
			cur = ready.pop(0)
			order.append(cur)
			for parent in sorted(edges.get(cur, set()), key=_iface_key):
				indegree[parent] -= 1
				if indegree[parent] == 0:
					ready.append(parent)
			ready.sort(key=_iface_key)
		if len(order) != len(ancestors):
			raise ValueError("interface inheritance cycle detected")
		out = [base_id] + order
		self._interface_linearization_cache[base_id] = list(out)
		return list(out)

	def interface_segment_offsets(self, base_id: TypeId) -> dict[TypeId, int]:
		cached = self._interface_segment_offsets_cache.get(base_id)
		if cached is not None:
			return dict(cached)
		linear = self.interface_linearization(base_id)
		offsets: dict[TypeId, int] = {}
		cur = 0
		for tid in linear:
			offsets[tid] = cur
			schema = self.interface_bases.get(tid)
			method_count = len(getattr(schema, "methods", []) or [])
			cur += 1 + method_count
		self._interface_segment_offsets_cache[base_id] = dict(offsets)
		return dict(offsets)

	def interface_method_lookup(self, base_id: TypeId, method_name: str) -> tuple[TypeId, InterfaceMethodSchema]:
		linear = self.interface_linearization(base_id)
		for tid in linear:
			schema = self.interface_bases.get(tid)
			for m in list(getattr(schema, "methods", []) or []):
				if m.name == method_name:
					return tid, m
		raise KeyError(method_name)

	def interface_method_vtable_slot(self, receiver_base: TypeId, owner_base: TypeId, method_name: str) -> int:
		schema = self.interface_bases.get(owner_base)
		if schema is None:
			raise ValueError("interface method owner schema missing")
		methods = list(getattr(schema, "methods", []) or [])
		for idx, m in enumerate(methods):
			if m.name == method_name:
				return 1 + idx
		raise ValueError("interface method not found in owner schema")

	def interface_instance_view_map(self, iface_ty: TypeId) -> dict[TypeId, TypeId]:
		"""
		Return base_id -> concrete interface TypeId for a given interface instance.
		"""
		inst = self.get_interface_instance(iface_ty)
		base_id = inst.base_id if inst is not None else iface_ty
		type_args = list(inst.type_args) if inst is not None else []
		out: dict[TypeId, TypeId] = {}
		visiting: set[TypeId] = set()

		def _walk(cur_base: TypeId, cur_args: list[TypeId]) -> None:
			if cur_base in visiting:
				raise ValueError("interface inheritance cycle detected")
			if cur_base in out:
				return
			visiting.add(cur_base)
			cur_inst = self.ensure_interface_instantiated(cur_base, cur_args)
			out[cur_base] = cur_inst
			schema = self.interface_bases.get(cur_base)
			for parent_expr in list(getattr(schema, "parents", []) or []):
				parent_ty = self._eval_generic_type_expr(parent_expr, cur_args, module_id=schema.module_id)
				parent_inst = self.get_interface_instance(parent_ty)
				parent_base = parent_inst.base_id if parent_inst is not None else parent_ty
				parent_args = list(parent_inst.type_args) if parent_inst is not None else []
				_walk(parent_base, parent_args)
			visiting.remove(cur_base)

		_walk(base_id, type_args)
		return out

	def get_interface_type_param_ids(self, base_id: TypeId) -> list[TypeParamId] | None:
		"""Return interface type parameter ids for a base TypeId, if known."""
		return self.interface_type_param_ids.get(base_id)

	def get_interface_base(self, *, module_id: str, name: str) -> TypeId | None:
		"""Return the base TypeId for a declared interface in `module_id`, if present."""
		tid = self.get_nominal(kind=TypeKind.INTERFACE, module_id=module_id, name=name)
		if tid is None:
			return None
		return tid if tid in self.interface_bases else None

	def get_interface_instance(self, ty: TypeId) -> InterfaceInstance | None:
		"""Return the concrete interface instance for an interface TypeId, if available."""
		return self.interface_instances.get(ty)

	def ensure_struct_instantiated(self, base_id: TypeId, type_args: list[TypeId]) -> TypeId:
		"""
		Return a stable TypeId for a concrete instantiation of a generic struct.

		Instances must be fully concrete (no TypeVar).
		"""
		schema = self.struct_bases.get(base_id)
		if schema is None:
			raise ValueError("ensure_struct_instantiated requires a declared struct base TypeId")
		if len(type_args) != len(schema.type_params):
			raise ValueError(
				f"type argument count mismatch for '{schema.name}': expected {len(schema.type_params)}, got {len(type_args)}"
			)
		if any(self.has_typevar(arg) for arg in type_args):
			raise ValueError(f"type arguments for '{schema.name}' must be concrete")
		if not schema.type_params:
			if base_id not in self.struct_instances:
				field_names = [f.name for f in schema.fields]
				field_types = list(self.get(base_id).param_types)
				self._define_struct_instance(base_id, base_id, type_args=[], field_names=field_names, field_types=field_types)
			return base_id
		key = (base_id, tuple(type_args))
		if key in self._struct_instantiation_cache:
			return self._struct_instantiation_cache[key]
		inst_id = self._add(
			TypeKind.STRUCT,
			schema.name,
			[],
			register_named=False,
			module_id=schema.module_id,
			field_names=[f.name for f in schema.fields],
		)
		self._struct_instantiation_cache[key] = inst_id
		field_names = [f.name for f in schema.fields]
		field_types = [self._eval_generic_type_expr(f.type_expr, type_args, module_id=schema.module_id) for f in schema.fields]
		self._define_struct_instance(base_id, inst_id, type_args=list(type_args), field_names=field_names, field_types=field_types)
		return inst_id

	def ensure_struct_template(self, base_id: TypeId, type_args: list[TypeId]) -> TypeId:
		"""
		Return a template struct TypeId that may include TypeVar arguments.

		This is used when resolving type annotations that mention impl type
		parameters (e.g., impl<T> Box<T>).
		"""
		schema = self.struct_bases.get(base_id)
		if schema is None:
			raise ValueError("ensure_struct_template requires a declared struct base TypeId")
		if len(type_args) != len(schema.type_params):
			raise ValueError(
				f"type argument count mismatch for '{schema.name}': expected {len(schema.type_params)}, got {len(type_args)}"
			)
		key = (base_id, tuple(type_args))
		if key in self._struct_template_cache:
			return self._struct_template_cache[key]
		inst_id = self._add(
			TypeKind.STRUCT,
			schema.name,
			[],
			register_named=False,
			module_id=schema.module_id,
			field_names=[f.name for f in schema.fields],
		)
		self._struct_template_cache[key] = inst_id
		field_names = [f.name for f in schema.fields]
		field_types = [self._eval_generic_type_expr(f.type_expr, type_args, module_id=schema.module_id) for f in schema.fields]
		self._define_struct_instance(base_id, inst_id, type_args=list(type_args), field_names=field_names, field_types=field_types)
		return inst_id

	def ensure_interface_instantiated(self, base_id: TypeId, type_args: list[TypeId]) -> TypeId:
		"""
		Return a stable TypeId for a concrete instantiation of a generic interface.
		"""
		schema = self.interface_bases.get(base_id)
		if schema is None:
			raise ValueError("ensure_interface_instantiated requires a declared interface base TypeId")
		if len(type_args) != len(schema.type_params):
			raise ValueError(
				f"type argument count mismatch for '{schema.name}': expected {len(schema.type_params)}, got {len(type_args)}"
			)
		if any(self.has_typevar(arg) for arg in type_args):
			raise ValueError(f"type arguments for '{schema.name}' must be concrete")
		if not schema.type_params:
			if base_id not in self.interface_instances:
				self.interface_instances[base_id] = InterfaceInstance(base_id=base_id, type_args=[])
			return base_id
		key = (base_id, tuple(type_args))
		if key in self._interface_instantiation_cache:
			return self._interface_instantiation_cache[key]
		inst_id = self._add(
			TypeKind.INTERFACE,
			schema.name,
			[],
			register_named=False,
			module_id=schema.module_id,
		)
		self._interface_instantiation_cache[key] = inst_id
		self.interface_instances[inst_id] = InterfaceInstance(base_id=base_id, type_args=list(type_args))
		return inst_id

	def ensure_interface_template(self, base_id: TypeId, type_args: list[TypeId]) -> TypeId:
		"""
		Return a template interface TypeId that may include TypeVar arguments.
		"""
		schema = self.interface_bases.get(base_id)
		if schema is None:
			raise ValueError("ensure_interface_template requires a declared interface base TypeId")
		if len(type_args) != len(schema.type_params):
			raise ValueError(
				f"type argument count mismatch for '{schema.name}': expected {len(schema.type_params)}, got {len(type_args)}"
			)
		key = (base_id, tuple(type_args))
		if key in self._interface_template_cache:
			return self._interface_template_cache[key]
		inst_id = self._add(
			TypeKind.INTERFACE,
			schema.name,
			[],
			register_named=False,
			module_id=schema.module_id,
		)
		self._interface_template_cache[key] = inst_id
		self.interface_instances[inst_id] = InterfaceInstance(base_id=base_id, type_args=list(type_args))
		return inst_id

	def ensure_variant_instantiated(self, base_id: TypeId, type_args: list[TypeId]) -> TypeId:
		"""
		Return a stable TypeId for a concrete instantiation of a generic nominal.

		MVP: only variants are instantiable. This is enforced by schema presence.
		"""
		schema = self.variant_schemas.get(base_id)
		if schema is None:
			raise ValueError("ensure_variant_instantiated requires a declared generic variant base TypeId")
		if len(type_args) != len(schema.type_params):
			raise ValueError(
				f"type argument count mismatch for '{schema.name}': expected {len(schema.type_params)}, got {len(type_args)}"
			)
		if any(self.has_typevar(arg) for arg in type_args):
			raise ValueError(f"type arguments for '{schema.name}' must be concrete")
		if not schema.type_params:
			# Non-generic variants use the base id directly. Ensure its concrete
			# instance exists (it may be created lazily after all variants are
			# declared to support mutual references between non-generic variants).
			if base_id not in self.variant_instances:
				self._define_variant_instance(base_id, base_id, [])
			return base_id
		key = (base_id, tuple(type_args))
		if key in self._instantiation_cache:
			inst_id = self._instantiation_cache[key]
			if inst_id not in self.variant_instances:
				self._define_variant_instance(base_id, inst_id, list(type_args))
			return inst_id
		# Concrete instantiations are not nominal (not registered in `_nominal`),
		# but they still carry the originating module id for deterministic package
		# encoding/linking and for clearer diagnostics.
		inst_id = self._add(
			TypeKind.VARIANT,
			schema.name,
			list(type_args),
			register_named=False,
			module_id=schema.module_id,
		)
		self._instantiation_cache[key] = inst_id
		self._define_variant_instance(base_id, inst_id, list(type_args))
		return inst_id

	def ensure_variant_template(self, base_id: TypeId, type_args: list[TypeId]) -> TypeId:
		"""
		Return a template variant TypeId that may include TypeVar arguments.

		Template instances are used during generic validation/inference.
		"""
		schema = self.variant_schemas.get(base_id)
		if schema is None:
			raise ValueError("ensure_variant_template requires a declared generic variant base TypeId")
		if len(type_args) != len(schema.type_params):
			raise ValueError(
				f"type argument count mismatch for '{schema.name}': expected {len(schema.type_params)}, got {len(type_args)}"
			)
		key = (base_id, tuple(type_args))
		if key in self._variant_template_cache:
			return self._variant_template_cache[key]
		inst_id = self._add(
			TypeKind.VARIANT,
			schema.name,
			list(type_args),
			register_named=False,
			module_id=schema.module_id,
		)
		self._variant_template_cache[key] = inst_id
		self._define_variant_instance(base_id, inst_id, list(type_args))
		return inst_id

	def ensure_instantiated(self, base_id: TypeId, type_args: list[TypeId]) -> TypeId:
		"""Deprecated alias for ensure_variant_instantiated (variants only)."""
		return self.ensure_variant_instantiated(base_id, type_args)

	def has_typevar(self, ty: TypeId) -> bool:
		"""Return True if `ty` contains any TypeVar (deep)."""
		td = self.get(ty)
		if td.kind is TypeKind.TYPEVAR:
			return True
		if td.kind is TypeKind.STRUCT:
			inst = self.struct_instances.get(ty)
			if inst is not None:
				return any(self.has_typevar(arg) for arg in inst.type_args)
		if td.kind is TypeKind.VARIANT:
			inst = self.variant_instances.get(ty)
			if inst is not None:
				return any(self.has_typevar(arg) for arg in inst.type_args)
		if td.kind is TypeKind.INTERFACE:
			inst = self.interface_instances.get(ty)
			if inst is not None:
				return any(self.has_typevar(arg) for arg in inst.type_args)
		for child in td.param_types:
			if self.has_typevar(child):
				return True
		return False

	def finalize_variants(self) -> None:
		"""
		Finalize variant declarations after all variant names/schemas are known.

		This currently creates concrete instances for non-generic variants so
		constructors and `match` can resolve arm field types without requiring an
		explicit `ensure_instantiated(base, [])` call.

		We defer this for correctness: a non-generic variant may reference another
		variant declared later in the file, so instantiating while still parsing
		can create placeholder types.
		"""
		# Determinism requirement: instantiation of non-generic variants must not
		# depend on dictionary insertion order (e.g. package discovery order).
		#
		# We create concrete instances in canonical (module_id,name) order so TypeId
		# assignment for any derived types instantiated during field evaluation is
		# stable across runs.
		items = [(base_id, schema) for base_id, schema in self.variant_schemas.items() if not schema.type_params]
		items.sort(key=lambda kv: (kv[1].module_id, kv[1].name))
		# Variant payload field types can depend on aliases declared in other modules.
		# Rebuild non-generic instances after declaration-finalization so any early
		# placeholder/Unknown payloads are refreshed with fully-resolved types.
		self._needs_drop_cache.clear()
		for base_id, schema in items:
			self._define_variant_instance(base_id, base_id, [])

	def get_variant_schema(self, ty: TypeId) -> VariantSchema | None:
		"""Return the variant schema for a base or instantiated variant TypeId."""
		td = self.get(ty)
		if td.kind is not TypeKind.VARIANT:
			return None
		# A concrete instantiation stores no separate base id in TypeDef; we look
		# up by (ty) first and fall back to "ty is base".
		if ty in self.variant_schemas:
			return self.variant_schemas[ty]
		# If `ty` is an instantiation, find its base by searching the instances.
		inst = self.variant_instances.get(ty)
		if inst is not None:
			return self.variant_schemas.get(inst.base_id)
		return None

	def get_variant_instance(self, ty: TypeId) -> VariantInstance | None:
		"""Return the concrete variant instance for a variant TypeId, if available."""
		return self.variant_instances.get(ty)

	def is_variant_base_named(self, name: str) -> bool:
		"""Return True if `name` refers to any declared variant base (any module)."""
		for key, tid in self._nominal.items():
			if key.kind is TypeKind.VARIANT and key.name == name and tid in self.variant_schemas:
				return True
		return False

	def get_variant_base(self, *, module_id: str, name: str) -> TypeId | None:
		"""Return the base TypeId for a declared variant in `module_id`, if present."""
		tid = self.get_nominal(kind=TypeKind.VARIANT, module_id=module_id, name=name)
		if tid is None:
			return None
		return tid if tid in self.variant_schemas else None

	def _define_variant_instance(self, base_id: TypeId, inst_id: TypeId, type_args: list[TypeId]) -> None:
		"""
		Create and store a concrete `VariantInstance` for `inst_id`.

		This evaluates arm field types by substituting generic type parameters
		according to `type_args`.
		"""
		schema = self.variant_schemas[base_id]
		arms: list[VariantArmInstance] = []
		by_name: dict[str, VariantArmInstance] = {}
		for tag, arm in enumerate(schema.arms):
			field_names = [f.name for f in arm.fields]
			field_types = [self._eval_generic_type_expr(f.type_expr, type_args, module_id=schema.module_id) for f in arm.fields]
			arm_inst = VariantArmInstance(tag=tag, name=arm.name, field_names=field_names, field_types=field_types)
			arms.append(arm_inst)
			by_name[arm.name] = arm_inst
		needs_drop = any(self.has_drop(fty) for arm in arms for fty in arm.field_types)
		internal_tombstone_ctor: str | None = None
		internal_tombstone_tag: int | None = None
		if needs_drop:
			ctor = schema.tombstone_ctor
			if ctor:
				tombstone_arm = by_name.get(ctor)
				if tombstone_arm is None:
					raise ValueError(f"tombstone_ctor '{ctor}' missing in variant '{schema.name}'")
				if tombstone_arm.field_types:
					raise ValueError(
						f"tombstone_ctor '{ctor}' for variant '{schema.name}' must have no payload"
					)
				for fty in tombstone_arm.field_types:
					if self.has_drop(fty):
						raise ValueError(
							f"tombstone_ctor '{ctor}' for variant '{schema.name}' must be non-droppable"
						)
				internal_tombstone_ctor = ctor
				internal_tombstone_tag = tombstone_arm.tag
			else:
				internal_tombstone_ctor = "__drift_internal_tombstone"
				internal_tombstone_tag = len(arms)
		self.variant_instances[inst_id] = VariantInstance(
			base_id=base_id,
			type_args=list(type_args),
			arms=arms,
			arms_by_name=by_name,
			internal_tombstone_ctor=internal_tombstone_ctor,
			internal_tombstone_tag=internal_tombstone_tag,
		)

	def has_drop(self, tid: TypeId) -> bool:
		cached = self._needs_drop_cache.get(tid)
		if cached is not None:
			return cached
		td = self.get(tid)
		if td.kind in {TypeKind.REF, TypeKind.RAW_PTR, TypeKind.FUNCTION}:
			self._needs_drop_cache[tid] = False
			return False
		if self.is_destructible(tid):
			self._needs_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.SCALAR:
			needs = td.name == "String"
			self._needs_drop_cache[tid] = needs
			return needs
		if td.kind is TypeKind.ERROR:
			self._needs_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.DIAGNOSTICVALUE:
			self._needs_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.INTERFACE:
			self._needs_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.ARRAY:
			self._needs_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.STRUCT:
			inst = self.get_struct_instance(tid)
			if inst is None:
				return False
			needs = any(self.has_drop(fty) for fty in inst.field_types)
			self._needs_drop_cache[tid] = needs
			return needs
		if td.kind is TypeKind.VARIANT:
			inst = self.get_variant_instance(tid)
			if inst is None:
				return False
			needs = any(self.has_drop(fty) for arm in inst.arms for fty in arm.field_types)
			self._needs_drop_cache[tid] = needs
			return needs
		if td.param_types:
			needs = any(self.has_drop(pt) for pt in td.param_types)
			self._needs_drop_cache[tid] = needs
			return needs
		self._needs_drop_cache[tid] = False
		return False

	def _type_needs_drop(self, tid: TypeId) -> bool:
		return self.has_drop(tid)

	def _define_struct_instance(
		self,
		base_id: TypeId,
		inst_id: TypeId,
		*,
		type_args: list[TypeId],
		field_names: list[str],
		field_types: list[TypeId],
	) -> None:
		"""Create and store a concrete StructInstance for `inst_id`."""
		self.struct_instances[inst_id] = StructInstance(
			base_id=base_id,
			type_args=list(type_args),
			field_names=list(field_names),
			field_types=list(field_types),
			fields_by_name={name: idx for idx, name in enumerate(field_names)},
		)

	def _eval_generic_type_expr(self, expr: GenericTypeExpr, type_args: list[TypeId], *, module_id: str) -> TypeId:
		"""
		Evaluate a schema-time `GenericTypeExpr` into a concrete `TypeId`.

		This is used when instantiating variants.

		Supported type constructors (MVP):
		- type parameters (`T`) by index,
		- references (`&T`, `&mut T`),
		- `Array<T>`,
		- nominal names (structs, variants) including instantiation `Name<A,B>`.
		"""
		if expr.param_index is not None:
			idx = int(expr.param_index)
			if idx < 0 or idx >= len(type_args):
				return self.ensure_unknown()
			return type_args[idx]
		name = expr.name
		# Builtins are toolchain-owned and are not module-scoped.
		if name == "Int":
			return self.ensure_int()
		if name == "Uint":
			return self.ensure_uint()
		if name in ("Uint64", "u64"):
			return self.ensure_uint64()
		if name == "Byte":
			return self.ensure_byte()
		if name == "Bool":
			return self.ensure_bool()
		if name == "Float":
			return self.ensure_float()
		if name == "String":
			return self.ensure_string()
		if name == "Void":
			return self.ensure_void()
		if name == "Error":
			return self.ensure_error()
		if name == "DiagnosticValue":
			return self.ensure_diagnostic_value()
		if name == "Unknown":
			return self.ensure_unknown()
		# Reference constructors as produced by the parser (`&` / `&mut`).
		if name in {"&", "&mut"} and expr.args:
			inner = self._eval_generic_type_expr(expr.args[0], type_args, module_id=module_id)
			return self.ensure_ref_mut(inner) if name == "&mut" else self.ensure_ref(inner)
		if name == "Array" and expr.args:
			elem = self._eval_generic_type_expr(expr.args[0], type_args, module_id=module_id)
			return self.new_array(elem)
		if name == "Ptr":
			origin_mod = expr.module_id or module_id
			if origin_mod != "std.mem":
				return self.ensure_unknown()
			if not expr.args:
				return self.ensure_unknown()
			inner = self._eval_generic_type_expr(expr.args[0], type_args, module_id=module_id)
			return self.new_ptr(inner, module_id=origin_mod)
		# Module-scoped type aliases may appear in schema-time expressions
		# (for example, variant payload fields using `containers.HashMap<...>`).
		# Expand aliases here so instantiations do not degrade to Unknown.
		origin_mod = expr.module_id or module_id
		alias_def = self.lookup_type_alias(module_id=origin_mod, name=name)
		if alias_def is None and expr.module_id is None:
			unique_alias = self.find_unique_type_alias_by_name(name=name)
			if unique_alias is not None:
				origin_mod, alias_params_u, alias_target_u, alias_loc_u = unique_alias
				alias_def = (alias_params_u, alias_target_u, alias_loc_u)
		if alias_def is not None:
			alias_params, alias_target, _loc = alias_def
			if len(expr.args) != len(alias_params):
				return self.ensure_unknown()
			try:
				from lang.driftc.core.type_resolve_common import resolve_opaque_type
			except Exception:
				return self.ensure_unknown()
			bindings: dict[str, TypeId] = {}
			for idx, param_name in enumerate(alias_params):
				bindings[param_name] = self._eval_generic_type_expr(expr.args[idx], type_args, module_id=module_id)
			return resolve_opaque_type(
				alias_target,
				self,
				module_id=origin_mod,
				type_params=bindings,
				allow_generic_base=True,
			)
		# Named nominal types: either a simple name, or a generic instantiation.
		#
		# The `module_id` on `GenericTypeExpr` is a resolved canonical origin module
		# for imported/qualified names. If absent, the name is unqualified and is
		# resolved in the declaring module scope (`module_id` argument).
		# MVP supports structs, variants, and interfaces as nominal names.
		base_id = (
			self.get_nominal(kind=TypeKind.STRUCT, module_id=origin_mod, name=name)
			or self.get_nominal(kind=TypeKind.VARIANT, module_id=origin_mod, name=name)
			or self.get_nominal(kind=TypeKind.INTERFACE, module_id=origin_mod, name=name)
			or self.ensure_named(name, module_id=origin_mod)
		)
		if self.get(base_id).kind is TypeKind.FORWARD_NOMINAL and expr.module_id is None:
			unique = (
				self.find_unique_nominal_by_name(kind=TypeKind.STRUCT, name=name)
				or self.find_unique_nominal_by_name(kind=TypeKind.VARIANT, name=name)
				or self.find_unique_nominal_by_name(kind=TypeKind.INTERFACE, name=name)
			)
			if unique is not None:
				base_id = unique
		if expr.args:
			arg_ids = [self._eval_generic_type_expr(a, type_args, module_id=module_id) for a in expr.args]
			if base_id in self.variant_schemas:
				schema = self.variant_schemas.get(base_id)
				if schema is not None and not schema.type_params:
					return self.ensure_unknown()
				if any(self.has_typevar(arg) for arg in arg_ids):
					return self.ensure_variant_template(base_id, arg_ids)
				return self.ensure_instantiated(base_id, arg_ids)
			if base_id in self.struct_bases:
				schema = self.struct_bases.get(base_id)
				if schema is not None and not schema.type_params:
					return self.ensure_unknown()
				if any(self.has_typevar(arg) for arg in arg_ids):
					return self.ensure_struct_template(base_id, arg_ids)
				return self.ensure_struct_instantiated(base_id, arg_ids)
			if base_id in self.interface_bases:
				schema = self.interface_bases.get(base_id)
				if schema is not None and not schema.type_params:
					return self.ensure_unknown()
				if any(self.has_typevar(arg) for arg in arg_ids):
					return self.ensure_interface_template(base_id, arg_ids)
				return self.ensure_interface_instantiated(base_id, arg_ids)
			return self.ensure_unknown()
		return base_id

	def define_struct_fields(self, struct_id: TypeId, field_types: List[TypeId]) -> None:
		"""Fill in the field TypeIds for a declared struct."""
		td = self.get(struct_id)
		if td.kind is not TypeKind.STRUCT or td.field_names is None:
			raise ValueError("define_struct_fields requires a STRUCT TypeId")
		if len(field_types) != len(td.field_names):
			raise ValueError("field_types length mismatch for struct definition")
		if td.param_types:
			existing = list(td.param_types)
			if existing == list(field_types):
				return
			if not all(self.get(t).kind is TypeKind.UNKNOWN for t in existing):
				raise ValueError(
					f"struct '{td.module_id}::{td.name}' fields already defined: "
					f"{existing} vs {field_types}"
				)
		self._defs[struct_id] = TypeDef(
			kind=TypeKind.STRUCT,
			name=td.name,
			param_types=list(field_types),
			module_id=td.module_id,
			ref_mut=None,
			field_names=list(td.field_names),
		)
		schema = self.struct_bases.get(struct_id)
		if schema is not None and not schema.type_params:
			inst = self.struct_instances.get(struct_id)
			if inst is not None:
				if list(inst.field_types) == list(field_types):
					return
				raise ValueError(
					f"struct '{td.module_id}::{td.name}' instance fields mismatch: "
					f"{inst.field_types} vs {field_types}"
				)
			self._define_struct_instance(
				struct_id,
				struct_id,
				type_args=[],
				field_names=list(td.field_names),
				field_types=list(field_types),
			)

	def struct_field(self, struct_id: TypeId, field_name: str) -> tuple[int, TypeId] | None:
		"""Return (field_index, field_type_id) for a struct field, or None."""
		info = self.struct_field_info(struct_id, field_name)
		if info is None:
			return None
		idx, field_ty, _is_pub = info
		return idx, field_ty

	def struct_field_info(self, struct_id: TypeId, field_name: str) -> tuple[int, TypeId, bool] | None:
		"""Return (field_index, field_type_id, is_pub) for a struct field, or None."""
		base_id = struct_id
		inst = self.struct_instances.get(struct_id)
		if inst is None:
			for (tmpl_base_id, tmpl_args), tmpl_inst_id in self._struct_template_cache.items():
				if tmpl_inst_id == struct_id:
					schema = self.struct_bases.get(tmpl_base_id)
					if schema is None:
						break
					field_names = [f.name for f in schema.fields]
					field_types = [self._eval_generic_type_expr(f.type_expr, list(tmpl_args), module_id=schema.module_id) for f in schema.fields]
					self._define_struct_instance(tmpl_base_id, struct_id, type_args=list(tmpl_args), field_names=field_names, field_types=field_types)
					inst = self.struct_instances.get(struct_id)
					break
		if inst is not None:
			idx = inst.fields_by_name.get(field_name)
			if idx is None:
				return None
			if idx >= len(inst.field_types):
				return None
			field_ty = inst.field_types[idx]
			base_id = inst.base_id
		else:
			td = self.get(struct_id)
			if td.kind is not TypeKind.STRUCT or td.field_names is None:
				return None
			try:
				idx = td.field_names.index(field_name)
			except ValueError:
				return None
			if idx >= len(td.param_types):
				return None
			field_ty = td.param_types[idx]
		schema = self.struct_bases.get(base_id)
		is_pub = False
		if schema is not None:
			if idx < len(schema.fields) and schema.fields[idx].name == field_name:
				is_pub = schema.fields[idx].is_pub
			else:
				for f in schema.fields:
					if f.name == field_name:
						is_pub = f.is_pub
						break
		return idx, field_ty, is_pub

	def ensure_uint(self) -> TypeId:
		"""Return a stable Uint TypeId, creating it once."""
		if getattr(self, "_uint_type", None) is None:
			self._uint_type = self.new_scalar("Uint")  # type: ignore[attr-defined]
		return self._uint_type  # type: ignore[attr-defined]

	def ensure_uint64(self) -> TypeId:
		"""Return a stable Uint64 TypeId, creating it once."""
		if getattr(self, "_uint64_type", None) is None:
			self._uint64_type = self.new_scalar("Uint64")  # type: ignore[attr-defined]
		return self._uint64_type  # type: ignore[attr-defined]

	def ensure_byte(self) -> TypeId:
		"""Return a stable Byte TypeId, creating it once."""
		if getattr(self, "_byte_type", None) is None:
			self._byte_type = self.new_scalar("Byte")  # type: ignore[attr-defined]
		return self._byte_type  # type: ignore[attr-defined]

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

		Float is a target-defined scalar (width decided by the backend).
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

	def ensure_optional_base(self) -> TypeId:
		"""
		Return the canonical `lang.core.Optional<T>` variant base.

		MVP contract:
		  variant Optional<T> { None, Some(value: T) }
		"""
		base = self.get_variant_base(module_id="lang.core", name="Optional")
		if base is not None:
			return base
		return self.declare_variant(
			"lang.core",
			"Optional",
			["T"],
			[
				VariantArmSchema(name="None", fields=[]),
				VariantArmSchema(
					name="Some",
					fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
				),
				VariantArmSchema(name="Tombstone", fields=[]),
			],
			tombstone_ctor="Tombstone",
		)

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
		return self.ensure_fnresult(ok, err)

	def ensure_fnresult(self, ok: TypeId, err: TypeId) -> TypeId:
		"""
		Return a stable shared FnResult<ok, err> TypeId, creating it once.

		FnResult is used as an internal ABI carrier in lang. Keeping a stable,
		reused TypeId for a given (ok, err) pair makes type comparisons and
		diagnostics less fragile across passes.
		"""
		if not hasattr(self, "_fnresult_cache"):
			self._fnresult_cache = {}  # type: ignore[attr-defined]
		key = (ok, err)
		cache = getattr(self, "_fnresult_cache")  # type: ignore[attr-defined]
		if key not in cache:
			cache[key] = self._add(TypeKind.FNRESULT, "FnResult", [ok, err])
		return cache[key]

	def ensure_function(
		self,
		param_types: List[TypeId],
		return_type: TypeId,
		*,
		can_throw: bool,
	) -> TypeId:
		"""Return a stable shared function TypeId, creating it once."""
		if not hasattr(self, "_function_cache"):
			self._function_cache = {}  # type: ignore[attr-defined]
		key = (bool(can_throw), tuple(param_types), return_type)
		cache = getattr(self, "_function_cache")  # type: ignore[attr-defined]
		if key not in cache:
			cache[key] = self._add(
				TypeKind.FUNCTION,
				"fn",
				[*param_types, return_type],
				fn_throws=bool(can_throw),
			)
		return cache[key]

	def new_function(
		self,
		param_types: List[TypeId],
		return_type: TypeId,
		*,
		can_throw: bool = True,
	) -> TypeId:
		"""Register a function type (params + return + throw mode)."""
		return self.ensure_function(param_types, return_type, can_throw=can_throw)

	def new_array(self, elem: TypeId) -> TypeId:
		"""Register an Array<elem> type."""
		existing = self._array_cache.get(elem)
		if existing is not None:
			return existing
		ty_id = self._add(TypeKind.ARRAY, "Array", [elem])
		self._array_cache[elem] = ty_id
		return ty_id

	def array_base_id(self) -> TypeId:
		"""Return a stable Array base TypeId used for trait impl lookup."""
		if self._array_base_id is not None:
			return self._array_base_id
		owner = FunctionId(module="__builtin__", name="Array", ordinal=0)
		param_id = TypeParamId(owner=owner, index=0)
		elem = self.ensure_typevar(param_id, name="T")
		self._array_base_id = self.new_array(elem)
		return self._array_base_id

	def new_ref(self, inner: TypeId, is_mut: bool) -> TypeId:
		"""Register a reference type to `inner` (mutable vs shared encoded in ref_mut/name)."""
		name = "RefMut" if is_mut else "Ref"
		for ty_id, ty_def in self._defs.items():
			if ty_def.kind is TypeKind.REF and ty_def.param_types and ty_def.param_types[0] == inner and ty_def.ref_mut == is_mut:
				return ty_id
		return self._add(TypeKind.REF, name, [inner], ref_mut=is_mut)
	def new_ptr(self, inner: TypeId, *, module_id: str | None = "std.mem") -> TypeId:
		"""Register a raw pointer type Ptr<inner>."""
		for ty_id, ty_def in self._defs.items():
			if ty_def.kind is TypeKind.RAW_PTR and ty_def.param_types and ty_def.param_types[0] == inner:
				return ty_id
		return self._add(TypeKind.RAW_PTR, "Ptr", [inner], module_id=module_id, register_named=False)

	def new_unknown(self, name: str = "Unknown") -> TypeId:
		"""Register an unknown type (debug/fallback)."""
		return self._add(TypeKind.UNKNOWN, name, [])

	def ensure_typevar(self, param_id: TypeParamId, *, name: str | None = None) -> TypeId:
		"""Return a stable TypeId for a type parameter."""
		if param_id in self._typevar_cache:
			return self._typevar_cache[param_id]
		display_name = name or f"T{param_id.index}"
		ty_id = self._add(
			TypeKind.TYPEVAR,
			display_name,
			[],
			register_named=False,
			type_param_id=param_id,
		)
		self._typevar_cache[param_id] = ty_id
		return ty_id

	def set_copy_query(self, query, *, allow_fallback: bool = False) -> None:
		"""Install a Copy query hook for trait-based Copy checks."""
		self._copy_query = query  # type: ignore[attr-defined]
		self._copy_query_allow_fallback = bool(allow_fallback)  # type: ignore[attr-defined]
		if hasattr(self, "_copy_cache"):
			self._copy_cache.clear()  # type: ignore[attr-defined]

	def set_diagnostic_query(self, query, *, allow_fallback: bool = False) -> None:
		"""Install a Diagnostic query hook for trait-based checks."""
		self._diagnostic_query = query  # type: ignore[attr-defined]
		self._diagnostic_query_allow_fallback = bool(allow_fallback)  # type: ignore[attr-defined]
		if hasattr(self, "_diagnostic_cache"):
			self._diagnostic_cache.clear()  # type: ignore[attr-defined]

	def set_destructible_query(self, query, *, allow_fallback: bool = False) -> None:
		"""Install a Destructible query hook for trait-based checks."""
		self._destructible_query = query  # type: ignore[attr-defined]
		self._destructible_query_allow_fallback = bool(allow_fallback)  # type: ignore[attr-defined]
		if hasattr(self, "_destructible_cache"):
			self._destructible_cache.clear()  # type: ignore[attr-defined]

	def is_copy(self, ty: TypeId) -> bool:
		"""Return True if `ty` is Copy under MVP structural rules."""
		status = self.copy_status(ty)
		return bool(status)

	def _contains_kind(self, tid: TypeId, kinds: set[TypeKind], *, seen: set[TypeId]) -> bool:
		if tid in seen:
			return False
		seen.add(tid)
		td = self.get(tid)
		if td.kind in kinds:
			return True
		if td.kind is TypeKind.STRUCT:
			inst = self.get_struct_instance(tid)
			if inst is None:
				return False
			return any(self._contains_kind(f, kinds, seen=seen) for f in inst.field_types)
		if td.kind is TypeKind.VARIANT:
			inst = self.get_variant_instance(tid)
			if inst is None:
				return False
			for arm in inst.arms:
				if any(self._contains_kind(f, kinds, seen=seen) for f in arm.field_types):
					return True
			return False
		if td.kind is TypeKind.ARRAY and td.param_types:
			return self._contains_kind(td.param_types[0], kinds, seen=seen)
		for child in td.param_types:
			if self._contains_kind(child, kinds, seen=seen):
				return True
		return False

	def copy_status(self, ty: TypeId) -> bool | None:
		if not hasattr(self, "_copy_cache_proof"):
			self._copy_cache_proof = {}  # type: ignore[attr-defined]
		if not hasattr(self, "_copy_cache_structural"):
			self._copy_cache_structural = {}  # type: ignore[attr-defined]
		cache_proof: Dict[TypeId, bool] = getattr(self, "_copy_cache_proof")  # type: ignore[attr-defined]
		cache_structural: Dict[TypeId, bool] = getattr(self, "_copy_cache_structural")  # type: ignore[attr-defined]
		if not hasattr(self, "_copy_query"):
			trait_worlds = getattr(self, "trait_worlds", None)
			if isinstance(trait_worlds, dict) and any(
				mod in {"std.core", "std.iter", "std.containers", "std.algo"} for mod in trait_worlds.keys()
			):
				raise ValueError("Copy query hook is missing while stdlib trait metadata is present")
		if ty in cache_proof:
			if drift_debug.enabled("copy_cache"):
				td = self.get(ty)
				print(f"[drift:debug][copy_cache] hit source=proof ty={ty} kind={td.kind.name} name={td.name} val={cache_proof[ty]}", file=sys.stderr)
			return cache_proof[ty]
		if ty in cache_structural:
			if drift_debug.enabled("copy_cache"):
				td = self.get(ty)
				print(f"[drift:debug][copy_cache] hit source=structural ty={ty} kind={td.kind.name} name={td.name} val={cache_structural[ty]}", file=sys.stderr)
			return cache_structural[ty]
		td = self.get(ty)
		if td.kind in {TypeKind.TYPEVAR, TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL}:
			if drift_debug.enabled("copy_cache"):
				print(f"[drift:debug][copy_cache] unresolved ty={ty} kind={td.kind.name} name={td.name}", file=sys.stderr)
			return None

		def _is_concrete_type(tid: TypeId, *, seen: set[TypeId]) -> bool:
			if tid in seen:
				return True
			seen.add(tid)
			td = self.get(tid)
			if td.kind in {TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL, TypeKind.TYPEVAR}:
				return False
			if td.kind is TypeKind.STRUCT:
				inst = self.get_struct_instance(tid)
				if inst is None:
					return False
				return all(_is_concrete_type(f, seen=seen) for f in inst.field_types)
			if td.kind is TypeKind.VARIANT:
				inst = self.get_variant_instance(tid)
				if inst is None:
					return False
				for arm in inst.arms:
					if not all(_is_concrete_type(f, seen=seen) for f in arm.field_types):
						return False
				return True
			if td.kind is TypeKind.ARRAY and td.param_types:
				return _is_concrete_type(td.param_types[0], seen=seen)
			for child in td.param_types:
				if not _is_concrete_type(child, seen=seen):
					return False
			return True

		def _eligible_structural_fallback(tid: TypeId) -> bool:
			td = self.get(tid)
			if td.kind not in {TypeKind.STRUCT, TypeKind.VARIANT}:
				return False
			if self.has_typevar(tid):
				return False
			if td.kind is TypeKind.STRUCT:
				inst = self.get_struct_instance(tid)
				if inst is None:
					return False
				if inst.base_id not in self.struct_bases:
					return False
				return _is_concrete_type(tid, seen=set())
			if td.kind is TypeKind.VARIANT:
				inst = self.get_variant_instance(tid)
				if inst is None:
					return False
				if inst.base_id not in self.variant_schemas:
					return False
				return _is_concrete_type(tid, seen=set())
			return False

		seen: set[TypeId] = set()

		def _assert_structural_cacheable(tid: TypeId) -> None:
			if not drift_debug.enabled("copy_cache_assert"):
				return
			td = self.get(tid)
			if td.kind in {TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL, TypeKind.TYPEVAR}:
				raise AssertionError(f"structural copy cache on unresolved type {tid}:{td.kind.name}:{td.name}")
			if td.kind is TypeKind.STRUCT:
				inst = self.get_struct_instance(tid)
				if inst is None or inst.base_id not in self.struct_bases:
					raise AssertionError(f"structural copy cache on unresolvable struct {tid}:{td.name}")
			if td.kind is TypeKind.VARIANT:
				inst = self.get_variant_instance(tid)
				if inst is None or inst.base_id not in self.variant_schemas:
					raise AssertionError(f"structural copy cache on unresolvable variant {tid}:{td.name}")

		def _is_copy_structural(tid: TypeId) -> bool:
			if tid in cache_structural:
				return cache_structural[tid]
			if tid in seen:
				return False
			seen.add(tid)
			td = self.get(tid)
			if td.kind in {TypeKind.SCALAR, TypeKind.REF, TypeKind.RAW_PTR, TypeKind.FUNCTION, TypeKind.VOID}:
				if td.kind is TypeKind.SCALAR and td.name == "String":
					return False
				if td.kind is TypeKind.REF and bool(td.ref_mut):
					_assert_structural_cacheable(tid)
					cache_structural[tid] = False
					return False
				ok = True
				_assert_structural_cacheable(tid)
				cache_structural[tid] = ok
				return ok
			if td.kind in {TypeKind.FORWARD_NOMINAL, TypeKind.UNKNOWN, TypeKind.ERROR, TypeKind.DIAGNOSTICVALUE, TypeKind.TYPEVAR}:
				if drift_debug.enabled("copy_cache"):
					print(f"[drift:debug][copy_cache] unresolved ty={tid} kind={td.kind.name} name={td.name}", file=sys.stderr)
				return False
			if td.kind is TypeKind.ARRAY:
				return False
			if td.kind is TypeKind.FNRESULT:
				return False
			if td.kind is TypeKind.STRUCT:
				inst = self.get_struct_instance(tid)
				if inst is None:
					if drift_debug.enabled("copy_cache"):
						print(f"[drift:debug][copy_cache] struct_no_instance ty={tid} name={td.name} module={td.module_id}", file=sys.stderr)
					return False
				ok = all(_is_copy_structural(f) for f in inst.field_types)
				_assert_structural_cacheable(tid)
				cache_structural[tid] = ok
				return ok
			if td.kind is TypeKind.VARIANT:
				inst = self.get_variant_instance(tid)
				if inst is None:
					if drift_debug.enabled("copy_cache"):
						print(f"[drift:debug][copy_cache] variant_no_instance ty={tid} name={td.name} module={td.module_id}", file=sys.stderr)
					return False
				ok = True
				for arm in inst.arms:
					for f in arm.field_types:
						if not _is_copy_structural(f):
							ok = False
							break
					if not ok:
						break
				_assert_structural_cacheable(tid)
				cache_structural[tid] = ok
				return ok
			return False

		if hasattr(self, "_copy_query"):
			query = getattr(self, "_copy_query")  # type: ignore[attr-defined]
			res = query(ty)
			if res is not None:
				cache_proof[ty] = bool(res)
				return cache_proof[ty]
			if _eligible_structural_fallback(ty):
				if drift_debug.enabled("copy_fallback"):
					td = self.get(ty)
					print(f"[drift:debug][copy_fallback] ty={ty} kind={td.kind.name} name={td.name} module={td.module_id}", file=sys.stderr)
				return _is_copy_structural(ty)
			if drift_debug.enabled("copy_fallback"):
				td = self.get(ty)
				print(f"[drift:debug][copy_fallback] ty={ty} kind={td.kind.name} name={td.name} module={td.module_id} status=defer", file=sys.stderr)
			return None
		return _is_copy_structural(ty)

	def copy_unknown_reason(self, ty: TypeId) -> str:
		"""Return a short explanation for why Copy is unknown."""
		seen: set[TypeId] = set()
		has_typevar = False
		has_unknown = False
		has_forward = False

		def _scan(tid: TypeId) -> None:
			nonlocal has_typevar, has_unknown, has_forward
			if tid in seen:
				return
			seen.add(tid)
			td = self.get(tid)
			if td.kind is TypeKind.TYPEVAR:
				has_typevar = True
			if td.kind is TypeKind.UNKNOWN:
				has_unknown = True
			if td.kind is TypeKind.FORWARD_NOMINAL:
				has_forward = True
			if td.kind is TypeKind.STRUCT:
				inst = self.get_struct_instance(tid)
				if inst is None:
					return
				for f in inst.field_types:
					_scan(f)
				return
			if td.kind is TypeKind.VARIANT:
				inst = self.get_variant_instance(tid)
				if inst is None:
					return
				for arm in inst.arms:
					for f in arm.field_types:
						_scan(f)
				return
			if td.kind is TypeKind.ARRAY and td.param_types:
				_scan(td.param_types[0])
				return
			for child in td.param_types:
				_scan(child)

		_scan(ty)
		if has_typevar:
			return "element type contains type variables"
		if has_unknown:
			return "element type contains Unknown"
		if has_forward:
			return "element type contains forward nominals"
		td = self.get(ty)
		if td.kind is TypeKind.STRUCT:
			inst = self.get_struct_instance(ty)
			if inst is None or inst.base_id not in self.struct_bases:
				return "element type not fully resolved yet"
		if td.kind is TypeKind.VARIANT:
			inst = self.get_variant_instance(ty)
			if inst is None or inst.base_id not in self.variant_schemas:
				return "element type not fully resolved yet"
		return "element type not fully resolved yet"

	def is_diagnostic(self, ty: TypeId) -> bool:
		"""Return True if `ty` implements Diagnostic."""
		if not hasattr(self, "_diagnostic_cache"):
			self._diagnostic_cache = {}  # type: ignore[attr-defined]
		cache: Dict[TypeId, bool] = getattr(self, "_diagnostic_cache")  # type: ignore[attr-defined]
		if not hasattr(self, "_diagnostic_query"):
			trait_worlds = getattr(self, "trait_worlds", None)
			if isinstance(trait_worlds, dict) and any(
				mod in {"std.core", "std.iter", "std.containers", "std.algo", "std.err"} for mod in trait_worlds.keys()
			):
				raise ValueError("Diagnostic query hook is missing while stdlib trait metadata is present")
		if ty in cache:
			return cache[ty]
		if hasattr(self, "_diagnostic_query"):
			query = getattr(self, "_diagnostic_query")  # type: ignore[attr-defined]
			res = query(ty)
			if res is True:
				cache[ty] = True
				return True
			if res is False:
				cache[ty] = False
				return False
			allow_fallback = bool(getattr(self, "_diagnostic_query_allow_fallback", False))  # type: ignore[attr-defined]
			if not allow_fallback:
				cache[ty] = False
				return False
		td = self.get(ty)
		if td.kind is TypeKind.DIAGNOSTICVALUE:
			cache[ty] = True
			return True
		if td.kind is TypeKind.SCALAR and td.name in {"Int", "Uint", "Bool", "Float", "String"}:
			cache[ty] = True
			return True
		cache[ty] = False
		return False

	def is_destructible(self, ty: TypeId) -> bool:
		"""Return True if `ty` implements Destructible."""
		if not hasattr(self, "_destructible_cache"):
			self._destructible_cache = {}  # type: ignore[attr-defined]
		cache: Dict[TypeId, bool] = getattr(self, "_destructible_cache")  # type: ignore[attr-defined]
		if not hasattr(self, "_destructible_query"):
			trait_worlds = getattr(self, "trait_worlds", None)
			if isinstance(trait_worlds, dict) and any(mod in {"std.core"} for mod in trait_worlds.keys()):
				raise ValueError("Destructible query hook is missing while stdlib trait metadata is present")
		if ty in cache:
			return cache[ty]
		if hasattr(self, "_destructible_query"):
			query = getattr(self, "_destructible_query")  # type: ignore[attr-defined]
			res = query(ty)
			if res is True:
				cache[ty] = True
				return True
			if res is False:
				cache[ty] = False
				return False
			allow_fallback = bool(getattr(self, "_destructible_query_allow_fallback", False))  # type: ignore[attr-defined]
			if not allow_fallback:
				cache[ty] = False
				return False
		cache[ty] = False
		return False

	def is_bitcopy(self, ty: TypeId) -> bool:
		"""
		Return True if `ty` is safe to bitwise-copy (memcpy).

		This is intentionally stricter than `is_copy`: a type can be Copy but still
		require semantic copying once non-bitwise Copy types are introduced.
		"""
		if not hasattr(self, "_bitcopy_cache"):
			self._bitcopy_cache = {}  # type: ignore[attr-defined]
		cache: Dict[TypeId, bool] = getattr(self, "_bitcopy_cache")  # type: ignore[attr-defined]
		if ty in cache:
			return cache[ty]
		seen: set[TypeId] = set()

		def _is_bitcopy(tid: TypeId) -> bool:
			if tid in cache:
				return cache[tid]
			if tid in seen:
				return False
			seen.add(tid)
			if not self.is_copy(tid):
				cache[tid] = False
				return False
			td = self.get(tid)
			if td.kind in {TypeKind.SCALAR, TypeKind.REF, TypeKind.RAW_PTR, TypeKind.FUNCTION, TypeKind.VOID}:
				if td.kind is TypeKind.SCALAR and td.name == "String":
					cache[tid] = False
					return False
				cache[tid] = True
				return True
			if td.kind in {TypeKind.FORWARD_NOMINAL, TypeKind.UNKNOWN, TypeKind.ERROR, TypeKind.DIAGNOSTICVALUE, TypeKind.TYPEVAR}:
				cache[tid] = False
				return False
			if td.kind is TypeKind.ARRAY:
				cache[tid] = False
				return False
			if td.kind is TypeKind.STRUCT:
				inst = self.get_struct_instance(tid)
				if inst is None:
					cache[tid] = False
					return False
				ok = all(_is_bitcopy(f) for f in inst.field_types)
				cache[tid] = ok
				return ok
			if td.kind is TypeKind.VARIANT:
				# Variants are never bitcopy: their representation is not a stable
				# bitwise ABI, and Copy requires per-arm semantics.
				cache[tid] = False
				return False
			cache[tid] = False
			return False

		return _is_bitcopy(ty)

	def _add(
		self,
		kind: TypeKind,
		name: str,
		params: List[TypeId],
		ref_mut: bool | None = None,
		fn_throws: bool = True,
		field_names: List[str] | None = None,
		type_param_id: TypeParamId | None = None,
		*,
		register_named: bool | None = None,
		module_id: str | None = None,
	) -> TypeId:
		ty_id = self._next_id
		self._next_id += 1
		self._defs[ty_id] = TypeDef(
			kind=kind,
			name=name,
			param_types=list(params),
			type_param_id=type_param_id if kind is TypeKind.TYPEVAR else None,
			module_id=module_id,
			ref_mut=ref_mut if kind is TypeKind.REF else None,
			fn_throws=bool(fn_throws) if kind is TypeKind.FUNCTION else False,
			field_names=list(field_names) if field_names is not None else None,
		)
		if register_named is None:
			register_named = kind in (TypeKind.SCALAR, TypeKind.FORWARD_NOMINAL, TypeKind.STRUCT, TypeKind.INTERFACE)
		if register_named:
			key = NominalKey(package_id=self._package_for_module(module_id), module_id=module_id, name=name, kind=kind)
			self._nominal.setdefault(key, ty_id)
		return ty_id

	def get(self, ty: TypeId) -> TypeDef:
		"""Fetch the TypeDef for a given TypeId."""
		return self._defs[ty]

	def type_key_string(self, ty: TypeId) -> str:
		"""
		Build a stable, argument-sensitive key for a TypeId.

		The key includes nominal identity (package/module/name), concrete type
		arguments, ref mutability, and function throwness.
		"""
		stack: list[TypeId] = []
		stack_index: dict[TypeId, int] = {}

		def _nominal_label(td: TypeDef) -> str:
			if td.module_id is None:
				return td.name
			return _qualify(td.name, td.module_id)

		def _qualify(name: str, module_id: str | None) -> str:
			if module_id is None:
				return name
			if module_id == "lang.core":
				pkg = self.module_packages.get(module_id, "lang.core")
			else:
				if self.package_id is not None and module_id not in self.module_packages:
					raise ValueError(f"module_packages missing provider for module '{module_id}'")
				pkg = self.module_packages.get(module_id, self.package_id)
			base = f"{module_id}.{name}"
			if pkg:
				return f"{pkg}::{base}"
			return base

		def _key(tid: TypeId) -> str:
			if tid in stack_index:
				td = self.get(tid)
				idx = stack_index[tid]
				return f"Rec{idx}@{_nominal_label(td)}"
			stack_index[tid] = len(stack)
			stack.append(tid)
			try:
				td = self.get(tid)
				if td.kind in {TypeKind.SCALAR, TypeKind.FORWARD_NOMINAL}:
					if td.module_id is not None:
						return _qualify(td.name, td.module_id)
					return td.name
				if td.kind is TypeKind.VOID:
					return "Void"
				if td.kind is TypeKind.ERROR:
					return "Error"
				if td.kind is TypeKind.DIAGNOSTICVALUE:
					return "DiagnosticValue"
				if td.kind is TypeKind.TYPEVAR and td.type_param_id is not None:
					owner = function_symbol(td.type_param_id.owner)
					return f"TypeVar<{owner}#{td.type_param_id.index}>"
				if td.kind is TypeKind.REF and td.param_types:
					inner = _key(td.param_types[0])
					return f"{'RefMut' if td.ref_mut else 'Ref'}<{inner}>"
				if td.kind is TypeKind.RAW_PTR and td.param_types:
					inner = _key(td.param_types[0])
					return f"Ptr<{inner}>"
				if td.kind is TypeKind.ARRAY and td.param_types:
					inner = _key(td.param_types[0])
					return f"Array<{inner}>"
				if td.kind is TypeKind.FNRESULT and len(td.param_types) >= 2:
					ok = _key(td.param_types[0])
					err = _key(td.param_types[1])
					return f"FnResult<{ok},{err}>"
				if td.kind is TypeKind.FUNCTION:
					if not td.param_types:
						return "Fn<?>"
					args = ",".join(_key(t) for t in td.param_types[:-1]) or "Void"
					ret = _key(td.param_types[-1])
					throw_tag = "throws" if td.fn_throws else "nothrow"
					return f"Fn({args})->{ret}:{throw_tag}"
				if td.kind is TypeKind.STRUCT:
					module_id = td.module_id
					inst = self.struct_instances.get(tid)
					args = []
					if inst is not None:
						args = [_key(t) for t in inst.type_args]
					base = _qualify(td.name, module_id)
					if args:
						return f"{base}<{','.join(args)}>"
					return base
				if td.kind is TypeKind.INTERFACE:
					module_id = td.module_id
					inst = self.interface_instances.get(tid)
					args = []
					if inst is not None:
						args = [_key(t) for t in inst.type_args]
					base = _qualify(td.name, module_id)
					if args:
						return f"{base}<{','.join(args)}>"
					return base
				if td.kind is TypeKind.VARIANT:
					module_id = td.module_id
					inst = self.variant_instances.get(tid)
					args = []
					if inst is not None:
						args = [_key(t) for t in inst.type_args]
					base = _qualify(td.name, module_id)
					if args:
						return f"{base}<{','.join(args)}>"
					return base
				return td.kind.name
			finally:
				stack.pop()
				stack_index.pop(tid, None)

		return _key(ty)

	def is_void(self, ty: TypeId) -> bool:
		"""Return True when the TypeId refers to the canonical Void type."""
		return self.get(ty).kind is TypeKind.VOID


__all__ = [
	"TypeId",
	"TypeKind",
	"TypeParamId",
	"NominalKey",
	"TypeDef",
	"StructFieldSchema",
	"StructSchema",
	"StructInstance",
	"InterfaceSchema",
	"InterfaceInstance",
	"InterfaceParamSchema",
	"InterfaceMethodSchema",
	"TypeTable",
	"VariantFieldSchema",
	"VariantArmSchema",
	"VariantSchema",
	"VariantArmInstance",
	"VariantInstance",
]
