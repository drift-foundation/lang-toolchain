from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple, TYPE_CHECKING

from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable
from lang.driftc.method_registry import ModuleId
if TYPE_CHECKING:
	from lang.driftc.traits.world import TraitKey
else:
	TraitKey = object  # type: ignore[misc]


# Impl index diagnostics are typecheck-phase.
def _impl_diag(*args, **kwargs):
	if "phase" not in kwargs or kwargs.get("phase") is None:
		kwargs["phase"] = "typecheck"
	return Diagnostic(*args, **kwargs)


@dataclass(frozen=True)
class ImplMethodMeta:
	fn_id: FunctionId
	name: str
	is_pub: bool
	fn_symbol: str | None = None
	loc: Span | None = None


@dataclass(frozen=True)
class ImplMeta:
	impl_id: int
	def_module: str
	target_type_id: TypeId
	trait_key: TraitKey | None = None
	trait_expr: object | None = None
	trait_args: List[TypeId] = field(default_factory=list)
	require_expr: object | None = None
	target_expr: object | None = None
	impl_type_params: List[str] = field(default_factory=list)
	methods: List[ImplMethodMeta] = field(default_factory=list)
	loc: Span | None = None


@dataclass(frozen=True)
class ImplMethodCandidate:
	fn_id: FunctionId
	name: str
	def_module_id: ModuleId
	is_pub: bool
	impl_id: int
	fn_symbol: str | None = None
	impl_loc: Span | None = None
	method_loc: Span | None = None


class GlobalImplIndex:
	def __init__(self) -> None:
		self._by_target_method: Dict[Tuple[TypeId, str], List[ImplMethodCandidate]] = {}
		self._seen_impl_methods: set[tuple[TypeId, str, ModuleId, int, FunctionId]] = set()

	@staticmethod
	def _target_base_id(type_table: TypeTable, target_type_id: TypeId) -> TypeId | None:
		inst = type_table.get_struct_instance(target_type_id)
		if inst is not None:
			return inst.base_id
		vinst = type_table.get_variant_instance(target_type_id)
		if vinst is not None:
			return vinst.base_id
		td = type_table.get(target_type_id)
		if td.kind is TypeKind.ARRAY:
			return type_table.array_base_id()
		if td.kind is TypeKind.STRUCT:
			return target_type_id
		if td.kind is TypeKind.VARIANT:
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
		if impl.trait_key is not None:
			return
		base_id = self._target_base_id(type_table, impl.target_type_id)
		if base_id is None:
			return
		def_module_id = module_ids.setdefault(impl.def_module, len(module_ids))
		for method in impl.methods:
			seen_key = (base_id, method.name, def_module_id, impl.impl_id, method.fn_id)
			if seen_key in self._seen_impl_methods:
				continue
			self._seen_impl_methods.add(seen_key)
			fn_symbol = method.fn_symbol
			if fn_symbol is None:
				fn_symbol = function_symbol(method.fn_id)
			cand = ImplMethodCandidate(
				fn_id=method.fn_id,
				name=method.name,
				def_module_id=def_module_id,
				is_pub=method.is_pub,
				impl_id=impl.impl_id,
				fn_symbol=fn_symbol,
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
		vinst = type_table.get_variant_instance(target_type_id)
		if vinst is not None:
			return vinst.base_id
		td = type_table.get(target_type_id)
		if td.kind is TypeKind.ARRAY:
			return type_table.array_base_id()
		if td.kind is TypeKind.STRUCT:
			return target_type_id
		if td.kind is TypeKind.VARIANT:
			return target_type_id
		if td.kind is TypeKind.SCALAR:
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
		if impl.trait_key is not None:
			continue
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
		conflicts.append(_impl_diag(message=msg, severity="error", span=span))
	return conflicts


def find_param_mode_overload_conflicts(
	*,
	callable_registry: object,
	signatures_by_id: Dict[FunctionId, object],
	type_table: TypeTable,
) -> List[Diagnostic]:
	"""R2 (reject-redundant-call-borrows): two concrete overloads whose
	signatures are identical after erasing each NON-RECEIVER parameter's
	outer mode among {T, &T, &mut T} are rejected at definition site —
	call-site `&` no longer selects overloads, so such sets are
	uncallable (bare calls are ambiguous). Receiver (`self`) modes stay
	out of the erasure: receiver-mode overloading is a separate, legal
	mechanism (the receiver slot is dropped from the key; sets differing
	ONLY in receiver mode never group as R2 conflicts because their
	non-receiver erased keys are identical AND their modes tuples are
	identical). Generic candidates are exempt per D4 — concrete-beats-
	generic handles mixed sets."""
	by_id = getattr(callable_registry, "_by_id", None)
	if not isinstance(by_id, dict):
		return []
	groups: Dict[tuple, list] = {}
	for decl in by_id.values():
		if getattr(decl, "is_generic", False):
			continue
		sig = getattr(decl, "signature", None)
		param_ids = list(getattr(sig, "param_types", ()) or ())
		name = getattr(decl, "name", None)
		if not name or not param_ids and getattr(decl, "kind", None) is None:
			continue
		kind = getattr(decl, "kind", None)
		kind_name = getattr(kind, "name", "")
		is_method = kind_name in ("METHOD_INHERENT", "METHOD_TRAIT")
		slots = param_ids[1:] if is_method and param_ids else list(param_ids)
		recv_key = param_ids[0] if is_method and param_ids else None
		erased = []
		modes = []
		for pid in slots:
			td = type_table.get(pid)
			if td.kind is TypeKind.REF and td.param_types:
				erased.append(td.param_types[0])
				modes.append("mut" if td.ref_mut else "ref")
			else:
				erased.append(pid)
				modes.append("val")
		groups.setdefault(
			(
				getattr(decl, "module_id", None),
				getattr(decl, "impl_target_type_id", None),
				recv_key,
				name,
				len(slots),
				tuple(erased),
			),
			[],
		).append((decl, tuple(modes)))
	out: list[Diagnostic] = []
	for (_mod, _tgt, _recv, name, _n, _key), cands in groups.items():
		if len(cands) < 2:
			continue
		if len({m for _d, m in cands}) < 2:
			continue  # identical modes = exact duplicate; other checks own it
		span = None
		for decl, _m in cands:
			fnid = getattr(decl, "fn_id", None)
			s = signatures_by_id.get(fnid) if fnid is not None else None
			span = getattr(s, "loc", None) if s is not None else None
			if span is not None:
				break
		shapes = sorted({"(" + ", ".join(m) + ")" for _d, m in cands})
		out.append(
			_impl_diag(
				message=(
					f"overload set for '{name}' differs only by parameter mode "
					f"(T vs &T vs &mut T) on non-receiver parameters ({' vs '.join(shapes)}); "
					f"call-site `&` no longer selects overloads, so this set is "
					f"uncallable — rename or merge one overload"
				),
				code="E_OVERLOAD_PARAM_MODE_ONLY_DIFF",
				severity="error",
				span=span,
			)
		)
	return out


__all__ = [
	"ImplMeta",
	"ImplMethodMeta",
	"ImplMethodCandidate",
	"GlobalImplIndex",
	"find_impl_method_conflicts",
	"find_param_mode_overload_conflicts",
]
