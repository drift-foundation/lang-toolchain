# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LB-8 structural regression: callsite_id ↔ instantiation/CallInfo alignment.

Bug: a for-in over a NON-Copy ConstShare-provable rvalue (`for xn in mknc()`)
makes the secondary `&T -> T` receiver coercion synthesize an implicit
`const_share()` HMethodCall.  That node was created BEFORE the per-function
callsite-id high-water mark was computed correctly: `_alloc_callsite_id`'s
`_max_callsite_id` walker did not descend into statement children of nested
blocks (`HLet`/`HLoop` produced by the for-in desugaring), so for a body whose
calls all live inside for-in blocks it returned -1 and allocation started at 0.

The const_share then collided at callsite_id 0 and `_record_call_info`
reassigned it, cascading +1 through every later call's callsite_id.  The
per-callsite instantiation map (recorded during resolution, keyed by the
pre-cascade ids) was NOT shifted in lockstep, so a FOLLOWING generic Box<T>
for-in loop's `iter()` callsite inherited the `next()` monomorphization — its
MIR call targeted `Box::...::next__inst...` instead of `Box::...::iter...`,
which LLVM rejected ("variant value where ptr expected").

This pins the structural invariant at the MIR boundary: in the combined
program, the generic Box loop's preheader lowers to a Call targeting the
`iter` instance (exactly one), and `next` instance calls are not emitted in
the iter position.  Run-level + memcheck coverage lives in the e2e case
`for_in_nc_loop_then_generic_box_loop`.
"""
from __future__ import annotations

import contextlib
import io

from lang.driftc.driftc import main as driftc_main
from lang.driftc.stage4 import ssa as _ssa_mod
from lang.driftc.stage2 import mir_nodes as M


# Non-Copy (String-owning → not Copy) self-iterating variant whose for-in goes
# through the implicit-const_share move path, FOLLOWED by a generic by-value
# Box<T> for-in loop.  This is the minimal combination that reproduced LB-8.
_NC_THEN_GENERIC_BOX = """
module main;
import std.iter as iter;
import std.core as core;
use trait iter.SinglePassIterator;

pub variant NC { Done, R(tag: String, n: Int) }
implement iter.SinglePassIterator<Int> for NC {
	pub fn next(self: &mut NC) nothrow -> Optional<Int> {
		match self { NC::R(t, n) => { if *n <= 0 { return Optional<Int>::None(); } val c = *n; *n = *n - 1; return Optional::Some(c); }, default => { return Optional<Int>::None(); } }
	}
}
implement iter.Iterable<NC, Int, NC> for NC { pub fn iter(var self: NC) nothrow -> NC { return move self; } }
fn mknc() nothrow -> NC { return NC::R("tag", 4); }

pub variant Box<T> { Empty, One(v: T) }
implement<T> iter.SinglePassIterator<T> for Box<T> require T is core.Copy {
	pub fn next(self: &mut Box<T>) nothrow -> Optional<T> {
		match self { Box::One(x) => { val r = *x; *self = Box<T>::Empty(); return Optional::Some(r); }, default => { return Optional<T>::None(); } }
	}
}
implement<T> iter.Iterable<Box<T>, T, Box<T>> for Box<T> require T is core.Copy {
	pub fn iter(var self: Box<T>) nothrow -> Box<T> { return move self; }
}
fn mkbox() nothrow -> Box<Int> { return Box::One(9); }

pub fn main() nothrow -> Int {
	var n = 0;
	for xn in mknc() { n = n + xn; }
	if n != 10 { return 3; }
	var g = 0;
	for xg in mkbox() { g = g + xg; }
	if g != 9 { return 5; }
	return 0;
}
""".lstrip()


def test_const_share_loop_does_not_desync_following_generic_loop_targets(tmp_path, monkeypatch) -> None:
	captured = []
	orig = _ssa_mod.MirToSSA.run

	def _spy(self, func):
		captured.append(func)
		return orig(self, func)

	monkeypatch.setattr(_ssa_mod.MirToSSA, "run", _spy)

	src = tmp_path / "main.drift"
	src.write_text(_NC_THEN_GENERIC_BOX, encoding="utf-8")
	with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
		rc = driftc_main(["--stdlib-root", "stdlib", "--entry", "main", "-o", str(tmp_path / "out.bin"), str(src)])

	# The whole point: it must COMPILE (pre-fix the generic Box iter() callsite
	# was retargeted to next() and clang rejected the IR).
	assert rc == 0, "NC-then-generic-Box for-in failed to compile (LB-8 callsite-id desync)"

	main_func = next((f for f in captured if getattr(f.fn_id, "name", None) == "main"), None)
	assert main_func is not None, "did not capture main()'s MIR"

	box_iter_calls = 0
	box_next_calls = 0
	_blocks = main_func.blocks
	_block_iter = _blocks.values() if isinstance(_blocks, dict) else _blocks
	for block in _block_iter:
		for instr in block.instructions:
			if isinstance(instr, M.Call):
				name = getattr(instr.fn_id, "name", "") or ""
				if name.startswith("Box<T>::") and "Iterable" in name and "::iter" in name:
					box_iter_calls += 1
				elif name.startswith("Box<T>::") and "SinglePassIterator" in name and "::next" in name:
					box_next_calls += 1

	# The generic Box loop calls iter() exactly once (the preheader) — before
	# the fix this was 0 (it had been retargeted to next()).  next() is called
	# in the loop header; the precise count is unimportant, but iter() MUST be
	# present and targeted at the Iterable::iter instance.
	assert box_iter_calls == 1, (
		f"expected exactly one Box Iterable::iter call (the generic loop preheader), "
		f"got {box_iter_calls}; iter() callsite was mis-targeted (LB-8 desync)"
	)
	assert box_next_calls >= 1, (
		f"expected at least one Box SinglePassIterator::next call, got {box_next_calls}"
	)


# Two DISTINCT generic instantiations (Box<Int>, Box<Bool>) bracketing a
# non-Copy ConstShare loop.  Normal-path monomorphization-separation coverage:
# each generic for-in callsite keeps its OWN per-callsite instantiation
# (Box<Int> vs Box<Bool>), and the implicit const_share inserted for the middle
# loop does not perturb that mapping.  (This is NOT collision-recovery coverage:
# callsite_ids are unique here, so the _record_call_info duplicate-id path is
# never exercised — that contract lives in
# test_node_ids_and_callinfo.py::test_duplicate_callsite_id_*.)
_DISTINCT_GENERIC_INSTS = """
module main;
import std.iter as iter;
import std.core as core;
use trait iter.SinglePassIterator;

