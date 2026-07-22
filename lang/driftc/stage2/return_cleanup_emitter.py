# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B2+C S5 — unified Return-boundary cleanup emitter (production authority).

The single Return authority that replaced string_arc's inline
Return-boundary release + drop emission.  It consumes the frozen
`CleanupPlan`'s `string_release` (R3/R4 String scope-exit releases) AND
`site3` (destructible drops) decisions ATOMICALLY per Return through the
S1 `EmitterPhase` postflight lifecycle, and APPENDS, before the PRESERVED
`M.Return` terminator, IN THIS ORDER (mirroring string_arc's former
`_release_all_locals` THEN `site3_return_drops` ordering):

  (a) the STRING-RELEASE BAND — for each `StringReleasePayload.locals`
      entry, in payload order:

          LoadLocal(old, local)
          ZeroValue(zero, string_ty)
          StoreLocal(local, zero)        # synthetic_zero_back=True
          StringRelease(old)

      with `local_types[old] = local_types[zero] = string_ty` (mirrors
      string_arc's `_release_local`);

  (b) the SITE-3 DESTRUCTIBLE DROP TAIL — for each `Site3Drop`, in payload
      order:

          LoadLocal(tmp, local)
          ZeroValue(zero, ty)
          StoreLocal(local, zero)        # synthetic_zero_back=True
          DropValue(tmp, ty)

Both decision families anchor the SAME preserved `M.Return` (the plan
supports multi-site per anchor).  The original Return object, its
value/span, and every original non-Return instruction object and its
order are left untouched — the two bands are the contiguous tail before
the terminator (the string band immediately precedes the site-3 tail).

Fail-closed through the plan contract + two pre-commit bijections (one
per band): a wrong payload/site/type binding, a missing / duplicated /
replaced / moved Return, field/value drift, an incomplete / over-authored
/ reordered emission, or an incompletely consumed population all raise
`PlanContractError`.  `mark_ledger_dirty` iff emission occurred.

The AUDIT `scope_exit_release` events for the string band are NOT recorded
here (this emitter is pure codegen).  They are reconstructed once, from
the driver-local frozen `C1Contribution` the plan slot froze, inside the
single deferred `StringArcAudit.finalize` — exactly once, no double count
(the planned C1 `released` set IS this emitter's R3 release set, both the
`string_return_releases` result).
"""
from __future__ import annotations

import dataclasses

from . import mir_nodes as M
from .cleanup_plan import CleanupPlan, PlanContractError
from .cleanup_payloads import (
	Site3Drop,
	Site3ReturnPayload,
	StringReleasePayload,
)
from .ledger_cache import mark_ledger_dirty


def _seed_used_names(func: "M.MirFunc") -> "set[str]":
	"""Every name already in play in `func` — `local_types` keys AND every
	string-valued dest/operand/local across all instructions + terminators.
	Over-seeding only makes fresh temps MORE unique, never less, so this is
	a safe collision-proof seed."""
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


def emit_return_cleanups(func: "M.MirFunc", plan: "CleanupPlan") -> int:
	"""Emit the string-release band + site-3 destructible drop tail for
	every planned Return.  Returns the total number of canonical quad
	sequences emitted (string releases + destructible drops).

	Consumes the `string_release` and `site3` sites of `plan` atomically
	in ONE `EmitterPhase`; self-enforces `assert_sites_consumed({"site3",
	"string_release"})`.
	"""
	phase = plan.begin_phase(func)
	site3 = plan.decisions_for_site("site3")
	string_release = plan.decisions_for_site("string_release")

	# Explicit payload validation (fail-closed to PlanContractError, never a
	# raw AttributeError).
	for dec in site3:
		pl = dec.payload
		if not isinstance(pl, Site3ReturnPayload):
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}'): site-3 decision at "
				f"{dec.coord.block}:{dec.coord.orig_index} carries a "
				f"{type(pl).__name__} payload (expected Site3ReturnPayload)"
			)
		for drop in pl.drops:
			if not isinstance(drop, Site3Drop):
				raise PlanContractError(
					f"return_cleanup_emitter (fn '{func.name}'): site-3 drop entry "
					f"{drop!r} at {dec.coord.block} is a {type(drop).__name__} "
					f"(expected Site3Drop)"
				)
	for dec in string_release:
		pl = dec.payload
		if not isinstance(pl, StringReleasePayload):
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}'): string_release decision "
				f"at {dec.coord.block}:{dec.coord.orig_index} carries a "
				f"{type(pl).__name__} payload (expected StringReleasePayload)"
			)
		_tb = dict(dec.type_bindings)
		for local in pl.locals:
			if not isinstance(local, str):
				raise PlanContractError(
					f"return_cleanup_emitter (fn '{func.name}'): string_release local "
					f"{local!r} at {dec.coord.block} is not a name"
				)
			if local not in _tb:
				raise PlanContractError(
					f"return_cleanup_emitter (fn '{func.name}'): string_release local "
					f"{local!r} at {dec.coord.block} has no frozen String type binding"
				)

	# PREFLIGHT: validate every Return anchor (both sites) against the
	# pre-rewrite MIR and stage it for consumption.
	for dec in site3:
		phase.stage(dec)
	for dec in string_release:
		phase.stage(dec)

	# REWRITE.  Temps are collision-proof against every existing name.
	used = _seed_used_names(func)
	counter = 0

	def _new_temp() -> str:
		nonlocal counter
		while True:
			counter += 1
			name = f".rce{counter}"
			if name not in used:
				used.add(name)
				return name

	# Group decisions by the shared Return anchor object.  Both sites
	# anchor the SAME M.Return; the plan's cross-site consistency guarantees
	# identical coord/value.
	site3_by_return: dict = {id(d.obj): d for d in site3}
	sr_by_return: dict = {id(d.obj): d for d in string_release}
	return_ids: list = []
	_seen: set = set()
	for d in list(site3) + list(string_release):
		if id(d.obj) not in _seen:
			_seen.add(id(d.obj))
			return_ids.append(id(d.obj))

	# Emitter-local authoring side tables (id(Return) -> [StringRelease] /
	# [DropValue] in EMISSION order) — authoring identity stays OUT of MIR
	# nodes.
	emitted_releases_by_return: dict = {}
	emitted_drops_by_return: dict = {}
	total = 0
	for rid in return_ids:
		s3 = site3_by_return.get(rid)
		sr = sr_by_return.get(rid)
		anchor_dec = s3 if s3 is not None else sr
		block = func.blocks[anchor_dec.coord.block]
		appended: list = []
		# (a) STRING-RELEASE BAND (first — before the site-3 tail).
		if sr is not None:
			_tb = dict(sr.type_bindings)
			for local in sr.payload.locals:
				sty = _tb[local]
				old = _new_temp()
				zero = _new_temp()
				load = M.LoadLocal(dest=old, local=local)
				zv = M.ZeroValue(dest=zero, ty=sty)
				func.local_types[zero] = sty
				zb = M.StoreLocal(local=local, value=zero)
				setattr(zb, "synthetic_zero_back", True)  # Slice B1 provenance
				rel = M.StringRelease(value=old)
				func.local_types[old] = sty
				emitted_releases_by_return.setdefault(rid, []).append(rel)
				appended.extend([load, zv, zb, rel])
				total += 1
		# (b) SITE-3 DESTRUCTIBLE DROP TAIL (contiguous final band).
		if s3 is not None:
			for drop in s3.payload.drops:
				ty = drop.ty
				tmp = _new_temp()
				zero = _new_temp()
				load = M.LoadLocal(dest=tmp, local=drop.local)
				zv = M.ZeroValue(dest=zero, ty=ty)
				func.local_types[zero] = ty
				zb = M.StoreLocal(local=drop.local, value=zero)
				setattr(zb, "synthetic_zero_back", True)  # Slice B1 provenance
				dv = M.DropValue(value=tmp, ty=ty)
				func.local_types[tmp] = ty
				emitted_drops_by_return.setdefault(rid, []).append(dv)
				appended.extend([load, zv, zb, dv])
				total += 1
		if appended:
			# Single in-place mutation per Return block, APPENDED after every
			# original instruction and before the preserved terminator.  Dirty
			# IFF emission.
			block.instructions.extend(appended)
			mark_ledger_dirty(func, "return_cleanup_emitter.emit")

	# Pre-commit BIJECTIONS (order-sensitive, both bands).
	_validate_string_release_emission(func, string_release, site3, emitted_releases_by_return)
	_validate_site3_emission(func, site3, emitted_drops_by_return)

	# POSTFLIGHT.
	phase.mark_rewritten()
	phase.commit()
	plan.assert_sites_consumed({"site3", "string_release"})
	return total


def _validate_string_release_emission(func, string_release, site3, emitted_releases_by_return: dict) -> None:
	"""ORDER-SENSITIVE bijection between the planned string releases and the
	appended canonical release band.  Each `StringReleasePayload.locals`
	entry maps to EXACTLY one canonical `LoadLocal -> ZeroValue ->
	StoreLocal[synthetic_zero_back] -> StringRelease` sequence, in payload
	order, sitting in the string-release band IMMEDIATELY before the site-3
	tail (positions `[L - 4n - 4m, L - 4n)` where `n` is the site-3 drop
	count and `m` the release count).  Missing / duplicate / orphan /
	wrong-order / misplaced all fail closed via `PlanContractError`."""
	dec_by_id = {id(d.obj): d for d in string_release}
	n_by_id = {id(d.obj): len(d.payload.drops) for d in site3}

	orphans = set(emitted_releases_by_return) - set(dec_by_id)
	if orphans:
		raise PlanContractError(
			f"return_cleanup_emitter (fn '{func.name}'): {len(orphans)} authored "
			f"release group(s) target no string_release decision (orphan authoring)."
		)

	for rid, dec in dec_by_id.items():
		rels = emitted_releases_by_return.get(rid, [])
		expected = list(dec.payload.locals)
		if len(rels) != len(expected):
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): Return authored {len(rels)} string "
				f"release(s) but the plan expected {len(expected)} "
				f"(missing/duplicate/suppressed authoring)."
			)
		block = func.blocks.get(dec.coord.block)
		if block is None or block.terminator is not dec.obj:
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): the string_release Return is not the "
				f"terminator of its own block (wrong block / replaced Return)."
			)
		m = len(expected)
		if m == 0:
			continue
		n = n_by_id.get(rid, 0)
		instrs = block.instructions
		# The m release sequences (4 instrs each) MUST be the 4m instructions
		# immediately BEFORE the site-3 tail (the final 4n), i.e. positions
		# [L - 4n - 4m, L - 4n).
		band_start = len(instrs) - 4 * n - 4 * m
		if band_start < 0:
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): no room for the {m}-release band before the "
				f"{n}-drop site-3 tail."
			)
		got_seq = []
		for k in range(m):
			base = band_start + 4 * k
			load, zv, zb, rel = instrs[base], instrs[base + 1], instrs[base + 2], instrs[base + 3]
			ok = (
				isinstance(load, M.LoadLocal)
				and isinstance(zv, M.ZeroValue)
				and isinstance(zb, M.StoreLocal)
				and zb.local == load.local and zb.value == zv.dest
				and getattr(zb, "synthetic_zero_back", False) is True
				and isinstance(rel, M.StringRelease)
				and rel.value == load.dest
			)
			if not ok:
				raise PlanContractError(
					f"return_cleanup_emitter (fn '{func.name}', block "
					f"'{dec.coord.block}'[{base}]): the string-release band is not a "
					f"correctly-linked canonical Load/Zero/Store/StringRelease sequence "
					f"(operand or placement mismatch)."
				)
			got_seq.append(load.local)
		# ORDER-SENSITIVE: the band order must equal the payload order exactly.
		if got_seq != expected:
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): authored release local SEQUENCE {got_seq} != "
				f"planned release order {expected}."
			)
		# The side-table releases must be EXACTLY the band releases.
		band_rel_ids = {id(instrs[band_start + 4 * k + 3]) for k in range(m)}
		if {id(r) for r in rels} != band_rel_ids:
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): an authored release is not in the band "
				f"immediately before the site-3 tail (misplaced authoring)."
			)


def _validate_site3_emission(func: "M.MirFunc", site3, emitted_by_return: dict) -> None:
	"""ORDER-SENSITIVE bijection between the planned site-3 drops and the
	appended canonical sequences — the CONTIGUOUS final `4n` instructions
	before the preserved Return (the string-release band, if any, precedes
	this tail).  Missing / duplicate / orphan / reorder / misplaced fail
	closed via `PlanContractError`."""
	dec_by_id = {id(d.obj): d for d in site3}

	orphans = set(emitted_by_return) - set(dec_by_id)
	if orphans:
		raise PlanContractError(
			f"return_cleanup_emitter (fn '{func.name}'): {len(orphans)} authored "
			f"drop group(s) target no site-3 decision (orphan authoring)."
		)

	for rid, dec in dec_by_id.items():
		dvs = emitted_by_return.get(rid, [])
		expected = list(dec.payload.drops)
		if len(dvs) != len(expected):
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): Return authored {len(dvs)} drop(s) but "
				f"the plan expected {len(expected)} (missing/duplicate authoring)."
			)
		block = func.blocks.get(dec.coord.block)
		if block is None or block.terminator is not dec.obj:
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): the site-3 Return is not the terminator "
				f"of its own block (wrong block / replaced Return)."
			)
		n = len(expected)
		if n == 0:
			continue
		instrs = block.instructions
		# CONTIGUOUS TAIL: the n canonical sequences (4 instrs each) MUST be
		# the final 4n instructions of the block, immediately before the
		# preserved Return terminator.
		tail_start = len(instrs) - 4 * n
		if tail_start < 0:
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}', block "
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
					f"return_cleanup_emitter (fn '{func.name}', block "
					f"'{dec.coord.block}'[{base}]): the contiguous drop tail is "
					f"not a correctly-linked canonical sequence (operand/type or "
					f"placement mismatch)."
				)
			got_seq.append((load.local, dv.ty))
		expected_seq = [(d.local, d.ty) for d in expected]
		if got_seq != expected_seq:
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): authored drop (local, ty) SEQUENCE "
				f"{got_seq} != planned destruction order {expected_seq}."
			)
		tail_drop_ids = {id(instrs[tail_start + 4 * k + 3]) for k in range(n)}
		if {id(dv) for dv in dvs} != tail_drop_ids:
			raise PlanContractError(
				f"return_cleanup_emitter (fn '{func.name}', block "
				f"'{dec.coord.block}'): an authored drop is not in the contiguous "
				f"tail immediately before the Return (misplaced authoring)."
			)


__all__ = ("emit_return_cleanups",)
