# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass

from lang.driftc.core.function_key import FunctionKey, function_key_str
from lang.driftc.core.types_core import TypeId
from lang.driftc.core.xxhash64 import hash64
from lang.driftc.traits.world import TypeKey, normalize_type_key, type_key_from_typeid, type_key_str


@dataclass(frozen=True)
class AbiFlags:
	can_throw: bool


@dataclass(frozen=True)
class InstantiationKey:
	generic_def_key: FunctionKey
	type_args: tuple[TypeKey, ...]
	trait_args: tuple[object, ...]
	abi: AbiFlags


def build_instantiation_key(
	fn_key: FunctionKey,
	type_args: tuple[TypeId, ...],
	*,
	type_table: object,
	can_throw: bool,
) -> InstantiationKey:
	module_id = fn_key.module_path or "main"
	type_keys = tuple(
		normalize_type_key(
			type_key_from_typeid(type_table, tid),
			module_name=module_id,
			default_package=getattr(type_table, "package_id", None),
			module_packages=getattr(type_table, "module_packages", None),
		)
		for tid in type_args
	)
	abi = AbiFlags(can_throw=can_throw)
	return InstantiationKey(
		generic_def_key=fn_key,
		type_args=type_keys,
		trait_args=(),
		abi=abi,
	)


def instantiation_key_str(key: InstantiationKey) -> str:
	base = function_key_str(key.generic_def_key)
	args = ",".join(type_key_str(k) for k in key.type_args)
	abi = f"can_throw={int(key.abi.can_throw)}"
	return f"{base}|{args}|{abi}"


def instantiation_key_hash(key: InstantiationKey) -> str:
	h = hash64(instantiation_key_str(key).encode("utf-8"))
	return f"{h:016x}"