pub variant Box<T> { Empty, One(v: T) }
implement<T> iter.SinglePassIterator<T> for Box<T> require T is core.Copy {
	pub fn next(self: &mut Box<T>) nothrow -> Optional<T> {
		match self { Box::One(x) => { val r = *x; *self = Box<T>::Empty(); return Optional::Some(r); }, default => { return Optional<T>::None(); } }
	}
}
implement<T> iter.Iterable<Box<T>, T, Box<T>> for Box<T> require T is core.Copy {
	pub fn iter(var self: Box<T>) nothrow -> Box<T> { return move self; }
}
fn mkbox_int() nothrow -> Box<Int> { return Box::One(5); }
fn mkbox_bool() nothrow -> Box<Bool> { return Box::One(true); }

pub variant NC { Done, R(tag: String, n: Int) }
implement iter.SinglePassIterator<Int> for NC {
	pub fn next(self: &mut NC) nothrow -> Optional<Int> {
		match self { NC::R(t, n) => { if *n <= 0 { return Optional<Int>::None(); } val c = *n; *n = *n - 1; return Optional::Some(c); }, default => { return Optional<Int>::None(); } }
	}
}
implement iter.Iterable<NC, Int, NC> for NC { pub fn iter(var self: NC) nothrow -> NC { return move self; } }
fn mknc() nothrow -> NC { return NC::R("tag", 4); }

pub fn main() nothrow -> Int {
	var a = 0;
	for xa in mkbox_int() { a = a + xa; }
	if a != 5 { return 1; }
	var n = 0;
	for xn in mknc() { n = n + xn; }
	if n != 10 { return 2; }
	var b = 0;
	for xb in mkbox_bool() { if xb { b = b + 1; } }
	if b != 1 { return 3; }
	return 0;
}
""".lstrip()


def test_distinct_generic_insts_keep_their_own_monomorphization(tmp_path, monkeypatch) -> None:
	captured = []
	orig = _ssa_mod.MirToSSA.run

	def _spy(self, func):
		captured.append(func)
		return orig(self, func)

	monkeypatch.setattr(_ssa_mod.MirToSSA, "run", _spy)

	src = tmp_path / "main.drift"
	src.write_text(_DISTINCT_GENERIC_INSTS, encoding="utf-8")
	with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
		rc = driftc_main(["--stdlib-root", "stdlib", "--entry", "main", "-o", str(tmp_path / "out.bin"), str(src)])

	assert rc == 0, "distinct-generic-insts-around-const_share program failed to compile"

	main_func = next((f for f in captured if getattr(f.fn_id, "name", None) == "main"), None)
	assert main_func is not None, "did not capture main()'s MIR"

	box_iter_targets: set[str] = set()
	_blocks = main_func.blocks
	_block_iter = _blocks.values() if isinstance(_blocks, dict) else _blocks
	for block in _block_iter:
		for instr in block.instructions:
			if isinstance(instr, M.Call):
				name = getattr(instr.fn_id, "name", "") or ""
				if name.startswith("Box<T>::") and "Iterable" in name and "::iter" in name:
					box_iter_targets.add(name)

	# Two distinct Box instantiations (Box<Int>, Box<Bool>) → two DISTINCT
	# `Iterable::iter` monomorphization targets.  Normal-path separation: each
	# generic for-in callsite is monomorphized independently and the middle
	# const_share loop does not perturb that mapping.
	assert len(box_iter_targets) == 2, (
		f"expected two distinct Box Iterable::iter monomorphizations (Box<Int>, Box<Bool>), "
		f"got {sorted(box_iter_targets)}; a generic instantiation was cross-attached"
	)
