# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 2 test: HIR→MIR lowering for try/catch routing throws intra-function.
"""

from lang.driftc import stage1 as H
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.stage2 import (
	HIRToMIR,
	make_builder,
	ConstUint64,
	ConstructError,
	Goto,
	ErrorEvent,
	IfTerminator,
)
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable


def test_try_routes_throw_to_catch_block():
	"""
	Lower:
	  try { throw Evt(msg = "x") } catch e { }

	Shape expectations:
	  - entry branches to try_body
	  - try_body constructs the Error, stores into hidden error_local, then Goto dispatch
	  - dispatch jumps to catch-all arm
	  - catch arm projects ErrorEvent and falls through to cont
	"""
	builder = make_builder(FunctionId(module="main", name="try_fn", ordinal=0))

	type_table = TypeTable()
	type_table.exception_schemas = {"m:Evt": ("m:Evt", ["msg"])}
	lower = HIRToMIR(builder, type_table=type_table)

	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(
					statements=[
						H.HThrow(
							value=H.HExceptionInit(
								event_fqn="m:Evt",
								pos_args=[],
								kw_args=[
									H.HKwArg(
										name="msg",
										value=H.HLiteralString("boom"),
									)
								],
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
	lower.lower_block(normalize_hir(hir))

	blocks = builder.func.blocks

	# Entry should jump to try_body
	assert blocks["entry"].terminator is not None
	assert isinstance(blocks["entry"].terminator, Goto)
	assert blocks["entry"].terminator.target.startswith("try_body")

	# Try body should build error and jump to dispatch.  Slice 7b
	# (2026-05-05): without a Path-A struct registered for `m:Evt`,
	# the unified Diagnostic owning-throw path falls through to the
	# empty-envelope ConstructError shape, so the field literals
	# ("boom", "msg") aren't materialized.  This test pins try
	# ROUTING (entry → body → dispatch → catch); per-field projection
	# is covered by user-source e2e tests.
	try_body = blocks[blocks["entry"].terminator.target]
	instrs = try_body.instructions
	assert any(isinstance(i, ConstUint64) for i in instrs)
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
	builder = make_builder(FunctionId(module="main", name="try_evt", ordinal=0))

	type_table = TypeTable()
	type_table.exception_schemas = {"m:EvtA": ("m:EvtA", ["msg"])}
	lower = HIRToMIR(builder, type_table=type_table, exc_env={"m:EvtA": 123})

	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(
					statements=[
						H.HThrow(
							value=H.HExceptionInit(
								event_fqn="m:EvtA",
								pos_args=[],
								kw_args=[
									H.HKwArg(
										name="msg",
										value=H.HLiteralString("boom"),
									)
								],
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
	lower.lower_block(normalize_hir(hir))

	blocks = builder.func.blocks
	body = blocks[blocks["entry"].terminator.target]
	dispatch = blocks[body.terminator.target]
	assert isinstance(dispatch.terminator, IfTerminator)
	then_target = dispatch.terminator.then_target
	# then-target should be first catch arm block
	catch_arm_block = blocks[then_target]
	assert any(isinstance(ins, ErrorEvent) for ins in catch_arm_block.instructions)
