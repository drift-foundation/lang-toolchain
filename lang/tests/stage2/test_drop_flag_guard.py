# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Unit pins for the SHARED drop-flag-guarded block-split primitive
(`lang/driftc/stage2/drop_flag_guard.py`).

Both the site-1 scope-drop authority (`cleanup_authoring`) and the site-4
drop-before-overwrite authority (`overwrite_cleanup`) author their
flag-guarded drops through THIS one primitive, so the split shape cannot
diverge between the two sites.  These tests pin: the canonical block
shape, site-neutral naming (caller-supplied prefix), span preservation of
the relocated instructions/terminator, and fresh-name collision
avoidance.
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.drop_flag_guard import (
	build_guarded_drop_blocks,
	fresh_block_name,
)


def _mk_func(name="f"):
	return M.MirFunc(
		name=f"test::{name}",
		params=[],
		locals=[],
		fn_id=FunctionId(module="test", name=name, ordinal=0),
		local_types={},
	)


def _temp_gen():
	n = [0]

	def _new():
		n[0] += 1
		return f"__t{n[0]}"
	return _new


def _drop_seq_factory(local, ty, recorder):
	def _seq(buf):
		tmp = "__moved"
		buf.append(M.MoveOut(dest=tmp, local=local, ty=ty))
		drop = M.DropValue(value=tmp, ty=ty)
		buf.append(drop)
		recorder.append(drop)
	return _seq


def test_canonical_guarded_shape_and_names():
	func = _mk_func()
	store = M.StoreLocal(local="x", value="%new")
	setattr(store, "span", ("f.drift", 10))
	term = M.Return(value=None)
	setattr(term, "span", ("f.drift", 12))
	pre = [M.StoreLocal(local="y", value="%pre")]
	recorded: list = []
	origin, drop_blk, post = build_guarded_drop_blocks(
		func,
		origin_block_name="entry",
		pre_instrs=pre,
		tail_instrs=[store],
		original_term=term,
		flag_local="__drop_flag_x",
		drop_sequence=_drop_seq_factory("x", 7, recorded),
		new_temp=_temp_gen(),
		pending=[],
		label="x",
	)

	# Default (site-neutral) prefix.
	assert drop_blk.name == "entry_guard_drop_x"
	assert post.name == "entry_guard_post_x"
	assert origin.name == "entry"

	# origin: pre + LoadLocal(flag) → IfTerminator(flag, drop, post)
	assert origin.instructions[0] is pre[0]
	assert isinstance(origin.instructions[-1], M.LoadLocal)
	assert origin.instructions[-1].local == "__drop_flag_x"
	it = origin.terminator
	assert isinstance(it, M.IfTerminator)
	assert it.cond == origin.instructions[-1].dest
	assert it.then_target == drop_blk.name and it.else_target == post.name

	# drop_blk: <drop_seq> + ConstBool(false) + StoreLocal(flag,false) → Goto(post)
	assert isinstance(drop_blk.instructions[0], M.MoveOut)
	assert drop_blk.instructions[1] is recorded[0]
	cb = drop_blk.instructions[-2]
	fc = drop_blk.instructions[-1]
	assert isinstance(cb, M.ConstBool) and cb.value is False
	assert isinstance(fc, M.StoreLocal) and fc.local == "__drop_flag_x" and fc.value == cb.dest
	assert isinstance(drop_blk.terminator, M.Goto) and drop_blk.terminator.target == post.name

	# post: tail + original terminator (SAME object → span preserved).
	assert post.instructions == [store]
	assert post.instructions[0] is store
	assert getattr(post.instructions[0], "span") == ("f.drift", 10)
	assert post.terminator is term
	assert getattr(post.terminator, "span") == ("f.drift", 12)


def test_name_prefix_is_site_neutral():
	"""cleanup_authoring passes name_prefix='cleanup' to keep its historical
	block names; the default is the neutral 'guard'."""
	func = _mk_func()
	origin, drop_blk, post = build_guarded_drop_blocks(
		func,
		origin_block_name="bb",
		pre_instrs=[],
		tail_instrs=[],
		original_term=M.Return(value=None),
		flag_local="__drop_flag_z",
		drop_sequence=lambda buf: buf.append(M.DropValue(value="__m", ty=1)),
		new_temp=_temp_gen(),
		pending=[],
		label="z",
		name_prefix="cleanup",
	)
	assert drop_blk.name == "bb_cleanup_drop_z"
	assert post.name == "bb_cleanup_post_z"


def test_fresh_block_name_avoids_existing_and_pending_collisions():
	func = _mk_func()
	func.blocks["bb_guard_drop_x"] = M.BasicBlock(name="bb_guard_drop_x")
	pending = [M.BasicBlock(name="bb_guard_drop_x_1")]
	name = fresh_block_name(func, "bb_guard_drop_x", pending)
	assert name not in func.blocks
	assert name not in {b.name for b in pending}
	assert name == "bb_guard_drop_x_2"
