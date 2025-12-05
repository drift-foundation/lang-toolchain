# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage4 (MIR → SSA) skeleton tests.

Currently validates the straight-line SSA pass: single-block MIR with
load/store locals succeeds and keeps instruction order. Guards against
load-before-store.
"""

from __future__ import annotations

import pytest

from lang2.stage2 import MirFunc, BasicBlock, StoreLocal, LoadLocal, ConstInt, Return, Goto
from lang2.stage4 import MirToSSA


def test_straight_line_ssa_passes():
	entry = BasicBlock(
		name="entry",
		instructions=[
			StoreLocal(local="x", value="v0"),
			LoadLocal(dest="t0", local="x"),
			ConstInt(dest="c1", value=1),
		],
		terminator=Return(value="t0"),
	)
	func = MirFunc(name="f", params=[], locals=["x"], blocks={"entry": entry}, entry="entry")
	ssa_func = MirToSSA().run(func)
	assert ssa_func.func is func
	assert ssa_func.func.blocks["entry"].instructions[0].local == "x"
	assert ssa_func.local_versions["x"] == 1
	assert ssa_func.current_value["x"] == "x_1"


def test_multiple_stores_version_increments():
	entry = BasicBlock(
		name="entry",
		instructions=[
			StoreLocal(local="x", value="v0"),
			StoreLocal(local="x", value="v1"),
			LoadLocal(dest="t0", local="x"),
		],
		terminator=Return(value="t0"),
	)
	func = MirFunc(name="f", params=[], locals=["x"], blocks={"entry": entry}, entry="entry")
	ssa_func = MirToSSA().run(func)
	assert ssa_func.local_versions["x"] == 2
	assert ssa_func.current_value["x"] == "x_2"


def test_ssa_load_before_store_raises():
	entry = BasicBlock(
		name="entry",
		instructions=[
			LoadLocal(dest="t0", local="x"),
		],
		terminator=Return(value="t0"),
	)
	func = MirFunc(name="f", params=[], locals=["x"], blocks={"entry": entry}, entry="entry")
	with pytest.raises(RuntimeError):
		MirToSSA().run(func)


def test_ssa_rejects_multi_block_funcs():
	entry = BasicBlock(
		name="entry",
		instructions=[],
		terminator=Goto(target="exit"),
	)
	exit_block = BasicBlock(name="exit", instructions=[], terminator=None)
	func = MirFunc(
		name="f", params=[], locals=["x"], blocks={"entry": entry, "exit": exit_block}, entry="entry"
	)
	with pytest.raises(NotImplementedError):
		MirToSSA().run(func)
