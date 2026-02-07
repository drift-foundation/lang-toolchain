from pathlib import Path

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.impl_index import ImplMeta
from lang.driftc.method_registry import ModuleId
from lang.driftc.trait_index import (
	GlobalTraitImplIndex,
	GlobalTraitIndex,
	TraitImplCandidate,
	validate_trait_scopes,
)
from lang.driftc.traits.world import TraitKey


def test_trait_scope_missing_from_index_reports_error() -> None:
	trait_index = GlobalTraitIndex()
	trait_impl_index = GlobalTraitImplIndex()
	module_ids: dict[str | None, ModuleId] = {"m_main": 0, "ext": 1}
	trait_scope_by_module = {"m_main": [TraitKey(package_id="__local__", module="ext", name="Show")]}

	diags = validate_trait_scopes(
		trait_index=trait_index,
		trait_impl_index=trait_impl_index,
		trait_scope_by_module=trait_scope_by_module,
		module_ids=module_ids,
	)

	assert diags
	assert any("scope for module 'm_main'" in d.message for d in diags)
	assert any("ext.Show" in d.message for d in diags)


def test_trait_impl_missing_from_index_reports_error() -> None:
	trait_index = GlobalTraitIndex()
	trait_impl_index = GlobalTraitImplIndex()
	module_ids: dict[str | None, ModuleId] = {"m_impl": 0}
	missing_trait = TraitKey(package_id="__local__", module="m_trait", name="Show")
	fn_id = FunctionId(module="m_impl", name="show", ordinal=0)
	cand = TraitImplCandidate(
		fn_id=fn_id,
		name="show",
		trait=missing_trait,
		def_module_id=module_ids["m_impl"],
		is_pub=True,
		impl_id=0,
	)
	trait_impl_index._by_trait_target_method[(missing_trait, 1, "show")] = [cand]

	diags = validate_trait_scopes(
		trait_index=trait_index,
		trait_impl_index=trait_impl_index,
		trait_scope_by_module={},
		module_ids=module_ids,
	)

	assert diags
	assert any("impls" in d.message for d in diags)
	assert any("m_trait.Show" in d.message for d in diags)
