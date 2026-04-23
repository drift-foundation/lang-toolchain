# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3B step 4 — `match_cleanup` consumer-swap pin.

Pins the step-4 narrow alignment of site 2 in
`lang/driftc/stage2/hir_to_mir.py`'s match-arm cleanup logic:

- The whole-scrutinee drop decision now goes through
  `HIRToMIR._match_scrutinee_drop_verdict(arm_scrut_local,
  arm_scrut_payload_moved, scrut_ty) -> (DropVerdict, REASON_*)` —
  the same three-state shape used by `_scope_drop_verdict` (step 3).
- The MIR emission is unchanged — the helper captures the existing
  per-arm decision logic; the per-field cleanup loop is untouched.
- Per K's directive ("treat per-field work as the risky boundary"),
  step 4 is a STRUCTURAL alignment patch, not a behavior change.
  Per-field cleanup remains the legacy authority because the 3A
  ledger has no per-field state (triage bucket 1, `per_field_gap`).

These tests pin the helper's verdict mapping directly; the end-to-end
match-cleanup behavior continues to be exercised by
`test_match_scrut_copy_store_emits_copyvalue.py` and
`test_ownership_ledger_three_quadrant_pin.py`.
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.stage2.ownership_ledger import DropVerdict
from lang.driftc.stage2.ownership_ledger_events import (
	REASON_FIELD_MOVED,
	REASON_NEEDS_DROP,
	REASON_NOT_DROP_NEEDING,
)


def _build_lowerer(type_table: TypeTable) -> HIRToMIR:
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	return HIRToMIR(builder, type_table=type_table)


def test_verdict_no_scrut_local_is_must_not_drop() -> None:
	"""`arm_scrut_local is None` (e.g. ref-pattern match, or no
	scrut-tmp ever bound) → MustNotDrop + not_drop_needing.
	The site's outer guard already filters this case before
	reaching the verdict; helper preserves the symmetry for any
	future caller that doesn't pre-filter."""
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	lower = _build_lowerer(type_table)
	verdict, reason = lower._match_scrutinee_drop_verdict(
		arm_scrut_local=None,
		arm_scrut_payload_moved=False,
		scrut_ty=string_ty,
	)
	assert verdict is DropVerdict.MUST_NOT_DROP
	assert reason == REASON_NOT_DROP_NEEDING


def test_verdict_partial_move_is_must_not_drop_with_field_moved() -> None:
	"""When `arm_scrut_payload_moved == True`, the per-field cleanup
	loop runs instead of a whole-scrutinee drop.  Helper says
	MustNotDrop with REASON_FIELD_MOVED — distinct from the plain
	not_drop_needing case so observe triage can identify partial-move
	branches separately."""
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	lower = _build_lowerer(type_table)
	verdict, reason = lower._match_scrutinee_drop_verdict(
		arm_scrut_local="__match_scrut_tmp1",
		arm_scrut_payload_moved=True,
		scrut_ty=string_ty,
	)
	assert verdict is DropVerdict.MUST_NOT_DROP
	assert reason == REASON_FIELD_MOVED


def test_verdict_no_partial_move_drop_needing_type_is_must_drop() -> None:
	"""The whole-drop branch: no field move, drop-needing scrutinee
	type → MustDrop with needs_drop reason.  This is the path that
	emits `MoveOut(arm_scrut_local) + DropValue` in site 2."""
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	lower = _build_lowerer(type_table)
	verdict, reason = lower._match_scrutinee_drop_verdict(
		arm_scrut_local="__match_scrut_tmp1",
		arm_scrut_payload_moved=False,
		scrut_ty=string_ty,
	)
	assert verdict is DropVerdict.MUST_DROP
	assert reason == REASON_NEEDS_DROP


def test_verdict_pod_scrut_type_is_must_not_drop() -> None:
	"""Defensive: a POD scrutinee type (Int) → MustNotDrop +
	not_drop_needing.  Today the site's outer flow only sets
	`arm_scrut_local` for drop-needing types so this branch is not
	reached, but the helper handles it for symmetry / robustness
	against future site refactors."""
	type_table = TypeTable()
	int_ty = type_table.ensure_int()
	lower = _build_lowerer(type_table)
	verdict, reason = lower._match_scrutinee_drop_verdict(
		arm_scrut_local="__match_scrut_tmp1",
		arm_scrut_payload_moved=False,
		scrut_ty=int_ty,
	)
	assert verdict is DropVerdict.MUST_NOT_DROP
	assert reason == REASON_NOT_DROP_NEEDING


def test_verdict_unknown_scrut_type_is_must_not_drop() -> None:
	"""`scrut_ty is None` → MustNotDrop + not_drop_needing.  Defensive
	(prior code's outer flow guarantees scrut_ty is set when
	arm_scrut_local is set, but the helper is defensive)."""
	type_table = TypeTable()
	lower = _build_lowerer(type_table)
	verdict, reason = lower._match_scrutinee_drop_verdict(
		arm_scrut_local="__match_scrut_tmp1",
		arm_scrut_payload_moved=False,
		scrut_ty=None,
	)
	assert verdict is DropVerdict.MUST_NOT_DROP
	assert reason == REASON_NOT_DROP_NEEDING
