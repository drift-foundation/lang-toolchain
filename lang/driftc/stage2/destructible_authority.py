# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Destructible drop-decision authority (Milestone A extraction, 2026-07-20).

This module owns the DECISION logic that string_arc's `insert_string_arc`
used to carry inline for non-string destructible locals:

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
    explicit-drop replay). string_arc and the standalone planner each
    invoke this SAME function INDEPENDENTLY (they do not share one
    `ReturnMoveState` instance today); once production planning is wired,
    the emitters consume the FROZEN PLAN rather than recompute here.
  * `flag_managed_at_return` — the drop-flag-managed membership at a
    Return boundary, via the canonical `drop_flags.is_flag_managed`.
  * `site3_return_drops` — the ordered list of destructible locals a
    Return terminator must drop (site 3).

The `DropClassifier` predicates and the definite-assignment /
moved-out dataflow bodies were moved VERBATIM out of `string_arc.py`;
only captured closure variables were reparameterized into method/`self`
or function parameters.  `site3_return_drops`, by contrast, is a
BEHAVIOR-PRESERVING RECOMPOSITION of string_arc's former inline
skip/init decision — same inputs, same `sorted(destructible_locals)`
output, but reassembled as a standalone function rather than lifted
line-for-line.  This module is a CLOSED authority: it imports the
canonical `DropVerdict` / `compute_drop_policy` / `zero_storage_drop_safe`
/ `is_flag_managed` helpers directly rather than accepting them as
caller-injected policy.  string_arc DELEGATES to this module and keeps
every emission, audit note, MIR-object identity, and counter exactly
where it was.  The EMISSION stays in string_arc; only the DECISION
moved here.
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
	identical to string_arc's former inner closures.  (The former
	`string_ty` state was dead — no predicate consulted it; the string
	identity check `_is_string_tid` lives in string_arc.)
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
	tripwire with byte-identical messages; the audit note, reporter check,
	and emission stay in string_arc.
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
			f"to be set by the driver before `string_arc`."
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
	result.  WITHIN string_arc, its two consumers (the inline Return skip
	and `site3_return_drops`) share the ONE instance string_arc computed
	via `compute_return_move_state`, so they cannot drift apart. The
	standalone planner computes its OWN instance via the same function
	(the two modules do not share an instance across the process).
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


def site3_return_drops(
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
) -> list[str]:
	"""Ordered destructible-drop set at a Return terminator (site 3).

	Returns the list of locals `_drop_all_destructibles` would emit a drop
	for, in the SAME `sorted(destructible_locals)` order string_arc emits.

	BEHAVIOR-PRESERVING RECOMPOSITION (not a verbatim body move): it
	reassembles string_arc's destructible-relevant skip/init decision from
	the same inputs:
	  * skip = moved_out ∪ explicitly_dropped ∪ (ledger MUST_NOT_DROP
	    destructibles) ∪ flag_managed;
	  * initialized = assigned_in[block] ∪ store_defs[block] ∪
	    store_defs[entry], widened by the PATH_DEPENDENT + zero-storage
	    fold.
	The `DropVerdict` / `compute_drop_policy` / `zero_storage_drop_safe`
	helpers are imported canonically at module level (closed authority).
	The String-only alias-walk additions to `skip_cleanup_locals` and the
	string release-elision fold stay in string_arc — they never touch
	`destructible_locals` (strings are excluded), so they do not affect
	this set.  The `move_state` (moved_out / explicitly_dropped) is the
	shared immutable bookkeeping from `compute_return_move_state`.
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
	# below, exactly as string_arc did.
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
	# cleanup universe AFTER the widening (string_arc ordering).
	skip_cleanup_locals |= flag_managed
	return [
		local
		for local in sorted(destructible_locals)
		if local not in skip_cleanup_locals
		if local in initialized_at_return
	]


__all__ = [
	"DropClassifier",
	"classify_destructible_locals",
	"site4_verdict",
	"compute_store_defs",
	"compute_assigned_in",
	"ReturnMoveState",
	"compute_return_move_state",
	"flag_managed_at_return",
	"site3_return_drops",
]
