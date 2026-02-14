from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable
from lang.driftc.impl_index import ImplMeta
from lang.driftc.method_registry import ModuleId
from lang.driftc.traits.world import TraitDef, TraitKey, TraitWorld


# Trait index diagnostics are typecheck-phase.
def _trait_diag(*args, **kwargs):
	if "phase" not in kwargs or kwargs.get("phase") is None:
		kwargs["phase"] = "typecheck"
	return Diagnostic(*args, **kwargs)


@dataclass(frozen=True)
class TraitMethodSig:
	trait: TraitKey
	name: str
	loc: Span | None = None


@dataclass(frozen=True)
class TraitImplCandidate:
	fn_id: FunctionId
	name: str
	trait: TraitKey
	def_module_id: ModuleId
	is_pub: bool
	impl_id: int
	fn_symbol: str | None = None
	impl_loc: Span | None = None
	method_loc: Span | None = None
	require_expr: object | None = None


class GlobalTraitIndex:
	def __init__(self) -> None:
		self.traits_by_id: Dict[TraitKey, TraitDef] = {}
		self.trait_methods: Dict[Tuple[TraitKey, str], TraitMethodSig] = {}
		self.missing_traits: set[TraitKey] = set()

	def add_trait(self, trait_key: TraitKey, trait: TraitDef) -> None:
		self.traits_by_id.setdefault(trait_key, trait)
		for method in getattr(trait, "methods", []) or []:
			self.trait_methods.setdefault(
				(trait_key, method.name),
				TraitMethodSig(trait=trait_key, name=method.name, loc=Span.from_loc(getattr(method, "loc", None))),
			)

	@classmethod
	def from_trait_worlds(cls, trait_worlds: Dict[str, TraitWorld] | None) -> "GlobalTraitIndex":
		index = cls()
		if not isinstance(trait_worlds, dict):
			return index
		for _mid, world in trait_worlds.items():
			for trait_key, trait in getattr(world, "traits", {}).items():
				index.add_trait(trait_key, trait)
		return index

	def has_method(self, trait_key: TraitKey, name: str) -> bool:
		return (trait_key, name) in self.trait_methods

	def mark_missing(self, trait_key: TraitKey) -> None:
		self.missing_traits.add(trait_key)

	def is_missing(self, trait_key: TraitKey) -> bool:
		return trait_key in self.missing_traits


class GlobalTraitImplIndex:
	def __init__(self) -> None:
		self._by_trait_target_method: Dict[Tuple[TraitKey, TypeId, str], List[TraitImplCandidate]] = {}
		self._seen_impl_methods: set[tuple[TraitKey, TypeId, str, ModuleId, int, FunctionId]] = set()
		self._trait_by_fn_id: Dict[FunctionId, TraitKey] = {}
		self._require_by_fn_id: Dict[FunctionId, object] = {}
		self.missing_modules: set[ModuleId] = set()
		self.module_names_by_id: Dict[ModuleId, str] = {}

	@staticmethod
	def _target_base_id(type_table: TypeTable, target_type_id: TypeId) -> TypeId | None:
		td = type_table.get(target_type_id)
		if td.kind is TypeKind.REF and td.param_types:
			inner_base = GlobalTraitImplIndex._target_base_id(type_table, td.param_types[0])
			if inner_base is not None:
				return inner_base
		inst = type_table.get_struct_instance(target_type_id)
		if inst is not None:
			return inst.base_id
		vinst = type_table.get_variant_instance(target_type_id)
		if vinst is not None:
			return vinst.base_id
		if td.kind is TypeKind.ARRAY:
			return type_table.array_base_id()
		if td.kind is TypeKind.STRUCT:
			return target_type_id
		if td.kind is TypeKind.VARIANT:
			return target_type_id
		if td.kind is TypeKind.DIAGNOSTICVALUE:
			return target_type_id
		if td.kind is TypeKind.SCALAR:
			return target_type_id
		return None

	def add_impl(
		self,
		*,
		impl: ImplMeta,
		type_table: TypeTable,
		module_ids: Dict[str | None, ModuleId],
	) -> None:
		if impl.trait_key is None:
			return
		base_id = self._target_base_id(type_table, impl.target_type_id)
		if base_id is None:
			return
		def_module_id = module_ids.setdefault(impl.def_module, len(module_ids))
		for method in impl.methods:
			seen_key = (impl.trait_key, base_id, method.name, def_module_id, impl.impl_id, method.fn_id)
			if seen_key in self._seen_impl_methods:
				continue
			self._seen_impl_methods.add(seen_key)
			fn_symbol = method.fn_symbol
			if fn_symbol is None:
				fn_symbol = function_symbol(method.fn_id)
			cand = TraitImplCandidate(
				fn_id=method.fn_id,
				name=method.name,
				trait=impl.trait_key,
				def_module_id=def_module_id,
				is_pub=method.is_pub,
				impl_id=impl.impl_id,
				fn_symbol=fn_symbol,
				impl_loc=impl.loc,
				method_loc=method.loc,
				require_expr=impl.require_expr,
			)
			self._by_trait_target_method.setdefault((impl.trait_key, base_id, method.name), []).append(cand)
			self._trait_by_fn_id.setdefault(method.fn_id, impl.trait_key)
			if impl.require_expr is not None:
				self._require_by_fn_id.setdefault(method.fn_id, impl.require_expr)

	def trait_key_for_fn_id(self, fn_id: FunctionId) -> TraitKey | None:
		return self._trait_by_fn_id.get(fn_id)

	def require_for_fn_id(self, fn_id: FunctionId) -> object | None:
		return self._require_by_fn_id.get(fn_id)

	def candidates_for_target_method(self, receiver_base: TypeId, name: str) -> List[TraitImplCandidate]:
		out: List[TraitImplCandidate] = []
		for (trait_key, base_id, method_name), cands in self._by_trait_target_method.items():
			if base_id == receiver_base and method_name == name:
				out.extend(cands)
		return out

	@classmethod
	def from_module_exports(
		cls,
		*,
		module_exports: Dict[str, Dict[str, object]] | None,
		type_table: TypeTable,
		module_ids: Dict[str | None, ModuleId],
	) -> "GlobalTraitImplIndex":
		index = cls()
		if not isinstance(module_exports, dict):
			return index
		for _mod, exp in module_exports.items():
			if not isinstance(exp, dict):
				continue
			impls = exp.get("impls")
			if not isinstance(impls, list):
				continue
			for impl in impls:
				if isinstance(impl, ImplMeta):
					index.add_impl(impl=impl, type_table=type_table, module_ids=module_ids)
		return index

	def get_candidates(self, trait_key: TraitKey, receiver_base: TypeId, name: str) -> List[TraitImplCandidate]:
		return list(self._by_trait_target_method.get((trait_key, receiver_base, name), []))

	def mark_missing_module(self, module_id: ModuleId) -> None:
		self.missing_modules.add(module_id)


