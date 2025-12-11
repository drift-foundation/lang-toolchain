# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Test the try/catch dispatch path when no catch-all exists and the thrown event
code does not match any arm: expect rethrow as FnResult.Err.
"""

from __future__ import annotations

from lang2.driftc.stage2 import HIRToMIR, MirBuilder, mir_nodes as M
from lang2.driftc import stage1 as H


def test_try_unmatched_event_rethrows_as_err():
	"""
	A try with only event-specific arms and no catch-all should rethrow as
	FnResult.Err when no arm matches the thrown event code.
	"""
	builder = MirBuilder(name="try_unmatched")
	# Only catch EvtA; exc_env maps it to 123.
	lower = HIRToMIR(builder, exc_env={"EvtA": 123})

	# Throw a DV with a different event name (no code mapping => 0), so it won't match.
	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="Other", args=[]))]),
				catches=[
					H.HCatchArm(event_name="EvtA", binder="a", block=H.HBlock(statements=[])),
				],
			)
		]
	)
	lower.lower_block(hir)

	func = builder.func
	dispatch = func.blocks["try_dispatch"]
	# Walk the dispatch chain to the final else block.
	while isinstance(dispatch.terminator, M.IfTerminator):
		dispatch = func.blocks[dispatch.terminator.else_target]

	# Dispatch should end with a Return of ConstructResultErr (rethrow as Err).
	assert isinstance(dispatch.terminator, M.Return)
	ret_val = dispatch.terminator.value
	assert ret_val is not None

	# The instructions in dispatch should end with ConstructResultErr feeding the return.
	construct_err = None
	for instr in dispatch.instructions[::-1]:
		if isinstance(instr, M.ConstructResultErr):
			construct_err = instr
			break
	assert construct_err is not None
	assert construct_err.dest == ret_val
