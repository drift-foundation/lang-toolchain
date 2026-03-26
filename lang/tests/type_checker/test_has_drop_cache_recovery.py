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
