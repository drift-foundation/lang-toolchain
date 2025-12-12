# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 2 test: HIR→MIR lowering for try/catch routing throws intra-function.
"""

from lang2.driftc import stage1 as H
from lang2.driftc.stage2 import (
	MirBuilder,
	HIRToMIR,
	ConstString,
	ConstInt,
	ConstructError,
	Goto,
	ErrorEvent,
	IfTerminator,
)


def test_try_routes_throw_to_catch_block():
	"""
	Lower:
	  try { throw Evt{msg="x"} } catch e { }

	Shape expectations:
	  - entry branches to try_body
	  - try_body constructs the Error, stores into hidden error_local, then Goto dispatch
	  - dispatch jumps to catch-all arm
	  - catch arm projects ErrorEvent and falls through to cont
	"""
	builder = MirBuilder(name="try_fn")
	lower = HIRToMIR(builder)

	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(
					statements=[
						H.HThrow(
							value=H.HExceptionInit(
								event_fqn="m:Evt",
								field_names=["msg"],
								field_values=[H.HDVInit(dv_type_name="Evt", args=[H.HLiteralString("boom")])],
							)
						)
					]
				),
				catches=[
					H.HCatchArm(event_fqn=None, binder="e", block=H.HBlock(statements=[]))
				],
			)
		]
	)
	lower.lower_block(hir)

	blocks = builder.func.blocks

	# Entry should jump to try_body
	assert blocks["entry"].terminator is not None
	assert isinstance(blocks["entry"].terminator, Goto)
	assert blocks["entry"].terminator.target.startswith("try_body")

	# Try body should build error and jump to dispatch
	try_body = blocks[blocks["entry"].terminator.target]
	instrs = try_body.instructions
	consts = [i for i in instrs if isinstance(i, ConstString)]
	assert any(c.value == "boom" for c in consts)
	assert any(c.value == "msg" for c in consts)
	assert any(isinstance(i, ConstInt) for i in instrs)
	assert any(isinstance(i, ConstructError) for i in instrs)
	assert isinstance(try_body.terminator, Goto)
	assert try_body.terminator.target.startswith("try_dispatch")

	# Dispatch should jump to the catch-all arm.
	dispatch = blocks[try_body.terminator.target]
	assert isinstance(dispatch.terminator, Goto)
	catch_name = dispatch.terminator.target

	# Catch block should start by projecting event code and end with Goto to cont
	catch_block = blocks[catch_name]
	# Catch block should have an ErrorEvent (projecting code from the stored Error).
	assert any(isinstance(ins, ErrorEvent) for ins in catch_block.instructions)
	assert isinstance(catch_block.terminator, Goto)
	assert catch_block.terminator.target.startswith("try_cont")


def test_try_dispatches_on_event_codes():
	"""
	Multi-arm try/catch should dispatch on ErrorEvent codes.
	"""
	builder = MirBuilder(name="try_evt")
	lower = HIRToMIR(builder, exc_env={"m:EvtA": 123})

	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(
					statements=[
						H.HThrow(
							value=H.HExceptionInit(
								event_fqn="m:EvtA",
								field_names=["msg"],
								field_values=[H.HDVInit(dv_type_name="EvtA", args=[H.HLiteralString("boom")])],
							)
						)
					]
				),
				catches=[
					H.HCatchArm(event_fqn="m:EvtA", binder="a", block=H.HBlock(statements=[])),
					H.HCatchArm(event_fqn=None, binder=None, block=H.HBlock(statements=[])),
				],
			)
		]
	)
	lower.lower_block(hir)

	blocks = builder.func.blocks
	body = blocks[blocks["entry"].terminator.target]
	dispatch = blocks[body.terminator.target]
	assert isinstance(dispatch.terminator, IfTerminator)
	then_target = dispatch.terminator.then_target
	# then-target should be first catch arm block
	catch_arm_block = blocks[then_target]
	assert any(isinstance(ins, ErrorEvent) for ins in catch_arm_block.instructions)
