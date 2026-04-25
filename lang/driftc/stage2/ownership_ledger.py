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
	# Phase 4 site-3 sub-step 2: destructor-method `self` is
	# implicitly consumed by the runtime at function exit.  Detect
	# the destructor by the same `func.fn_id.name` pattern site 3
	# uses today, so the lattice and the (now-retired) site-local
	# guard agree on which functions are destructors.  The
	# transition is applied at the end of every Return-terminator
	# block, AFTER the regular instruction loop, so mid-body
	# per-instruction snapshots still see `self` as `LIVE`.
	is_destructor = "std.core.Destructible::destroy" in getattr(func.fn_id, "name", "")
	destructor_self_local: Optional[str] = "self" if (is_destructor and "self" in tracked) else None
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
		# Destructor-method self consumption: applied at the end of
		# every Return-terminator block.  Lands on block_out AND on
		# the last per-instruction snapshot, so site 3's query at
		# `(block, len(instructions))` (which reads
		# `post_instr[len-1]` via `state_pre`) sees MOVED_OUT.
		# Mid-body snapshots (any earlier index) are unchanged —
		# the destructor body may freely use `self`.
		if (
			destructor_self_local is not None
			and isinstance(block.terminator, M.Return)
		):
			out_state[destructor_self_local] = LiveState.MOVED_OUT
			if per_instr:
				last_idx, last_snap = per_instr[-1]
				last_snap = dict(last_snap)
				last_snap[destructor_self_local] = LiveState.MOVED_OUT
				per_instr[-1] = (last_idx, last_snap)
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
	# Per-block chain-aware MovedOut detection state.  Together with
	# `field_addr_dests` these track the canonical
	#   `VariantGetFieldAddr → LoadRef → StoreLocal(L) → MoveOut(_, L)`
	# chain that constitutes a real per-field ownership transfer.  See
	# `_apply_field_state` docstring.
	loadref_field_origin: Dict[str, Tuple[str, str, int]] = {}
	local_field_origin: Dict[str, Tuple[str, str, int]] = {}
	return_consumed = _identify_return_consumed_loads(block, tracked)
	consumed_by_idx: Dict[int, List[str]] = {}
	for lidx, llocal in return_consumed:
		consumed_by_idx.setdefault(lidx, []).append(llocal)
	for idx, ins in enumerate(block.instructions):
		_apply(ins, state, tracked, zero_values)
		_apply_field_state(
			ins,
			field_state,
			state,
			tracked,
			addr_of_dest_to_local,
			field_addr_dests,
			loadref_field_origin,
			local_field_origin,
		)
		if idx in consumed_by_idx:
			# Phase 4: this LoadLocal feeds the block's Return
			# terminator (directly, via AssignSSA chain, or through
			# a composite constructor — `ConstructStruct` /
			# `ConstructVariant` / `ConstructResultOk` /
			# `ConstructIfaceValue` — that wraps the value).
			# Treat as a `MoveOut`-equivalent.  Transition lands AT
			# the LoadLocal index so any later cursor in the same
			# block (notably site 3's return-cleanup query, which
			# runs at `(block, len(instructions))`) reads
			# `MOVED_OUT` via `state_pre`.
			for llocal in consumed_by_idx[idx]:
				state[llocal] = LiveState.MOVED_OUT
				_clear_local_field_state(field_state, llocal)
		per_instr.append((idx, dict(state)))
		field_per_instr.append((idx, dict(field_state)))
	return state, per_instr, field_state, field_per_instr


