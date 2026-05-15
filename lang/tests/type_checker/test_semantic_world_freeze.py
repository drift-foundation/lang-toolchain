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


def test_signature_annotations_overlay() -> None:
	"""Analysis annotations augment but do not replace canonical signatures."""
	world = SemanticWorld()
	world.base_signatures = {"fn_a": "canonical_sig_a"}

	# Annotate without mutating the canonical signature.
	world.annotate_signature("fn_a", "non_retaining_params", {0, 2})
	world.annotate_signature("fn_a", "escape_safe", True)

	# Canonical signature is unchanged.
	assert world.get_signature("fn_a") == "canonical_sig_a"

	# Annotations are available via separate lookup.
	assert world.get_signature_annotation("fn_a", "non_retaining_params") == {0, 2}
	assert world.get_signature_annotation("fn_a", "escape_safe") is True
	assert world.get_signature_annotation("fn_a", "missing_key") is None
	assert world.get_signature_annotation("fn_missing", "any_key") is None


def test_effective_param_escape_level_overlay_priority() -> None:
	"""World accessor checks overlay before signature fallback."""
	from lang.driftc.borrow_checker import EscapeLevel
	from lang.driftc.checker import FnSignature
	from lang.driftc.core.function_id import FunctionId

	fn_id = FunctionId(module="test", name="foo", ordinal=0)
	sig = FnSignature(
		name="foo", module="test",
		param_escape_level=[EscapeLevel.THREAD, EscapeLevel.THREAD],
	)
	world = SemanticWorld()
	world.base_signatures = {fn_id: sig}

	# Without overlay: falls back to signature field.
	assert world.effective_param_escape_level(fn_id, 0) == EscapeLevel.THREAD

	# With overlay: overlay wins.
	world.annotate_signature(fn_id, "param_escape_level", [EscapeLevel.LOCAL, None])
	assert world.effective_param_escape_level(fn_id, 0) == EscapeLevel.LOCAL
	# Param 1: overlay has None → falls through to signature.
	assert world.effective_param_escape_level(fn_id, 1) == EscapeLevel.THREAD


def test_stale_overlay_cleared_by_none_write() -> None:
	"""Analysis writing None to the overlay clears a previously set annotation."""
	from lang.driftc.borrow_checker import EscapeLevel
	from lang.driftc.core.function_id import FunctionId

	fn_id = FunctionId(module="test", name="foo", ordinal=0)
	world = SemanticWorld()

	# Simulate prior analysis run that found LOCAL.
	world.annotate_signature(fn_id, "param_escape_level", [EscapeLevel.LOCAL])
	assert world.get_signature_annotation(fn_id, "param_escape_level") == [EscapeLevel.LOCAL]

	# Later analysis clears the result by writing None.
	world.annotate_signature(fn_id, "param_escape_level", None)
	assert world.get_signature_annotation(fn_id, "param_escape_level") is None

	# World accessor falls through to signature fallback (not the stale overlay).
	assert world.effective_param_escape_level(fn_id, 0) == EscapeLevel.THREAD


def test_free_fn_escape_sig_cache_uses_overlay() -> None:
	"""BorrowChecker._free_fn_escape_sig must find free functions whose
	escape annotations exist only in the world overlay (not on the sig)."""
	from lang.driftc.borrow_checker import EscapeLevel
	from lang.driftc.borrow_checker_pass import BorrowChecker
	from lang.driftc.checker import FnSignature
	from lang.driftc.core.function_id import FunctionId

	fn_a = FunctionId(module="std.concurrent", name="spawn_cb", ordinal=0)
	sig_a = FnSignature(name="spawn_cb", module="std.concurrent")
	# No param_escape_level on the sig — overlay only.
	assert sig_a.param_escape_level is None

	tt = TypeTable()
	world = SemanticWorld(type_table=tt)
	world.annotate_signature(fn_a, "param_escape_level", [EscapeLevel.THREAD])

	bc = BorrowChecker(
		type_table=tt,
		fn_types={},
		signatures_by_id={fn_a: sig_a},
		semantic_world=world,
	)
	# The cache should find spawn_cb via the overlay annotation.
	assert ("std.concurrent", "spawn_cb") in bc._free_fn_escape_sig


