# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.function_key import FunctionKey
from lang.driftc.traits.world import ImplKey, TraitKey, TypeKey


@dataclass
class IdRegistry:
	"""
	Intern stable keys to internal ids.

	This keeps the boundary between stable keys (package-scoped identities)
	and in-memory numeric ids explicit.
	"""

	_fn_key_to_id: Dict[FunctionKey, FunctionId] = field(default_factory=dict)
	_fn_id_to_key: Dict[FunctionId, FunctionKey] = field(default_factory=dict)
	_next_fn_ordinal: Dict[Tuple[str, str], int] = field(default_factory=dict)
	_used_fn_ordinals: Dict[Tuple[str, str], set[int]] = field(default_factory=dict)
	_trait_key_to_id: Dict[TraitKey, int] = field(default_factory=dict)
	_trait_id_to_key: Dict[int, TraitKey] = field(default_factory=dict)
	_next_trait_id: int = 0
	_type_key_to_id: Dict[TypeKey, int] = field(default_factory=dict)
	_type_id_to_key: Dict[int, TypeKey] = field(default_factory=dict)
	_next_type_id: int = 0
	_impl_key_to_id: Dict[ImplKey, int] = field(default_factory=dict)
	_impl_id_to_key: Dict[int, ImplKey] = field(default_factory=dict)
	_next_impl_id: int = 0

	def intern_function(self, key: FunctionKey, *, preferred: FunctionId | None = None) -> FunctionId:
		existing = self._fn_key_to_id.get(key)
		if existing is not None:
			if preferred is not None and existing != preferred:
				raise ValueError("function key already interned with a different FunctionId")
			return existing
		if preferred is not None:
			prev_key = self._fn_id_to_key.get(preferred)
			if prev_key is not None and prev_key != key:
				raise ValueError("FunctionId already interned with a different FunctionKey")
			self._fn_key_to_id[key] = preferred
			self._fn_id_to_key[preferred] = key
			self._reserve_fn_id(preferred)
			return preferred
		fn_id = self._mint_fn_id(key)
		self._fn_key_to_id[key] = fn_id
		self._fn_id_to_key[fn_id] = key
		return fn_id

	def function_key_for_id(self, fn_id: FunctionId) -> FunctionKey | None:
		return self._fn_id_to_key.get(fn_id)

	def function_id_for_key(self, key: FunctionKey) -> FunctionId | None:
		return self._fn_key_to_id.get(key)

	def function_keys_by_id(self) -> Dict[FunctionId, FunctionKey]:
		return dict(self._fn_id_to_key)

	def intern_trait(self, key: TraitKey, *, preferred: int | None = None) -> int:
		existing = self._trait_key_to_id.get(key)
		if existing is not None:
			if preferred is not None and existing != preferred:
				raise ValueError("trait key already interned with a different id")
			return existing
		if preferred is not None:
			prev_key = self._trait_id_to_key.get(preferred)
			if prev_key is not None and prev_key != key:
				raise ValueError("trait id already interned with a different key")
			self._trait_key_to_id[key] = preferred
			self._trait_id_to_key[preferred] = key
			if preferred >= self._next_trait_id:
				self._next_trait_id = preferred + 1
			return preferred
		trait_id = self._next_trait_id
		self._next_trait_id += 1
		self._trait_key_to_id[key] = trait_id
		self._trait_id_to_key[trait_id] = key
		return trait_id

	def trait_key_for_id(self, trait_id: int) -> TraitKey | None:
		return self._trait_id_to_key.get(trait_id)

	def intern_type(self, key: TypeKey, *, preferred: int | None = None) -> int:
		existing = self._type_key_to_id.get(key)
		if existing is not None:
			if preferred is not None and existing != preferred:
				raise ValueError("type key already interned with a different id")
			return existing
		if preferred is not None:
			prev_key = self._type_id_to_key.get(preferred)
			if prev_key is not None and prev_key != key:
				raise ValueError("type id already interned with a different key")
			self._type_key_to_id[key] = preferred
			self._type_id_to_key[preferred] = key
			if preferred >= self._next_type_id:
				self._next_type_id = preferred + 1
			return preferred
		type_id = self._next_type_id
		self._next_type_id += 1
		self._type_key_to_id[key] = type_id
		self._type_id_to_key[type_id] = key
		return type_id

	def type_key_for_id(self, type_id: int) -> TypeKey | None:
		return self._type_id_to_key.get(type_id)

	def intern_impl(self, key: ImplKey, *, preferred: int | None = None) -> int:
		existing = self._impl_key_to_id.get(key)
		if existing is not None:
			if preferred is not None and existing != preferred:
				raise ValueError("impl key already interned with a different id")
			return existing
		if preferred is not None:
			prev_key = self._impl_id_to_key.get(preferred)
			if prev_key is not None and prev_key != key:
				raise ValueError("impl id already interned with a different key")
			self._impl_key_to_id[key] = preferred
			self._impl_id_to_key[preferred] = key
			if preferred >= self._next_impl_id:
				self._next_impl_id = preferred + 1
			return preferred
		impl_id = self._next_impl_id
		self._next_impl_id += 1
		self._impl_key_to_id[key] = impl_id
		self._impl_id_to_key[impl_id] = key
		return impl_id

	def impl_key_for_id(self, impl_id: int) -> ImplKey | None:
		return self._impl_id_to_key.get(impl_id)

	def _reserve_fn_id(self, fn_id: FunctionId) -> None:
		key = (fn_id.module, fn_id.name)
		used = self._used_fn_ordinals.setdefault(key, set())
		used.add(fn_id.ordinal)
		next_ord = self._next_fn_ordinal.get(key, 0)
		if fn_id.ordinal >= next_ord:
			self._next_fn_ordinal[key] = fn_id.ordinal + 1

	def _mint_fn_id(self, key: FunctionKey) -> FunctionId:
		name_key = (key.module_path, key.name)
		used = self._used_fn_ordinals.setdefault(name_key, set())
		ordinal = self._next_fn_ordinal.get(name_key, 0)
		while ordinal in used:
			ordinal += 1
		fn_id = FunctionId(module=key.module_path, name=key.name, ordinal=ordinal)
		self._reserve_fn_id(fn_id)
		return fn_id


__all__ = ["IdRegistry"]
