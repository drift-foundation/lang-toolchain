# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Shared drop-flag-guarded block-split authority.

The SINGLE implementation of "at a program point, drop a
flag-managed destructible local iff its runtime drop flag says it is
live" via a block split:

	pre_instrs
	LoadLocal(flag_tmp, flag_local)
	IfTerminator(flag_tmp, drop_blk, post_blk)

	drop_blk:  <drop_sequence>                # caller-provided nodes
	           StoreLocal(flag_local, false)  # flag-clear
	           Goto(post_blk)

	post_blk:  tail_instrs                     # + original terminator

Both the site-1 scope-drop authority (`cleanup_authoring`, whose
`drop_sequence` is `MoveOut + DropValue`) and the site-4
drop-before-overwrite authority (`overwrite_cleanup`, whose
`drop_sequence` is the retain-before-release `LoadLocal + ZeroValue +
StoreLocal(zero) + DropValue` and whose `tail_instrs` re-run the
overwriting store) call THIS function, so the flag-guarded emission
shape cannot drift between the two sites.

Uniform flag invariant preserved: "flag bit ≡ currently owns
destructible storage."  drop_blk clears the flag after dropping; the
caller's own set-instrumentation (drop_flags) re-establishes it on
any subsequent initializing store in `tail_instrs`.
"""
from __future__ import annotations

from typing import Callable, List, Tuple

from . import mir_nodes as M


def fresh_block_name(func: "M.MirFunc", base: str, pending: List["M.BasicBlock"]) -> str:
	"""A block name colliding with neither `func.blocks` nor `pending`."""
	pending_names = {b.name for b in pending}
	if base not in func.blocks and base not in pending_names:
		return base
	i = 1
	while True:
		name = f"{base}_{i}"
		if name not in func.blocks and name not in pending_names:
			return name
		i += 1


def build_guarded_drop_blocks(
	func: "M.MirFunc",
	*,
	origin_block_name: str,
	pre_instrs: List["M.MInstr"],
	tail_instrs: List["M.MInstr"],
	original_term,
	flag_local: str,
	drop_sequence: Callable[[List["M.MInstr"]], None],
	new_temp: Callable[[], str],
	pending: List["M.BasicBlock"],
	label: str,
	name_prefix: str = "guard",
) -> Tuple["M.BasicBlock", "M.BasicBlock", "M.BasicBlock"]:
	"""Author the guarded-drop split.

	Returns `(origin_blk_updated, drop_blk, post_blk)` — three
	`M.BasicBlock`s the caller registers into `func.blocks` (and marks
	the ledger dirty for).  `origin_blk_updated` is a FRESH block with
	`pre_instrs + LoadLocal(flag)` and an `IfTerminator`; the caller
	replaces the origin block's instructions/terminator with it (or
	uses it as the split cursor).  `drop_sequence(buf)` appends the
	type-specific drop nodes into `buf` (drop_blk's body, before the
	flag-clear).
	"""
	flag_tmp = new_temp()
	origin_instrs = list(pre_instrs)
	origin_instrs.append(M.LoadLocal(dest=flag_tmp, local=flag_local))

	drop_blk = M.BasicBlock(name=fresh_block_name(func, f"{origin_block_name}_{name_prefix}_drop_{label}", pending))
	drop_body: List["M.MInstr"] = []
	drop_sequence(drop_body)
	clear_dest = new_temp()
	drop_body.append(M.ConstBool(dest=clear_dest, value=False))  # ledger-cache-safety-audit: allow new-block
	drop_body.append(M.StoreLocal(local=flag_local, value=clear_dest))  # ledger-cache-safety-audit: allow new-block
	drop_blk.instructions = drop_body

	post_blk = M.BasicBlock(name=fresh_block_name(func, f"{origin_block_name}_{name_prefix}_post_{label}", pending + [drop_blk]))
	post_blk.instructions = list(tail_instrs)
	post_blk.terminator = original_term

	drop_blk.terminator = M.Goto(target=post_blk.name)  # ledger-cache-safety-audit: allow new-block

	origin_blk = M.BasicBlock(name=origin_block_name)
	origin_blk.instructions = origin_instrs
	origin_blk.terminator = M.IfTerminator(
		cond=flag_tmp,
		then_target=drop_blk.name,
		else_target=post_blk.name,
	)
	return origin_blk, drop_blk, post_blk
