# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for SemanticWorld lifecycle and freeze semantics."""

from __future__ import annotations

import pytest

from lang.driftc.core.semantic_world import SemanticWorld, WorldPhase
from lang.driftc.core.types_core import TypeTable


def test_phase_progression_monotonic() -> None:
	world = SemanticWorld()
	assert world.phase is WorldPhase.EMPTY
	world.advance_to(WorldPhase.PACKAGES_READY)
	assert world.phase is WorldPhase.PACKAGES_READY
	world.advance_to(WorldPhase.READY)
	assert world.phase is WorldPhase.READY


def test_phase_cannot_go_backward() -> None:
	world = SemanticWorld()
	world.advance_to(WorldPhase.READY)
	with pytest.raises(RuntimeError, match="cannot move world backward"):
		world.advance_to(WorldPhase.PACKAGES_READY)


def test_phase_skip_allowed() -> None:
	"""Source-only builds skip PACKAGE_INGRESS/PACKAGES_READY."""
	world = SemanticWorld()
	world.advance_to(WorldPhase.SOURCE_INGRESS)
	assert world.phase is WorldPhase.SOURCE_INGRESS


def test_freeze_blocks_type_declaration() -> None:
	tt = TypeTable()
	world = SemanticWorld(type_table=tt)
	world.advance_to(WorldPhase.READY)
	world.freeze()
	with pytest.raises(RuntimeError, match="frozen.*declare_struct"):
		tt.declare_struct("test", "Foo", ["x"])


def test_freeze_blocks_alias_definition() -> None:
	tt = TypeTable()
	world = SemanticWorld(type_table=tt)
	world.advance_to(WorldPhase.READY)
	world.freeze()
	with pytest.raises(RuntimeError, match="frozen.*define_type_alias"):
		tt.define_type_alias(module_id="test", name="Bar", type_params=[], target=None)


def test_freeze_allows_structural_interning() -> None:
	"""ensure_ref, new_array, etc. must still work after freeze."""
	tt = TypeTable()
	world = SemanticWorld(type_table=tt)
	world.advance_to(WorldPhase.READY)
	world.freeze()
	# These should NOT raise.
	int_id = tt.ensure_int()
	ref_id = tt.ensure_ref(int_id)
	assert ref_id != int_id
	arr_id = tt.new_array(int_id)
	assert arr_id != int_id


def test_freeze_blocks_callable_registration() -> None:
	from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility
	cr = CallableRegistry()
	tt = TypeTable()
	world = SemanticWorld(type_table=tt, callable_registry=cr)
	world.advance_to(WorldPhase.READY)
	world.freeze()
	with pytest.raises(RuntimeError, match="frozen.*register_free_function"):
		cr.register_free_function(
			callable_id=1,
			name="foo",
			module_id=0,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=(), result_type=tt.ensure_int()),
		)


def test_freeze_blocks_ensure_named() -> None:
	"""ensure_named must not create new FORWARD_NOMINALs after freeze."""
	tt = TypeTable()
	world = SemanticWorld(type_table=tt)
	world.advance_to(WorldPhase.READY)
	world.freeze()
	with pytest.raises(RuntimeError, match="frozen.*ensure_named"):
		tt.ensure_named("NewType", module_id="test")


def test_freeze_allows_ensure_named_existing() -> None:
	"""ensure_named must still return existing types after freeze."""
	tt = TypeTable()
	tid = tt.declare_struct("test", "Existing", ["x"])
	world = SemanticWorld(type_table=tt)
	world.advance_to(WorldPhase.READY)
	world.freeze()
	# Looking up an existing name should NOT raise.
	result = tt.ensure_named("Existing", module_id="test")
	assert result == tid


def test_freeze_blocks_declare_scalar() -> None:
	tt = TypeTable()
	world = SemanticWorld(type_table=tt)
	world.advance_to(WorldPhase.READY)
	world.freeze()
	with pytest.raises(RuntimeError, match="frozen.*declare_scalar"):
		tt.declare_scalar("test", "MyScalar")


def test_freeze_blocks_declare_variant() -> None:
	tt = TypeTable()
	world = SemanticWorld(type_table=tt)
	world.advance_to(WorldPhase.READY)
	world.freeze()
	with pytest.raises(RuntimeError, match="frozen.*declare_variant"):
		tt.declare_variant("test", "MyVariant", [], [])


def test_freeze_blocks_declare_interface() -> None:
	tt = TypeTable()
	world = SemanticWorld(type_table=tt)
	world.advance_to(WorldPhase.READY)
	world.freeze()
	with pytest.raises(RuntimeError, match="frozen.*declare_interface"):
		tt.declare_interface("test", "MyIface")


def test_freeze_blocks_register_inherent_method() -> None:
	from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility, SelfMode
	cr = CallableRegistry()
	tt = TypeTable()
	struct_id = tt.declare_struct("test", "App", ["x"])
	world = SemanticWorld(type_table=tt, callable_registry=cr)
	world.advance_to(WorldPhase.READY)
	world.freeze()
	with pytest.raises(RuntimeError, match="frozen.*register_inherent_method"):
		cr.register_inherent_method(
			callable_id=1, name="greet", module_id=0,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=(struct_id,), result_type=tt.ensure_int()),
			fn_id=None, impl_id=1, impl_target_type_id=struct_id,
			self_mode=SelfMode.SELF_BY_REF,
		)


