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

from lang2.driftc.stage2 import (
	AssignSSA,
	MirFunc,
	BasicBlock,
	StoreLocal,
	LoadLocal,
	ConstInt,
	Return,
	Goto,
)
from lang2.driftc.stage4 import MirToSSA
from lang2.driftc.stage4.ssa import CfgKind


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
	instrs = ssa_func.func.blocks["entry"].instructions
	assert isinstance(instrs[0], AssignSSA)
	assert instrs[0].dest == "x_1"
	assert instrs[0].src == "v0"
	assert isinstance(instrs[1], AssignSSA)
	assert instrs[1].dest == "t0"
	assert instrs[1].src == "x_1"
	assert ssa_func.local_versions["x"] == 1
	assert ssa_func.current_value["x"] == "x_1"
	assert ssa_func.value_for_instr[("entry", 0)] == "x_1"
	assert ssa_func.value_for_instr[("entry", 1)] == "x_1"


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
	assert ssa_func.value_for_instr[("entry", 0)] == "x_1"
	assert ssa_func.value_for_instr[("entry", 1)] == "x_2"
	assert ssa_func.value_for_instr[("entry", 2)] == "x_2"


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


def test_ssa_accepts_loop_cfg():
	# A CFG with a backedge (loop) is supported by SSA now.
	entry = BasicBlock(name="entry", instructions=[], terminator=Goto(target="loop"))
	loop = BasicBlock(name="loop", instructions=[], terminator=Goto(target="loop"))
	func = MirFunc(name="f", params=[], locals=["x"], blocks={"entry": entry, "loop": loop}, entry="entry")
	ssa = MirToSSA().run(func)
	assert ssa.cfg_kind is CfgKind.GENERAL
