# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.types_core import TypeTable
from lang.driftc.traits.linked_world import LinkedWorld, RequireEnv, build_require_env, link_trait_worlds
from lang.driftc.driftc import _install_copy_query, _install_diagnostic_query
from lang.driftc.module_lowered import ModuleLowered
from lang.driftc.traits.world import TypeKey


def assert_module_lowered_consistent(module: ModuleLowered) -> None:
	for fn_id, sig in module.signatures_by_id.items():
		if fn_id.module != module.module_id:
			raise AssertionError(f"signature fn_id {fn_id} does not match module {module.module_id}")
		if getattr(sig, "module", None) != module.module_id:
			raise AssertionError(f"signature module {getattr(sig, 'module', None)} != {module.module_id}")
	for name, ids in module.fn_ids_by_name.items():
		for fn_id in ids:
			if fn_id not in module.signatures_by_id:
				raise AssertionError(f"fn_ids_by_name[{name}] missing signature for {fn_id}")
	for fn_id in module.requires_by_fn.keys():
		if fn_id not in module.signatures_by_id:
			raise AssertionError(f"requires_by_fn has unknown fn_id {fn_id}")
	for key in module.requires_by_struct.keys():
		if not isinstance(key, TypeKey):
			raise AssertionError(f"requires_by_struct key is not a TypeKey: {key}")


def build_linked_world(type_table: TypeTable | None) -> tuple[LinkedWorld, RequireEnv]:
	"""
	Shared helper for tests: build LinkedWorld + RequireEnv from a TypeTable.

	This centralizes the linking logic so test harnesses don't drift from the
	compiler pipeline.
	"""
	trait_worlds = getattr(type_table, "trait_worlds", {}) if type_table is not None else {}
	if not isinstance(trait_worlds, dict):
		trait_worlds = {}
	linked_world = link_trait_worlds(trait_worlds)
	if type_table is not None:
		_install_copy_query(type_table, linked_world)
		_install_diagnostic_query(type_table, linked_world)
	default_package = getattr(type_table, "package_id", None)
	module_packages = getattr(type_table, "module_packages", None)
	return linked_world, build_require_env(
		linked_world,
		default_package=default_package,
		module_packages=module_packages,
	)