def validate_trait_scopes(
	*,
	trait_index: GlobalTraitIndex,
	trait_impl_index: GlobalTraitImplIndex,
	trait_scope_by_module: Dict[str, List[TraitKey]] | None,
	module_ids: Dict[str | None, ModuleId] | None,
) -> List[Diagnostic]:
	diags: List[Diagnostic] = []
	if not trait_scope_by_module and not getattr(trait_impl_index, "_by_trait_target_method", {}):
		return diags

	module_names_by_id: Dict[ModuleId, str] = {}
	if module_ids:
		module_names_by_id = {mid: name for name, mid in module_ids.items() if name is not None}

	def _trait_label(trait_key: TraitKey) -> str:
		base = f"{trait_key.module}.{trait_key.name}" if trait_key.module else trait_key.name
		if trait_key.package_id:
			return f"{trait_key.package_id}::{base}"
		return base

	seen_scope: set[TraitKey] = set()
	for mod_name, trait_keys in trait_scope_by_module.items():
		for trait_key in trait_keys:
			if trait_key in trait_index.traits_by_id:
				continue
			if trait_index.is_missing(trait_key):
				continue
			if trait_key in seen_scope:
				continue
			seen_scope.add(trait_key)
			diags.append(
				_trait_diag(
					message=(
						f"trait '{_trait_label(trait_key)}' in scope for module '{mod_name}' "
						"does not exist in the trait index (scope)"
					),
					severity="error",
					span=Span(),
				)
			)

	seen_impl: set[TraitKey] = set()
	for (trait_key, _base_id, _name), cands in getattr(trait_impl_index, "_by_trait_target_method", {}).items():
		if trait_key in trait_index.traits_by_id:
			continue
		if trait_index.is_missing(trait_key):
			continue
		if trait_key in seen_impl:
			continue
		seen_impl.add(trait_key)
		modules = {
			module_names_by_id.get(c.def_module_id, str(c.def_module_id))
			for c in cands
			if isinstance(c, TraitImplCandidate)
		}
		mod_list = ", ".join(sorted(modules)) if modules else "<unknown>"
		diags.append(
			_trait_diag(
				message=(
					f"trait '{_trait_label(trait_key)}' referenced by impls in modules {mod_list} "
					"does not exist in the trait index (impls)"
				),
				severity="error",
				span=Span(),
			)
		)

	return diags


__all__ = [
	"TraitMethodSig",
	"TraitImplCandidate",
	"GlobalTraitIndex",
	"GlobalTraitImplIndex",
	"validate_trait_scopes",
]
