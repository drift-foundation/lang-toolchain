# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Observational ownership ledger for MIR (Phase 3A of the ownership-drop ledger
rollout).

Builds a per-program-point map of live-state for every named local in a
`MirFunc`, and exposes a drop-verdict view that sites consult without needing
to know the raw provenance.

Phase 3A contract:
- No MIR emission changes.  The ledger is a pure reader.
- No `TypeTable` / `DropPolicy` bypasses.  The builder consumes a caller-
  supplied `drop_policy` callable and does not import policy internals.
- The ledger is built post-HIR-to-MIR, pre-`string_arc`.  Sites record or
  consult verdicts through the reporter module; this file has no knowledge
  of the sites themselves.

Raw state vs. drop verdict — deliberate separation.  Raw state preserves
provenance (was it moved out?  tombstoned?  never written?) for diagnostics
and for Phase 4 tombstone fusion.  Drop verdict collapses provenance into
the three-valued question the emission sites actually ask: MustDrop,
MustNotDrop, or PathDependent.  `MovedOut`, `Tombstoned`, and `Uninit` all
map to `MustNotDrop`; joining any pair of them does NOT produce
`PathDependent` — that is the signal to 3C only when `Live` meets a
non-`Live` state at a join.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple

from . import mir_nodes as M
from lang.driftc.core.types_core import TypeId

if TYPE_CHECKING:
	from .hir_to_mir import DropPolicy


ProgramPoint = Tuple[str, int]

# Phase 4 step 3a: per-field state lattice for match-cleanup work.
#
# `FieldPath` keys a projection from a tracked local down to a specific
# field (or, future, sub-field).  An empty tuple = whole-local state
# (already tracked elsewhere as the per-local map).  A single
# `(ctor_name, field_index)` pair = direct variant field.  Longer
# tuples (nested projections — e.g. `obj.outer.inner`) are reserved
# for future steps; the data structure is keyed by full-tuple to allow
# that extension without re-shaping.
#
# Step 3a populates field_post_instr from two MIR shapes:
#   - `VariantGetField` (by-value extraction; treated as a per-field
#     MovedOut), and
#   - `VariantGetFieldAddr` whose `variant_ref` traces back to an
#     `AddrOfLocal(_, v_local, _)` dest.  3a applies an IMMEDIATE
#     CONSERVATIVE MovedOut at the `VariantGetFieldAddr`, regardless
#     of what consumes the resulting dest.  This OVER-REPORTS for
#     read-only borrows and Copy-classified fields (which use the
#     same emission shape but `CopyValue` instead of `MoveOut`).
#     Step 3a deliberately does NOT do chain-aware downstream-
#     consumer detection; that tightening is reserved for 3b/3c if
#     telemetry shows the over-report matters in practice.
#
# Step 3b consumes the data structure for site-2 telemetry only —
# and because 3a over-reports, 3b's telemetry will be reading an
# upper-bound signal, not a tight one.  Plan 3b's filters
# accordingly.
# Step 3c upgrades site 2 to consume it for emission authority;
# that step likely REQUIRES tightening the VariantGetFieldAddr rule
# to chain-aware detection before it can be trusted for drop
# dispatch, not just telemetry.
FieldPath = Tuple[Tuple[str, int], ...]


class LiveState(Enum):
	"""
	Raw provenance state of a named local at a program point.

	Distinct from the drop verdict.  `MovedOut` and `Tombstoned` differ in
	history (one transferred ownership; the other wrote drop-safe bytes in
	place) but share the "non-owning at this point" drop verdict.
	"""
	UNINIT = "uninit"
	LIVE = "live"
	MOVED_OUT = "moved_out"
	TOMBSTONED = "tombstoned"
	MAYBE_UNINIT = "maybe_uninit"


class DropVerdict(Enum):
	"""
	Derived view of a local's state at a program point, answering the one
	question emission sites ask: should a drop run here?

	`MaybeUninit` raw state maps to `PathDependent`.  Every other state
	maps deterministically to `MustDrop` (Live with a drop-needing type) or
	`MustNotDrop`.
	"""
	MUST_DROP = "must_drop"
	MUST_NOT_DROP = "must_not_drop"
	PATH_DEPENDENT = "path_dependent"


