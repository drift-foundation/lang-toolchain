# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 site-1 patch 1 — cleanup re-authoring pass for function-exit
scope drops.

HIR→MIR's seven function-exit `_emit_scope_drops(scope_index=0)`
call sites now emit a single `M.CleanupHook` instruction (via
`_emit_function_exit_cleanup_hook`) instead of inline drops.  This
pass runs after `build_ledger` and before `drop_flags` /
`string_arc`; for each `CleanupHook` it consults the ledger's
`verdict_at` for every candidate (local, type) pair and emits real
`MoveOut + DropValue` sequences in legacy emission order.

Authority: site 1's function-exit drop decisions are now driven by
`verdict_at`, NOT by HIR→MIR's `_moved_locals` set.  Nested-scope
`_emit_scope_drops(scope_index>0)` calls remain on legacy in this
patch — `_moved_locals` retirement is gated on those migrating in
follow-up patches.

Variant zero-tag widening: when the verdict is `PathDependent`
(state `MAYBE_UNINIT`), the policy bit `variant_zero_tag_drop_safe`
(centralised in `string_arc.py` since site-3 sub-step 3) controls
whether we emit anyway — variants are safe-to-drop on PHI-zero
storage (tag-0 destructor is a no-op) AND necessary on live paths.
For non-variant `PathDependent`, we skip emission AND record an
observe-mode telemetry record (see `_emit_path_dependent_skip_record`)
so we can detect the case if it ever fires in real Drift.

RAII timing: drops are emitted at the marker position in the
block's instruction list (via list splice), preserving the original
source-scope-exit point.  Marker is removed after authoring; the
next pass sees only canonical `MoveOut + DropValue` sequences.
"""

from __future__ import annotations

import sys
from typing import List, Optional

from lang.driftc.core.types_core import TypeId, TypeTable
from lang.driftc import debug as drift_debug
from . import mir_nodes as M
from . import ownership_ledger_events as _ledger_events
from .ownership_ledger import DropVerdict, LiveStateMap
from .drop_policy_compute import compute_drop_policy
from .string_arc import variant_zero_tag_drop_safe


def author_cleanup(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
) -> int:
	"""Walk every block, replace each `M.CleanupHook` with the
	canonical drop sequences the ledger says are needed.

	Returns the number of `MoveOut + DropValue` pairs emitted across
	all hooks (telemetry; not used for control flow).

	No-op when the function has no `_ownership_ledger` attached
	(e.g. ad-hoc test harnesses that build MIR without the driver
	wiring).  Any `CleanupHook` instructions remain in place in that
	case — they would surface as a downstream pass error, which is
	the intended fail-loud signal.
	"""
	ledger: Optional[LiveStateMap] = getattr(func, "_ownership_ledger", None)
	if ledger is None:
		return 0
	emitted_drops = 0
	new_temp_counter = 0
	# MIR shares one string namespace across locals and SSA value-ids
	# (LocalId and ValueId are both strings).  Seed `used_temps` from
	# every name the function already references so a synthesised
	# `__cleanup_tN` cannot collide with a real local — the prefix
	# alone is not a guarantee (user code could legally name a local
	# `__cleanup_t1`).  Sources: instruction dests, func.locals,
	# func.local_types keys, and func.params.
	used_temps: set[str] = set()
	used_temps.update(func.params)
	used_temps.update(func.locals)
	used_temps.update(func.local_types.keys())
	for blk in func.blocks.values():
		for ins in blk.instructions:
			dest = getattr(ins, "dest", None)
			if isinstance(dest, str):
				used_temps.add(dest)

	def _new_temp() -> str:
		nonlocal new_temp_counter
		while True:
			new_temp_counter += 1
			name = f"__cleanup_t{new_temp_counter}"
			if name not in used_temps:
				used_temps.add(name)
				return name

	for blk in func.blocks.values():
		new_instrs: List[M.MInstr] = []
		for idx, ins in enumerate(blk.instructions):
			if not isinstance(ins, M.CleanupHook):
				new_instrs.append(ins)
				continue
			# Hook program point is `(block_name, len(new_instrs))` —
			# the cursor where the hook would be appended in the
			# rewritten instruction list.  But for `verdict_at`
			# semantics we want the pre-hook state, which is
			# `state_pre((blk.name, idx))` on the original
			# instruction list.  These coincide when no prior
			# instruction in the block was removed/added; for patch
			# 1 we only ever REMOVE the hook itself, so the
			# original-index `idx` is the right query point.
			hook_point = (blk.name, idx)
			for local, ty in ins.candidates:
				verdict = ledger.verdict_at(
					hook_point,
					local,
					needs_drop=bool(compute_drop_policy(type_table, ty).needs_drop),
				)
				should_emit = False
				if verdict is DropVerdict.MUST_DROP:
					should_emit = True
				elif verdict is DropVerdict.PATH_DEPENDENT:
					if variant_zero_tag_drop_safe(ty, type_table):
						# Variant zero-tag widening: tag=0 destructor
						# is a no-op on uninit paths; drops on live
						# paths are necessary.
						should_emit = True
					else:
						# Non-variant + PathDependent at function-exit
						# is a tripwire shape — site 1's `_moved_locals`
						# legacy behaviour skipped here too, so for now
						# we match (no leak), but record telemetry so
						# the case is visible if it appears.
						_emit_path_dependent_skip_record(func, blk.name, idx, local)
						continue
				if not should_emit:
					continue
				tmp = _new_temp()
				new_instrs.append(M.MoveOut(dest=tmp, local=local, ty=ty))
				new_instrs.append(M.DropValue(value=tmp, ty=ty))
				func.local_types[tmp] = ty
				emitted_drops += 1
		blk.instructions = new_instrs
	return emitted_drops


def _emit_path_dependent_skip_record(
	func: M.MirFunc,
	block_name: str,
	idx: int,
	local: str,
) -> None:
	"""Observe-mode tripwire for the `non-variant + PathDependent`
	case at function-exit cleanup.  Today this case is a no-op
	(legacy site 1 also skipped here via `_moved_locals`), but the
	record lets us detect any real Drift code that hits it.

	Gated on `drift_debug.enabled("ownership_ledger")` so production
	builds stay quiet.
	"""
	if not drift_debug.enabled("ownership_ledger"):
		return
	import json
	payload = {
		"site": "scope_drop",
		"fn_name": func.name,
		"program_point": [block_name, idx],
		"local": local,
		"site_verdict": _ledger_events.VERDICT_MUST_NOT_DROP,
		"site_reason": "path_dependent_non_variant_skip",
		"ledger_verdict": "path_dependent",
		"raw_state": "maybe_uninit",
		"classification": "agree",
		"field_path": [],
	}
	sys.stderr.write("[drift:ownership_ledger] " + json.dumps(payload, sort_keys=True) + "\n")
