# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Destructible drop-decision authority (Milestone A extraction, 2026-07-20).

This module owns the DECISION logic that the legacy `string_arc` pass
used to carry inline for non-string destructible locals (its surviving
mutations live in `ownership_normalization`; the emissions in the
frozen-plan emitters):

  * `DropClassifier` — the type-level classification predicates
    (`type_needs_drop`, `is_destructible_tid`, `is_error_tid`,
    `is_nullsafe_drop`), including the cycle-guarded caches.
  * `classify_destructible_locals` — the per-function
    `destructible_locals` / `nullsafe_destructible_locals` split.
  * `site4_verdict` — the StoreLocal-into-destructible ledger verdict
    (drop-before-overwrite, site 4), with its two tripwires.
  * `compute_store_defs` / `compute_assigned_in` — the definite-assignment
    dataflow that feeds the Return-boundary drop set.
  * `compute_return_move_state` — the per-block moved-out / explicitly-
    dropped bookkeeping (moved-out intersection fixpoint + intra-block
    explicit-drop replay).  The planner is the sole production caller;
    the emitters consume the FROZEN PLAN rather than recompute here.
  * `flag_managed_at_return` — the drop-flag-managed membership at a
    Return boundary, via the canonical `drop_flags.is_flag_managed`.
  * `site3_return_decision` — the structured `Site3Decision` at a Return
    terminator (site 3): the ordered drop list PLUS the flag-managed /
    generic-skip / initialized facts observation needs (Phase D binding
    decision 3 — one authority result for planning AND reporting).