def join(a: LiveState, b: LiveState) -> LiveState:
	"""
	Merge two predecessor states at a block join.

	Invariant: only `Live` meeting a non-`Live` state (or a `MaybeUninit`
	meeting a state that disagrees with it) produces `MaybeUninit`.
	`MovedOut ∪ Tombstoned`, `MovedOut ∪ Uninit`, `Tombstoned ∪ Uninit` are
	all drop-equivalent (non-owning on every path); the join preserves the
	representative that minimises false 3C reports.
	"""
	if a is b:
		return a
	if a is LiveState.MAYBE_UNINIT or b is LiveState.MAYBE_UNINIT:
		return LiveState.MAYBE_UNINIT
	if a is LiveState.LIVE or b is LiveState.LIVE:
		return LiveState.MAYBE_UNINIT
	pair = frozenset({a, b})
	if pair == frozenset({LiveState.MOVED_OUT, LiveState.TOMBSTONED}):
		return LiveState.MOVED_OUT
	if pair == frozenset({LiveState.MOVED_OUT, LiveState.UNINIT}):
		return LiveState.MOVED_OUT
	if pair == frozenset({LiveState.TOMBSTONED, LiveState.UNINIT}):
		return LiveState.TOMBSTONED
	raise AssertionError(f"unreachable join pair: {a} ∪ {b}")


def classify(state: LiveState, *, needs_drop: bool) -> DropVerdict:
	"""
	Map a raw state plus a type's drop-policy `needs_drop` axis to a
	verdict.

	`needs_drop` is the DropPolicy-funnelled answer to "does a scope-exit
	drop do any work for a value of this type" — POD types are False and
	collapse every verdict to `MustNotDrop` regardless of raw state.
	"""
	if not needs_drop:
		return DropVerdict.MUST_NOT_DROP
	if state is LiveState.MAYBE_UNINIT:
		return DropVerdict.PATH_DEPENDENT
	if state is LiveState.LIVE:
		return DropVerdict.MUST_DROP
	return DropVerdict.MUST_NOT_DROP