def _identify_return_consumed_loads(
	block: M.BasicBlock,
	tracked: Set[str],
) -> List[Tuple[int, str]]:
	"""Return every `(loadlocal_index, source_local)` pair for
	`LoadLocal(_, X)` instructions whose dest transitively feeds the
	block's `Return` terminator.

	Three composition shapes are recognized:

	1. Direct: `Return(t)` where `t` alias-chains (via `AssignSSA`)
	   to a `LoadLocal(_, X)` in this block.
	2. Composite: `Return(t)` where `t` alias-chains to the dest of
	   a `ConstructStruct` / `ConstructVariant` / `ConstructResultOk`
	   / `ConstructIfaceValue`, and one or more of that constructor's
	   args further alias-chain to `LoadLocal(_, X_i)`.  Composite
	   constructors can nest (e.g., `Result::Ok(Variant::Ctor(x))`).
	3. Mixed: AssignSSA chains may appear at any level.

	Each candidate `(loadlocal_idx, source_local)` must satisfy the
	per-candidate external-use check: the LoadLocal's dest is
	consumed only by chain participants (AssignSSA or Construct*
	instructions on the chain) — any other consumer disqualifies
	that specific candidate, but does not poison sibling candidates
	in the same constructor args list.

	Single-block analysis only; cross-block alias propagation would
	require phi / predecessor reasoning and is deferred.
	"""
	term = getattr(block, "terminator", None)
	if not isinstance(term, M.Return):
		return []
	val = term.value
	if val is None:
		return []
	instrs = block.instructions
	# Producer index: ValueId → instruction index producing that dest.
	producer_idx: Dict[str, int] = {}
	for idx, ins in enumerate(instrs):
		dest = getattr(ins, "dest", None)
		if isinstance(dest, str):
			producer_idx[dest] = idx
	# Chain-consumer indices: instructions that are part of the
	# alias/compose chain from Return.value downward.  A candidate's
	# LoadLocal.dest is allowed to be read by these; anything else
	# is an external use.
	chain_consumer_indices: Set[int] = set()
	candidates: List[Tuple[int, str]] = []

	def _trace(start_alias: str) -> None:
		# Walk AssignSSA chain from start_alias.
		alias = start_alias
		while alias in producer_idx:
			pidx = producer_idx[alias]
			pins = instrs[pidx]
			if isinstance(pins, M.AssignSSA):
				chain_consumer_indices.add(pidx)
				alias = pins.src
				continue
			break
		# Examine the producer of the chain-endpoint alias.
		idx = producer_idx.get(alias)
		if idx is None:
			return  # opaque source (parameter, external value)
		ins = instrs[idx]
		if isinstance(ins, M.LoadLocal):
			if ins.local in tracked:
				candidates.append((idx, ins.local))
			return
		if isinstance(ins, M.ConstructStruct):
			chain_consumer_indices.add(idx)
			for arg in ins.args:
				_trace(arg)
			return
		if isinstance(ins, M.ConstructVariant):
			chain_consumer_indices.add(idx)
			for arg in ins.args:
				_trace(arg)
			return
		if isinstance(ins, M.ConstructResultOk):
			chain_consumer_indices.add(idx)
			if ins.value is not None:
				_trace(ins.value)
			return
		if isinstance(ins, M.ConstructIfaceValue):
			chain_consumer_indices.add(idx)
			_trace(ins.value)
			return
		# Other producer kinds (BinaryOp, ConstInt, etc.) — opaque.
		return

	_trace(val)
	if not candidates:
		return []
	# Per-candidate external-use check.
	valid: List[Tuple[int, str]] = []
	for cand_idx, cand_local in candidates:
		cand_dest = getattr(instrs[cand_idx], "dest", None)
		if not isinstance(cand_dest, str):
			continue
		external = False
		for j, other in enumerate(instrs):
			if j == cand_idx or j in chain_consumer_indices:
				continue
			for v in _iter_value_uses(other):
				if v == cand_dest:
					external = True
					break
			if external:
				break
		if not external:
			valid.append((cand_idx, cand_local))
	return valid


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
	if isinstance(ins, M.MoveFromRef):
		# Atomic ownership transfer into `local`: destination becomes
		# LIVE (owns the transferred value's stake).  The source-side
		# per-field MovedOut transition lives in `_apply_field_state`.
		if ins.local in tracked:
			state[ins.local] = LiveState.LIVE