def test_freeze_blocks_register_trait_method() -> None:
	from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility, SelfMode
	cr = CallableRegistry()
	tt = TypeTable()
	struct_id = tt.declare_struct("test", "App", ["x"])
	world = SemanticWorld(type_table=tt, callable_registry=cr)
	world.advance_to(WorldPhase.READY)
	world.freeze()
	with pytest.raises(RuntimeError, match="frozen.*register_trait_method"):
		cr.register_trait_method(
			callable_id=1, name="destroy", module_id=0,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=(struct_id,), result_type=tt.ensure_void()),
			fn_id=None, impl_id=1, impl_target_type_id=struct_id,
			trait_id=99, self_mode=SelfMode.SELF_BY_VALUE,
		)


def test_assert_ready_before_ready() -> None:
	world = SemanticWorld()
	world.advance_to(WorldPhase.SOURCE_INGRESS)
	with pytest.raises(RuntimeError, match="not ready"):
		world.assert_ready()


def test_assert_ready_after_ready() -> None:
	world = SemanticWorld()
	world.advance_to(WorldPhase.READY)
	world.assert_ready()  # should not raise


def test_compile_stubbed_funcs_rejects_conflicting_type_table() -> None:
	"""compile_stubbed_funcs must reject a type_table that differs from the world's."""
	from lang.driftc.driftc import compile_stubbed_funcs

	tt_world = TypeTable()
	tt_other = TypeTable()
	world = SemanticWorld(type_table=tt_world)
	world.advance_to(WorldPhase.READY)

	with pytest.raises(RuntimeError, match="conflicting type_table"):
		compile_stubbed_funcs(
			func_hirs={},
			type_table=tt_other,
			semantic_world=world,
		)


def test_compile_stubbed_funcs_rejects_conflicting_module_deps() -> None:
	"""compile_stubbed_funcs must reject module_deps that differ from the world's."""
	from lang.driftc.driftc import compile_stubbed_funcs

	tt = TypeTable()
	world_deps = {"a": {"b"}}
	other_deps = {"x": {"y"}}
	world = SemanticWorld(type_table=tt, module_deps=world_deps)
	world.advance_to(WorldPhase.READY)

	with pytest.raises(RuntimeError, match="conflicting module_deps"):
		compile_stubbed_funcs(
			func_hirs={},
			type_table=tt,
			module_deps=other_deps,
			semantic_world=world,
		)


def test_compile_stubbed_funcs_rejects_conflicting_trait_defs() -> None:
	from lang.driftc.driftc import compile_stubbed_funcs

	tt = TypeTable()
	world_traits = []
	other_traits = ["different"]
	world = SemanticWorld(type_table=tt, external_trait_defs=world_traits)
	world.advance_to(WorldPhase.READY)

	with pytest.raises(RuntimeError, match="conflicting external_trait_defs"):
		compile_stubbed_funcs(
			func_hirs={},
			type_table=tt,
			external_trait_defs=other_traits,
			semantic_world=world,
		)


def test_compile_stubbed_funcs_rejects_conflicting_impl_metas() -> None:
	from lang.driftc.driftc import compile_stubbed_funcs

	tt = TypeTable()
	world_impls = []
	other_impls = ["different"]
	world = SemanticWorld(type_table=tt, external_impl_metas=world_impls)
	world.advance_to(WorldPhase.READY)

	with pytest.raises(RuntimeError, match="conflicting external_impl_metas"):
		compile_stubbed_funcs(
			func_hirs={},
			type_table=tt,
			external_impl_metas=other_impls,
			semantic_world=world,
		)


def test_compile_stubbed_funcs_rejects_conflicting_missing_traits() -> None:
	from lang.driftc.driftc import compile_stubbed_funcs

	tt = TypeTable()
	world_missing = set()
	other_missing = {"different"}
	world = SemanticWorld(type_table=tt, external_missing_traits=world_missing)
	world.advance_to(WorldPhase.READY)

	with pytest.raises(RuntimeError, match="conflicting external_missing_traits"):
		compile_stubbed_funcs(
			func_hirs={},
			type_table=tt,
			external_missing_traits=other_missing,
			semantic_world=world,
		)


def test_parser_rejects_conflicting_type_table() -> None:
	"""Parser must reject a type_table that differs from the world's."""
	from lang.driftc.parser import parse_drift_workspace_to_hir
	from pathlib import Path
	import tempfile

	tt_world = TypeTable()
	tt_other = TypeTable()
	world = SemanticWorld(type_table=tt_world)
	world.advance_to(WorldPhase.PACKAGES_READY)

	src = Path(tempfile.mktemp(suffix=".drift"))
	src.write_text("module m;\nfn main() nothrow -> Int { return 0; }\n")
	try:
		with pytest.raises(RuntimeError, match="conflicting type_table"):
			parse_drift_workspace_to_hir(
				[src],
				type_table=tt_other,
				semantic_world=world,
			)
	finally:
		src.unlink(missing_ok=True)