def test_free_fn_escape_fallback_uses_overlay_only_annotation() -> None:
	"""BorrowChecker._resolve_sig_for_call fallback path must find a free
	function's escape annotation when it exists only in the world overlay
	and the call has no call_resolutions entry (intrinsic-style path)."""
	from lang.driftc.borrow_checker import EscapeLevel
	from lang.driftc.borrow_checker_pass import BorrowChecker
	from lang.driftc.checker import FnSignature
	from lang.driftc.core.function_id import FunctionId
	from lang.driftc import stage1 as H

	fn_id = FunctionId(module="std.concurrent", name="spawn_cb", ordinal=0)
	sig = FnSignature(name="spawn_cb", module="std.concurrent")
	assert sig.param_escape_level is None  # no sig-level annotation

	tt = TypeTable()
	world = SemanticWorld(type_table=tt)
	world.annotate_signature(fn_id, "param_escape_level", [EscapeLevel.THREAD])

	bc = BorrowChecker(
		type_table=tt,
		fn_types={},
		signatures_by_id={fn_id: sig},
		semantic_world=world,
		call_resolutions={},  # empty — forces fallback path
	)

	# Simulate a call expression with no call_resolutions entry.
	call_expr = H.HCall(
		fn=H.HVar(name="spawn_cb", module_id="std.concurrent"),
		args=[],
		kwargs=[],
	)
	call_expr.node_id = 999  # not in call_resolutions

	resolved = bc._resolve_sig_for_call(call_expr)
	assert resolved is sig, "fallback lookup must find the sig via overlay-populated cache"


def test_effective_param_escape_level_missing_signature() -> None:
	"""World accessor returns THREAD default when no signature exists."""
	from lang.driftc.borrow_checker import EscapeLevel
	from lang.driftc.core.function_id import FunctionId

	fn_id = FunctionId(module="test", name="missing", ordinal=0)
	world = SemanticWorld()
	assert world.effective_param_escape_level(fn_id, 0) == EscapeLevel.THREAD


def test_get_signature_priority() -> None:
	"""get_signature returns derived > base > external."""
	world = SemanticWorld()
	world.base_signatures = {"fn_a": "base_a", "fn_shared": "base_shared"}
	world.derived_signatures = {"fn_b": "derived_b", "fn_shared": "derived_shared"}
	world.external_signatures = {"fn_c": "ext_c", "fn_shared": "ext_shared"}

	assert world.get_signature("fn_b") == "derived_b"
	assert world.get_signature("fn_a") == "base_a"
	assert world.get_signature("fn_c") == "ext_c"
	assert world.get_signature("fn_shared") == "derived_shared"  # derived wins
	assert world.get_signature("fn_missing") is None


def test_all_signatures_merged_view() -> None:
	"""all_signatures returns a merged view with correct priority."""
	world = SemanticWorld()
	world.base_signatures = {"fn_a": "base_a"}
	world.external_signatures = {"fn_a": "ext_a", "fn_b": "ext_b"}

	sigs = world.all_signatures()
	assert sigs["fn_a"] == "base_a"  # base wins over external
	assert sigs["fn_b"] == "ext_b"


def test_package_id_property() -> None:
	tt = TypeTable()
	tt.package_id = "my-pkg"
	world = SemanticWorld(type_table=tt)
	assert world.package_id == "my-pkg"


def test_package_id_none_without_type_table() -> None:
	world = SemanticWorld()
	assert world.package_id is None


def test_module_packages_property() -> None:
	tt = TypeTable()
	tt.module_packages["std.core"] = "std"
	world = SemanticWorld(type_table=tt)
	assert world.module_packages["std.core"] == "std"


def test_parser_rejects_conflicting_type_table() -> None:
	"""Parser must reject a type_table that differs from the world's."""
	from lang.driftc.parser import parse_drift_workspace_to_hir
	from pathlib import Path
	import tempfile

	tt_world = TypeTable()
	tt_other = TypeTable()
	world = SemanticWorld(type_table=tt_world)
	world.advance_to(WorldPhase.PACKAGES_READY)

	from lang.test_support.drift_tmp import drift_mkdtemp as _drift_mkdtemp
	src = Path(_drift_mkdtemp(prefix="semworld_freeze_")) / ("source" + ".drift")
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