def _apply_field_state(
	ins: M.MInstr,
	field_state: Dict[Tuple[str, FieldPath], LiveState],
	state: Dict[str, LiveState],
	tracked: Set[str],
	addr_of_dest_to_local: Dict[str, str],
	field_addr_dests: Dict[str, Tuple[str, str, int]],
	loadref_field_origin: Dict[str, Tuple[str, str, int]],
	local_field_origin: Dict[str, Tuple[str, str, int]],
) -> None:
	"""Per-field transfer functions (chain-aware MovedOut detection).

	Two MIR shapes generate per-field `MovedOut` transitions:

	1. **`VariantGetField(dest, variant=v_local, ctor=C, field_index=I, ...)`**
	   — by-value extraction of a variant field directly from a named
	   local.  Treated as an immediate field-MovedOut.  This shape
	   appears when HIRToMIR's binder loop has `arm_scrut_ptr is None`
	   (rare today; mostly the by-ref path is used) and when
	   `scrut_is_ref` doesn't apply.

	2. **`VariantGetFieldAddr → LoadRef → StoreLocal(L) → MoveOut(_, L)`**
	   — the canonical by-reference ownership-transfer chain emitted
	   by HIRToMIR's binder loop MOVE branch (`hir_to_mir.py:1633-1643`).
	   The transition fires AT the `MoveOut` step, only when the
	   complete chain is present.

	   The earlier conservative model marked the field MovedOut at the
	   `VariantGetFieldAddr` step, which over-reported for both read-
	   only borrows AND Copy-classified binders (which emit
	   `VariantGetFieldAddr → LoadRef → CopyValue → StoreLocal`, with
	   no `MoveOut` — the slot retains its +1, only the binder gets a
	   retained copy).  The over-report caused
	   `match_cleanup_authoring` to skip the per-field drop for the
	   slot, leaking the slot's refcount.  Pinned as a LANGUAGE_BUG
	   by `test_partial_move_copy_binder_string_slot_leak.py`.

	   Chain-tracking state (per-block, all reset at block entry):
	     - `field_addr_dests[fa] = (v_local, ctor, fidx)` —
	       `VariantGetFieldAddr(dest=fa)` provenance.  Records that
	       `fa` is the address of variant field `(v_local, ctor, fidx)`.
	     - `loadref_field_origin[loaded] = (v_local, ctor, fidx)` —
	       `LoadRef(dest=loaded, ptr=fa)` propagates provenance from
	       `fa` (when `fa in field_addr_dests`).
	     - `local_field_origin[L] = (v_local, ctor, fidx)` —
	       `StoreLocal(local=L, value=loaded)` propagates provenance
	       from `loaded` (when `loaded in loadref_field_origin`).  A
	       `StoreLocal` whose source is NOT in the chain pops any
	       prior origin for `L` — only fresh-from-the-chain stores
	       carry origin forward.
	     - On `MoveOut(_, local=L)`: if `L in local_field_origin`,
	       fire the field MovedOut transition for the recorded source
	       field, then clear `L`'s origin.  This is the only
	       `MovedOut` transition for shape 2.

	   `CopyValue` deliberately does NOT propagate origin — its dest
	   is an independent owned copy whose later StoreLocal/MoveOut
	   chain (the binder's scope-drop) doesn't transfer ownership out
	   of the variant slot.  The chain breaks at the CopyValue step
	   because CopyValue's dest is not in `loadref_field_origin`.

	Whole-local moves (`MoveOut(_, local, _)`) and re-stores
	(`StoreLocal(local, _)` with a non-tombstone source) reset ALL
	per-field state for `local`: a fresh value's fields start `Live`;
	a moved-out local's fields don't make sense as a per-field query
	(but defaulting to `MovedOut` is consistent with "the slot is
	gone")."""
	# Whole-local writes invalidate per-field state.  StoreLocal also
	# participates in the chain-aware MovedOut detection (shape 2): it
	# either propagates the LoadRef-of-VariantGetFieldAddr provenance
	# from `value` to `local`, or pops any stale provenance.
	if isinstance(ins, M.StoreLocal) and ins.local in tracked:
		_clear_local_field_state(field_state, ins.local)
		# Propagate (or break) the chain-aware origin for `local`.
		val = getattr(ins, "value", None)
		if isinstance(val, str) and val in loadref_field_origin:
			local_field_origin[ins.local] = loadref_field_origin[val]
		else:
			local_field_origin.pop(ins.local, None)
		return
	if isinstance(ins, M.MoveOut) and ins.local in tracked:
		# Chain-aware shape-2 MovedOut transition: if this local was
		# stored from a LoadRef of a variant-field address, mark the
		# source field MovedOut.  Consume the origin so a stale entry
		# can't fire a second transition.
		origin = local_field_origin.pop(ins.local, None)
		if origin is not None:
			v_local, ctor, fidx = origin
			if v_local in tracked:
				key = (v_local, ((ctor, fidx),))
				field_state[key] = LiveState.MOVED_OUT
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
	# Shape 2 step 1: VariantGetFieldAddr — record provenance only.
	# The MovedOut transition (if any) fires later at the chain's
	# MoveOut step.  Read-only borrows and Copy-classified binders
	# both reach this instruction; only the chain ending in MoveOut
	# transitions the field.
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
		if isinstance(dest, str):
			field_addr_dests[dest] = (v_local, ctor, fidx)
		return
	# Shape 2 step 2: LoadRef from a tracked field address — propagate
	# the source-field provenance to the loaded value.  Subsequent
	# `StoreLocal(L, loaded)` carries this forward to `L`; if instead
	# the loaded value flows into `CopyValue`, the chain naturally
	# breaks (CopyValue's dest is not in `loadref_field_origin`).
	if isinstance(ins, M.LoadRef):
		ptr = getattr(ins, "ptr", None)
		dest = getattr(ins, "dest", None)
		if isinstance(ptr, str) and isinstance(dest, str) and ptr in field_addr_dests:
			loadref_field_origin[dest] = field_addr_dests[ptr]
		return
	# Shape 3: `MoveFromRef(local=L, ptr=fa, inner_ty=T)` — atomic
	# ownership transfer.  When `fa` is a tracked variant-field
	# address (`field_addr_dests[fa]` resolves), the source field
	# transitions to `MovedOut` directly at this instruction.  This
	# parallels the legacy `LoadRef → StoreLocal → MoveOut(local)`
	# chain but uses the explicit `MoveFromRef` ownership primitive
	# emitted by `match_cleanup_authoring`.  See the `MoveFromRef`
	# MIR docstring for the contract.
	if isinstance(ins, M.MoveFromRef):
		ptr = getattr(ins, "ptr", None)
		local = getattr(ins, "local", None)
		if isinstance(ptr, str) and ptr in field_addr_dests:
			v_local, ctor, fidx = field_addr_dests[ptr]
			if v_local in tracked:
				key = (v_local, ((ctor, fidx),))
				field_state[key] = LiveState.MOVED_OUT
		# Destination local: clear any prior per-field state and any
		# stale chain origin (the local is freshly assigned).
		if isinstance(local, str) and local in tracked:
			_clear_local_field_state(field_state, local)
			local_field_origin.pop(local, None)
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
