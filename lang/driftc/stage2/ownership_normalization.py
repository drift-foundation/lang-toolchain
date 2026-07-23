# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Ownership normalization for MIR (Phase D — string_arc.py's permanent
successor).

The LAST pre-codegen ownership mutation pass.  It owns exactly the
output-bearing responsibilities that survived the B2+C migrations, in one
traversal that reproduces the historical emission order byte-for-byte:

  * **R1 — entry-block zero-storage initialization.**  At the entry-block
    head, for every non-param local, in THREE ordered groups (String
    locals, then Array locals, then null-safe destructible locals — each
    in `func.locals` order): `ZeroValue + StoreLocal[synthetic_zero_back]`.
    THE zero-safety foundation: every uninit-path release/drop authored by
    the frozen-plan emitters (null-safe releases/drops, unguarded
    zero-safe drops, array-header drops) is a runtime no-op ONLY because
    this pass zeroed the storage first.  Non-null-safe destructibles are
    deliberately NOT zero-inited (their scope-exit cleanup is
    flag-managed / authored).

  * **R5 — MoveOut expansion.**  In-place rewrite `MoveOut(dest, local,
    ty)` → `LoadLocal(dest, local) + ZeroValue(zero, ty) +
    StoreLocal(local, zero)[synthetic_zero_back]`, with the
    `moveout_expansion` audit note anchored at the ORIGINAL source index
    and carrying the `moveout_feeds_drop` next-instruction DropValue
    pairing snapshot (the C3 classification inputs).

  * **R8 — materialized-release validation + copy-through.**  The frozen
    `R8Recognition` (computed at the pre-mutation plan window; the
    driver's per-fn freeze is UNCONDITIONAL) is validated as a CLOSED
    vessel — wrong function, missing/extra block, malformed value all
    fail before any rewrite — and each recognized pre-materialized
    `StringRelease` is copied through BY IDENTITY with the
    `materialized_lastuse_release` audit note.  This is a production
    fail-closed release-placement handshake, not telemetry: an
    out-of-contract input release fails at the plan window's single
    recognition entry point (`compute_recognized_releases`).

  * **`local_types` seeding — a first-class permanent contract.**  The
    complete destination-type seeding downstream SSA/LLVM lowering relies
    on, carried verbatim from the historical pass: the shared
    `seed_string_dest_types` prepass; the per-block prescan registration
    of String `ZeroValue` / `ArrayIndexLoad*` dests (only-if-missing);
    and the per-arm assignments — `LoadLocal` (copies the local's current
    type when known), `LoadRef`/`StructGetField`/`VariantGetField`/
    `ArrayIndexLoad`/`ArrayIndexLoadUnchecked`/`ArrayElemTake`/`PtrRead`/
    `RawBufferRead` (instruction-carried types, unconditional overwrite),
    `MoveOut` dests, and the R1/R5 zero temps.  Pinned table-driven by
    `test_ownership_normalization.py`.

Everything else the historical string_arc did was output-neutral
bookkeeping (the identity `_ensure_owned` funnel and its move/own
machinery) and is GONE: every non-`MoveOut` instruction passes through BY
OBJECT IDENTITY — never reconstructed — and Return terminators are never
touched (the frozen plan's TERM anchors survive untouched).  The pass
reads NO ledger and builds NO ledger (S7 gate); it marks the ledger dirty
iff R1/R5 actually changed a block's instruction stream.

Temp naming keeps the historical `__arc{n}` scheme and allocation order
(R1 groups at the entry block's position in sorted block order, then R5
zeros in traversal order) so normalized MIR is structurally identical to
the deleted implementation's output.
"""

from __future__ import annotations

from typing import Dict, Mapping, Set

from lang.driftc.checker import FnInfo
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeId, TypeTable
from . import mir_nodes as M
from . import ownership_ledger_reporter as _ledger_reporter
from .ledger_cache import mark_ledger_dirty
from .destructible_authority import DropClassifier, classify_destructible_locals
from .string_ownership_analysis import (
	classify_string_array_locals,
	compute_recognized_releases,
	seed_string_dest_types,
)


def normalize_ownership_mir(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
	fn_infos: Mapping[FunctionId, FnInfo],
	audit_collector=None,
	r8_recognition=None,
) -> M.MirFunc:
	"""Normalize `func` in place (R1 + R5 + R8 copy-through + seeding).

	`audit_collector` is the driver-supplied `StringArcAudit` (audit
	enabled) or None — this pass is a NOTE PRODUCER only (deferred-
	finalize contract).  `r8_recognition` is the frozen plan-window
	`R8Recognition`; when None (bare unit invocation) the pass falls back
	to the SINGLE shared entry point `compute_recognized_releases`.
	Returns `func`.
	"""
	_audit = audit_collector
	local_types: Dict[str, TypeId] = func.local_types

	string_ty, string_locals, array_locals = classify_string_array_locals(
		func, type_table
	)
	_clf = DropClassifier(type_table)
	_destructible_locals, nullsafe_destructible_locals = classify_destructible_locals(
		func,
		_clf,
		local_types=local_types,
		string_locals=string_locals,
		array_locals=array_locals,
	)
	block_order = sorted(func.blocks.keys())

	# Shared dest-type seeding prepass (single source with the plan-window
	# recognition wrapper, which seeds a COPY of this same map).
	seed_string_dest_types(
		[func.blocks[bname] for bname in block_order],
		local_types,
		fn_infos=fn_infos,
		type_table=type_table,
	)

	# R8 vessel resolution + CLOSED-vessel validation (fail closed BEFORE
	# any rewrite).
	_recognized = (
		r8_recognition
		if r8_recognition is not None
		else compute_recognized_releases(func, type_table=type_table, fn_infos=fn_infos)
	)
	if _recognized.fn_name != func.name:
		raise AssertionError(
			f"ownership_normalization: R8 recognition belongs to fn "
			f"{_recognized.fn_name!r}, not {func.name!r} "
			f"(wrong-function recognition — fail closed)"
		)
	_rec_blocks = set(_recognized.recognized_by_block.keys())
	_fn_blocks = set(func.blocks.keys())
	if _rec_blocks != _fn_blocks:
		raise AssertionError(
			f"ownership_normalization: R8 recognition block set != function "
			f"block set for {func.name!r} "
			f"(missing={sorted(_fn_blocks - _rec_blocks)}, "
			f"extra={sorted(_rec_blocks - _fn_blocks)}) — fail closed"
		)
	for _rb_name, _rb_vals in _recognized.recognized_by_block.items():
		if not isinstance(_rb_vals, frozenset):
			raise AssertionError(
				f"ownership_normalization: R8 recognition for block "
				f"{_rb_name!r} is {type(_rb_vals).__name__}, not frozenset "
				f"(malformed vessel)"
			)

	# Historical `__arc{n}` temp naming + allocation order (structural
	# identity with the deleted implementation's output).
	used_ids: Set[str] = set(local_types.keys())
	arc_counter = 0

	def _new_temp() -> str:
		nonlocal arc_counter
		while True:
			arc_counter += 1
			name = f"__arc{arc_counter}"
			if name not in used_ids:
				used_ids.add(name)
				return name

	def _is_string_tid(tid: "TypeId | None") -> bool:
		return tid == string_ty

	for name in block_order:
		block = func.blocks[name]
		new_instrs: list[M.MInstr] = []
		changed = False

		# ── R1: entry-block zero-storage initialization ──
		if block.name == func.entry:
			for local in func.locals:
				if local in func.params:
					continue
				if local_types.get(local) != string_ty:
					continue
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=string_ty))
				_zb = M.StoreLocal(local=local, value=zero)
				setattr(_zb, "synthetic_zero_back", True)
				new_instrs.append(_zb)
				local_types[zero] = string_ty
				changed = True
			for local in func.locals:
				if local in func.params:
					continue
				if local not in array_locals:
					continue
				arr_ty = local_types.get(local)
				if arr_ty is None:
					continue
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=arr_ty))
				_zb = M.StoreLocal(local=local, value=zero)
				setattr(_zb, "synthetic_zero_back", True)
				new_instrs.append(_zb)
				local_types[zero] = arr_ty
				changed = True
			for local in func.locals:
				if local in func.params:
					continue
				if local not in nullsafe_destructible_locals:
					continue
				dest_ty = local_types.get(local)
				if dest_ty is None:
					continue
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=dest_ty))
				_zb = M.StoreLocal(local=local, value=zero)
				setattr(_zb, "synthetic_zero_back", True)
				new_instrs.append(_zb)
				local_types[zero] = dest_ty
				changed = True

		recognized_released: frozenset = _recognized.for_block(block.name)

		# ── Prescan seeding (only-if-missing; historical prescan order) ──
		for instr in block.instructions:
			if (
				isinstance(instr, M.ZeroValue)
				and _is_string_tid(instr.ty)
				and instr.dest not in local_types
			):
				local_types[instr.dest] = instr.ty
			elif (
				isinstance(instr, (M.ArrayIndexLoad, M.ArrayIndexLoadUnchecked))
				and _is_string_tid(instr.elem_ty)
				and instr.dest not in local_types
			):
				local_types[instr.dest] = instr.elem_ty

		# ── Rewrite traversal (original indices; identity pass-through) ──
		for _instr_idx, instr in enumerate(block.instructions):
			if isinstance(instr, M.StringRelease) and instr.value in recognized_released:
				# R8 copy-through: the pre-materialized release passes
				# through BY IDENTITY; the audit note keeps
				# `materialized_lastuse_release` author-independent.
				if _audit is not None:
					_audit.note(
						_ledger_reporter.STAKE_RELEASE, instr.value,
						_ledger_reporter.SITE_CLASS_MATERIALIZED_LASTUSE_RELEASE,
						pre_point=(block.name, _instr_idx),
						post_point=(block.name, len(new_instrs)),
					)
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.MoveOut):
				# R5 expansion.  Audit note BEFORE emission (historical
				# order): pre_point at the ORIGINAL source index, pairing
				# snapshotted from the SOURCE stream.
				if _audit is not None:
					_nxt = (
						block.instructions[_instr_idx + 1]
						if _instr_idx + 1 < len(block.instructions)
						else None
					)
					_audit.note(
						_ledger_reporter.STAKE_MOVEOUT_EXPANSION, instr.local,
						_ledger_reporter.SITE_CLASS_MOVEOUT_EXPANSION,
						pre_point=(block.name, _instr_idx),
						post_point=(block.name, len(new_instrs)),
						moveout_feeds_drop=(
							isinstance(_nxt, M.DropValue)
							and _nxt.value == instr.dest
						),
					)
				new_instrs.append(M.LoadLocal(dest=instr.dest, local=instr.local))
				local_types[instr.dest] = instr.ty
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=instr.ty))
				_zb = M.StoreLocal(local=instr.local, value=zero)
				setattr(_zb, "synthetic_zero_back", True)
				new_instrs.append(_zb)
				local_types[zero] = instr.ty
				changed = True
				continue

			# Per-arm `local_types` seeding (permanent contract; exact
			# historical semantics — LoadLocal copies only when the local's
			# type is known, the rest are unconditional instruction-carried
			# assignments).
			if isinstance(instr, M.LoadLocal):
				load_ty = local_types.get(instr.local)
				if load_ty is not None:
					local_types[instr.dest] = load_ty
			elif isinstance(instr, M.LoadRef):
				local_types[instr.dest] = instr.inner_ty
			elif isinstance(instr, M.StructGetField):
				local_types[instr.dest] = instr.field_ty
			elif isinstance(instr, M.VariantGetField):
				local_types[instr.dest] = instr.field_ty
			elif isinstance(instr, (M.ArrayIndexLoad, M.ArrayIndexLoadUnchecked)):
				local_types[instr.dest] = instr.elem_ty
			elif isinstance(instr, M.ArrayElemTake):
				local_types[instr.dest] = instr.elem_ty
			elif isinstance(instr, M.PtrRead):
				local_types[instr.dest] = instr.elem_ty
			elif isinstance(instr, M.RawBufferRead):
				local_types[instr.dest] = instr.elem_ty

			# Identity pass-through — NEVER reconstructed.
			new_instrs.append(instr)

		if changed:
			block.instructions = new_instrs
			# Real dirty mark within the audit's proximity window of the
			# actual mutation: R1/R5 changed this block's stream.
			mark_ledger_dirty(func, "ownership_normalization.rewrite_block")

	return func


__all__ = ["normalize_ownership_mir"]