@dataclass
class LiveStateMap:
	"""
	Ledger result for one `MirFunc`.

	`block_in[block]` — state of each tracked local at block entry, after
	joining all predecessors.
	`block_out[block]` — state after the block's last instruction (before
	the terminator).
	`post_instr[(block, idx)]` — state after instruction at index `idx` in
	`block`.

	Sites usually want the state *before* a hypothetical emission.  Use
	`verdict_at(point, local, needs_drop=...)` — it handles the off-by-one
	convention (post-state of the previous instruction, or the block's
	in-state for idx=0).
	"""
	tracked_locals: Set[str]
	local_types: Dict[str, TypeId]
	block_in: Dict[str, Dict[str, LiveState]] = field(default_factory=dict)
	block_out: Dict[str, Dict[str, LiveState]] = field(default_factory=dict)
	post_instr: Dict[ProgramPoint, Dict[str, LiveState]] = field(default_factory=dict)
	# Per-field state — Phase 4 step 3a.  Builder populates from
	# field-extraction MIR shapes (see `_apply_field_state`).
	# Site consumers do NOT read this in step 3a (this is the
	# data-structure-only landing).  Step 3b wires site 2 telemetry to
	# read via `field_verdict_at`.  Step 3c upgrades to emission
	# authority.
	field_post_instr: Dict[ProgramPoint, Dict[Tuple[str, FieldPath], LiveState]] = field(default_factory=dict)
	field_block_in: Dict[str, Dict[Tuple[str, FieldPath], LiveState]] = field(default_factory=dict)

	def state_pre(self, point: ProgramPoint, local: str) -> LiveState:
		"""
		Raw state of `local` immediately before the instruction at `point`.

		For `idx == 0`, falls back to the block's in-state.  For a local
		the ledger does not track (SSA temp, unknown name), returns
		`LiveState.LIVE` conservatively — sites should not query untracked
		locals, but a defensive default keeps the reporter robust.
		"""
		block_name, idx = point
		if local not in self.tracked_locals:
			return LiveState.LIVE
		if idx == 0:
			return self.block_in.get(block_name, {}).get(local, LiveState.UNINIT)
		prev = self.post_instr.get((block_name, idx - 1))
		if prev is None:
			return LiveState.UNINIT
		return prev.get(local, LiveState.UNINIT)

	def state_post(self, point: ProgramPoint, local: str) -> LiveState:
		"""Raw state immediately after the instruction at `point`."""
		if local not in self.tracked_locals:
			return LiveState.LIVE
		post = self.post_instr.get(point)
		if post is None:
			return LiveState.UNINIT
		return post.get(local, LiveState.UNINIT)

	def verdict_at(
		self,
		point: ProgramPoint,
		local: str,
		*,
		needs_drop: bool,
	) -> DropVerdict:
		"""
		Drop verdict for `local` at the point where an emission site is
		about to decide.

		Uses pre-state (i.e. "what ownership looks like just before this
		site runs").  `needs_drop` is the DropPolicy.needs_drop axis for
		the local's type — callers already have a policy handle, so the
		ledger does not re-derive it to keep the Phase 1 funnel pure.
		"""
		return classify(self.state_pre(point, local), needs_drop=needs_drop)

	# -- Per-field state APIs (Phase 4 step 3a) ----------------------

	def field_state_pre(
		self,
		point: ProgramPoint,
		local: str,
		field_path: FieldPath,
	) -> LiveState:
		"""Per-field analogue of `state_pre`.

		`field_path` is a tuple of `(ctor_name, field_index)` pairs.
		For step-3a-supported queries (single-projection variant
		fields), `field_path` is a 1-element tuple.

		Default for any (local, field_path) the builder didn't see
		is `LiveState.LIVE` — defensive: an untouched field in a
		live local is still owned by it.  Sites should still range-
		check ctor / field index against the type table before
		calling; this default is a guard, not a contract."""
		block_name, idx = point
		key = (local, field_path)
		if idx == 0:
			return self.field_block_in.get(block_name, {}).get(key, LiveState.LIVE)
		prev = self.field_post_instr.get((block_name, idx - 1))
		if prev is None:
			return LiveState.LIVE
		return prev.get(key, LiveState.LIVE)

	def field_state_post(
		self,
		point: ProgramPoint,
		local: str,
		field_path: FieldPath,
	) -> LiveState:
		"""Per-field analogue of `state_post`."""
		key = (local, field_path)
		post = self.field_post_instr.get(point)
		if post is None:
			return LiveState.LIVE
		return post.get(key, LiveState.LIVE)

	def field_verdict_at(
		self,
		point: ProgramPoint,
		local: str,
		field_path: FieldPath,
		*,
		needs_drop: bool,
	) -> DropVerdict:
		"""Per-field analogue of `verdict_at`.

		Drives the "should this field's slot be dropped here" question
		that site 2's per-field cleanup loop will eventually consume
		(step 3c).  Step 3a exposes the API; step 3b wires telemetry;
		step 3c wires emission authority."""
		return classify(self.field_state_pre(point, local, field_path), needs_drop=needs_drop)


