from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple

from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.core.span import Span
from lang2.driftc.core.types_core import TypeId, TypeKind, TypeTable
from lang2.driftc.method_registry import ModuleId


@dataclass(frozen=True)
class ImplMethodMeta:
	fn_id: FunctionId
	name: str
	is_pub: bool
	loc: Span | None = None


@dataclass(frozen=True)
class ImplMeta:
	impl_id: int
	def_module: str
	target_type_id: TypeId
	methods: List[ImplMethodMeta] = field(default_factory=list)
	loc: Span | None = None


@dataclass(frozen=True)
class ImplMethodCandidate:
	fn_id: FunctionId
	name: str
	def_module_id: ModuleId
	is_pub: bool
	impl_id: int
	impl_loc: Span | None = None
	method_loc: Span | None = None


class GlobalImplIndex:
	def __init__(self) -> None:
		self._by_target_method: Dict[Tuple[TypeId, str], List[ImplMethodCandidate]] = {}

	@staticmethod
	def _target_base_id(type_table: TypeTable, target_type_id: TypeId) -> TypeId | None:
		inst = type_table.get_struct_instance(target_type_id)
		if inst is not None:
			return inst.base_id
		td = type_table.get(target_type_id)
		if td.kind is TypeKind.STRUCT:
			return target_type_id
		return None

	def add_impl(
		self,
		*,
		impl: ImplMeta,
		type_table: TypeTable,
		module_ids: Dict[str | None, ModuleId],
	) -> None:
		base_id = self._target_base_id(type_table, impl.target_type_id)
		if base_id is None:
			return
		def_module_id = module_ids.setdefault(impl.def_module, len(module_ids))
		for method in impl.methods:
			cand = ImplMethodCandidate(
				fn_id=method.fn_id,
				name=method.name,
				def_module_id=def_module_id,
				is_pub=method.is_pub,
				impl_id=impl.impl_id,
				impl_loc=impl.loc,
				method_loc=method.loc,
			)
			self._by_target_method.setdefault((base_id, method.name), []).append(cand)

	@classmethod
	def from_module_exports(
		cls,
		*,
		module_exports: Dict[str, Dict[str, object]] | None,
		type_table: TypeTable,
		module_ids: Dict[str | None, ModuleId],
	) -> GlobalImplIndex:
		index = cls()
		if not isinstance(module_exports, dict):
			return index
		for mod, exp in module_exports.items():
			if not isinstance(exp, dict):
				continue
			impls = exp.get("impls")
			if not isinstance(impls, list):
				continue
			for impl in impls:
				if isinstance(impl, ImplMeta):
					index.add_impl(impl=impl, type_table=type_table, module_ids=module_ids)
		return index

	def get_candidates(self, receiver_base: TypeId, name: str) -> List[ImplMethodCandidate]:
		return list(self._by_target_method.get((receiver_base, name), []))


def find_impl_method_conflicts(
	*,
	module_exports: Dict[str, Dict[str, object]] | None,
	signatures_by_id: Dict[FunctionId, object],
	type_table: TypeTable,
	visible_modules_by_name: Dict[str, set[str]],
) -> List[Diagnostic]:
	"""Detect duplicate inherent method signatures across visible modules."""
	if not isinstance(module_exports, dict):
		return []
	impls: list[ImplMeta] = []
	for exp in module_exports.values():
		if isinstance(exp, dict):
			items = exp.get("impls")
			if isinstance(items, list):
				for impl in items:
					if isinstance(impl, ImplMeta):
						impls.append(impl)

	def _base_id(target_type_id: TypeId) -> TypeId | None:
		inst = type_table.get_struct_instance(target_type_id)
		if inst is not None:
			return inst.base_id
		td = type_table.get(target_type_id)
		if td.kind is TypeKind.STRUCT:
			return target_type_id
		return None

	def _type_label(type_id: TypeId) -> str:
		td = type_table.get(type_id)
		if td.module_id:
			return f"{td.module_id}::{td.name}"
		return td.name

	conflicts: list[Diagnostic] = []
	candidate_map: Dict[tuple[TypeId, str, tuple[tuple[TypeId, ...], TypeId | None]], list[ImplMethodCandidate]] = {}
	for impl in impls:
		base_id = _base_id(impl.target_type_id)
		if base_id is None:
			continue
		for method in impl.methods:
			sig = signatures_by_id.get(method.fn_id)
			param_ids = getattr(sig, "param_type_ids", None)
			return_id = getattr(sig, "return_type_id", None)
			if not param_ids:
				continue
			sig_key = (tuple(param_ids), return_id)
			cand = ImplMethodCandidate(
				fn_id=method.fn_id,
				name=method.name,
				def_module_id=ModuleId(-1),
				is_pub=method.is_pub,
				impl_id=impl.impl_id,
				impl_loc=impl.loc,
				method_loc=method.loc,
			)
			cand_key = (base_id, method.name, sig_key)
			candidate_map.setdefault(cand_key, []).append(cand)

	for (base_id, method_name, _sig_key), cands in candidate_map.items():
		if len(cands) < 2:
			continue
		by_module: dict[str, list[ImplMethodCandidate]] = {}
		for cand in cands:
			mod = signatures_by_id.get(cand.fn_id).module or ""
			by_module.setdefault(mod, []).append(cand)
		conflict = False
		for mod, mods in by_module.items():
			if len(mods) > 1:
				conflict = True
				break
		if not conflict:
			pub_modules = {mod for mod, mods in by_module.items() if any(c.is_pub for c in mods)}
			if len(pub_modules) < 2:
				continue
			for consumer, visible in visible_modules_by_name.items():
				if len(pub_modules & visible) >= 2:
					conflict = True
					break
		if not conflict:
			continue
		ordered = sorted(by_module.items(), key=lambda item: item[0])
		mod_names = [name for name, _mods in ordered if name]
		msg = (
			f"duplicate inherent method '{method_name}' for type '{_type_label(base_id)}' "
			f"in modules {', '.join(repr(m) for m in mod_names)}"
		)
		span = ordered[0][1][0].method_loc or ordered[0][1][0].impl_loc
		conflicts.append(Diagnostic(message=msg, severity="error", span=span))
	return conflicts


__all__ = [
	"ImplMeta",
	"ImplMethodMeta",
	"ImplMethodCandidate",
	"GlobalImplIndex",
	"find_impl_method_conflicts",
]
