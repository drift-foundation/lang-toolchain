# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 1 ConstShare structural synthesis — visibility regression
unit test (test #11a per
`work/constshare-substrate/post-link-mandatory-design.md` §5).

Synthesis qualifies fields via
`linked_world.visible_world(visible_modules_for_M)` where M is
the type-defining module — NOT via `linked_world.global_world`
directly.  This test pins that the visibility view actually
excludes impls from non-visible modules.

The end-to-end driver scenario "field type from a module not
imported by the type-defining module" is unconstructable in
pure Drift source — if M can name the type, M imports the type's
module, which transitively gives M visibility into the impl's
module.  So the regression is tested at the unit level: drive
`prove_is` against the visible-world projection, with an
`impl-bearing` module deliberately excluded from M's visibility,
and confirm `prove_is` returns REFUTED (not PROVED).

If this test fails, the regression is in the Phase 1
synthesizer's switch from `global_world` to `visible_world(M)`
— a global-world fallback would silently allow synthesis to
cross visibility boundaries the user's source code couldn't
have written.
"""
from __future__ import annotations

from lang.driftc.traits.linked_world import LinkedWorld, link_trait_worlds
from lang.driftc.traits.solver import Env as TraitEnv, ProofStatus, prove_is
from lang.driftc.traits.world import (
	ImplDef,
	TraitDef,
	TraitKey,
	TraitWorld,
	TypeKey,
)


_TRAIT_KEY = TraitKey(
	package_id="testpkg",
	module="testpkg.shareable",
	name="MyTrait",
)


def _empty_world_for(module_id: str) -> TraitWorld:
	w = TraitWorld()
	# Register the trait declaration in EVERY module's world (it's
	# expected to be in scope wherever it's queried).
	w.traits[_TRAIT_KEY] = TraitDef(
		key=_TRAIT_KEY,
		name="MyTrait",
		methods=[],
		require=None,
	)
	return w


def _add_impl(world: TraitWorld, *, target_module: str, target_name: str) -> None:
	target_key = TypeKey(
		package_id="testpkg",
		module=target_module,
		name=target_name,
		args=(),
	)
	target_head = target_key.head()
	impl_def = ImplDef(
		trait=_TRAIT_KEY,
		trait_args=(),
		target=target_key,
		target_head=target_head,
		methods=[],
		require=None,
		type_params=[],
	)
	impl_id = len(world.impls)
	world.impls.append(impl_def)
	world.impls_by_trait.setdefault(_TRAIT_KEY, []).append(impl_id)
	world.impls_by_target_head.setdefault(target_head, []).append(impl_id)
	world.impls_by_trait_target.setdefault((_TRAIT_KEY, target_head), []).append(impl_id)


def _build_linked_world() -> LinkedWorld:
	"""Constructs a linked world with two modules:
	  - `mod_consumer`: empty (no impls);
	  - `mod_provider`: contains `implement MyTrait for SomeType`.

	Both modules know about MyTrait (declared in
	`testpkg.shareable`).
	"""
	consumer_world = _empty_world_for("mod_consumer")
	provider_world = _empty_world_for("mod_provider")
	# Provider has the impl for SomeType (which conceptually lives
	# in mod_provider).
	_add_impl(provider_world, target_module="mod_provider", target_name="SomeType")
	# Trait declaration also needs to be in `linked_world`'s
	# trait_worlds so prove_is can find the trait_key.
	trait_world = _empty_world_for("testpkg.shareable")
	trait_worlds = {
		"mod_consumer": consumer_world,
		"mod_provider": provider_world,
		"testpkg.shareable": trait_world,
	}
	return link_trait_worlds(trait_worlds)


_SUBJECT = TypeKey(
	package_id="testpkg",
	module="mod_provider",
	name="SomeType",
	args=(),
)


def _env() -> TraitEnv:
	return TraitEnv(
		default_module="mod_consumer",
		default_package="testpkg",
		module_packages={
			"mod_consumer": "testpkg",
			"mod_provider": "testpkg",
			"testpkg.shareable": "testpkg",
		},
		type_table=None,
	)


def test_visible_world_excludes_non_visible_impls():
	"""When `mod_consumer`'s visible world is built WITHOUT
	`mod_provider`, the impl is NOT visible — `prove_is` returns
	non-PROVED."""
	linked = _build_linked_world()
	# Visible from mod_consumer: only itself and the trait module.
	# DELIBERATELY excludes mod_provider.
	visible = {"mod_consumer", "testpkg.shareable"}
	proof_world = linked.visible_world(visible)
	result = prove_is(proof_world, _env(), {}, _SUBJECT, _TRAIT_KEY)
	assert result.status is not ProofStatus.PROVED, (
		"impl in mod_provider must NOT be visible from mod_consumer's "
		"perspective when mod_provider is excluded from "
		"visible_module_names_by_name['mod_consumer'].  "
		f"Got status={result.status}.  This means synthesis would "
		"silently use a globally-present impl that user code in "
		"mod_consumer could not name through normal imports."
	)


def test_visible_world_includes_visible_impls():
	"""Compare control: when `mod_provider` IS visible, the impl
	resolves — `prove_is` returns PROVED.  Confirms the test #1
	signal comes from VISIBILITY, not from a fundamental issue
	with the linked world."""
	linked = _build_linked_world()
	visible = {"mod_consumer", "mod_provider", "testpkg.shareable"}
	proof_world = linked.visible_world(visible)
	result = prove_is(proof_world, _env(), {}, _SUBJECT, _TRAIT_KEY)
	assert result.status is ProofStatus.PROVED, (
		"control: with mod_provider in the visible set, the impl "
		f"MUST resolve.  Got status={result.status}."
	)


def test_global_world_sees_all_impls():
	"""Sanity check: the GLOBAL world sees impls from every
	module.  This is what a buggy synthesizer that bypassed
	`visible_world(M)` would see — and exactly what we DON'T
	want for qualification."""
	linked = _build_linked_world()
	# global_world contains everything regardless of visibility.
	result = prove_is(linked.global_world, _env(), {}, _SUBJECT, _TRAIT_KEY)
	assert result.status is ProofStatus.PROVED, (
		"global_world must contain the provider's impl.  This is "
		"the regression condition — if synthesis used global_world "
		"directly, it would silently see this impl from "
		"non-importing consumer modules."
	)
