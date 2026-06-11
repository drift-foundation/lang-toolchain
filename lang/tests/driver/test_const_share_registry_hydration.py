# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression for the LB-7 idempotence split: a PREPARED TypeTable (semantic
ConstShare synthesis already performed) reused with a FRESH pipeline-owned
callable registry must still get its synthesized `const_share` methods registered
— WITHOUT re-running semantic synthesis (which excludes already-covered targets
and would register nothing) and WITHOUT duplicating impls in `module_exports`.

`hydrate_const_share_registry` is the registry-only counterpart of
`synthesize_const_share_phase1`; this pins that it (a) registers a synthesized
method missing from a fresh registry, (b) leaves the `module_exports` impl set
unchanged, (c) is idempotent, (d) matches the canonical ConstShare TraitKey by
equality (not just by name), and (e) treats a missing synthesized signature as an
internal invariant failure rather than silently marking the registry complete.
"""
from __future__ import annotations

from collections import namedtuple
from types import SimpleNamespace

import pytest

from lang.driftc.const_share_synth import hydrate_const_share_registry
from lang.driftc.method_registry import CallableRegistry
from lang.driftc.core.function_id import FunctionId

# Hashable + value-equal TraitKey stand-in (real TraitKey is matched by equality
# and used as a dict key in the trait world).
_TKey = namedtuple("_TKey", ["name", "module", "package_id"])


def _canonical_cs_key():
	"""Stand-in for the resolved canonical `std.core.shareable.ConstShare` TraitKey.
	Matched by equality, so an impl's trait_key must compare EQUAL to count."""
	return _TKey(name="ConstShare", module="std.core.shareable", package_id="std")


def _scenario(*, impl_trait_key=None, sig=None, missing_sig=False):
	canonical = _canonical_cs_key()
	fn_id = FunctionId(module="m", name="CS::const_share", ordinal=0)
	method = SimpleNamespace(name="const_share", fn_id=fn_id)
	impl = SimpleNamespace(
		trait_key=canonical if impl_trait_key is None else impl_trait_key,
		methods=[method],
		target_type_id=10,
		impl_type_params=[],
		def_module="m",
	)
	module_exports = {"m": {"impls": [impl]}}
	signatures_by_id = {}
	if not missing_sig:
		signatures_by_id[fn_id] = sig or SimpleNamespace(param_type_ids=[11], return_type_id=10)
	module_ids = {None: 0, "m": 1}
	return fn_id, module_exports, signatures_by_id, module_ids, canonical


def test_fresh_registry_is_hydrated_with_synthesized_method() -> None:
	fn_id, module_exports, signatures_by_id, module_ids, key = _scenario()
	reg = CallableRegistry()
	assert reg.get_by_fn_id(fn_id) is None, "method must be absent from a fresh registry"

	impls_before = len(module_exports["m"]["impls"])
	box = [1]
	n = hydrate_const_share_registry(
		module_exports=module_exports,
		signatures_by_id=signatures_by_id,
		callable_registry=reg,
		module_ids=module_ids,
		next_callable_id_box=box,
		const_share_trait_key=key,
	)
	assert n == 1, "exactly one synthesized const_share should be hydrated"
	assert reg.get_by_fn_id(fn_id) is not None, "synthesized method must now be in the registry"
	# Semantic state untouched: no re-derivation, no duplicated impls.
	assert len(module_exports["m"]["impls"]) == impls_before, "module_exports impls must be unchanged"


def test_hydration_is_idempotent_on_an_already_populated_registry() -> None:
	fn_id, module_exports, signatures_by_id, module_ids, key = _scenario()
	reg = CallableRegistry()
	box = [1]
	first = hydrate_const_share_registry(
		module_exports=module_exports, signatures_by_id=signatures_by_id,
		callable_registry=reg, module_ids=module_ids, next_callable_id_box=box,
		const_share_trait_key=key,
	)
	assert first == 1
	# Re-running over the same (now populated) registry registers nothing more.
	second = hydrate_const_share_registry(
		module_exports=module_exports, signatures_by_id=signatures_by_id,
		callable_registry=reg, module_ids=module_ids, next_callable_id_box=box,
		const_share_trait_key=key,
	)
	assert second == 0, "already-registered method must not be hydrated twice"


