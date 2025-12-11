# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Ported exception scenarios to pin legacy semantics:

* Throw event B, inner try catches event A, outer catches event B -> outer catch is taken.
* Multi-arm try: event-specific arms followed by catch-all; thrown event matches the correct arm.
"""

from __future__ import annotations

from lang2.stage2 import HIRToMIR, MirBuilder, mir_nodes as M
from lang2 import stage1 as H


def _walk_else(func: M.MirFunc, block: M.BasicBlock) -> M.BasicBlock:
	"""Follow the else-targets of IfTerminator until a non-If terminator."""
	while isinstance(block.terminator, M.IfTerminator):
		block = func.blocks[block.terminator.else_target]
	return block


def test_throw_b_skips_inner_catch_a_hits_outer_catch_b():
	"""
	Inner try catches EvtA, outer catches EvtB.
	Throw EvtB in inner body should skip inner catch and land in outer catch.
	"""
	exc_env = {"EvtA": 1, "EvtB": 2}
	builder = MirBuilder(name="legacy_inner_outer_events")
	lower = HIRToMIR(builder, exc_env=exc_env)

	inner_try = H.HTry(
		body=H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="EvtB", args=[]))]),
		catches=[H.HCatchArm(event_name="EvtA", binder=None, block=H.HBlock(statements=[]))],
	)
	outer_try = H.HTry(
		body=H.HBlock(statements=[inner_try]),
		catches=[H.HCatchArm(event_name="EvtB", binder="b", block=H.HBlock(statements=[]))],
	)
	lower.lower_block(H.HBlock(statements=[outer_try]))
	func = builder.func

	# Identify the two main dispatch blocks (not the chained "next" blocks).
	dispatch_blocks = {
		name: blk for name, blk in func.blocks.items()
		if name.startswith("try_dispatch") and "next" not in name
	}
	assert len(dispatch_blocks) >= 2
	sorted_dispatch = sorted(dispatch_blocks.items(), key=lambda item: item[0])
	outer_dispatch_name, outer_dispatch = sorted_dispatch[0]
	inner_dispatch_name, inner_dispatch = sorted_dispatch[1]

	# Inner unmatched path should goto outer dispatch.
	inner_final = _walk_else(func, inner_dispatch)
	assert isinstance(inner_final.terminator, M.Goto)
	assert inner_final.terminator.target == outer_dispatch_name

	# Outer dispatch should take the then-branch to the EvtB catch (the one that binds "b").
	assert isinstance(outer_dispatch.terminator, M.IfTerminator)
	then_target = outer_dispatch.terminator.then_target
	# Identify the outer catch block by looking for the binder store.
	catch_blocks = {name: blk for name, blk in func.blocks.items() if name.startswith("try_catch")}
	target_block = catch_blocks[then_target]
	assert any(isinstance(instr, M.StoreLocal) and instr.local == "b" for instr in target_block.instructions)


def test_multi_event_with_catch_all_matches_specific_arm():
	"""
	Multi-arm try with two events followed by catch-all; thrown event matches
	the correct event-specific arm, not the catch-all.
	"""
	exc_env = {"EvtA": 1, "EvtB": 2}
	builder = MirBuilder(name="legacy_multi_event")
	lower = HIRToMIR(builder, exc_env=exc_env)

	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="EvtB", args=[]))]),
				catches=[
					H.HCatchArm(event_name="EvtA", binder=None, block=H.HBlock(statements=[])),
					H.HCatchArm(event_name="EvtB", binder="b", block=H.HBlock(statements=[])),
					H.HCatchArm(event_name=None, binder=None, block=H.HBlock(statements=[])),
				],
			)
		]
	)
	lower.lower_block(hir)
	func = builder.func

	dispatch = func.blocks["try_dispatch"]
	# First IfTerminator should compare against EvtA code; else branch continues chain.
	assert isinstance(dispatch.terminator, M.IfTerminator)
	first_then = dispatch.terminator.then_target
	first_else = dispatch.terminator.else_target
	assert first_then == "try_catch_0"

	# Second IfTerminator (in else branch) should jump to EvtB catch, not catch-all.
	second_dispatch = func.blocks[first_else]
	assert isinstance(second_dispatch.terminator, M.IfTerminator)
	assert second_dispatch.terminator.then_target == "try_catch_1"

	# Catch-all is only in the final else of the second dispatch.
	final_else = func.blocks[second_dispatch.terminator.else_target]
	final_term = _walk_else(func, final_else)
	assert isinstance(final_term.terminator, M.Goto)
	assert final_term.terminator.target == "try_catch_2"


def test_throw_inside_catch_rethrows_to_outer_try():
	"""
	Throw from inside an inner catch should unwind to the outer try, not back into
	the inner. Inner catch handles EvtA, outer handles EvtB; inner handler throws B.
	"""
	exc_env = {"EvtA": 1, "EvtB": 2}
	builder = MirBuilder(name="legacy_throw_in_catch")
	lower = HIRToMIR(builder, exc_env=exc_env)

	inner_try = H.HTry(
		body=H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="EvtA", args=[]))]),
		catches=[
			H.HCatchArm(
				event_name="EvtA",
				binder=None,
				block=H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="EvtB", args=[]))]),
			)
		],
	)
	outer_try = H.HTry(
		body=H.HBlock(statements=[inner_try]),
		catches=[H.HCatchArm(event_name="EvtB", binder="b", block=H.HBlock(statements=[]))],
	)
	lower.lower_block(H.HBlock(statements=[outer_try]))
	func = builder.func

	# Identify dispatch blocks (main ones).
	dispatch_blocks = {
		name: blk for name, blk in func.blocks.items()
		if name.startswith("try_dispatch") and "next" not in name
	}
	assert len(dispatch_blocks) >= 2
	sorted_dispatch = sorted(dispatch_blocks.items(), key=lambda item: item[0])
	outer_dispatch_name, outer_dispatch = sorted_dispatch[0]
	inner_dispatch_name, inner_dispatch = sorted_dispatch[1]

	# Inner dispatch is reached twice: first for EvtA (matched), then handler throws EvtB.
	# We care about the second throw: unmatched for inner (no arm for EvtB) should goto outer dispatch.
	inner_final = _walk_else(func, inner_dispatch)
	assert isinstance(inner_final.terminator, M.Goto)
	assert inner_final.terminator.target == outer_dispatch_name

	# Outer dispatch then catches EvtB (binder b).
	assert isinstance(outer_dispatch.terminator, M.IfTerminator)
	then_target = outer_dispatch.terminator.then_target
	catch_blocks = {name: blk for name, blk in func.blocks.items() if name.startswith("try_catch")}
	target_block = catch_blocks[then_target]
	assert any(isinstance(instr, M.StoreLocal) and instr.local == "b" for instr in target_block.instructions)


def test_inner_catch_all_handles_error_before_outer_specific_arm():
	"""
	Inner try has a catch-all; outer has an event-specific arm for the same event.
	The inner catch-all must handle the thrown error, so outer dispatch is never used.
	"""
	exc_env = {"EvtX": 7}
	builder = MirBuilder(name="legacy_inner_catchall_outer_specific")
	lower = HIRToMIR(builder, exc_env=exc_env)

	inner_try = H.HTry(
		body=H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="EvtX", args=[]))]),
		catches=[H.HCatchArm(event_name=None, binder=None, block=H.HBlock(statements=[]))],
	)
	outer_try = H.HTry(
		body=H.HBlock(statements=[inner_try]),
		catches=[H.HCatchArm(event_name="EvtX", binder="x", block=H.HBlock(statements=[]))],
	)
	lower.lower_block(H.HBlock(statements=[outer_try]))
	func = builder.func

	# The inner dispatch should immediately goto its catch-all (no IfTerminator chain).
	inner_dispatch = func.blocks["try_dispatch1"]  # inner try is lowered after outer, so dispatch1
	assert isinstance(inner_dispatch.terminator, M.Goto)
	target_block = func.blocks[inner_dispatch.terminator.target]
	# Inner catch-all has no binder store (outer catch stores binder x).
	assert not any(isinstance(instr, M.StoreLocal) and instr.local == "x" for instr in target_block.instructions)

	# The outer dispatch exists but should not be the target of the inner unmatched path.
	outer_dispatch = func.blocks["try_dispatch"]
	assert isinstance(outer_dispatch.terminator, M.IfTerminator)
	# Walk the inner else chain to ensure it never reaches the outer dispatch.
	assert inner_dispatch.terminator.target != outer_dispatch.name


def test_inner_matching_catch_handles_and_stops_propagation():
	"""
	Inner try has an event-specific arm for the thrown event; outer has a different arm.
	The thrown event must be caught by the inner arm and not propagate to the outer dispatch.
	"""
	exc_env = {"EvtInner": 11, "EvtOuter": 22}
	builder = MirBuilder(name="legacy_inner_matches")
	lower = HIRToMIR(builder, exc_env=exc_env)

	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(
					statements=[
						H.HTry(
							body=H.HBlock(
								statements=[H.HThrow(value=H.HDVInit(dv_type_name="EvtInner", args=[]))]
							),
							catches=[
								H.HCatchArm(
									event_name="EvtInner",
									binder="inner",
									block=H.HBlock(statements=[]),
								)
							],
						)
					]
				),
				catches=[
					H.HCatchArm(
						event_name="EvtOuter",
						binder="outer",
						block=H.HBlock(statements=[]),
					)
				],
			)
		]
	)
	lower.lower_block(hir)
	func = builder.func

	# Find the inner dispatch by looking for the binder 'inner' in a catch block reached via then-branch.
	dispatch_blocks = {
		name: blk for name, blk in func.blocks.items()
		if name.startswith("try_dispatch") and "next" not in name
	}
	assert dispatch_blocks
	inner_dispatch_name = None
	for name, block in dispatch_blocks.items():
		if isinstance(block.terminator, M.IfTerminator):
			then_block = func.blocks[block.terminator.then_target]
			if any(isinstance(instr, M.StoreLocal) and instr.local == "inner" for instr in then_block.instructions):
				inner_dispatch_name = name
				break
	assert inner_dispatch_name is not None, "could not find inner dispatch"
	inner_dispatch = func.blocks[inner_dispatch_name]

	# The matched path should bind 'inner' in its catch block.
	then_block = func.blocks[inner_dispatch.terminator.then_target]
	assert any(isinstance(instr, M.StoreLocal) and instr.local == "inner" for instr in then_block.instructions)

	# The unmatched path remains available for other events: it will unwind to the outer try.
	inner_final = _walk_else(func, inner_dispatch)
	assert isinstance(inner_final.terminator, M.Goto)
