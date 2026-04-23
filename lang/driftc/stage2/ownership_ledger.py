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
	preds = _compute_predecessors(func)
	worklist: List[str] = [func.entry]
	seen: Set[str] = {func.entry}
	while worklist:
		block_name = worklist.pop(0)
		block = func.blocks.get(block_name)
		if block is None:
			continue
		in_state = dict(ledger.block_in.get(block_name, {}))
		out_state, per_instr = _walk_block(in_state, block, tracked)
		ledger.block_out[block_name] = out_state
		for idx, snap in per_instr:
			ledger.post_instr[(block_name, idx)] = snap
		for succ in _successors(block.terminator):
			incoming = ledger.block_in.get(succ)
			if succ not in preds:
				preds[succ] = []
			if incoming is None:
				ledger.block_in[succ] = dict(out_state)
				worklist.append(succ)
				seen.add(succ)
				continue
			merged = _join_dicts(incoming, out_state, tracked)
			if merged != incoming:
				ledger.block_in[succ] = merged
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


def _walk_block(
	in_state: Dict[str, LiveState],
	block: M.BasicBlock,
	tracked: Set[str],
) -> Tuple[Dict[str, LiveState], List[Tuple[int, Dict[str, LiveState]]]]:
	"""
	Apply transfer functions sequentially; return out-state and per-instruction snapshots.

	Zero/tombstone producers are tracked within-block: a `ZeroValue` or
	`TombstoneValue` publishes its `dest` into a local set, and a following
	`StoreLocal` that consumes that dest transitions the target local to
	`TOMBSTONED` instead of `LIVE`.
	"""
	state = dict(in_state)
	per_instr: List[Tuple[int, Dict[str, LiveState]]] = []
	zero_values: Set[str] = set()
	for idx, ins in enumerate(block.instructions):
		_apply(ins, state, tracked, zero_values)
		per_instr.append((idx, dict(state)))
	return state, per_instr


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
		return
