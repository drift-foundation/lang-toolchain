# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3C — runtime drop-flag PLANNING / INSTRUMENTATION for
path-dependent destructible locals.

Pipeline placement (Bug 2 architecture, 2026-05-15): BEFORE
`cleanup_authoring`, between `build_ledger` and the pre-cleanup
ledger rebuild.  The pass plans flags and inserts the bookkeeping
instrumentation; `cleanup_authoring` is the sole emitter of cleanup
drops (guarded or unguarded) and consults this pass's metadata to
decide which CleanupHooks need flag-guarded emission.

Gated on whether the function has any path-dependent destructible
local; when none (the common case per Task #5 triage), the pass is
a no-op.

What this pass does:

  - Identify path-dependent destructible locals via the ledger.
    Trigger fires when EITHER (a) the local is potentially live at
    a function exit (the original bucket-6 carrier — `return move
    L` on one path, fall-through on another), OR (b) the ledger
    reports non-variant `PathDependent` at any reachable
    `CleanupHook` for the local (Bug 2: conditional move inside a
    loop body where the cleanup hook is mid-function, not at
    Return).
  - Allocate a Bool flag local for each tracked local.
  - Init flag at function entry: `true` for params (live at entry),
    `false` for declared locals (uninitialized at entry).
  - After every user `StoreLocal(L, _)`: insert `StoreLocal(flag,
    true)`.
  - After every user `MoveOut(_, L, _)`: insert `StoreLocal(flag,
    false)`.
  - Attach `func._drop_flag_managed_locals` (set of source local
    names) and `func._drop_flag_for_local` (explicit `local →
    flag_local` mapping; collision-safe, used by
    `cleanup_authoring` to emit `LoadLocal(flag)` reliably without
    name reconstruction).

What this pass does NOT do (the Bug 2 architecture flip):

  - It does NOT emit cleanup drops.  The pre-flip Step 5 (function-
    exit flag-guarded MoveOut + DropValue at Return blocks) is
    retired; that work moved into `cleanup_authoring` at the
    CleanupHook positions that HIR→MIR already emits at every
    scope exit (including function exit).  Single emit point ≡
    single RAII timing rule.
  - It does NOT move drops earlier than their source-syntactic
    scope-exit point.
  - It does NOT touch locals that are not path-dependent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Dict, List, Set

from lang.driftc.core.types_core import TypeId, TypeTable
from . import mir_nodes as M
from .drop_policy_compute import compute_drop_policy
from .ledger_cache import mark_ledger_dirty
from .ownership_ledger import DropVerdict, LiveState, build_ledger

if TYPE_CHECKING:
	from .hir_to_mir import DropPolicy


def insert_drop_flags(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
	drop_policy: Callable[[TypeId], "DropPolicy"],
) -> "tuple[M.MirFunc, bool]":
	"""Rewrite `func` so every path-dependent destructible local has a
	runtime drop-flag whose state governs scope-exit cleanup.

	Returns `(func, mutated)` where `mutated` is `True` iff the pass
	actually inserted any instructions (and therefore shifted block
	instruction indices).  Callers that key cached MIR analyses by
	`(block, instr_idx)` (notably the `_ownership_ledger`) MUST rebuild
	those caches when `mutated` is `True` and may keep them when
	`mutated` is `False`.

	`func` is mutated in place when `mutated` is `True`.  Functions
	with no path-dependent destructible locals are returned unchanged
	with `mutated=False`.
	"""
	# Step 1: build the ledger; identify path-dependent destructible
	# locals.  `func.params` and `func.locals` may overlap (params
	# are typically also tracked as locals); dedupe before iterating
	# so we don't re-allocate a flag for the same name.
	ledger = build_ledger(func, drop_policy=drop_policy)
	all_locals: List[str] = []
	seen: Set[str] = set()
	for name in list(func.params) + list(func.locals):
		if name in seen:
			continue
		seen.add(name)
		all_locals.append(name)
	flag_for: Dict[str, str] = {}
	for name in all_locals:
		# Skip compiler-internal locals (HIR→MIR-generated `__` names
		# such as `__match_scrut_tmp*`, `__try_err*`, intermediate
		# diagnostic temps, etc.).  These have specialised handling in
		# downstream passes (`string_arc.destructible_locals` excludes
		# them via the same prefix rule with a small allowlist for
		# `__match_binder_*` / `__borrow_tmp`).  Adding flag plumbing
		# for them produces MIR that downstream passes do not expect
		# (e.g. address-taken-without-known-type errors in LLVM
		# codegen).  The bucket-6 carrier shapes are all user-named
		# locals — the pass need not touch internal temps to satisfy
		# the acceptance contract.
		if name.startswith("__"):
			continue
		ty = func.local_types.get(name)
		if ty is None:
			continue
		try:
			policy = drop_policy(ty)
		except Exception:
			continue
		if not policy.needs_drop:
			continue
		# Trigger criteria.  Criterion (1) is mandatory; EITHER (2a)
		# OR (2b) must also hold.
		#
		#   (1) The local has at least one USER `move L` expression
		#       (a MoveOut whose dest is consumed by something OTHER
		#       than an immediately-following DropValue).  Without a
		#       user move, the local is never PathDependent — every
		#       reachable cleanup site sees Live.
		#
		#   (2a) FUNCTION-EXIT carrier (the original bucket-6 class).
		#       The local is potentially live at the pre-terminator
		#       point of at least one Return / Unreachable block.
		#       Bug-6 shape: `if b { return move L; } return "fresh";`
		#       — the trailing return sees PathDependent for L.
		#
		#   (2b) CLEANUP-HOOK carrier (Bug 2, 2026-05-15).  The
		#       ledger reports non-variant `PathDependent` at some
		#       reachable `CleanupHook` for the local.  This covers
		#       the loop-iteration shape: `while ... { var w =
		#       arr.remove(0); if w.raw > 0 { out.push(move w); } }`
		#       — the end-of-iteration CleanupHook sees PathDependent
		#       for w (moved on one branch, not on the other), but
		#       w is loop-local and never reaches a Return block.
		#       Without (2b) such locals would not get flagged and
		#       `cleanup_authoring` would silently skip emission
		#       (`path_dependent_non_variant_skip` tripwire), leaving
		#       `string_arc.drop_before_overwrite` to crash on the
		#       next iteration.
		#
		# Together these criteria pick out every shape where
		# cleanup_authoring needs a runtime flag to decide whether
		# to drop on the non-move branch.  They exclude (a)
		# compiler-internal scope-drop MoveOuts on locals with no
		# user moves, and (b) unconditional user moves whose only
		# exit is the move point itself (e.g. `return move out;`).
		if not _has_user_moveout(func, name):
			continue
		if not (
			_is_potentially_live_at_some_exit(ledger, func, name)
			or _has_non_variant_path_dependent_at_cleanup_hook(
				ledger=ledger,
				func=func,
				type_table=type_table,
				drop_policy=drop_policy,
				local_name=name,
			)
		):
			continue
		flag_for[name] = _allocate_flag_name(name, all_locals + list(flag_for.values()))
	if not flag_for:
		return func, False
	# Step 2: declare flag locals on the function and seed their types.
	bool_ty = type_table.ensure_bool()
	for flag_name in flag_for.values():
		func.locals.append(flag_name)
		func.local_types[flag_name] = bool_ty
	# Allocate a fresh-temp counter that does not collide with any
	# existing value-id in the function (locals or SSA temps).
	used_ids: Set[str] = set(func.local_types.keys())
	for blk in func.blocks.values():
		for ins in blk.instructions:
			for attr in ("dest", "value", "local"):
				v = getattr(ins, attr, None)
				if isinstance(v, str):
					used_ids.add(v)
	temp_counter = [0]
	def _new_temp() -> str:
		while True:
			temp_counter[0] += 1
			name = f"__df{temp_counter[0]}"
			if name not in used_ids:
				used_ids.add(name)
				return name
	# Step 3: init flags at function entry.  Prepend to entry block.
	entry = func.blocks[func.entry]
	init_instrs: List[M.MInstr] = []
	param_set = set(func.params)
	for local_name, flag_name in flag_for.items():
		init_value = local_name in param_set
		const_dest = _new_temp()
		init_instrs.append(M.ConstBool(dest=const_dest, value=init_value))
		init_instrs.append(M.StoreLocal(local=flag_name, value=const_dest))
	entry.instructions = init_instrs + entry.instructions
	mark_ledger_dirty(func, "drop_flags.entry_prepend")
	# Step 4: walk every block; after each StoreLocal/MoveOut for a
	# tracked local, insert flag-set/clear.
	for blk in func.blocks.values():
		new_instrs: List[M.MInstr] = []
		for ins in blk.instructions:
			new_instrs.append(ins)
			# Skip the flag-init StoreLocals we just inserted in entry
			# (their target IS a flag local; another flag-set would loop
			# forever).
			tgt = getattr(ins, "local", None)
			if isinstance(ins, M.StoreLocal) and isinstance(tgt, str):
				if tgt in flag_for:
					flag_name = flag_for[tgt]
					const_dest = _new_temp()
					new_instrs.append(M.ConstBool(dest=const_dest, value=True))
					new_instrs.append(M.StoreLocal(local=flag_name, value=const_dest))
			elif isinstance(ins, M.MoveOut) and isinstance(tgt, str) and tgt in flag_for:
				flag_name = flag_for[tgt]
				const_dest = _new_temp()
				new_instrs.append(M.ConstBool(dest=const_dest, value=False))
				new_instrs.append(M.StoreLocal(local=flag_name, value=const_dest))
		blk.instructions = new_instrs
		mark_ledger_dirty(func, "drop_flags.insert_flag_set_clear")
	# Step 5 (REMOVED 2026-05-15, Bug 2 architecture flip): flag-
	# guarded drops at Return blocks have moved into
	# `cleanup_authoring.author_cleanup`.  The HIR→MIR pass already
	# emits a `M.CleanupHook` at every scope exit (including
	# function exit via `_emit_function_exit_cleanup_hook`), so
	# `cleanup_authoring` now owns the sole emission point for
	# cleanup drops — guarded or unguarded.  This pass is pure
	# planning: instrumentation + metadata only.
	#
	# Attach explicit metadata so cleanup_authoring can decide
	# emission and emit `LoadLocal(flag)` reliably:
	#
	#   `_drop_flag_managed_locals` — set of SOURCE local names
	#   this pass chose to flag.  Existing consumers (e.g.
	#   `string_arc_return`'s site 3) consult via
	#   `is_flag_managed(func, L)`.
	#
	#   `_drop_flag_for_local` — explicit `local → flag_local`
	#   mapping.  Required because `_allocate_flag_name` may emit
	#   `_<n>`-suffixed flag names on collision, so
	#   `flag_local_name_for(L)` is unsafe as a direct lookup.
	#
	# Name-parsing (`__drop_flag_<L>` prefix probe) is unsafe here:
	# when `_allocate_flag_name` resolves a collision by suffixing
	# `_1`, the resulting flag-local name is indistinguishable from
	# "the canonical flag for a local named `<L>_1`."  Explicit
	# metadata removes the ambiguity.
	existing_managed: Set[str] = getattr(func, "_drop_flag_managed_locals", None) or set()
	setattr(func, "_drop_flag_managed_locals", existing_managed | set(flag_for.keys()))
	existing_map: Dict[str, str] = getattr(func, "_drop_flag_for_local", None) or {}
	merged_map = dict(existing_map)
	merged_map.update(flag_for)
	setattr(func, "_drop_flag_for_local", merged_map)
	return func, True


def _has_non_variant_path_dependent_at_cleanup_hook(
	*,
	ledger,
	func: M.MirFunc,
	type_table: TypeTable,
	drop_policy: Callable[[TypeId], "DropPolicy"],
	local_name: str,
) -> bool:
	# Lazy import to break the drop_flags ↔ string_arc cycle
	# (string_arc imports `is_flag_managed` from drop_flags).
	from .string_arc import variant_zero_tag_drop_safe
	"""Bug 2 trigger criterion (2b).  True iff the ledger reports a
	non-variant `PathDependent` verdict for `local_name` at some
	reachable `M.CleanupHook` for which the local is a candidate.

	The variant zero-tag widening case is excluded: those PathDependent
	hooks already emit unguarded in cleanup_authoring (tag=0 destructor
	is a no-op on uninit paths).  Only non-variant PathDependent needs
	a runtime flag, because the struct destructor would crash on PHI-
	zero data if invoked unconditionally.
	"""
	for blk in func.blocks.values():
		for idx, ins in enumerate(blk.instructions):
			if not isinstance(ins, M.CleanupHook):
				continue
			for cand_local, cand_ty in ins.candidates:
				if cand_local != local_name:
					continue
				try:
					policy = drop_policy(cand_ty)
				except Exception:
					policy = None
				needs_drop_axis = bool(policy.needs_drop) if policy is not None else False
				try:
					verdict = ledger.verdict_at(
						(blk.name, idx),
						cand_local,
						needs_drop=needs_drop_axis,
					)
				except Exception:
					continue
				if verdict is DropVerdict.PATH_DEPENDENT:
					if not variant_zero_tag_drop_safe(cand_ty, type_table):
						return True
	return False


def flag_local_name_for(local_name: str) -> str:
	"""Canonical flag-local name for `local_name`.

	`_allocate_flag_name` may emit this base name OR a `_<n>`-suffixed
	variant when the base collides with an existing local in
	`func.locals`.  The read-side helper `is_flag_managed` does NOT
	reverse-parse either shape — name-parsing is unsafe under
	collision (`__drop_flag_x_1` is indistinguishable as the canonical
	flag for `x_1` vs the collision-suffixed flag for `x`).  Instead,
	`insert_drop_flags` attaches an explicit
	`func._drop_flag_managed_locals: set[str]` of SOURCE-local names
	at the end of the pass, and `is_flag_managed` reads that set.
	See the regression at
	`test_string_arc_return_swap.py::test_is_flag_managed_does_not_misattribute_collision_suffixed_flag`."""
	return f"__drop_flag_{local_name}"


def is_flag_managed(func: M.MirFunc, local_name: str) -> bool:
	"""True iff Phase 3C `insert_drop_flags` allocated a flag for
	`local_name` in this function.

	Reads the explicit `func._drop_flag_managed_locals` set attached
	by `insert_drop_flags`.  NAME-PARSING IS UNSAFE HERE: when
	`_allocate_flag_name` resolves a collision by suffixing `_N`,
	the resulting flag-local name collides with the canonical-flag
	name of a hypothetical source local named `<original>_N`, and a
	name-parsing helper cannot distinguish the two.  A regression
	for this shape lives at
	`test_string_arc_return_swap.py::test_is_flag_managed_does_not_misattribute_collision_suffixed_flag`.

	Returns False when the metadata set is missing (no-op pass run
	or function that had no flag-managed locals) — the pre-3C
	default assumption is "no local is flag-managed."

	Phase 3B consumers (notably `string_arc_return` / site 3) call
	this to decide "this local is 3C's responsibility — skip my own
	emission for it at scope-exit.\""""
	managed: Set[str] = getattr(func, "_drop_flag_managed_locals", None) or set()
	return local_name in managed


def _has_user_moveout(func: M.MirFunc, local_name: str) -> bool:
	"""True iff the function contains a `MoveOut(_, local_name, _)` that
	is NOT immediately a scope-drop emission.

	Cleanup-authored scope-drops follow a fixed shape: `MoveOut(t, L, ty)`
	immediately followed by `DropValue(t, ty)`.
	A user-side `move L` expression emits `MoveOut(t, L, ty)` whose `t`
	is consumed by *something other than* an immediately-following
	DropValue (a binder StoreLocal, a return value, etc.).

	This filter is what distinguishes the bucket-6 carrier shapes (real
	user moves on conditional paths) from compiler-internal scope-drop
	emissions on locals that simply have a destructible type (e.g.
	loop-body-internal `var chunk = io.buffer(...)` in
	`std.console::_write_all_stream`, where there is no user move and
	HIR→MIR's body-scope drop fires at end-of-iteration correctly
	without flag plumbing).

	Without this filter the pass would treat every destructible local
	with a body-scope drop as "needs a flag," producing flag-guarded
	drops at function-exit blocks for locals whose lifetime is
	loop-internal — which causes downstream codegen errors because
	the function-exit blocks have no prior storage context for those
	locals.
	"""
	for blk in func.blocks.values():
		instrs = blk.instructions
		for idx, ins in enumerate(instrs):
			if not isinstance(ins, M.MoveOut):
				continue
			if getattr(ins, "local", None) != local_name:
				continue
			move_dest = getattr(ins, "dest", None)
			# Look at the immediately-following instruction; if it's a
			# DropValue consuming this MoveOut's dest, this is the
			# scope-drop pattern — not a user move.
			if idx + 1 < len(instrs):
				nxt = instrs[idx + 1]
				if isinstance(nxt, M.DropValue) and getattr(nxt, "value", None) == move_dest:
					continue
			return True
	return False


def _is_potentially_live_at_some_exit(ledger, func: M.MirFunc, local_name: str) -> bool:
	"""True iff at the pre-terminator point of any **Return** block,
	the ledger reports the local's state as Live or MaybeUninit.

	Only `Return` terminators count as runtime-reachable exits.
	`Unreachable` terminators are statically dead code (e.g. match-
	dispatch chain fall-through past all real arms, post-throw stubs
	in nothrow contexts, etc.) — cleanup there is by definition
	never executed at runtime.  Treating them as exits would make the
	ledger's Live-or-MaybeUninit signal at unreachable blocks (which
	can arise from CFG join artifacts even when no real runtime path
	carries the local live there) trigger spurious flag plumbing,
	which then breaks downstream codegen for address-taken locals
	whose alloca isn't established along the spurious dead-block
	processing order.
	"""
	for blk_name, blk in func.blocks.items():
		if not isinstance(blk.terminator, M.Return):
			continue
		n_instrs = len(blk.instructions)
		if n_instrs == 0:
			state = ledger.block_in.get(blk_name, {}).get(local_name, LiveState.UNINIT)
		else:
			post = ledger.post_instr.get((blk_name, n_instrs - 1), {})
			state = post.get(local_name, LiveState.UNINIT)
		if state is LiveState.LIVE or state is LiveState.MAYBE_UNINIT:
			return True
	return False


def _allocate_flag_name(local_name: str, taken: List[str]) -> str:
	"""Generate a flag local name that does not collide with existing
	function locals or other allocated flag names.  Suffix-handling
	must stay in sync with `is_flag_managed` above."""
	base = flag_local_name_for(local_name)
	if base not in taken:
		return base
	i = 1
	while True:
		name = f"{base}_{i}"
		if name not in taken:
			return name
		i += 1