def build_ledger(
	func: M.MirFunc,
	*,
	drop_policy: Callable[[TypeId], "DropPolicy"],
) -> LiveStateMap:
	"""
	Worklist dataflow over `func`'s CFG, producing a `LiveStateMap`.

	The builder does not call into `TypeTable` or the raw policy queries
	directly; `drop_policy` is threaded through for future axis use and
	for symmetry with sites — but Phase 3A actually only needs the shape
	of the function's instructions, not per-type policy, because the raw
	state lattice is a pure function of MIR ops.
	"""
	del drop_policy
	tracked = set(func.params) | set(func.locals)
	entry_in: Dict[str, LiveState] = {}
	for name in tracked:
		entry_in[name] = LiveState.LIVE if name in func.params else LiveState.UNINIT
	ledger = LiveStateMap(tracked_locals=tracked, local_types=dict(func.local_types))
	ledger.block_in[func.entry] = entry_in
	# Phase 4 step 3a: pre-pass to map AddrOfLocal dest → underlying
	# local.  Required because `VariantGetFieldAddr.variant_ref` is a
	# value-id pointing at the address-of, not the local name itself.
	# Same scan pattern `string_arc.py` uses for `addr_taken_locals`.
	addr_of_dest_to_local: Dict[str, str] = {}
	for blk in func.blocks.values():
		for ins in blk.instructions:
			if isinstance(ins, M.AddrOfLocal):
				addr_of_dest_to_local[ins.dest] = ins.local
	preds = _compute_predecessors(func)
	worklist: List[str] = [func.entry]
	seen: Set[str] = {func.entry}
	while worklist:
		block_name = worklist.pop(0)
		block = func.blocks.get(block_name)
		if block is None:
			continue
		in_state = dict(ledger.block_in.get(block_name, {}))
		field_in_state = dict(ledger.field_block_in.get(block_name, {}))
		out_state, per_instr, field_out_state, field_per_instr = _walk_block(
			in_state, field_in_state, block, tracked, addr_of_dest_to_local
		)
		ledger.block_out[block_name] = out_state
		for idx, snap in per_instr:
			ledger.post_instr[(block_name, idx)] = snap
		for idx, fsnap in field_per_instr:
			ledger.field_post_instr[(block_name, idx)] = fsnap
		for succ in _successors(block.terminator):
			incoming = ledger.block_in.get(succ)
			if succ not in preds:
				preds[succ] = []
			if incoming is None:
				ledger.block_in[succ] = dict(out_state)
				ledger.field_block_in[succ] = dict(field_out_state)
				worklist.append(succ)
				seen.add(succ)
				continue
			merged = _join_dicts(incoming, out_state, tracked)
			# Field state join: simple intersection-style — a field
			# is `MovedOut` post-join iff every reaching predecessor
			# has it `MovedOut` (otherwise still `Live`, i.e. some
			# arm did not move it).  Conservative for step 3a: this
			# prevents false-positive MovedOut at joins.  Step 3c
			# may need a finer model (MaybeMovedOut analogue) once
			# emission authority is on the line.
			merged_field = _join_field_dicts(
				ledger.field_block_in.get(succ, {}),
				field_out_state,
			)
			changed = (merged != incoming) or (merged_field != ledger.field_block_in.get(succ, {}))
			if changed:
				ledger.block_in[succ] = merged
				ledger.field_block_in[succ] = merged_field
				if succ not in worklist:
					worklist.append(succ)
	return ledger


def _compute_predecessors(func: M.MirFunc) -> Dict[str, List[str]]:
	preds: Dict[str, List[str]] = {}
	for block in func.blocks.values():
		for succ in _successors(block.terminator):
			preds.setdefault(succ, []).append(block.name)
	return preds


def _successors(term: Optional[M.MTerminator]) -> List[str]:
	if term is None:
		return []
	if isinstance(term, M.Goto):
		return [term.target]
	if isinstance(term, M.IfTerminator):
		return [term.then_target, term.else_target]
	return []


def _join_dicts(
	a: Dict[str, LiveState],
	b: Dict[str, LiveState],
	tracked: Set[str],
) -> Dict[str, LiveState]:
	merged: Dict[str, LiveState] = {}
	for name in tracked:
		sa = a.get(name, LiveState.UNINIT)
		sb = b.get(name, LiveState.UNINIT)
		merged[name] = join(sa, sb)
	return merged


def _join_field_dicts(
	a: Dict[Tuple[str, FieldPath], LiveState],
	b: Dict[Tuple[str, FieldPath], LiveState],
) -> Dict[Tuple[str, FieldPath], LiveState]:
	"""Step 3a: per-field state join is an *intersection*-style rule
	for the MovedOut signal.  A field is reported `MovedOut` post-join
	only when **every** reaching predecessor has it `MovedOut`; if any
	predecessor still has it `Live`, the join's view is `Live`.

	Default for any key absent from either side is `LiveState.LIVE`
	(matches `field_state_pre`'s defensive default).  Step 3c may
	need a finer model (a `MaybeMovedOut` analogue parallel to the
	per-local `MaybeUninit`); step 3a uses the conservative answer
	that biases toward "still owned, drop required" — never silently
	suppressing a drop on partial-move arms."""
	keys = set(a.keys()) | set(b.keys())
	merged: Dict[Tuple[str, FieldPath], LiveState] = {}
	for k in keys:
		sa = a.get(k, LiveState.LIVE)
		sb = b.get(k, LiveState.LIVE)
		if sa is LiveState.MOVED_OUT and sb is LiveState.MOVED_OUT:
			merged[k] = LiveState.MOVED_OUT
		else:
			merged[k] = LiveState.LIVE
	return merged


