# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 site-1 — cleanup re-authoring pass for scope drops, with
observe parity.

All of HIR→MIR's scope-drop call sites (function-exit at HReturn /
HThrow, `lower_function_body` fall-through, `lower_block`
fall-through, lambda-block exits, and HBreak / HContinue) now emit
`M.CleanupHook` markers via `_emit_scope_cleanup_hook(scope_index)`
instead of inline drops.  This pass runs after `build_ledger` and
before `drop_flags` / `string_arc`; for each `CleanupHook` it
consults the ledger's `verdict_at` for every candidate (local, type)
pair and emits real `MoveOut + DropValue` sequences in legacy
emission order.

Authority: site 1's drop decisions are now driven by `verdict_at`
across all emission sites, NOT by HIR→MIR's `_moved_locals` set.
Consume-via-intrinsic gap class (DROP_VALUE / RAW_WRITE /
PTR_WRITE / MAYBE_WRITE / REPLACE) is closed at the HIR→MIR
boundary via `_lower_owning_consume`, so cleanup_authoring's
verdict lookups see the intrinsic consumption as a real MoveOut
in MIR.  `_moved_locals` retirement can now proceed in a
follow-up patch (patch 6 in the Path Y plan).

Variant zero-tag widening: when the verdict is `PathDependent`
(state `MAYBE_UNINIT`), the policy bit `variant_zero_tag_drop_safe`
(centralised in `string_arc.py` since site-3 sub-step 3) controls
whether we emit anyway — variants are safe-to-drop on PHI-zero
storage (tag-0 destructor is a no-op) AND necessary on live paths.
For non-variant `PathDependent`, we skip emission.

Observe parity (patch 2, 2026-04-23): every per-candidate decision
is emitted as a `[drift:ownership_ledger]` stderr line, gated on
`drift_debug.enabled("ownership_ledger")`.  Records carry
`site=scope_drop` so observe triage routes them through the same
buckets legacy site-1 records used.  Classification is hard-coded
`agree` — cleanup_authoring IS the ledger consultation, so by
construction the site verdict matches the ledger verdict at this
program point.  This avoids re-running `compare_events` (which would
use the quarantined `has_drop` approximation in
`driftc.py::_needs_drop` and could spuriously flag bucket-6
records).  Reason tags reuse the legacy site-1 set:
`needs_drop` / `not_drop_needing` / `moved_unconditional` plus the
new `path_dependent_non_variant_skip` tripwire.

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
from .ownership_ledger import DropVerdict, LiveState, LiveStateMap
from .drop_policy_compute import compute_drop_policy
from .string_arc import variant_zero_tag_drop_safe


# Patch-2 reason tag for the non-variant + PathDependent skip case.
# Site 1's legacy `_moved_locals` would have skipped here too; this
# tag makes the case visible in observe so we can detect any real
# Drift code that hits it.
_REASON_PATH_DEPENDENT_NON_VARIANT_SKIP = "path_dependent_non_variant_skip"


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

	observe_on = drift_debug.enabled("ownership_ledger")
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
				needs_drop_axis = bool(compute_drop_policy(type_table, ty).needs_drop)
				verdict = ledger.verdict_at(hook_point, local, needs_drop=needs_drop_axis)
				raw_state = ledger.state_pre(hook_point, local)
				should_emit = False
				if verdict is DropVerdict.MUST_DROP:
					should_emit = True
				elif verdict is DropVerdict.PATH_DEPENDENT:
					# Variant zero-tag widening: tag=0 destructor is a
					# no-op on uninit paths; drops on live paths are
					# necessary.
					if variant_zero_tag_drop_safe(ty, type_table):
						should_emit = True
				# Patch 2: emit observe parity record per candidate,
				# regardless of decision.  Reason tag mirrors the
				# legacy site-1 set; classification is hard-coded
				# `agree` (cleanup_authoring IS the ledger
				# consultation; see module docstring).
				if observe_on:
					_emit_decision_record(
						func=func,
						block_name=blk.name,
						idx=idx,
						local=local,
						verdict=verdict,
						raw_state=raw_state,
						should_emit=should_emit,
					)
				if not should_emit:
					continue
				tmp = _new_temp()
				new_instrs.append(M.MoveOut(dest=tmp, local=local, ty=ty))
				new_instrs.append(M.DropValue(value=tmp, ty=ty))
				func.local_types[tmp] = ty
				emitted_drops += 1
		blk.instructions = new_instrs
	return emitted_drops


def _emit_decision_record(
	*,
	func: M.MirFunc,
	block_name: str,
	idx: int,
	local: str,
	verdict: DropVerdict,
	raw_state: LiveState,
	should_emit: bool,
) -> None:
	"""Patch-2 observe parity emit.  One JSON line per per-candidate
	decision, format-compatible with the existing
	`[drift:ownership_ledger]` channel that legacy site-1 records
	flow through.

	Classification is hard-coded `agree` — cleanup_authoring IS the
	ledger consultation, so by construction the site verdict matches
	the ledger verdict at this program point.  Routing through
	`compare_events` would re-derive the verdict using the
	quarantined `has_drop` approximation in `driftc.py::_needs_drop`
	and could spuriously bucket-6 the records; the direct emit
	avoids that.

	Reason tag mapping (mirrors legacy site-1 reason set):

	  - emit + MUST_DROP / PathDependent variant-widening → `needs_drop`
	  - skip + state=MOVED_OUT → `moved_unconditional` (definite move)
	  - skip + needs_drop=False / state=TOMBSTONED / state=UNINIT
	    → `not_drop_needing`
	  - skip + PathDependent non-variant → `path_dependent_non_variant_skip`
	    (tripwire — site 1 legacy `_moved_locals` skipped here too;
	    the tag makes the case visible if any real Drift hits it)
	"""
	import json
	site_verdict = _ledger_events.VERDICT_MUST_DROP if should_emit else _ledger_events.VERDICT_MUST_NOT_DROP
	if should_emit:
		site_reason = _ledger_events.REASON_NEEDS_DROP
	elif verdict is DropVerdict.PATH_DEPENDENT:
		# Non-variant path-dependent skip — variant case took the
		# emit branch above.
		site_reason = _REASON_PATH_DEPENDENT_NON_VARIANT_SKIP
	elif raw_state is LiveState.MOVED_OUT:
		site_reason = _ledger_events.REASON_MOVED_UNCONDITIONAL
	else:
		site_reason = _ledger_events.REASON_NOT_DROP_NEEDING
	ledger_verdict_str = _verdict_to_str(verdict)
	payload = {
		"site": _ledger_events.SITE_SCOPE_DROP,
		"fn_name": func.name,
		"program_point": [block_name, idx],
		"local": local,
		"site_verdict": site_verdict,
		"site_reason": site_reason,
		"ledger_verdict": ledger_verdict_str,
		"raw_state": raw_state.value,
		"classification": "agree",
		"field_path": [],
	}
	sys.stderr.write("[drift:ownership_ledger] " + json.dumps(payload, sort_keys=True) + "\n")


def _verdict_to_str(v: DropVerdict) -> str:
	if v is DropVerdict.MUST_DROP:
		return _ledger_events.VERDICT_MUST_DROP
	if v is DropVerdict.MUST_NOT_DROP:
		return _ledger_events.VERDICT_MUST_NOT_DROP
	return "path_dependent"
