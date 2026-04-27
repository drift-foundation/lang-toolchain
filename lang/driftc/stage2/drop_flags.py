# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3C — runtime drop-flag insertion for path-dependent destructible
locals.

Pipeline placement: between HIR→MIR and `string_arc`.  Gated on whether
the function has any path-dependent destructible local; when none (the
common case per Task #5 triage), the pass is a no-op.

Contract: see `work/ownership-ledger/3c-design.md` "Implementation
strategy: runtime drop flags."  Summary:

  - Identify path-dependent destructible locals via the 3A ledger
    (raw state `MaybeUninit` at any program point AND
    `DropPolicy.needs_drop = True`).
  - Allocate a Bool flag local for each.
  - Init flag at function entry: `true` for params (live at entry),
    `false` for declared locals (uninitialized at entry).
  - After every `StoreLocal(L, _)`: insert `StoreLocal(flag, true)`.
  - After every `MoveOut(_, L, _)`: insert `StoreLocal(flag, false)`.
  - At every Return / Unreachable terminator block, before the
    terminator: insert a flag-guarded drop sequence
    `if flag { MoveOut(t, L); DropValue(t); }` — IfTerminator on the
    flag plus a drop-block plus a join-block.

The pass DOES NOT move drops earlier than their source-syntactic
scope-exit point.  It does NOT touch locals that are not path-
dependent.  It is uniform: every path-dependent destructible local
gets the same plumbing; no terminating-arm or same-arm-as-scope-exit
optimizations.

For correctness on the bucket-6 carrier shapes:

  Shape 1 — `if b { return move s; } return "fresh";`
    The lattice's per-instruction state at the trailing return is
    PathDependent (Live on b=false, MovedOut on b=true), and the
    cleanup-authoring path skips emission for non-variant
    PathDependent.  This pass inserts a flag-guarded drop at that
    return: on b=false the flag is true (no MoveOut on this path)
    → drop fires; on b=true the return inside the if executes
    before reaching the trailing return, so the inserted drop is
    unreachable.

  Shape 2 — `if b { val t = move s; } return "fresh";`
    Same poisoning; trailing return skips drop.  This pass inserts
    a flag-guarded drop at the trailing return: on b=true the move
    cleared the flag → drop skipped; on b=false the flag stayed
    true → drop fires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Dict, List, Set

from lang.driftc.core.types_core import TypeId, TypeTable
from . import mir_nodes as M
from .ownership_ledger import LiveState, build_ledger

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
		# Trigger criteria (BOTH must hold):
		#
		#   (1) The local has at least one USER `move L` expression
		#       (a MoveOut whose dest is consumed by something OTHER
		#       than an immediately-following DropValue).  This is
		#       the direct cause of the bucket-6 LANGUAGE_BUG class:
		#       a user move on one path makes the trailing scope-exit's
		#       lattice state PathDependent (Live on the no-move path,
		#       MovedOut on the move path), and cleanup-authoring's
		#       non-variant PathDependent skip means no drop fires
		#       on the no-move path without this pass.
		#
		#   (2) The local is potentially live (Live or MaybeUninit
		#       state) at the pre-terminator point of at least one
		#       Return / Unreachable block.  If at every exit the
		#       ledger proves the local is MovedOut / Tombstoned /
		#       Uninit, the move was unconditional and no flag is
		#       needed — the existing scope-drop emission is correct.
		#
		# Together these criteria pick out exactly the bucket-6
		# carrier shapes (user-conditional moves where some exit
		# still owns the value) and exclude (a) compiler-internal
		# scope-drop MoveOuts on locals with no user moves, and
		# (b) unconditional user moves whose only exit is the move
		# point itself (e.g. `return move out;` in `_hash_sha256`).
		# Without (b), the pass would emit flag-guarded drops at
		# Return blocks for every destructible local with a user
		# move — wasteful and triggers downstream codegen errors
		# on address-taken locals.
		if not _has_user_moveout(func, name):
			continue
		if not _is_potentially_live_at_some_exit(ledger, func, name):
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
	# Step 5: insert flag-guarded drops at every Return / Unreachable
	# terminator block.  For each tracked local L that does NOT already
	# have an existing drop emission in this block, split the block
	# and emit:
	#
	#     ... pre-existing instructions ...
	#     load flag → t_flag
	#     if t_flag:
	#         drop_block:
	#             MoveOut(t, L)
	#             DropValue(t)
	#             goto join_block
	#         else:
	#             goto join_block
	#     join_block:
	#         <original terminator>
	#
	# We process locals in deterministic order so the resulting block
	# graph is reproducible.
	# Insert flag-guarded drops only at Return terminators.  Unreachable
	# terminators are statically dead — cleanup there is by definition
	# never executed and would only add wasted MIR + downstream codegen
	# pressure on locals appearing in spurious Live states at those
	# blocks (see `_is_potentially_live_at_some_exit` rationale).
	exit_blocks = [
		(name, blk)
		for name, blk in list(func.blocks.items())
		if isinstance(blk.terminator, M.Return)
	]
	for orig_block_name, exit_block in exit_blocks:
		_insert_flag_guarded_drops(
			func=func,
			block=exit_block,
			flag_for=flag_for,
			new_temp=_new_temp,
		)
	# Attach explicit metadata listing the source locals this pass
	# chose to flag.  Phase 3B consumers (e.g. `string_arc_return` /
	# site 3) consult this set via `is_flag_managed(func, L)` to
	# decide "this local is 3C's responsibility — skip my own
	# emission."  Name-parsing (`__drop_flag_<L>` prefix probe) is
	# unsafe here: when `_allocate_flag_name` resolves a collision by
	# suffixing `_1`, the resulting flag-local name is
	# indistinguishable from "the canonical flag for a local named
	# `<L>_1`."  Explicit metadata removes the ambiguity.
	existing: Set[str] = getattr(func, "_drop_flag_managed_locals", None) or set()
	setattr(func, "_drop_flag_managed_locals", existing | set(flag_for.keys()))
	return func, True


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


def _has_maybe_uninit(ledger, local_name: str) -> bool:
	for state_dict in ledger.post_instr.values():
		if state_dict.get(local_name) is LiveState.MAYBE_UNINIT:
			return True
	for state_dict in ledger.block_in.values():
		if state_dict.get(local_name) is LiveState.MAYBE_UNINIT:
			return True
	for state_dict in ledger.block_out.values():
		if state_dict.get(local_name) is LiveState.MAYBE_UNINIT:
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


def _block_already_drops(block: M.BasicBlock, local_name: str) -> bool:
	"""Detect whether `block` already contains a `MoveOut(t, local) +
	DropValue(t)` pair.  HIR→MIR emits this pair at scope-drop sites;
	if it's already there for `local_name`, this pass must NOT add
	another flag-guarded drop or it would double-drop."""
	moveout_dests: Set[str] = set()
	for ins in block.instructions:
		if isinstance(ins, M.MoveOut) and getattr(ins, "local", None) == local_name:
			dest = getattr(ins, "dest", None)
			if isinstance(dest, str):
				moveout_dests.add(dest)
		elif isinstance(ins, M.DropValue) and getattr(ins, "value", None) in moveout_dests:
			return True
	return False


def _insert_flag_guarded_drops(
	*,
	func: M.MirFunc,
	block: M.BasicBlock,
	flag_for: Dict[str, str],
	new_temp: Callable[[], str],
) -> None:
	"""Wrap `block`'s terminator with flag-guarded drop sequences for
	every tracked local that doesn't already have a drop emission in
	this block.  Mutates `func.blocks` to add new drop/join blocks."""
	tracked = [name for name in sorted(flag_for) if not _block_already_drops(block, name)]
	if not tracked:
		return
	original_term = block.terminator
	# Process locals in reverse so the first-tracked local ends up
	# being the OUTERMOST guard (executed first); later locals nest
	# inside.  Equivalent to a left-to-right sequence of guards
	# leading into the original terminator.
	#
	# Sequence after rewrite, conceptually:
	#   block: ...orig instrs...
	#          load flag_A → tA
	#          if tA goto drop_A else goto post_A
	#   drop_A: MoveOut + DropValue; goto post_A
	#   post_A: load flag_B → tB
	#           if tB goto drop_B else goto post_B
	#   drop_B: MoveOut + DropValue; goto post_B
	#   post_B: <original terminator>
	#
	# Because we mutate `block` in place, the cleanest way is to work
	# from the end backwards: the LAST tracked local's post-block
	# becomes the original-terminator block.  Then for each preceding
	# local, allocate a new "current" block whose terminator is an If
	# branching to drop and post; the post becomes the next iteration's
	# "current" entry point.  Finally, append the first guard's load+
	# if-terminator to `block`.
	#
	# We retain `block`'s original instructions; only its terminator
	# changes, and any earlier guards' drop/post blocks are appended
	# to func.blocks.
	cur_post: M.BasicBlock = M.BasicBlock(name=_new_block_name(func, f"{block.name}_dropfinal"))
	cur_post.terminator = original_term
	func.blocks[cur_post.name] = cur_post
	# Process tracked locals in REVERSE so the first one ends up
	# nearest the original block's tail (outermost).
	for local_name in reversed(tracked):
		flag_name = flag_for[local_name]
		ty = func.local_types.get(local_name)
		if ty is None:
			continue
		drop_block = M.BasicBlock(name=_new_block_name(func, f"{block.name}_drop_{local_name}"))
		moveout_dest = new_temp()
		drop_block.instructions.append(M.MoveOut(dest=moveout_dest, local=local_name, ty=ty))
		drop_block.instructions.append(M.DropValue(value=moveout_dest, ty=ty))
		drop_block.terminator = M.Goto(target=cur_post.name)
		func.blocks[drop_block.name] = drop_block
		guard_block: M.BasicBlock
		if local_name == tracked[0]:
			# Outermost guard — emit into `block` itself, replacing its
			# terminator with the IfTerminator.
			guard_block = block
		else:
			guard_block = M.BasicBlock(name=_new_block_name(func, f"{block.name}_guard_{local_name}"))
			func.blocks[guard_block.name] = guard_block
		flag_load = new_temp()
		guard_block.instructions.append(M.LoadLocal(dest=flag_load, local=flag_name))
		guard_block.terminator = M.IfTerminator(
			cond=flag_load,
			then_target=drop_block.name,
			else_target=cur_post.name,
		)
		# The guard becomes the next iteration's post.
		cur_post = guard_block


def _new_block_name(func: M.MirFunc, base: str) -> str:
	if base not in func.blocks:
		return base
	i = 1
	while True:
		name = f"{base}_{i}"
		if name not in func.blocks:
			return name
		i += 1