def _walk_block(
	in_state: Dict[str, LiveState],
	field_in_state: Dict[Tuple[str, FieldPath], LiveState],
	block: M.BasicBlock,
	tracked: Set[str],
	addr_of_dest_to_local: Dict[str, str],
) -> Tuple[
	Dict[str, LiveState],
	List[Tuple[int, Dict[str, LiveState]]],
	Dict[Tuple[str, FieldPath], LiveState],
	List[Tuple[int, Dict[Tuple[str, FieldPath], LiveState]]],
]:
	"""
	Apply transfer functions sequentially; return out-state and per-instruction snapshots.

	Zero/tombstone producers are tracked within-block: a `ZeroValue` or
	`TombstoneValue` publishes its `dest` into a local set, and a following
	`StoreLocal` that consumes that dest transitions the target local to
	`TOMBSTONED` instead of `LIVE`.

	Phase 4 step 3a also tracks per-field state (`field_state`) from
	`VariantGetField` (by-value extraction) and `VariantGetFieldAddr`
	followed by the binder LoadRef/StoreLocal/MoveOut chain.  See
	`_apply_field_state` for the exact MIR-shape detection rules.

	Phase 4 (Return-as-move): a pre-scan identifies the index of a
	`LoadLocal(_, X)` whose dest transitively feeds the block's
	`Return` terminator (and is not consumed by any other instruction
	in the block).  At that index the ledger transitions X →
	`MOVED_OUT` as if a `MoveOut(_, X, _)` had been emitted.
	Transition lands AT the LoadLocal index so per-instruction
	snapshots at any cursor emitted afterward (notably site 1's
	scope-drop cursor, which runs AFTER `_lower_return_value`) read
	the consumption via `state_pre`, not just `block_out`.  See
	`_identify_return_consumed_load` for the trace + uniqueness rule.

	Scope note: this closes the modeled `LoadLocal+Return` gap and
	its unit-tested carrier shapes.  It does NOT eliminate the
	observed bucket-5 residual today — those records come from a
	different disagreement class (site 1 over-reports "moved" on
	paths where the local is still Live, due to HIR's path-
	insensitive `_moved_locals`).  The Return-as-move enhancement
	is prerequisite groundwork, not a direct observe-bucket
	reduction.  See `work/ownership-ledger/aggregate_triage.py`
	bucket-5 comment for the updated characterisation.
	"""
	state = dict(in_state)
	field_state: Dict[Tuple[str, FieldPath], LiveState] = dict(field_in_state)
	per_instr: List[Tuple[int, Dict[str, LiveState]]] = []
	field_per_instr: List[Tuple[int, Dict[Tuple[str, FieldPath], LiveState]]] = []
	zero_values: Set[str] = set()
	field_addr_dests: Dict[str, Tuple[str, str, int]] = {}
	return_consumed = _identify_return_consumed_load(block, tracked)
	for idx, ins in enumerate(block.instructions):
		_apply(ins, state, tracked, zero_values)
		_apply_field_state(
			ins,
			field_state,
			state,
			tracked,
			addr_of_dest_to_local,
			field_addr_dests,
		)
		if return_consumed is not None and return_consumed[0] == idx:
			# Phase 4: this LoadLocal feeds the block's Return
			# terminator and has no other consumers — treat as a
			# `MoveOut`-equivalent.  Transition lands AT the
			# LoadLocal index so any later cursor in the same block
			# (e.g. site 1's scope-drop emitted after the return-
			# value lowering) reads `MOVED_OUT` via `state_pre`.
			state[return_consumed[1]] = LiveState.MOVED_OUT
			_clear_local_field_state(field_state, return_consumed[1])
		per_instr.append((idx, dict(state)))
		field_per_instr.append((idx, dict(field_state)))
	return state, per_instr, field_state, field_per_instr


