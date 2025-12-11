# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Additional try/catch lowering coverage:

* Binder vs no-binder catch arms.
* Unknown event names fall back to code 0 but still match between throw/catch.
* Nested try with outer catch-all: inner unmatched error unwinds to outer catch-all.
"""

from __future__ import annotations

from lang2.stage2 import HIRToMIR, MirBuilder, mir_nodes as M
from lang2 import stage1 as H


def test_catch_binder_and_no_binder():
	"""First arm has a binder, second does not; binder produces a StoreLocal."""
	builder = MirBuilder(name="try_binder_shapes")
	lower = HIRToMIR(builder, exc_env={"EvtA": 1, "EvtB": 2})

	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="EvtA", args=[]))]),
				catches=[
					H.HCatchArm(event_name="EvtA", binder="e", block=H.HBlock(statements=[])),
					H.HCatchArm(event_name="EvtB", binder=None, block=H.HBlock(statements=[])),
				],
			)
		]
	)
	lower.lower_block(hir)
	func = builder.func

	# First catch arm (binder) should store the error into 'e'.
	catch0 = func.blocks["try_catch_0"]
	assert any(isinstance(instr, M.StoreLocal) and instr.local == "e" for instr in catch0.instructions)

	# Second catch arm (no binder) should not contain a StoreLocal for a binder.
	catch1 = func.blocks["try_catch_1"]
	assert not any(isinstance(instr, M.StoreLocal) and instr.local == "e" for instr in catch1.instructions)


def test_unknown_event_name_matches_via_code_zero():
	"""
	When an event name is unknown (not in exc_env), both throw and catch use code 0,
	so the dispatch still matches.
	"""
	builder = MirBuilder(name="try_unknown_event")
	lower = HIRToMIR(builder, exc_env={})

	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="Unknown", args=[]))]),
				catches=[H.HCatchArm(event_name="Unknown", binder=None, block=H.HBlock(statements=[]))],
			)
		]
	)
	lower.lower_block(hir)
	func = builder.func
	dispatch = func.blocks["try_dispatch"]

	# The dispatch block should compare against ConstInt 0 in the first (and only) event arm.
	# Instructions in dispatch: LoadLocal(err_tmp), ErrorEvent(code_tmp), ConstInt(arm_code_const=0), BinaryOpInstr(cmp_tmp)
	const_instrs = [instr for instr in dispatch.instructions if isinstance(instr, M.ConstInt)]
	assert const_instrs, "expected a ConstInt arm code in dispatch"
	assert const_instrs[-1].value == 0


def test_outer_catch_all_catches_unmatched_inner():
	"""
	Inner try has no matching arm; outer catch-all should receive the propagated error.
	"""
	builder = MirBuilder(name="try_outer_catch_all")
	lower = HIRToMIR(builder, exc_env={"Inner": 1})

	inner_try = H.HTry(
		body=H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="Inner", args=[]))]),
		catches=[H.HCatchArm(event_name="Other", binder=None, block=H.HBlock(statements=[]))],
	)
	outer_try = H.HTry(
		body=H.HBlock(statements=[inner_try]),
		catches=[H.HCatchArm(event_name=None, binder=None, block=H.HBlock(statements=[]))],
	)
	lower.lower_block(H.HBlock(statements=[outer_try]))

	func = builder.func
	# Find dispatch blocks (main ones, not the chained _next).
	dispatch_blocks = {
		name: blk for name, blk in func.blocks.items()
		if name.startswith("try_dispatch") and "next" not in name
	}
	assert len(dispatch_blocks) >= 2
	sorted_dispatch = sorted(dispatch_blocks.items(), key=lambda item: item[0])
	outer_dispatch_name, outer_dispatch = sorted_dispatch[0]
	inner_dispatch_name, inner_dispatch = sorted_dispatch[1]

	# Inner unmatched path should goto outer dispatch, not return.
	def _walk_dispatch_else(block: M.BasicBlock) -> M.BasicBlock:
		while isinstance(block.terminator, M.IfTerminator):
			block = func.blocks[block.terminator.else_target]
		return block

	inner_final = _walk_dispatch_else(inner_dispatch)
	assert isinstance(inner_final.terminator, M.Goto)
	assert inner_final.terminator.target == outer_dispatch_name

	# Outer dispatch should resolve to the catch-all (Goto, not Return).
	outer_final = _walk_dispatch_else(outer_dispatch)
	assert isinstance(outer_final.terminator, M.Goto)
	assert outer_final.terminator.target.startswith("try_catch")
