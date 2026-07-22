# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B2+C S3 — isolated site-3 Return-boundary cleanup emitter (UNWIRED).

Consumes the frozen `Site3ReturnPayload` decisions of a `CleanupPlan`
through the S1 `EmitterPhase` postflight lifecycle and inserts the
established Return-boundary destructible drop sequence for each ordered
payload entry:

    LoadLocal(tmp, local)
    ZeroValue(zero, ty)
    StoreLocal(local, zero)        # synthetic_zero_back=True
    DropValue(tmp, ty)

The drops are APPENDED to the Return block's instruction list — after
every original instruction, before the (PRESERVED) `M.Return`
terminator. The original Return object, its value/span, and every
original non-Return instruction object and its order are left untouched.

This module is UNWIRED: it has no driver/production caller in S3 — it is
the HARDENED BASE the S5 unified site-3 + R3/R4 Return authority builds
on. It is exercised only by focused tests
(`test_site3_return_emitter.py`).
"""
from __future__ import annotations

import dataclasses

from . import mir_nodes as M
from .cleanup_plan import CleanupPlan, PlanContractError
from .cleanup_payloads import Site3Drop, Site3ReturnPayload
from .ledger_cache import mark_ledger_dirty


def _seed_used_names(func: "M.MirFunc") -> "set[str]":
	"""Every name already in play in `func` — `local_types` keys AND every
	string-valued dest/operand/local across all instructions + terminators.
	Over-seeding only makes fresh temps MORE unique, never less, so this is
	a safe collision-proof seed (mirrors overwrite_cleanup's `_new_temp`
	seeding from `local_types`, extended to SSA dests/values)."""
	used: set[str] = set(func.local_types.keys())
	for blk in func.blocks.values():
		nodes = list(blk.instructions)
		if blk.terminator is not None:
			nodes.append(blk.terminator)
		for node in nodes:
			if not dataclasses.is_dataclass(node):
				continue
			for f in dataclasses.fields(node):
				v = getattr(node, f.name, None)
				if isinstance(v, str):
					used.add(v)
				elif isinstance(v, (list, tuple)):
					for item in v:
						if isinstance(item, str):
							used.add(item)
	return used


def emit_site3_returns(func: "M.MirFunc", plan: "CleanupPlan") -> int:
	"""Emit the Return-boundary destructible drops for every planned site-3
	decision. Returns the total number of drops emitted.

	Fail-closed through the plan contract: a wrong payload/site/type
	binding, a missing / duplicated / replaced / moved Return, field or
	value drift, or a postflight mismatch all raise `PlanContractError`
	(via `EmitterPhase.stage`/`commit`); a malformed payload raises
	`PlanContractError` (never a raw `AttributeError`); an incomplete or
	over-authored site-3 emission is caught by the pre-commit bijection;
	and an incompletely consumed site-3 population is caught by
	`assert_sites_consumed({"site3"})`, which this emitter calls itself.
	"""
	phase = plan.begin_phase(func)
	site3 = plan.decisions_for_site("site3")

	# Explicit payload validation (fail-closed to PlanContractError, never a
	# raw AttributeError): each site-3 payload must be a Site3ReturnPayload,
	# and each drop entry must carry `.local` and `.ty`.
	for dec in site3:
		pl = dec.payload
		if not isinstance(pl, Site3ReturnPayload):
			raise PlanContractError(
				f"site3_return_emitter (fn '{func.name}'): site-3 decision at "
				f"{dec.coord.block}:{dec.coord.orig_index} carries a "
				f"{type(pl).__name__} payload (expected Site3ReturnPayload)"
			)
		for drop in pl.drops:
			if not isinstance(drop, Site3Drop):
				raise PlanContractError(
					f"site3_return_emitter (fn '{func.name}'): site-3 drop entry "
					f"{drop!r} at {dec.coord.block} is a {type(drop).__name__} "
					f"(expected Site3Drop)"
				)

	# PREFLIGHT: validate every site-3 Return anchor against the pre-rewrite
	# MIR and stage it for consumption.
	for dec in site3:
		phase.stage(dec)

	# REWRITE: append the ordered drop sequences to each Return block, after
	# all original instructions and before the preserved terminator. Temps
	# are collision-proof against every existing name (incl. any pre-existing
	# `.s3d*`-shaped name).
	used = _seed_used_names(func)
	counter = 0

	def _new_temp() -> str:
		nonlocal counter
		while True:
			counter += 1
			name = f".s3d{counter}"
			if name not in used:
				used.add(name)
				return name

	# Emitter-local authoring side table: id(Return obj) -> [DropValue] in
	# EMISSION (= payload) order. Keeps authoring identity OUT of MIR nodes
	# (no dynamic `s3_authored_for` attribute).
	emitted_by_return: dict = {}
	emitted = 0
	for dec in site3:
		block = func.blocks[dec.coord.block]
		appended: list = []
		rid = id(dec.obj)
		for drop in dec.payload.drops:
			ty = drop.ty
			tmp = _new_temp()
			zero = _new_temp()
			load = M.LoadLocal(dest=tmp, local=drop.local)
			zv = M.ZeroValue(dest=zero, ty=ty)
			func.local_types[zero] = ty
			zb = M.StoreLocal(local=drop.local, value=zero)
			setattr(zb, "synthetic_zero_back", True)  # Slice B1 provenance
			dv = M.DropValue(value=tmp, ty=ty)
			emitted_by_return.setdefault(rid, []).append(dv)
			func.local_types[tmp] = ty
			appended.extend([load, zv, zb, dv])
			emitted += 1
		if appended:
			# Single in-place mutation per Return block, APPENDED after every
			# original instruction and before the preserved terminator.  Dirty
			# IFF emission: the appended drops + `local_types` writes
			# invalidate cached (block, idx) ledger state; a Return with no
			# planned drops mutates nothing and marks nothing.
			block.instructions.extend(appended)
			mark_ledger_dirty(func, "site3_return_emitter.emit")

	# Pre-commit BIJECTION: prove each site-3 decision produced EXACTLY its
	# planned ordered drop sequences (no missing / duplicate / orphan /
	# REORDER — destruction order is contractual).
	_validate_site3_emission(func, site3, emitted_by_return)

	# POSTFLIGHT: fresh-validate every staged Return anchor against the
	# mutated MIR (Return object preserved, end-index shifted — allowed),
	# then mark the site-3 decisions consumed.
	phase.mark_rewritten()
	phase.commit()
	# Self-enforced completeness — do not rely on callers.
	plan.assert_sites_consumed({"site3"})
	return emitted


def _validate_site3_emission(func: "M.MirFunc", site3, emitted_by_return: dict) -> None:
	"""ORDER-SENSITIVE bijection between the planned site-3 drops and the
	appended canonical sequences.  Authoring identity comes from the
	emitter-local `emitted_by_return` side table (id(Return) -> [DropValue]),
	NOT a MIR attribute.  For each Return decision, the emitted `(local, ty)`
	sequence — taken in BLOCK (instruction) order — must equal the payload's
	`sorted(destructible_locals)` order EXACTLY (no sorting either side, so a
	reordered drop sequence FAILS), each backed by a correctly-linked
	canonical `LoadLocal -> ZeroValue -> StoreLocal[synthetic_zero_back] ->
	DropValue`.  A side-table entry for no site-3 decision is an orphan.
	Fail-closed via `PlanContractError`."""
	dec_by_id = {id(d.obj): d for d in site3}

	orphans = set(emitted_by_return) - set(dec_by_id)
	if orphans:
		raise PlanContractError(
			f"site3_return_emitter (fn '{func.name}'): {len(orphans)} authored "
			f"drop group(s) target no site-3 decision (orphan authoring)."
		)

	for rid, dec in dec_by_id.items():
		dvs = emitted_by_return.get(rid, [])
		expected = list(dec.payload.drops)
		if len(dvs) != len(expected):
			raise PlanContractError(
				f"site3_return_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): Return authored {len(dvs)} drop(s) but "
				f"the plan expected {len(expected)} (missing/duplicate "
				f"authoring)."
			)
		# SAME-BLOCK + Return preserved: the decision's Return object must still
		# be the terminator of the decision's OWN block. (This is the S5-wiring
		# placement rule: an authored sequence in the wrong block, or a
		# replaced Return, fails here.)
		block = func.blocks.get(dec.coord.block)
		if block is None or block.terminator is not dec.obj:
			raise PlanContractError(
				f"site3_return_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): the site-3 Return is not the terminator "
				f"of its own block (wrong block / replaced Return)."
			)
		n = len(expected)
		if n == 0:
			continue
		instrs = block.instructions
		# CONTIGUOUS TAIL: the n canonical sequences (4 instrs each) MUST be
		# the final 4n instructions of the block, immediately before the
		# preserved Return terminator — no gaps, nothing interleaved.
		tail_start = len(instrs) - 4 * n
		if tail_start < 0:
			raise PlanContractError(
				f"site3_return_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): no room for the contiguous {n}-drop tail "
				f"before the Return."
			)
		got_seq = []
		for k in range(n):
			base = tail_start + 4 * k
			load, zv, zb, dv = instrs[base], instrs[base + 1], instrs[base + 2], instrs[base + 3]
			ok = (
				isinstance(load, M.LoadLocal)
				and isinstance(zv, M.ZeroValue)
				and isinstance(zb, M.StoreLocal)
				and zb.local == load.local and zb.value == zv.dest
				and getattr(zb, "synthetic_zero_back", False) is True
				and isinstance(dv, M.DropValue)
				and dv.value == load.dest and dv.ty == zv.ty
			)
			if not ok:
				raise PlanContractError(
					f"site3_return_emitter (fn '{func.name}', block "
					f"'{dec.coord.block}'[{base}]): the contiguous drop tail is "
					f"not a correctly-linked canonical sequence (operand/type or "
					f"placement mismatch)."
				)
			got_seq.append((load.local, dv.ty))
		# ORDER-SENSITIVE: the tail order must equal the payload's
		# `sorted(destructible_locals)` destruction order exactly (no sort).
		expected_seq = [(d.local, d.ty) for d in expected]
		if got_seq != expected_seq:
			raise PlanContractError(
				f"site3_return_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): authored drop (local, ty) SEQUENCE "
				f"{got_seq} != planned destruction order {expected_seq}."
			)
		# The side-table drops must be EXACTLY the tail drops (no authored
		# sequence outside the contiguous tail / in another block).
		tail_drop_ids = {id(instrs[tail_start + 4 * k + 3]) for k in range(n)}
		if {id(dv) for dv in dvs} != tail_drop_ids:
			raise PlanContractError(
				f"site3_return_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): an authored drop is not in the contiguous "
				f"tail immediately before the Return (misplaced authoring)."
			)


__all__ = ("emit_site3_returns",)
