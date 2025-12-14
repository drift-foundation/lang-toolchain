# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Nested try/catch unwinding: an unmatched inner try should unwind to the nearest
outer try (in the same function) before ultimately rethrowing as FnResult.Err.
"""

from __future__ import annotations

from lang2.driftc.stage2 import HIRToMIR, MirBuilder, mir_nodes as M
from lang2.driftc import stage1 as H


def _walk_dispatch_else(func: M.MirFunc, start: M.BasicBlock) -> M.BasicBlock:
	"""Follow else-targets of IfTerminator until a non-If terminator is found."""
	block = start
	while isinstance(block.terminator, M.IfTerminator):
		block = func.blocks[block.terminator.else_target]
	return block


def test_inner_unmatched_unwinds_to_outer_try():
	"""
	Inner try has no matching arm and no catch-all; outer try has a matching arm.
	The inner dispatch must jump to the outer dispatch, not return Err directly.
	"""
	builder = MirBuilder(name="try_nested_unwind")
	exc_env = {"m:Inner": 10, "m:Outer": 20}
	lower = HIRToMIR(builder, exc_env=exc_env, can_throw_by_name={"try_nested_unwind": True})

	# Structure:
	# outer try {
	#   inner try { throw Inner } catch Other { }
	# } catch Outer { }
	inner_try = H.HTry(
		body=H.HBlock(statements=[H.HThrow(value=H.HExceptionInit(event_fqn="m:Inner", field_names=[], field_values=[]))]),
		catches=[H.HCatchArm(event_fqn="m:Other", binder=None, block=H.HBlock(statements=[]))],
	)
	outer_try = H.HTry(
		body=H.HBlock(statements=[inner_try]),
		catches=[H.HCatchArm(event_fqn="m:Outer", binder="e", block=H.HBlock(statements=[]))],
	)
	lower.lower_block(H.HBlock(statements=[outer_try]))

	func = builder.func
	# Identify dispatch blocks (main dispatch blocks, not the chained _next blocks).
	dispatch_blocks = {
		name: blk for name, blk in func.blocks.items()
		if name.startswith("try_dispatch") and "next" not in name
	}
	# Ordering is deterministic: outer dispatch was created first.
	assert len(dispatch_blocks) >= 2
	sorted_dispatch = sorted(dispatch_blocks.items(), key=lambda item: item[0])
	outer_dispatch_name, outer_dispatch = sorted_dispatch[0]
	inner_dispatch_name, inner_dispatch = sorted_dispatch[1]

	# Walk the inner else-chain: it should end in a Goto to the outer dispatch, not a Return.
	final_block = _walk_dispatch_else(func, inner_dispatch)
	assert isinstance(final_block.terminator, M.Goto)
	outer_dispatch_target = final_block.terminator.target
	assert outer_dispatch_target == outer_dispatch_name

	# Outer dispatch should end in Return(Err) because it has no matching arm for Inner.
	outer_final = _walk_dispatch_else(func, outer_dispatch)
	assert isinstance(outer_final.terminator, M.Return)


def test_inner_and_outer_unmatched_rethrow_err():
	"""
	Inner try unmatched, outer also unmatched: rethrow should ultimately return Err.
	"""
	builder = MirBuilder(name="try_nested_rethrow")
	lower = HIRToMIR(builder, exc_env={}, can_throw_by_name={"try_nested_rethrow": True})

	inner_try = H.HTry(
		body=H.HBlock(statements=[H.HThrow(value=H.HExceptionInit(event_fqn="m:X", field_names=[], field_values=[]))]),
		catches=[H.HCatchArm(event_fqn="m:EvtA", binder=None, block=H.HBlock(statements=[]))],
	)
	outer_try = H.HTry(
		body=H.HBlock(statements=[inner_try]),
		catches=[H.HCatchArm(event_fqn="m:EvtB", binder=None, block=H.HBlock(statements=[]))],
	)
	lower.lower_block(H.HBlock(statements=[outer_try]))

	func = builder.func
	dispatch_blocks = {
		name: blk for name, blk in func.blocks.items()
		if name.startswith("try_dispatch") and "next" not in name
	}
	assert len(dispatch_blocks) >= 2
	sorted_dispatch = sorted(dispatch_blocks.items(), key=lambda item: item[0])
	outer_dispatch_name, outer_dispatch = sorted_dispatch[0]
	inner_dispatch_name, inner_dispatch = sorted_dispatch[1]

	# Inner else-chain should jump to outer dispatch (not return).
	inner_final = _walk_dispatch_else(func, inner_dispatch)
	assert isinstance(inner_final.terminator, M.Goto)
	assert inner_final.terminator.target == outer_dispatch.name

	# Outer else-chain should end in Return(Err).
	outer_final = _walk_dispatch_else(func, outer_dispatch)
	assert isinstance(outer_final.terminator, M.Return)