The `DropClassifier` predicates and the definite-assignment /
moved-out dataflow bodies were moved VERBATIM out of `string_arc.py`;
only captured closure variables were reparameterized into method/`self`
or function parameters.  `site3_return_decision`, by contrast, is a
BEHAVIOR-PRESERVING RECOMPOSITION of string_arc's former inline
skip/init decision — same inputs, same `sorted(destructible_locals)`
output, but reassembled as a standalone function rather than lifted
line-for-line.  This module is a CLOSED authority: it imports the
canonical `DropVerdict` / `compute_drop_policy` / `zero_storage_drop_safe`
/ `is_flag_managed` helpers directly rather than accepting them as
caller-injected policy.  The DECISIONS live here; the EMISSIONS live in
the frozen-plan emitters (`return_cleanup_emitter`, `overwrite_cleanup`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Set

from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable
from . import mir_nodes as M
from . import cfg as _cfg
from .ownership_ledger import DropVerdict
from .drop_policy_compute import compute_drop_policy, zero_storage_drop_safe
from .drop_flags import is_flag_managed


class DropClassifier:
	"""Type-level drop classification.

	Holds the `type_table`, the destructor-fn map, and the four caches /
	active-sets that back the cycle-guarded predicates.  Bodies are
	identical to the legacy string_arc pass's former inner closures.
	(The former `string_ty` state was dead — no predicate consulted it.)
	"""

	def __init__(self, type_table: TypeTable):
		self._tt = type_table
		self._type_needs_drop_cache: Dict[TypeId, bool] = {}
		# Cycle guard for type_needs_drop: tids whose by-value field closure is
		# still being computed up the call stack. A directly-recursive value type is
		# rejected earlier by validate_no_recursive_value_types, but malformed/legacy
		# package metadata could still present one here; this prevents a raw Python
		# RecursionError on the back-edge (the result cache is written only AFTER the
		# recursion returns, so it cannot break the cycle on its own).
		self._type_needs_drop_active: Set[TypeId] = set()
		self._dtor_fns = getattr(type_table, "destructor_fns", None) or {}
		self._nullsafe_drop_cache: Dict[TypeId, bool] = {}
		# Cycle guard for is_nullsafe_drop, mirroring _type_needs_drop_active: the
		# result cache is written only after recursion returns, so it cannot break a
		# self-loop in a malformed/legacy recursive value type on its own.
		self._nullsafe_drop_active: Set[TypeId] = set()

	def type_needs_drop(self, tid: TypeId) -> bool:
		cached = self._type_needs_drop_cache.get(tid)
		if cached is not None:
			return cached
		if tid in self._type_needs_drop_active:
			# Cycle back-edge: a directly-recursive value type should have been
			# rejected by validate_no_recursive_value_types; break the edge as
			# False (the correct least-fixpoint seed for the OR-of-fields below)
			# instead of recursing forever into a Python RecursionError. Do NOT
			# cache this provisional False — the outer in-progress call computes
			# and caches the real result.
			return False
		self._type_needs_drop_active.add(tid)
		try:
			td = self._tt.get(tid)
			if hasattr(self._tt, "is_destructible"):
				try:
					if bool(self._tt.is_destructible(tid)):
						self._type_needs_drop_cache[tid] = True
						return True
				except Exception:
					pass
			if td.kind is TypeKind.SCALAR:
				needs = td.name == "String"
				self._type_needs_drop_cache[tid] = needs
				return needs
			if td.kind is TypeKind.REF:
				self._type_needs_drop_cache[tid] = False
				return False
			if td.kind is TypeKind.ERROR:
				self._type_needs_drop_cache[tid] = True
				return True
			if td.kind is TypeKind.ARRAY and td.param_types:
				self._type_needs_drop_cache[tid] = True
				return True
			if td.kind is TypeKind.STRUCT:
				inst = self._tt.get_struct_instance(tid)
				if inst is not None:
					needs = any(self.type_needs_drop(fty) for fty in inst.field_types)
					self._type_needs_drop_cache[tid] = needs
					return needs
			if td.kind is TypeKind.VARIANT:
				inst = self._tt.get_variant_instance(tid)
				if inst is not None:
					needs = any(self.type_needs_drop(fty) for arm in inst.arms for fty in arm.field_types)
					self._type_needs_drop_cache[tid] = needs
					return needs
			if td.param_types:
				needs = any(self.type_needs_drop(pt) for pt in td.param_types)
				self._type_needs_drop_cache[tid] = needs
				return needs
			self._type_needs_drop_cache[tid] = False
			return False
		finally:
			self._type_needs_drop_active.discard(tid)

	# __borrow_tmp: Stage 1 (borrow_materialize.py) normally materialises rvalue
	# borrows into __tmp_borrow* locals that get scope-based drops.  This Stage 2
	# inclusion is a defensive fallback in case that assumption breaks.
	def is_destructible_tid(self, tid: TypeId | None) -> bool:
		if tid is None:
			return False
		return self.type_needs_drop(tid)

	def is_error_tid(self, tid: TypeId | None) -> bool:
		if tid is None:
			return False
		return self._tt.get(tid).kind is TypeKind.ERROR

	def is_nullsafe_drop(self, tid: TypeId) -> bool:
		cached = self._nullsafe_drop_cache.get(tid)
		if cached is not None:
			return cached
		if tid in self._nullsafe_drop_active:
			# Cycle back-edge: a directly-recursive value type should have been
			# rejected before stage 2. True is the identity for the `all()`
			# aggregation below, so the back-edge does not alter the real
			# (non-cyclic) verdict; recurse no further (avoids RecursionError).
			# Not cached — the outer in-progress call caches the real result.
			return True
		self._nullsafe_drop_active.add(tid)
		try:
			return self._is_nullsafe_drop_body(tid)
		finally:
			self._nullsafe_drop_active.discard(tid)

	def _is_nullsafe_drop_body(self, tid: TypeId) -> bool:
		if tid in self._dtor_fns:
			self._nullsafe_drop_cache[tid] = False
			return False
		td = self._tt.get(tid)
		if td.kind is TypeKind.SCALAR:
			self._nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.ARRAY:
			self._nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.ERROR:
			self._nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.INTERFACE:
			self._nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.STRUCT:
			inst = self._tt.get_struct_instance(tid)
			if inst is not None:
				safe = all(self.is_nullsafe_drop(fty) for fty in inst.field_types if self.type_needs_drop(fty))
				self._nullsafe_drop_cache[tid] = safe
				return safe
		if td.kind is TypeKind.VARIANT:
			inst = self._tt.get_variant_instance(tid)
			if inst is not None:
				safe = all(self.is_nullsafe_drop(fty) for arm in inst.arms for fty in arm.field_types if self.type_needs_drop(fty))
				self._nullsafe_drop_cache[tid] = safe
				return safe
		self._nullsafe_drop_cache[tid] = False
		return False


def classify_destructible_locals(
	func: M.MirFunc,
	clf: DropClassifier,
	*,
	local_types: Dict[str, TypeId],
	string_locals: Set[str],
	array_locals: Set[str],
) -> tuple[Set[str], Set[str]]:
	"""The `destructible_locals` / `nullsafe_destructible_locals` split.

	Body identical to string_arc's former inline comprehensions; the
	classifier closures are now `clf` methods.
	"""
	# __borrow_tmp: defensive fallback — see comment above array_locals.
	destructible_locals: Set[str] = {
		name
		for name in (list(func.params) + list(func.locals))
		if (not name.startswith("__")) or name.startswith("__match_binder_") or name.startswith("__borrow_tmp") or clf.is_error_tid(local_types.get(name))
		if name not in string_locals
		if name not in array_locals
		if clf.is_destructible_tid(local_types.get(name))
	}
	nullsafe_destructible_locals: Set[str] = {name for name in destructible_locals if clf.is_nullsafe_drop(local_types[name])}
	return destructible_locals, nullsafe_destructible_locals


def site4_verdict(
	ledger,
	*,
	fn_name: str,
	block_name: str,
	instr_idx: int,
	local: str,
	local_ty: "TypeId | None",
	type_table: TypeTable,
) -> "tuple[DropVerdict, bool]":
	"""Site-4 (drop-before-overwrite) ledger verdict.

	CLOSED AUTHORITY: the type-level `needs_drop` axis is computed HERE
	from `type_table + local_ty` via the canonical `compute_drop_policy`,
	so callers cannot obtain a different verdict by injecting a divergent
	axis. Returns `(verdict, needs_drop)` — the typed
	`DropVerdict.MUST_DROP`/`DropVerdict.MUST_NOT_DROP` member (never a
	bare string) plus the computed axis the caller may retain. Preserves
	the missing-ledger RuntimeError and the PathDependent proof-obligation
	tripwire with byte-identical messages.  The audit count lives in
	`overwrite_cleanup` (counted-only recorder), the observe-mode reporter
	check in `destructible_planner`'s site-4 arm, and the emission in
	`overwrite_cleanup`'s plan phase.
	"""
	needs_drop = (
		bool(compute_drop_policy(type_table, local_ty).needs_drop)
		if local_ty is not None
		else False
	)
	if ledger is None:
		raise RuntimeError(
			f"drop_before_overwrite invoked without an "
			f"attached ownership ledger (fn={fn_name}, "
			f"block={block_name}, local={local}); "
			f"Tier-1 site requires `func._ownership_ledger` "
			f"to be set by the driver before ownership normalization."
		)
	_verdict = ledger.verdict_at(
		(block_name, instr_idx),
		local,
		needs_drop=needs_drop,
	)
	if _verdict is DropVerdict.MUST_DROP:
		return DropVerdict.MUST_DROP, needs_drop
	elif _verdict is DropVerdict.MUST_NOT_DROP:
		return DropVerdict.MUST_NOT_DROP, needs_drop
	else:
		# PathDependent — proof-obligation tripwire.  The
		# observe re-run said the lattice never yields
		# MaybeUninit at drop_before_overwrite points.  If
		# it ever does, this raise signals the regression.
		raise RuntimeError(
			f"drop_before_overwrite: ledger returned "
			f"PathDependent at (fn={fn_name}, "
			f"block={block_name}, idx={instr_idx}, "
			f"local={local}).  Tier-1 promotion "
			f"retired the `initialized_destructibles` "
			f"fallback — if PathDependent is now reachable, "
			f"either tighten the lattice or restore a "
			f"flag-guarded path here before re-landing."
		)


def _block_succs(term) -> list[str]:
	# Central MIR CFG-successor contract (stage2/cfg.py).
	return _cfg.terminator_successors(term)


def _block_preds(func: M.MirFunc, block_order: list[str]) -> Dict[str, Set[str]]:
	preds: Dict[str, Set[str]] = {name: set() for name in block_order}
	for name in block_order:
		for succ in _block_succs(func.blocks[name].terminator):
			if succ in preds:
				preds[succ].add(name)
	return preds


def compute_store_defs(func: M.MirFunc) -> Dict[str, Set[str]]:
	"""Definite-assignment store set per block.

	Body identical to string_arc's former inline loop.
	"""
	block_order = sorted(func.blocks.keys())
	store_defs: Dict[str, Set[str]] = {}
	for name in block_order:
		stores: Set[str] = set()
		for instr in func.blocks[name].instructions:
			if isinstance(instr, M.StoreLocal):
				stores.add(instr.local)
			elif isinstance(instr, M.MoveFromRef):
				# MoveFromRef is a definite assignment to `local` — the
				# transferred bytes land in the local's storage.
				stores.add(instr.local)
		store_defs[name] = stores
	return store_defs


def compute_assigned_in(
	func: M.MirFunc,
	store_defs: Dict[str, Set[str]],
) -> Dict[str, Set[str]]:
	"""Definite-assignment `assigned_in` fixpoint.

	Body identical to string_arc's former inline fixpoint (the internal
	`assigned_out` map is a fixpoint temporary that never escaped the
	loop, so it stays local here).
	"""
	block_order = sorted(func.blocks.keys())
	preds = _block_preds(func, block_order)
	assigned_in: Dict[str, Set[str]] = {name: set() for name in block_order}
	assigned_out: Dict[str, Set[str]] = {name: set() for name in block_order}
	assigned_in[func.entry] = set(func.params)
	assigned_out[func.entry] = set(func.params) | store_defs.get(func.entry, set())
	changed = True
	while changed:
		changed = False
		for name in block_order:
			if name == func.entry:
				new_in = set(func.params)
			else:
				ps = preds.get(name, set())
				if not ps:
					new_in = set()
				else:
					it = iter(ps)
					new_in = set(assigned_out[next(it)])
					for p in it:
						new_in &= assigned_out[p]
			new_out = new_in | store_defs.get(name, set())
			if new_in != assigned_in[name] or new_out != assigned_out[name]:
				assigned_in[name] = new_in
				assigned_out[name] = new_out
				changed = True
	return assigned_in


@dataclass(frozen=True)
class ReturnMoveState:
	"""Immutable per-block move/drop bookkeeping (one instance per block).

	`moved_out` is the block-END value of the moved-out intersection
	fixpoint; `explicitly_dropped` is the intra-block explicit-drop replay
	result.  The planner's Return-arm consumers (`site3_return_decision`
	and `string_return_releases`) share the ONE instance the planner
	computed via `compute_return_move_state`, so they cannot drift apart.
	"""

	moved_out: frozenset
	explicitly_dropped: frozenset


def compute_return_move_state(
	func: M.MirFunc,
	*,
	destructible_locals: Set[str],
	string_ty: "TypeId | None",
) -> Dict[str, ReturnMoveState]:
	"""Per-block moved-out / explicitly-dropped bookkeeping, computed once.

	`moved_out[b]` — the moved-in/out INTERSECTION FIXPOINT (moved into the
	authority VERBATIM from string_arc's former inline loop): forward
	dataflow where `moved_in[b]` is the intersection over predecessors of
	`moved_out[pred]`, and `moved_out[b]` replays over `b`'s instructions
	(`discard` on `StoreLocal` / `MoveFromRef`, `add` on `MoveOut`).  This
	block-END value equals string_arc's former `moved_out_locals` at the
	Return terminator.

	`explicitly_dropped[b]` — the INTRA-BLOCK replay (seeded EMPTY per
	block, NOT cross-block): a `load_local_src` map tracks LoadLocal dests;
	StoreLocal / MoveFromRef `discard(local)`; a non-string `DropValue`
	whose value traces back through `load_local_src` to a destructible
	local `add`s that local.  String `DropValue`s never touch this set.
	"""
	block_order = sorted(func.blocks.keys())
	preds = _block_preds(func, block_order)

	# Moved-in/out intersection fixpoint (verbatim from string_arc).
	moved_in: Dict[str, Set[str]] = {name: set() for name in block_order}
	moved_out: Dict[str, Set[str]] = {name: set() for name in block_order}
	changed = True
	while changed:
		changed = False
		for name in block_order:
			if name == func.entry:
				new_in: Set[str] = set()
			else:
				ps = preds.get(name, set())
				if not ps:
					new_in = set()
				else:
					it = iter(ps)
					new_in = set(moved_out[next(it)])
					for p in it:
						new_in &= moved_out[p]
			cur = set(new_in)
			for instr in func.blocks[name].instructions:
				if isinstance(instr, M.StoreLocal):
					cur.discard(instr.local)
				elif isinstance(instr, M.MoveFromRef):
					# Fresh assignment; local is no longer "moved-out."
					cur.discard(instr.local)
				elif isinstance(instr, M.MoveOut):
					cur.add(instr.local)
			new_out = cur
			if new_in != moved_in[name] or new_out != moved_out[name]:
				moved_in[name] = new_in
				moved_out[name] = new_out
				changed = True

	# Intra-block explicit-drop replay (verbatim from string_arc's
	# per-block bookkeeping — seed empty, discard on (re)assignment, add on
	# a non-string DropValue of a loaded destructible local).
	result: Dict[str, ReturnMoveState] = {}
	for name in block_order:
		explicitly_dropped: Set[str] = set()
		load_local_src: Dict[str, str] = {}
		for instr in func.blocks[name].instructions:
			if isinstance(instr, M.LoadLocal):
				load_local_src[instr.dest] = instr.local
			elif isinstance(instr, M.StoreLocal):
				explicitly_dropped.discard(instr.local)
			elif isinstance(instr, M.MoveFromRef):
				explicitly_dropped.discard(instr.local)
			elif isinstance(instr, M.DropValue) and instr.ty != string_ty:
				src = load_local_src.get(instr.value)
				if src is not None and src in destructible_locals:
					explicitly_dropped.add(src)
		result[name] = ReturnMoveState(
			moved_out=frozenset(moved_out[name]),
			explicitly_dropped=frozenset(explicitly_dropped),
		)
	return result


def flag_managed_at_return(func: M.MirFunc, destructible_locals: Set[str]) -> Set[str]:
	"""Drop-flag-managed destructible membership at a Return boundary.

	Delegates to the canonical `drop_flags.is_flag_managed` (the same set
	comprehension string_arc used to build inline).
	"""
	return {dl for dl in destructible_locals if is_flag_managed(func, dl)}


@dataclass(frozen=True)
class Site3Decision:
	"""Structured, immutable site-3 Return-boundary decision (Phase D
	binding decision 3): the ONE authority result shared by the frozen
	plan payload AND the debug observe records — observation is never
	inferred back from the emitted-drop tuple (an absent local may mean a
	generic skip, flag ownership, or genuinely uninitialized storage, and
	those had different record behavior).

	  * `point` — the original Return coordinate
	    `(block, len(original_instructions))`;
	  * `drops` — the ORDERED emitted-drop locals (sorted destructible
	    order minus skips, intersected with initialized) — exactly the
	    former `site3_return_drops` list;
	  * `flag_managed` — destructibles folded out because drop-flag
	    plumbing owns their scope-exit cleanup (observe: MUST_NOT_DROP /
	    REASON_DROP_FLAG_OWNED; checked FIRST, even when also generically
	    skipped — string_arc's historical branch order);
	  * `generic_skips` — moved-out / explicitly-dropped / ledger
	    MUST_NOT_DROP skips BEFORE the flag fold (observe: SILENT);
	  * `initialized` — the FINAL initialized-at-return set (post
	    PATH_DEPENDENT zero-storage widening): a non-skipped local is
	    MUST_DROP iff member (== drops), else MUST_NOT_DROP /
	    REASON_NOT_DROP_NEEDING."""
	point: "tuple[str, int]"
	drops: "tuple[str, ...]"
	flag_managed: frozenset
	generic_skips: frozenset
	initialized: frozenset


def site3_return_decision(
	func: M.MirFunc,
	block: M.BasicBlock,
	*,
	ledger,
	type_table: TypeTable,
	destructible_locals: Set[str],
	local_types: Dict[str, TypeId],
	move_state: ReturnMoveState,
	assigned_in: Dict[str, Set[str]],
	store_defs: Dict[str, Set[str]],
	flag_managed: Set[str],
) -> "Site3Decision":
	"""Structured destructible decision at a Return terminator (site 3).

	`decision.drops` is the list of locals `_drop_all_destructibles` would
	have emitted a drop for, in the SAME `sorted(destructible_locals)`
	order the historical string_arc emission used.

	BEHAVIOR-PRESERVING RECOMPOSITION (not a verbatim body move): it
	reassembles the destructible-relevant skip/init decision from the same
	inputs:
	  * skip = moved_out ∪ explicitly_dropped ∪ (ledger MUST_NOT_DROP
	    destructibles) ∪ flag_managed;
	  * initialized = assigned_in[block] ∪ store_defs[block] ∪
	    store_defs[entry], widened by the PATH_DEPENDENT + zero-storage
	    fold.
	The `DropVerdict` / `compute_drop_policy` / `zero_storage_drop_safe`
	helpers are imported canonically at module level (closed authority).
	The String R3/R4 decisions live in `string_return_releases` — they
	never touch `destructible_locals` (strings are excluded), so they do
	not affect this set.  The `move_state` (moved_out /
	explicitly_dropped) is the shared immutable bookkeeping from
	`compute_return_move_state`.
	"""
	skip_cleanup_locals: Set[str] = set()
	skip_cleanup_locals |= move_state.moved_out
	skip_cleanup_locals |= move_state.explicitly_dropped
	# Phase 4 sub-step 1 — ledger consultation for returned-value source
	# suppression on DESTRUCTIBLES.  Every destructible local whose
	# `verdict_at` at the return cursor returns `MUST_NOT_DROP` joins
	# `skip_cleanup_locals`.
	if ledger is not None:
		_ledger_point = (block.name, len(block.instructions))
		for _local in destructible_locals:
			if _local in skip_cleanup_locals:
				continue
			_local_ty = local_types.get(_local)
			if _local_ty is None:
				continue
			_needs_drop_axis = bool(
				compute_drop_policy(type_table, _local_ty).needs_drop
			)
			_verdict = ledger.verdict_at(
				_ledger_point,
				_local,
				needs_drop=_needs_drop_axis,
			)
			if _verdict is DropVerdict.MUST_NOT_DROP:
				skip_cleanup_locals.add(_local)
	initialized_at_return = assigned_in.get(block.name, set()) | store_defs.get(block.name, set()) | store_defs.get(func.entry, set())
	# Phase 4 site-3 sub-step 3 — variant zero-tag widening, ledger-driven.
	# The widening consults skip_cleanup_locals BEFORE the flag-managed fold
	# below, exactly as the legacy string_arc did.
	if ledger is not None:
		_ledger_point = (block.name, len(block.instructions))
		for _local in destructible_locals:
			if _local in initialized_at_return or _local in skip_cleanup_locals:
				continue
			_local_ty = local_types.get(_local)
			if _local_ty is None or not zero_storage_drop_safe(_local_ty, type_table):
				continue
			_verdict = ledger.verdict_at(
				_ledger_point,
				_local,
				needs_drop=True,
			)
			if _verdict is DropVerdict.PATH_DEPENDENT:
				initialized_at_return.add(_local)
	# Phase 3B step 2 — flag-managed locals are folded out of site-3's
	# cleanup universe AFTER the widening (historical string_arc ordering).
	# `generic_skips` is snapshotted BEFORE the fold so observation can
	# distinguish flag ownership (recorded) from generic skips (silent).
	generic_skips = frozenset(skip_cleanup_locals)
	skip_cleanup_locals |= flag_managed
	drops = tuple(
		local
		for local in sorted(destructible_locals)
		if local not in skip_cleanup_locals
		if local in initialized_at_return
	)
	return Site3Decision(
		point=(block.name, len(block.instructions)),
		drops=drops,
		flag_managed=frozenset(flag_managed),
		generic_skips=generic_skips,
		initialized=frozenset(initialized_at_return),
	)


def string_return_source_skip(block: "M.BasicBlock", string_locals: Set[str]) -> "str | None":
	"""R4: the STRING storage local (if any) that backs the Return value, via
	the intra-block AssignSSA-chain + LoadLocal alias walk (legacy
	string_arc ~1219-1257).  This local is NOT released again at scope exit — the
	0.27.145 never-released-twice guarantee — because its +1 travels with the
	returned value (Return-as-move).  Reproduced over ORIGINAL block
	instructions (the pipeline only INSERTS AssignSSA/LoadLocal, never
	removes, so the walk resolves to the same source).  Returns the local or None.
	"""
	term = block.terminator
	val = getattr(term, "value", None) if term is not None else None
	if val is None:
		return None
	alias = val
	while True:
		moved = False
		for prev in reversed(block.instructions):
			if isinstance(prev, M.AssignSSA) and prev.dest == alias:
				alias = prev.src
				moved = True
				break
		if not moved:
			break
	for prev in reversed(block.instructions):
		if isinstance(prev, M.LoadLocal) and prev.dest == alias:
			if prev.local in string_locals:
				return prev.local
			return None
	return None


def string_return_releases(
	func: M.MirFunc,
	block: M.BasicBlock,
	*,
	ledger,
	type_table: TypeTable,
	string_locals: Set[str],
	string_ty: "TypeId | None",
	move_state: ReturnMoveState,
) -> "list[str]":
	"""R3/R4: the ORDERED String scope-exit releases at a Return terminator,
	= `sorted(string_locals)` minus the skip set, reproducing the legacy
	`_release_all_locals(skip=skip_cleanup_locals)` decision (string_arc
	~1418) at the ORIGINAL return coordinate.

	skip = move_state.moved_out ∪ move_state.explicitly_dropped
	       ∪ {R4 alias-derived returned-String source local}
	       ∪ {R3 ledger MUST_NOT_DROP strings}

	The destructible ledger consultation adds only destructibles (never
	strings), so it does not affect this set.  BEHAVIOR-PRESERVING
	RECOMPOSITION; the emitted release quad is authored by the unified Return
	authority, not here.
	"""
	skip: Set[str] = set()
	skip |= move_state.moved_out
	skip |= move_state.explicitly_dropped
	# R4 (added BEFORE the R3 elision, the historical string_arc ordering).
	r4 = string_return_source_skip(block, string_locals)
	if r4 is not None:
		skip.add(r4)
	# R3 elision: ledger MUST_NOT_DROP over sorted(string_locals) at the
	# ORIGINAL return point `(block, len(instructions))`.  Only when a ledger
	# is attached (no ledger → legacy, nothing elided).
	if ledger is not None:
		string_needs_drop = bool(compute_drop_policy(type_table, string_ty).needs_drop)
		point = (block.name, len(block.instructions))
		for sl in sorted(string_locals):
			if sl in skip:
				continue
			if ledger.verdict_at(point, sl, needs_drop=string_needs_drop) is DropVerdict.MUST_NOT_DROP:
				skip.add(sl)
	return [sl for sl in sorted(string_locals) if sl not in skip]


__all__ = [
	"DropClassifier",
	"classify_destructible_locals",
	"site4_verdict",
	"compute_store_defs",
	"compute_assigned_in",
	"ReturnMoveState",
	"compute_return_move_state",
	"flag_managed_at_return",
	"Site3Decision",
	"site3_return_decision",
	"string_return_source_skip",
	"string_return_releases",
]
