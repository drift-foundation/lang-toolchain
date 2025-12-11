# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage2 (HIR → MIR) unit tests.

These cover the currently supported lowering:
  - straight-line code (literals/vars/unary/binary/field/index)
  - let/assign/expr/return
  - `if` and `loop` with break/continue
  - basic calls/method calls/DV construction

The tests build HIR by hand and assert on the MIR blocks/instructions emitted
by HIRToMIR + MirBuilder.
"""

from __future__ import annotations

import pytest

from lang2.stage1 import (
	HBlock,
	HLet,
	HAssign,
	HReturn,
	HBinary,
	HVar,
	HLiteralInt,
	HIf,
	HLoop,
	HBreak,
	HContinue,
	HCall,
	HMethodCall,
	HDVInit,
	HExprStmt,
	BinaryOp,
)
from lang2.stage2 import (
	MirBuilder,
	HIRToMIR,
	ConstInt,
	BinaryOpInstr,
	StoreLocal,
	LoadLocal,
	Return,
	IfTerminator,
	Goto,
	MethodCall,
	Call,
	ConstructDV,
)


def _build_and_lower(block: HBlock):
	builder = MirBuilder("test_func")
	lowerer = HIRToMIR(builder)
	lowerer.lower_block(block)
	return builder.func


def test_straight_line():
	# let x = 1; x = x + 2; return x;
	block = HBlock(
		statements=[
			HLet(name="x", value=HLiteralInt(1)),
			HAssign(
				target=HVar("x"),
				value=HBinary(op=BinaryOp.ADD, left=HVar("x"), right=HLiteralInt(2)),
			),
			HReturn(value=HVar("x")),
		]
	)
	func = _build_and_lower(block)
	entry = func.blocks[func.entry]
	ops = entry.instructions
	assert any(isinstance(op, ConstInt) for op in ops)
	assert any(isinstance(op, StoreLocal) for op in ops)
	assert any(isinstance(op, BinaryOpInstr) for op in ops)
	assert isinstance(entry.terminator, Return)


def test_if_lowering():
	# if (x) { x = x + 1; }
	block = HBlock(
		statements=[
			HIf(
				cond=HVar("x"),
				then_block=HBlock(
					statements=[
						HAssign(
							target=HVar("x"),
							value=HBinary(op=BinaryOp.ADD, left=HVar("x"), right=HLiteralInt(1)),
						)
					]
				),
				else_block=None,
			)
		]
	)
	func = _build_and_lower(block)
	assert "if_then" in func.blocks
	assert "if_join" in func.blocks
	entry = func.blocks[func.entry]
	assert isinstance(entry.terminator, IfTerminator)
	then_block = func.blocks["if_then"]
	assert isinstance(then_block.terminator, Goto)


def test_loop_and_break_continue():
	# loop { continue; break; }
	loop_block = HBlock(statements=[HContinue(), HBreak()])
	block = HBlock(statements=[HLoop(body=loop_block)])
	func = _build_and_lower(block)
	assert "loop_header" in func.blocks
	assert "loop_body" in func.blocks
	assert "loop_exit" in func.blocks
	body_term = func.blocks["loop_body"].terminator
	assert isinstance(body_term, Goto)


def test_calls_and_dv():
	# f(1); obj.m(2); MyDV(3)
	block = HBlock(
		statements=[
			HExprStmt(expr=HCall(fn=HVar("f"), args=[HLiteralInt(1)])),
			HExprStmt(expr=HMethodCall(receiver=HVar("obj"), method_name="m", args=[HLiteralInt(2)])),
			HExprStmt(expr=HDVInit(dv_type_name="MyDV", args=[HLiteralInt(3)])),
		]
	)
	func = _build_and_lower(block)
	entry = func.blocks[func.entry]
	kinds = {type(instr) for instr in entry.instructions}
	assert Call in kinds
	assert MethodCall in kinds
	assert ConstructDV in kinds


def test_call_with_non_var_fn_raises():
	call_hir = HCall(fn=HBinary(op=BinaryOp.ADD, left=HLiteralInt(1), right=HLiteralInt(2)), args=[])
	with pytest.raises(NotImplementedError):
		_build_and_lower(HBlock(statements=[HExprStmt(expr=call_hir)]))