def _identify_return_consumed_load(
	block: M.BasicBlock,
	tracked: Set[str],
) -> Optional[Tuple[int, str]]:
	"""Return `(loadlocal_index, source_local)` if the block's
	`Return` terminator consumes its operand from a `LoadLocal(_, X)`
	in this block (transitively through `AssignSSA` chains) AND
	nothing outside the alias chain reads any value in the chain.
	Otherwise `None`.

	Single-block analysis only — alias propagation across blocks
	would need phi/predecessor reasoning; deferred until a real
	carrier shape requires it.

	The "no external use" rule is what enforces K's "non-return
	uses must not count as transfer."  A `LoadLocal` whose dest is
	stored into another local, copied, or otherwise read outside the
	chain is left alone.
	"""
	term = getattr(block, "terminator", None)
	if not isinstance(term, M.Return):
		return None
	val = term.value
	if val is None:
		return None
	chain_aliases: Set[str] = {val}
	alias = val
	while True:
		moved = False
		for prev in reversed(block.instructions):
			if isinstance(prev, M.AssignSSA) and prev.dest == alias:
				alias = prev.src
				chain_aliases.add(alias)
				moved = True
				break
		if not moved:
			break
	loadlocal_idx: Optional[int] = None
	source_local: Optional[str] = None
	for idx in range(len(block.instructions) - 1, -1, -1):
		ins = block.instructions[idx]
		if isinstance(ins, M.LoadLocal) and ins.dest == alias:
			if ins.local in tracked:
				loadlocal_idx = idx
				source_local = ins.local
			break
	if loadlocal_idx is None or source_local is None:
		return None
	# External-use check: any non-AssignSSA-on-the-chain instruction
	# (other than the LoadLocal itself) that reads any alias in the
	# chain disqualifies the consumption.
	for j, other in enumerate(block.instructions):
		if j == loadlocal_idx:
			continue
		if isinstance(other, M.AssignSSA) and other.dest in chain_aliases:
			# Chain link — its read of `src` is part of the chain.
			continue
		for v in _iter_value_uses(other):
			if v in chain_aliases:
				return None
	return (loadlocal_idx, source_local)


def _iter_value_uses(ins: M.MInstr) -> List[str]:
	"""Yield the `ValueId`s an instruction reads.  Excludes outputs
	(`dest`) and non-value identifiers (`local`, block-name targets,
	type ids — those are int).  Used by the Return-as-move
	external-use check, which only cares whether a particular SSA
	value-id appears as an *input* to any instruction in the block.
	"""
	import dataclasses
	uses: List[str] = []
	if not dataclasses.is_dataclass(ins):
		return uses
	for f in dataclasses.fields(ins):
		if f.name in ("dest", "local", "name", "ty", "field_ty",
				"variant_ty", "inner_ty", "ctor", "field_index",
				"is_mut", "fn_id", "method_name", "kind",
				"then_target", "else_target", "target", "ordinal",
				"span", "loc"):
			continue
		val = getattr(ins, f.name, None)
		if isinstance(val, str):
			uses.append(val)
		elif isinstance(val, list):
			for sub in val:
				if isinstance(sub, str):
					uses.append(sub)
	return uses


def _apply(
	ins: M.MInstr,
	state: Dict[str, LiveState],
	tracked: Set[str],
	zero_values: Set[str],
) -> None:
	if isinstance(ins, (M.ZeroValue, M.TombstoneValue)):
		zero_values.add(ins.dest)
		return
	if isinstance(ins, M.StoreLocal):
		if ins.local in tracked:
			if ins.value in zero_values:
				state[ins.local] = LiveState.TOMBSTONED
			else:
				state[ins.local] = LiveState.LIVE
		return
	if isinstance(ins, M.MoveOut):
		if ins.local in tracked:
			state[ins.local] = LiveState.MOVED_OUT


