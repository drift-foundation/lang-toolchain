# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: has_drop must not cache False for structs with missing instances.

Proven discriminator for the Arc leak in package-consumer builds:
  - has_drop(StructWithArcField) called before struct instance is linked → False
  - struct instance populated later with field_types including Arc
  - has_drop(StructWithArcField) called again → must return True (not stale False)

Without the fix, the second call returns the cached False and the DropValue
for the struct is never emitted, leaking the Arc allocation.
"""
from __future__ import annotations

import pytest

from lang.driftc.core.types_core import TypeTable, TypeKind


def _make_table_with_arc_like_struct() -> tuple[TypeTable, int, int, int]:
	"""Build a type table with:
	  - Arc-like struct (implements Destructible via destructor_fns)
	  - ServerHandle-like struct (field: Arc, field: Int)
	  - Int scalar (no drop)
	Returns (table, arc_tid, handle_tid, int_tid).
	"""
	table = TypeTable()
	int_tid = table.ensure_int()

	# Arc-like struct: needs drop because it has a destructor.
	arc_tid = table.declare_struct(module_id="std.concurrent", name="Arc", field_names=["inner"])
	table.define_struct_fields(arc_tid, field_types=[int_tid])

	# Register a destroy function for Arc (simulates Destructible impl).
	from lang.driftc.core.function_id import FunctionId
	destroy_fn = FunctionId(module="std.concurrent", name="Arc::destroy", ordinal=0)
	table.destructor_fns = {arc_tid: destroy_fn}

	# ServerHandle-like struct: no Destructible impl, but has an Arc field.
	# Declare with field names but do NOT define field types yet —
	# simulates pre-linking state where the struct is known but instances
	# aren't populated.
	handle_tid = table.declare_struct(module_id="web.rest.server", name="ServerHandle", field_names=["flag", "value"])

	return table, arc_tid, handle_tid, int_tid


def test_has_drop_stale_cache_on_missing_instance() -> None:
	"""has_drop must not permanently cache False when struct instance is missing."""
	table, arc_tid, handle_tid, int_tid = _make_table_with_arc_like_struct()

	# Arc has a destructor → has_drop must return True.
	assert table.has_drop(arc_tid) is True, "Arc with destructor_fns should need drop"

	# ServerHandle has NO struct instance yet.
	assert table.get_struct_instance(handle_tid) is None

	# First call: has_drop should return False (no instance, can't check fields).
	result_before = table.has_drop(handle_tid)
	assert result_before is False, "has_drop should return False when instance is missing"

	# Now link the struct instance — ServerHandle has fields [Arc, Int].
	table.define_struct_fields(handle_tid, field_types=[arc_tid, int_tid])
	assert table.get_struct_instance(handle_tid) is not None, "instance should exist after define_struct_fields"

	# Second call: has_drop MUST return True now (Arc field needs drop).
	# With the stale-cache bug, this returns False.
	result_after = table.has_drop(handle_tid)
	assert result_after is True, (
		"has_drop must return True after struct instance is linked with "
		"a droppable field (Arc). A stale False cache from the pre-linking "
		"call must not persist."
	)


def test_has_drop_caches_true_correctly() -> None:
	"""When the instance IS available and fields need drop, cache the True result."""
	table, arc_tid, handle_tid, int_tid = _make_table_with_arc_like_struct()

	# Define fields immediately (instance available from the start).
	table.define_struct_fields(handle_tid, field_types=[arc_tid, int_tid])

	# First call with instance available → should return True and cache it.
	assert table.has_drop(handle_tid) is True

	# Verify cached (second call should be fast and return same result).
	assert table.has_drop(handle_tid) is True


def test_has_drop_caches_false_when_instance_confirms_no_drop() -> None:
	"""When the instance IS available and NO fields need drop, cache False."""
	table = TypeTable()
	int_tid = table.ensure_int()

	# Simple struct with only Int fields — no drop needed.
	simple_tid = table.declare_struct(module_id="mylib", name="Point", field_names=["x", "y"])
	table.define_struct_fields(simple_tid, field_types=[int_tid, int_tid])

	assert table.has_drop(simple_tid) is False
	# Call again — should use cache.
	assert table.has_drop(simple_tid) is False


def test_has_drop_pre_destructor_fns_cache_invalidation() -> None:
	"""has_drop must not return stale False after destructor_fns is installed.

	Exercises the cache invalidation mechanism: has_drop queried before
	destructor_fns exists caches False; after destructor_fns is set,
	re-evaluation must return True.  This is the local mechanism behind
	the producer-side DropValue omission traced in the web-rest Arc leak,
	but does not constitute an end-to-end package-consumer repro.
	"""
	from lang.driftc.core.function_id import FunctionId

	table = TypeTable()
	int_tid = table.ensure_int()

	# Arc struct — no destructor_fns yet.
	arc_tid = table.declare_struct(module_id="std.concurrent", name="Arc", field_names=["inner"])
	table.define_struct_fields(arc_tid, field_types=[int_tid])

	# ServerHandle with Arc field.
	handle_tid = table.declare_struct(module_id="web.rest.server", name="ServerHandle", field_names=["stopped", "value"])
	table.define_struct_fields(handle_tid, field_types=[arc_tid, int_tid])

	assert not hasattr(table, "destructor_fns") or table.destructor_fns is None

	# Before destructor_fns: Arc has no destructor, has_drop returns False.
	assert table.has_drop(arc_tid) is False
	assert table.has_drop(handle_tid) is False

	# Install destructor_fns — Arc now has a registered destroy function.
	destroy_fn = FunctionId(module="std.concurrent", name="Arc::destroy", ordinal=0)
	table.destructor_fns = {arc_tid: destroy_fn}

	# After destructor_fns: has_drop must return True, not stale cached False.
	assert table.has_drop(arc_tid) is True, (
		"has_drop(Arc) must return True after destructor_fns is installed, "
		"not stale False from the pre-destructor cache"
	)
	assert table.has_drop(handle_tid) is True, (
		"has_drop(ServerHandle) must return True after destructor_fns is "
		"installed — its Arc field now has a registered destructor"
	)


def test_has_drop_generic_destructible_different_instantiation() -> None:
	"""A struct field whose type is a generic Destructible instantiation
	that has no direct destructor_fns entry must still be recognized as
	needing a drop — via name+module match against other instantiations.

	This is the exact producer-side root cause of the Arc leak:
	  - destructor_fns has Arc(tid=X) from one instantiation
	  - ServerHandle has field Arc(tid=Y) — a DIFFERENT instantiation
	  - Arc(tid=Y) has no destructor_fns entry, no struct instance
	  - has_drop(ServerHandle) must return True because Arc(tid=X)
	    proves the generic Arc type is Destructible
	"""
	from lang.driftc.core.function_id import FunctionId

	table = TypeTable()
	int_tid = table.ensure_int()

	# Arc instantiation 1 (tid=X): has a registered destructor.
	arc_x = table.declare_struct(module_id="std.concurrent", name="Arc", field_names=["inner"])
	table.define_struct_fields(arc_x, field_types=[int_tid])
	destroy_fn = FunctionId(module="std.concurrent", name="Arc::destroy__inst__aaa", ordinal=0)
	table.destructor_fns = {arc_x: destroy_fn}

	# Arc instantiation 2 (tid=Y): same name+module but different TypeId.
	# No struct instance, no destructor_fns entry. This simulates a
	# cross-package generic instantiation that the producer's type table
	# doesn't fully link. Use a raw TypeDef to avoid reusing arc_x's base.
	from lang.driftc.core.types_core import TypeDef, TypeKind
	arc_y = table._next_id
	table._next_id += 1
	table._defs[arc_y] = TypeDef(kind=TypeKind.STRUCT, name="Arc", param_types=[], module_id="std.concurrent", field_names=["inner"])

	assert table.get_struct_instance(arc_y) is None, "arc_y should have no struct instance"
	assert table.destructor_fns.get(arc_y) is None, "arc_y should have no destructor_fns entry"

	# ServerHandle has arc_y as a field.
	handle_tid = table.declare_struct(module_id="web.rest.server", name="ServerHandle", field_names=["stopped", "value"])
	table.define_struct_fields(handle_tid, field_types=[arc_y, int_tid])

	# has_drop(arc_y) must return True — the name+module match against arc_x
	# proves the generic Arc type is Destructible.
	assert table.has_drop(arc_y) is True, (
		"Arc instantiation without direct destructor_fns entry must be "
		"recognized as Destructible via name+module match against other "
		"registered Arc instantiations"
	)

	# has_drop(ServerHandle) must return True — its field arc_y needs drop.
	assert table.has_drop(handle_tid) is True, (
		"ServerHandle with Arc field must need drop even when the specific "
		"Arc instantiation has no direct destructor_fns entry"
	)
