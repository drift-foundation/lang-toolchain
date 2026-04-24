# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 site-2 patch 5 — match_cleanup_authoring pins.

Two contracts:

1. **Authoring pin**: `MatchCleanupHook` → canonical per-field
   cleanup chain (`VariantGetFieldAddr + LoadRef + StoreLocal` at
   hook position, `MoveOut + DropValue` at arm_end position) when
   `field_verdict_at` is `MUST_DROP`.  No chain emitted for any
   other verdict.

2. **UNINIT contract pin (K-requested)**: a `drop_tmp` local that
   was pre-allocated by HIR→MIR and appears in a site-1
   `CleanupHook` candidate list — but receives NO `StoreLocal` on
   any path — must be treated as `UNINIT` by the ledger and
   therefore SKIPPED by `cleanup_authoring`.  This is the property
   Mechanism 4 of patch 5's design leans on: if
   `match_cleanup_authoring` decides `MUST_NOT_DROP` for a
   candidate and emits no chain, the pre-allocated drop_tmp stays
   UNINIT and subsequent site-1 hooks see
   `classify(UNINIT, needs_drop=True) = MUST_NOT_DROP`.
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import (
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import (
	DropVerdict,
	LiveState,
	build_ledger,
)
from lang.driftc.stage2.cleanup_authoring import author_cleanup
from lang.driftc.stage2.match_cleanup_authoring import author_match_cleanup


def _make_func(name: str, *, locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="main", name=name, ordinal=0)
	return M.MirFunc(
		name=name,
		params=[],
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


def _make_variant_with_destructible_field(type_table: TypeTable) -> tuple[int, int, str]:
	"""Create a variant `V { Some(String) }`.  String is the canonical
	drop-needing builtin; `compute_drop_policy(type_table, field_ty)`
	returns `needs_drop=True` for it, which is what the authoring
	pass reads."""
	field_ty = type_table.ensure_string()
	variant_base = type_table.declare_variant(
		module_id="main",
		name="V",
		type_params=[],
		arms=[
			VariantArmSchema(
				name="Some",
				fields=[
					VariantFieldSchema(name="f", type_expr=GenericTypeExpr.named("String")),
				],
			),
		],
	)
	variant_ty = type_table.ensure_variant_instantiated(variant_base, [])
	return (variant_ty, field_ty, "Some")


def test_authoring_emits_chain_for_must_drop_candidate() -> None:
	"""Given a hand-built function whose `MatchCleanupHook` references
	a live variant field, `author_match_cleanup` must emit the canonical
	chain: `VariantGetFieldAddr + LoadRef + StoreLocal(drop_tmp)` at
	the hook position, and `MoveOut(drop_tmp) + DropValue` at the
	arm_end position.  The hook marker itself is removed."""
	type_table = TypeTable()
	variant_ty, field_ty, ctor = _make_variant_with_destructible_field(type_table)
	func = _make_func(
		"arm",
		locals_=["v", "v_ptr", "drop_tmp"],
		types={"v": variant_ty, "v_ptr": type_table.ensure_ref_mut(variant_ty), "drop_tmp": field_ty},
	)
	entry = M.BasicBlock(name="entry")
	# StoreLocal(v, t_init)  — v becomes Live
	entry.instructions.append(M.StoreLocal(local="v", value="t_init"))
	# AddrOfLocal(v_ptr, v, is_mut=True)
	entry.instructions.append(M.AddrOfLocal(dest="v_ptr", local="v", is_mut=True))
	# Hook at index 2 — arm-end position will be index 3 (pre-return).
	entry.instructions.append(M.MatchCleanupHook(
		scope_id=0,
		arm_scrut_local="v",
		arm_scrut_ptr_local="v_ptr",
		variant_ty=variant_ty,
		ctor=ctor,
		candidates=[("drop_tmp", 0, field_ty)],
		arm_end_block="entry",
		arm_end_index=3,
	))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	# Build ledger, attach to func, run authoring.
	ledger = build_ledger(func, drop_policy=lambda _t: None)
	setattr(func, "_ownership_ledger", ledger)
	emitted = author_match_cleanup(func, type_table=type_table)
	assert emitted == 1, f"expected 1 authored chain for MUST_DROP candidate, got {emitted}"
	# After authoring: no MatchCleanupHook left.
	assert not any(
		isinstance(ins, M.MatchCleanupHook) for ins in func.blocks["entry"].instructions
	), "MatchCleanupHook must be removed after authoring"
	# Hook-position chain: VariantGetFieldAddr + LoadRef + StoreLocal(drop_tmp).
	kinds = [type(ins).__name__ for ins in func.blocks["entry"].instructions]
	assert "VariantGetFieldAddr" in kinds, f"missing VariantGetFieldAddr in authored chain; got {kinds}"
	assert "LoadRef" in kinds, f"missing LoadRef in authored chain; got {kinds}"
	store_idx = next(
		(
			i for i, ins in enumerate(func.blocks["entry"].instructions)
			if isinstance(ins, M.StoreLocal) and ins.local == "drop_tmp"
		),
		None,
	)
	assert store_idx is not None, "authoring must emit StoreLocal(drop_tmp, ...)"
	# Arm-end chain: MoveOut(drop_tmp) + DropValue must appear after the StoreLocal.
	mo_idx = next(
		(
			i for i, ins in enumerate(func.blocks["entry"].instructions)
			if isinstance(ins, M.MoveOut) and ins.local == "drop_tmp"
		),
		None,
	)
	assert mo_idx is not None, "authoring must emit MoveOut(drop_tmp, ...) at arm_end"
	assert mo_idx > store_idx, "MoveOut(drop_tmp) must come AFTER StoreLocal(drop_tmp)"
	# DropValue follows the MoveOut, consuming its dest.
	drop_ins = func.blocks["entry"].instructions[mo_idx + 1]
	assert isinstance(drop_ins, M.DropValue), (
		f"expected DropValue immediately after MoveOut(drop_tmp); got {type(drop_ins).__name__}"
	)
	assert drop_ins.value == func.blocks["entry"].instructions[mo_idx].dest, (
		"DropValue must consume the MoveOut's dest"
	)


def test_uninit_drop_tmp_is_skipped_by_site1_cleanup_authoring() -> None:
	"""K-guardrail pin: a `drop_tmp` that was pre-allocated and appears
	in a site-1 `CleanupHook` candidate list, but received NO
	`StoreLocal` anywhere in the function, must be treated as `UNINIT`
	by the ledger and SKIPPED by `cleanup_authoring`.  Contract:

	    state = UNINIT
	    verdict = classify(UNINIT, needs_drop=True) = MUST_NOT_DROP
	    cleanup_authoring skips — no MoveOut + DropValue for drop_tmp

	This is the correctness property Mechanism 4 of patch 5 leans on:
	match_cleanup_authoring's MUST_NOT_DROP decisions leave drop_tmp
	never stored, and site-1 cleanup must NOT invent a drop for an
	uninitialised local."""
	type_table = TypeTable()
	_variant_ty, field_ty, _ctor = _make_variant_with_destructible_field(type_table)
	func = _make_func(
		"uninit_contract",
		locals_=["drop_tmp"],
		types={"drop_tmp": field_ty},
	)
	entry = M.BasicBlock(name="entry")
	# Site-1 CleanupHook with drop_tmp as the sole candidate.
	# NO StoreLocal for drop_tmp anywhere in the function.
	entry.instructions.append(M.CleanupHook(
		scope_id=0,
		candidates=[("drop_tmp", field_ty)],
	))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=lambda _t: None)
	setattr(func, "_ownership_ledger", ledger)
	# Verify lattice contract directly: drop_tmp at hook position is UNINIT.
	assert ledger.state_pre(("entry", 0), "drop_tmp") is LiveState.UNINIT, (
		"pre-allocated never-stored local must be UNINIT at cleanup-hook position"
	)
	# Verify verdict contract: UNINIT + needs_drop=True -> MUST_NOT_DROP.
	assert ledger.verdict_at(("entry", 0), "drop_tmp", needs_drop=True) is DropVerdict.MUST_NOT_DROP, (
		"UNINIT + needs_drop=True must classify as MUST_NOT_DROP "
		"(not PATH_DEPENDENT) — this is the Mechanism 4 correctness axis"
	)
	# Run authoring.  It must NOT emit any MoveOut/DropValue for drop_tmp.
	emitted = author_cleanup(func, type_table=type_table)
	assert emitted == 0, (
		f"cleanup_authoring must not emit a drop for UNINIT drop_tmp; got {emitted} emissions"
	)
	# After authoring, the CleanupHook is removed but no
	# MoveOut/DropValue for drop_tmp is present.
	for ins in func.blocks["entry"].instructions:
		if isinstance(ins, M.MoveOut):
			assert ins.local != "drop_tmp", "MUST NOT emit MoveOut(drop_tmp) for UNINIT local"
		if isinstance(ins, M.DropValue):
			# The dest (if from a MoveOut) would be a cleanup temp; we
			# assert above that the MoveOut itself isn't present.
			pass


def test_authoring_skips_chain_for_must_not_drop_candidate() -> None:
	"""If `field_verdict_at` returns `MUST_NOT_DROP` (e.g. because the
	field is already MovedOut at the hook program point), authoring
	emits nothing for that candidate — no StoreLocal, no VariantGetFieldAddr,
	no arm-end chain.  The pre-allocated `drop_tmp` local stays
	UNINIT.  This is the symmetric companion to the MUST_DROP pin
	above."""
	type_table = TypeTable()
	variant_ty, field_ty, ctor = _make_variant_with_destructible_field(type_table)
	func = _make_func(
		"skip",
		locals_=["v", "v_ptr", "drop_tmp"],
		types={"v": variant_ty, "v_ptr": type_table.ensure_ref_mut(variant_ty), "drop_tmp": field_ty},
	)
	entry = M.BasicBlock(name="entry")
	# Store + address-of as before.
	entry.instructions.append(M.StoreLocal(local="v", value="t_init"))
	entry.instructions.append(M.AddrOfLocal(dest="v_ptr", local="v", is_mut=True))
	# Move the field out BEFORE the hook so field_verdict_at returns
	# MUST_NOT_DROP.  VariantGetFieldAddr marks the field MovedOut in
	# the 3a conservative-chain detection.
	entry.instructions.append(M.VariantGetFieldAddr(
		dest="pre_field_addr",
		variant_ref="v_ptr",
		variant_ty=variant_ty,
		ctor=ctor,
		field_index=0,
		field_ty=field_ty,
	))
	# Hook after the pre-move; field_verdict_at should now see the
	# field as MovedOut → MUST_NOT_DROP.
	entry.instructions.append(M.MatchCleanupHook(
		scope_id=0,
		arm_scrut_local="v",
		arm_scrut_ptr_local="v_ptr",
		variant_ty=variant_ty,
		ctor=ctor,
		candidates=[("drop_tmp", 0, field_ty)],
		arm_end_block="entry",
		arm_end_index=4,
	))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	func.local_types["pre_field_addr"] = type_table.ensure_ref_mut(field_ty)
	ledger = build_ledger(func, drop_policy=lambda _t: None)
	setattr(func, "_ownership_ledger", ledger)
	emitted = author_match_cleanup(func, type_table=type_table)
	assert emitted == 0, (
		f"authoring must skip chain emission for MUST_NOT_DROP candidate; got {emitted} emissions"
	)
	# Hook is still removed.
	assert not any(
		isinstance(ins, M.MatchCleanupHook) for ins in func.blocks["entry"].instructions
	), "MatchCleanupHook must be removed even for skip decisions"
	# No StoreLocal(drop_tmp) anywhere.
	assert not any(
		isinstance(ins, M.StoreLocal) and ins.local == "drop_tmp"
		for ins in func.blocks["entry"].instructions
	), "authoring must emit no StoreLocal(drop_tmp) for MUST_NOT_DROP"
	# No MoveOut(drop_tmp) anywhere.
	assert not any(
		isinstance(ins, M.MoveOut) and ins.local == "drop_tmp"
		for ins in func.blocks["entry"].instructions
	), "authoring must emit no MoveOut(drop_tmp) for MUST_NOT_DROP"
