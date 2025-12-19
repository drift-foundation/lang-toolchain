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

from lang2.driftc.core.generic_type_expr import GenericTypeExpr


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
	VARIANT = auto()
	UNKNOWN = auto()


@dataclass(frozen=True)
class NominalKey:
	"""
	Canonical identity for a nominal (named) type.

	For production correctness, user-defined nominal types are module-scoped.
	That means `struct Point` declared in module `a.geom` is a different type
	from `struct Point` declared in module `b.geom`, even though both share the
	short name `Point`.

	Builtins use `module_id=None` and are toolchain-owned.
	"""

	module_id: str | None
	name: str
	kind: TypeKind


@dataclass(frozen=True)
class TypeDef:
	"""Definition of a type stored in the TypeTable."""

	kind: TypeKind
	name: str
	param_types: List[TypeId]
	# Module id for nominal types (STRUCT/VARIANT and any other module-scoped
	# named types). Builtins use module_id=None.
	module_id: str | None = None
	ref_mut: bool | None = None  # only meaningful for TypeKind.REF
	field_names: List[str] | None = None  # only meaningful for TypeKind.STRUCT


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
	instantiations are created via `TypeTable.ensure_instantiated(...)` which
	evaluates field `GenericTypeExpr`s into concrete `TypeId`s.
	"""

	module_id: str
	name: str
	type_params: list[str]
	arms: list[VariantArmSchema]


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


class TypeTable:
	"""
	Simple type table that owns TypeIds.

	This is intentionally tiny: enough to represent scalars, Error, FnResult,
	and function types. It can be extended as the checker grows.
	"""

	def __init__(self) -> None:
		self._defs: Dict[TypeId, TypeDef] = {}
		self._next_id: TypeId = 1  # reserve 0 for "invalid"
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
		# Exception schemas keyed by canonical event FQN strings. Values are
		# (canonical_fqn, [declared_field_names]) so later stages can:
		# - resolve constructor-call args (positional/keyword) to declared fields
		# - enforce exact coverage (no missing/unknown/duplicates)
		# - attach attrs deterministically in lowering.
		self.exception_schemas: dict[str, tuple[str, list[str]]] = {}
		# Struct schemas keyed by nominal identity. Values are (name, [field_names]).
		# Field types live in the STRUCT TypeDef itself.
		self.struct_schemas: dict[NominalKey, tuple[str, list[str]]] = {}
		# Variant schemas keyed by the *base* TypeId (declared name).
		self.variant_schemas: dict[TypeId, VariantSchema] = {}
		# Concrete instantiations keyed by the instantiated TypeId.
		self.variant_instances: dict[TypeId, VariantInstance] = {}
		# Instantiation cache: (base_id, args...) -> instantiated TypeId.
		self._instantiation_cache: dict[tuple[TypeId, tuple[TypeId, ...]], TypeId] = {}

	def new_scalar(self, name: str) -> TypeId:
		"""Register a scalar type (e.g., Int, Bool) and return its TypeId."""
		return self._add(TypeKind.SCALAR, name, [])

	def ensure_named(self, name: str, *, module_id: str | None = None) -> TypeId:
		"""
		Return a stable TypeId for a nominal scalar name.

		This is used for user-defined type names that appear in annotations.
		If the name has not been declared yet, we conservatively create a scalar
		nominal type. Later, a richer kind (STRUCT/ENUM) may be declared under
		that name; callers should prefer explicit `declare_struct` for structs.
		"""
		key = NominalKey(module_id=module_id, name=name, kind=TypeKind.SCALAR)
		prev = self._nominal.get(key)
		if prev is not None:
			return prev
		return self._add(TypeKind.SCALAR, name, [], module_id=module_id)

	def get_nominal(self, *, kind: TypeKind, module_id: str | None, name: str) -> TypeId | None:
		"""Return a nominal TypeId by identity, or None if not present."""
		return self._nominal.get(NominalKey(module_id=module_id, name=name, kind=kind))

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

	def declare_struct(self, module_id: str, name: str, field_names: List[str]) -> TypeId:
		"""
		Declare a struct nominal type with placeholder field types.

		This supports recursive type references by first declaring all struct
		names, then filling field types in a second pass via `define_struct_fields`.
		"""
		key = NominalKey(module_id=module_id, name=name, kind=TypeKind.STRUCT)
		if key in self._nominal:
			ty_id = self._nominal[key]
			td = self.get(ty_id)
			if td.kind is TypeKind.STRUCT:
				return ty_id
			raise ValueError(f"type name '{name}' already defined as {td.kind}")
		unknown = self.ensure_unknown()
		placeholder = [unknown for _ in field_names]
		ty_id = self._add(TypeKind.STRUCT, name, placeholder, field_names=list(field_names), module_id=module_id)
		self.struct_schemas[key] = (name, list(field_names))
		return ty_id

	def declare_variant(self, module_id: str, name: str, type_params: list[str], arms: list[VariantArmSchema]) -> TypeId:
		"""
		Declare a variant nominal type (generic or non-generic).

		The returned `TypeId` is the *base* type for the declared name.
		Concrete instantiations are created via `ensure_instantiated`.

		For non-generic variants (`type_params == []`), the base type is also a
		concrete instantiation with zero type arguments, and is available via
		`get_variant_instance(base_id)`.
		"""
		key = NominalKey(module_id=module_id, name=name, kind=TypeKind.VARIANT)
		if key in self._nominal:
			ty_id = self._nominal[key]
			td = self.get(ty_id)
			if td.kind is TypeKind.VARIANT:
				return ty_id
			raise ValueError(f"type name '{name}' already defined as {td.kind}")
		# Base variant type. Note: base is named; instantiations are not.
		base_id = self._add(TypeKind.VARIANT, name, [], register_named=True, module_id=module_id)
		self.variant_schemas[base_id] = VariantSchema(module_id=module_id, name=name, type_params=list(type_params), arms=list(arms))
		return base_id

	def ensure_instantiated(self, base_id: TypeId, type_args: list[TypeId]) -> TypeId:
		"""
		Return a stable TypeId for a concrete instantiation of a generic nominal.

		MVP: only variants are instantiable. This is enforced by schema presence.
		"""
		schema = self.variant_schemas.get(base_id)
		if schema is None:
			raise ValueError("ensure_instantiated requires a declared generic variant base TypeId")
		if len(type_args) != len(schema.type_params):
			raise ValueError(
				f"type argument count mismatch for '{schema.name}': expected {len(schema.type_params)}, got {len(type_args)}"
			)
		if not schema.type_params:
			# Non-generic variants use the base id directly. Ensure its concrete
			# instance exists (it may be created lazily after all variants are
			# declared to support mutual references between non-generic variants).
			if base_id not in self.variant_instances:
				self._define_variant_instance(base_id, base_id, [])
			return base_id
		key = (base_id, tuple(type_args))
		if key in self._instantiation_cache:
			return self._instantiation_cache[key]
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
		for base_id, schema in list(self.variant_schemas.items()):
			if schema.type_params:
				continue
			if base_id not in self.variant_instances:
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
		self.variant_instances[inst_id] = VariantInstance(
			base_id=base_id,
			type_args=list(type_args),
			arms=arms,
			arms_by_name=by_name,
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
		# Named nominal types: either a simple name, or a generic instantiation.
		#
		# The `module_id` on `GenericTypeExpr` is a resolved canonical origin module
		# for imported/qualified names. If absent, the name is unqualified and is
		# resolved in the declaring module scope (`module_id` argument).
		origin_mod = expr.module_id or module_id
		# MVP supports structs and variants as nominal names.
		base_id = (
			self.get_nominal(kind=TypeKind.STRUCT, module_id=origin_mod, name=name)
			or self.get_nominal(kind=TypeKind.VARIANT, module_id=origin_mod, name=name)
			or self.ensure_named(name, module_id=origin_mod)
		)
		if expr.args:
			arg_ids = [self._eval_generic_type_expr(a, type_args, module_id=module_id) for a in expr.args]
			# Only variants are instantiable in MVP.
			if base_id in self.variant_schemas:
				return self.ensure_instantiated(base_id, arg_ids)
		return base_id

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
			module_id=td.module_id,
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
			module_id=module_id,
			ref_mut=ref_mut if kind is TypeKind.REF else None,
			field_names=list(field_names) if field_names is not None else None,
		)
		if register_named is None:
			register_named = kind in (TypeKind.SCALAR, TypeKind.STRUCT)
		if register_named:
			key = NominalKey(module_id=module_id, name=name, kind=kind)
			self._nominal.setdefault(key, ty_id)
		return ty_id

	def get(self, ty: TypeId) -> TypeDef:
		"""Fetch the TypeDef for a given TypeId."""
		return self._defs[ty]

	def is_void(self, ty: TypeId) -> bool:
		"""Return True when the TypeId refers to the canonical Void type."""
		return self.get(ty).kind is TypeKind.VOID


__all__ = [
	"TypeId",
	"TypeKind",
	"NominalKey",
	"TypeDef",
	"TypeTable",
	"VariantFieldSchema",
	"VariantArmSchema",
	"VariantSchema",
	"VariantArmInstance",
	"VariantInstance",
]
