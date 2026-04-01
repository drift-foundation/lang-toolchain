# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: match-arm codegen must not drop the scrutinee's moved payload.

When a Result<OwnedPayload, Error> is matched with Ok(x), the payload is
extracted and ownership transfers to x.  The scrutinee must NOT be dropped
after extraction if the payload was moved out — otherwise the destructor
runs on the already-consumed payload (use-after-move).

Proven discriminator:
  - PEX path: copy_status(OwnedPayload) returned True (structural fallback)
    because the trait prover couldn't resolve Destructible for a cross-package
    generic VirtualThread field
  - arm_scrut_payload_moved was False → scrutinee drop emitted
  - scrutinee drop called VirtualThread::destroy → cancelled server fiber
  - source path: copy_status returned False → arm_scrut_payload_moved True
    → no scrutinee drop → correct

This test verifies that copy_status returns False for structs containing
Destructible fields when destructor_fns is populated, even if the trait
prover doesn't resolve.
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable, TypeKind


def test_copy_status_false_for_struct_with_destructible_field() -> None:
	"""A struct containing an Arc (Destructible) field must not be Copy,
	even when the trait prover returns None for the struct."""
	table = TypeTable()
	int_tid = table.ensure_int()

	# Arc — Destructible via destructor_fns.
	arc_tid = table.declare_struct(module_id="std.concurrent", name="Arc", field_names=["inner"])
	table.define_struct_fields(arc_tid, field_types=[int_tid])
	destroy_fn = FunctionId(module="std.concurrent", name="Arc::destroy", ordinal=0)
	table.destructor_fns = {arc_tid: destroy_fn}

	# VirtualThread — has an Arc field (indirectly Destructible).
	vt_tid = table.declare_struct(module_id="std.concurrent", name="VirtualThread", field_names=["state", "buf", "arc"])
	table.define_struct_fields(vt_tid, field_types=[int_tid, int_tid, arc_tid])

	# RunningServer — has VirtualThread and ServerHandle fields.
	handle_tid = table.declare_struct(module_id="mymod", name="ServerHandle", field_names=["flag", "value"])
	table.define_struct_fields(handle_tid, field_types=[arc_tid, int_tid])

	rs_tid = table.declare_struct(module_id="mymod", name="RunningServer", field_names=["handle", "port", "vt"])
	table.define_struct_fields(rs_tid, field_types=[handle_tid, int_tid, vt_tid])

	# No _copy_query installed — simulates PEX path where trait prover
	# doesn't resolve for cross-package types.

	# Before fix: structural fallback → True (VirtualThread looks Copy
	# because its Arc field's struct instance has no destructor_fns check).
	# After fix: destructor_fns check → Arc has a destructor → not Copy.
	status = table.copy_status(rs_tid)
	assert status is not True, (
		f"RunningServer must NOT be Copy: it contains Arc (Destructible). "
		f"copy_status returned {status}"
	)


def test_copy_status_true_for_plain_struct_without_destructors() -> None:
	"""A struct with only scalar fields and no destructors is still Copy."""
	table = TypeTable()
	int_tid = table.ensure_int()

	point_tid = table.declare_struct(module_id="mymod", name="Point", field_names=["x", "y"])
	table.define_struct_fields(point_tid, field_types=[int_tid, int_tid])

	status = table.copy_status(point_tid)
	assert status is True, (
		f"Point (only Int fields) should be Copy. copy_status returned {status}"
	)