def _apply_field_state(
	ins: M.MInstr,
	field_state: Dict[Tuple[str, FieldPath], LiveState],
	state: Dict[str, LiveState],
	tracked: Set[str],
	addr_of_dest_to_local: Dict[str, str],
	field_addr_dests: Dict[str, Tuple[str, str, int]],
) -> None:
	"""Phase 4 step 3a per-field transfer functions.

	Two MIR shapes generate per-field state transitions:

	1. **`VariantGetField(dest, variant=v_local, ctor=C, field_index=I, ...)`**
	   — by-value extraction of a variant field directly from a named
	   local.  Treated as an immediate field-MovedOut.  This shape
	   appears when HIRToMIR's binder loop has `arm_scrut_ptr is None`
	   (rare today; mostly the by-ref path is used) and when
	   `scrut_is_ref` doesn't apply.

	2. **`VariantGetFieldAddr(dest, variant_ref=ref, ctor=C, field_index=I, ...)`**
	   followed within the same block by a downstream consumer that
	   transfers ownership.  Step 3a uses a CONSERVATIVE detection:
	   any `VariantGetFieldAddr` whose `variant_ref` traces back to
	   an `AddrOfLocal(_, v_local, _)` marks the field as
	   `MovedOut`.  This over-reports for read-only borrows and
	   Copy-classified fields (which use the same emission shape but
	   then `CopyValue` instead of `MoveOut`); a tighter model is a
	   step-3b/3c follow-up.  For step-3a's "data-structure exists"
	   landing the over-report is a known limitation pinned by the
	   tests in `test_ownership_ledger_field_state.py`.

	   `field_addr_dests` records the `(local, ctor, field_idx)` for
	   each VariantGetFieldAddr's dest.  **SCAFFOLDING ONLY in step
	   3a — populated but not consumed.**  Reserved for a future step
	   (likely 3c) that wants to upgrade the current
	   "mark-on-AddrOfLocal-provenance" rule to a chain-aware
	   "mark-on-confirmed-downstream-move" detection: scan forward
	   from each field_addr dest, look for `LoadRef → StoreLocal(_,
	   loaded) → MoveOut(_, dest, _)` or equivalent, and only then
	   transition to MovedOut.  Step 3a does not do this; the map
	   exists so the change is a local edit when the time comes.

	Whole-local moves (`MoveOut(_, local, _)`) and re-stores
	(`StoreLocal(local, _)` with a non-tombstone source) reset ALL
	per-field state for `local`: a fresh value's fields start `Live`;
	a moved-out local's fields don't make sense as a per-field query
	(but defaulting to `MovedOut` is consistent with "the slot is
	gone")."""
	# Whole-local writes invalidate per-field state.
	if isinstance(ins, M.StoreLocal) and ins.local in tracked:
		_clear_local_field_state(field_state, ins.local)
		return
	if isinstance(ins, M.MoveOut) and ins.local in tracked:
		_clear_local_field_state(field_state, ins.local)
		return
	# Shape 1: VariantGetField by-value extraction.
	if isinstance(ins, M.VariantGetField):
		v_local = getattr(ins, "variant", None)
		if isinstance(v_local, str) and v_local in tracked:
			ctor = getattr(ins, "ctor", None)
			fidx = getattr(ins, "field_index", None)
			if isinstance(ctor, str) and isinstance(fidx, int):
				key = (v_local, ((ctor, fidx),))
				field_state[key] = LiveState.MOVED_OUT
		return
	# Shape 2: VariantGetFieldAddr + (assumed) downstream move.
	if isinstance(ins, M.VariantGetFieldAddr):
		ref = getattr(ins, "variant_ref", None)
		ctor = getattr(ins, "ctor", None)
		fidx = getattr(ins, "field_index", None)
		dest = getattr(ins, "dest", None)
		if not (isinstance(ref, str) and isinstance(ctor, str) and isinstance(fidx, int)):
			return
		v_local = addr_of_dest_to_local.get(ref)
		if v_local is None or v_local not in tracked:
			return
		key = (v_local, ((ctor, fidx),))
		# Conservative: mark MovedOut.  See docstring for rationale
		# and limitations.
		field_state[key] = LiveState.MOVED_OUT
		if isinstance(dest, str):
			field_addr_dests[dest] = (v_local, ctor, fidx)
		return


def _clear_local_field_state(
	field_state: Dict[Tuple[str, FieldPath], LiveState],
	local: str,
) -> None:
	"""Reset all (local, field_path) entries when the whole local is
	written or moved.  A new value's fields are `Live` from scratch;
	a moved-out local's fields collapse to "moved with the local."""
	stale = [k for k in field_state if k[0] == local]
	for k in stale:
		del field_state[k]
		return