def test_same_named_unrelated_trait_is_not_hydrated() -> None:
	"""An impl of a trait merely NAMED `ConstShare` from a different module/package
	must NOT be hydrated as the canonical `std.core.shareable.ConstShare` — matched
	by full TraitKey equality, not by name."""
	other = _TKey(name="ConstShare", module="other.mod", package_id="other")
	fn_id, module_exports, signatures_by_id, module_ids, canonical = _scenario(impl_trait_key=other)
	reg = CallableRegistry()
	box = [1]
	n = hydrate_const_share_registry(
		module_exports=module_exports, signatures_by_id=signatures_by_id,
		callable_registry=reg, module_ids=module_ids, next_callable_id_box=box,
		const_share_trait_key=canonical,
	)
	assert n == 0, "a same-named trait from another module must not be hydrated"
	assert reg.get_by_fn_id(fn_id) is None


def test_missing_synthesized_signature_is_an_invariant_failure() -> None:
	"""A canonical ConstShare method present in module_exports but missing its
	signature is an internal invariant violation — it must raise, never silently
	mark the registry hydrated while skipping the method."""
	_fn_id, module_exports, signatures_by_id, module_ids, key = _scenario(missing_sig=True)
	reg = CallableRegistry()
	box = [1]
	with pytest.raises(AssertionError, match="hydration invariant"):
		hydrate_const_share_registry(
			module_exports=module_exports, signatures_by_id=signatures_by_id,
			callable_registry=reg, module_ids=module_ids, next_callable_id_box=box,
			const_share_trait_key=key,
		)


def test_helper_hydrates_prepared_typetable_with_fresh_frozen_registry() -> None:
	"""Integration over `_run_post_link_const_share_synthesis` itself: a PREPARED
	TypeTable (`_const_share_synthesized=True`) reused with a FRESH, FROZEN pipeline
	registry must take the hydration path — register the synthesized method, restore
	the registry's frozen state, and set `_const_share_hydrated` — resolving the
	canonical ConstShare key from the linked world, without re-running semantic
	synthesis."""
	from lang.driftc.driftc import _run_post_link_const_share_synthesis

	fn_id, module_exports, signatures_by_id, module_ids, canonical = _scenario()
	# Linked world exposing the canonical ConstShare trait so the helper's
	# `resolve_const_share_trait_key` returns the SAME key the impl carries.
	linked_world = SimpleNamespace(global_world=SimpleNamespace(traits={canonical: object()}))
	type_table = SimpleNamespace(_const_share_synthesized=True, module_packages={})
	reg = CallableRegistry()
	reg._frozen = True  # pipeline registries arrive frozen
	impls_before = len(module_exports["m"]["impls"])

	out_id = _run_post_link_const_share_synthesis(
		linked_world=linked_world,
		type_table=type_table,
		module_exports=module_exports,
		signatures_by_id=signatures_by_id,
		normalized_hirs_by_id={},
		func_hirs_by_id={},
		fn_ids_by_name={},
		module_ids=module_ids,
		visible_module_names_by_name=None,
		package_id="app",
		module_packages={},
		callable_registry=reg,
		next_callable_id=100,
		source_modules=set(),
	)

	assert reg.get_by_fn_id(fn_id) is not None, "fresh registry must be hydrated with the synthesized method"
	assert reg._frozen is True, "registry frozen state must be restored after hydration"
	assert getattr(reg, "_const_share_hydrated", False) is True, "registry must be marked hydrated"
	assert out_id > 100, "a callable id should have been allocated for the hydrated method"
	assert len(module_exports["m"]["impls"]) == impls_before, "no semantic re-derivation (impls unchanged)"

	# Already hydrated -> second call is a no-op (registry gate).
	again = _run_post_link_const_share_synthesis(
		linked_world=linked_world, type_table=type_table, module_exports=module_exports,
		signatures_by_id=signatures_by_id, normalized_hirs_by_id={}, func_hirs_by_id={},
		fn_ids_by_name={}, module_ids=module_ids, visible_module_names_by_name=None,
		package_id="app", module_packages={}, callable_registry=reg,
		next_callable_id=out_id, source_modules=set(),
	)
	assert again == out_id, "an already-hydrated registry must be a no-op"
