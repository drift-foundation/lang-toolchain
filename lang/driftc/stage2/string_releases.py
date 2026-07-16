# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""TLR-2b..6: materialize last-use releases for block-local family
temps (`is_materialized_release_family_producer` — the single source of
membership: ConstString since TLR-2b, StringConcat since TLR-3, proven
non-throw String-returning calls since TLR-4, StringFrom{Int,Bool,Uint,
Float} + ExcGetParamsJson/ExcGetContextJson since TLR-5, CopyValue since
TLR-6) as explicit MIR, before the ledger build that feeds `string_arc`.

Problem (TLR measurement, 2026-07-14): 618,744 corpus releases of owned
String temps whose LAST use is non-consuming existed only as
`string_arc`'s private per-block bookkeeping (`use_counts` /
`owned_values`) — no ledger authority models SSA temp lifetimes.  TLR-1
split the dominant family into its own audit class via an in-string_arc
shim; this pass makes the family's releases REAL MIR with a dedicated
author (611,346 of the 618,744 after TLR-6 — everything except the
cross-block tail, 7,398, which awaits its own lifetime-analysis gate):

    %t = ConstString "..."            %t = ConstString "..."
    StringEq(%e, %t, %u)        →     StringEq(%e, %t, %u)
                                      StringRelease(%t)

The release sits IMMEDIATELY AFTER the draining instruction — the same
position `string_arc`'s in-pass emission used — so the output stream is
byte-identical for the migrated family.  `string_arc` recognizes the
in-contract releases (shape AND placement, validated fail-closed by the
shared analysis), excludes them from use counting, never adds the temps
to `owned_values` (a second release is impossible by construction), and
notes `materialized_lastuse_release` at its recognition arm — the audit
counter keeps its author-independent meaning and `events` stays
constant.

Contracts consumed (single-author rule — this pass re-implements NO
classification):
- `seed_string_dest_types` — String dest type seeding, run on a private
  COPY of `local_types` (production MIR lacks types for many temps; the
  pass must not depend on upstream metadata completeness and must not
  mutate func metadata);
- `compute_string_temp_liveness` — the per-block live-out fixpoint
  extracted from `insert_string_arc` (identical result on pre- and
  post-materialization MIR: in-contract releases never reach
  `block_use` because their temps are defined earlier in the block);
- `compute_lastuse_release_points` — contract 2, the occurrence-level
  release-point calculator (three-way CONSUME/USE/IGNORE dispositions,
  multiplicity rule, recognition of already-materialized releases —
  which also makes this pass idempotent: a second run recognizes its own
  output and computes no new points).

Ledger placement (TLR-2 design §1/§2): this pass runs BEFORE the per-fn
ledger build (`_ol_build_and_attach` in the driver's cleanup_authoring
loop), so the one ledger `string_arc` consumes is built on
post-materialization MIR — no extra rebuild.  `StringRelease` has no
transfer-function arm in the ledger walker (it cannot change any
tracked local's state), so the shifted snapshot is state-identical at
corresponding program points; only `(block, idx)` keys shift.
"""

from typing import Dict, List, Mapping

from lang.driftc.checker import FnInfo
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable

from . import cfg as _cfg
from . import mir_nodes as M
from .ledger_cache import mark_ledger_dirty
from .string_arc import (
	compute_lastuse_release_points,
	compute_string_temp_liveness,
	iter_used_values,
	seed_string_dest_types,
)


def materialize_lastuse_releases(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
	fn_infos: Mapping[FunctionId, FnInfo],
) -> bool:
	"""Emit `StringRelease(%t)` immediately after the draining
	instruction of every qualified block-local family temp
	(`is_materialized_release_family_producer`).  Returns True iff any
	release was inserted."""
	string_ty = type_table.ensure_string()
	block_order = sorted(func.blocks.keys())
	local_types = dict(getattr(func, "local_types", {}) or {})
	seed_string_dest_types(
		[func.blocks[name] for name in block_order],
		local_types,
		fn_infos=fn_infos,
		type_table=type_table,
	)
	live_out = compute_string_temp_liveness(
		func.blocks,
		block_order,
		local_types=local_types,
		string_ty=string_ty,
	)
	changed = False
	for name in block_order:
		block = func.blocks[name]
		points = compute_lastuse_release_points(
			block,
			local_types=local_types,
			fn_infos=fn_infos,
			type_table=type_table,
			live_out_names=live_out.get(name, set()),
		)
		if not points:
			continue
		# Group temps by draining index.  Same-index temps release in
		# DRAIN order — the position of each temp's LAST operand
		# occurrence in the draining instruction's `iter_used_values`
		# walk — mirroring `_note_use`'s decrement sequence in
		# string_arc's generic fallthrough, so the materialized stream
		# matches the in-pass emission order.
		by_idx: Dict[int, List[str]] = {}
		for temp, idx in points.items():
			by_idx.setdefault(idx, []).append(temp)

		def _drain_order(idx: int, temps: List[str]) -> List[str]:
			if len(temps) == 1:
				return temps
			if idx < len(block.instructions):
				walk = list(iter_used_values(block.instructions[idx]))
			elif block.terminator is not None:
				walk = list(_cfg.terminator_value_uses(block.terminator))
			else:
				walk = []

			def _last_pos(t: str) -> int:
				pos = -1
				for i, v in enumerate(walk):
					if v == t:
						pos = i
				return pos

			return sorted(temps, key=_last_pos)

		# Insert bottom-up (descending idx) so original indices stay
		# valid; a terminator-drained group (idx == len(instructions),
		# processed first) lands at the end of the instruction list,
		# before the terminator.
		new_instrs = list(block.instructions)
		for idx in sorted(by_idx, reverse=True):
			releases = [
				M.StringRelease(value=t) for t in _drain_order(idx, by_idx[idx])
			]
			insert_at = len(new_instrs) if idx >= len(block.instructions) else idx + 1
			new_instrs[insert_at:insert_at] = releases
		block.instructions = new_instrs
		changed = True
	if changed:
		mark_ledger_dirty(func, "string_releases.materialize_lastuse_releases")
	return changed


__all__ = ["materialize_lastuse_releases"]
