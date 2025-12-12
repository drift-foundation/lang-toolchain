# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage3 (MIR pre-analysis) unit tests.

Currently validates address-taken detection over simple MIR snippets.
"""

from __future__ import annotations

from lang2.driftc.stage2 import (
	MirFunc,
	BasicBlock,
	AddrOfLocal,
	LoadLocal,
	Goto,
	Call,
	MethodCall,
	ConstructDV,
	ConstructError,
	ConstInt,
	ConstString,
)
from lang2.driftc.stage3 import MirPreAnalysis


def test_address_taken_detected():
	# Build a tiny MIR function:
	# entry:
	#   %t0 = load_local x
	#   %t1 = addrof x
	#   goto exit
	entry = BasicBlock(
		name="entry",
		instructions=[
			LoadLocal(dest="t0", local="x"),
			AddrOfLocal(dest="t1", local="x"),
		],
		terminator=Goto(target="exit"),
	)
	exit_block = BasicBlock(name="exit", instructions=[], terminator=None)
	func = MirFunc(
		name="test",
		params=[],
		locals=["x"],
		blocks={"entry": entry, "exit": exit_block},
		entry="entry",
	)

	result = MirPreAnalysis().analyze(func)
	assert result.address_taken == {"x"}
	assert result.may_fail == set()


def test_calls_tracked_separately_from_may_fail():
	entry = BasicBlock(
		name="entry",
		instructions=[
			Call(dest="t0", fn="foo", args=[]),
			MethodCall(dest="t1", receiver="obj", method_name="bar", args=[]),
			ConstructDV(dest="t2", dv_type_name="Err", args=[]),
			ConstInt(dest="c0", value=1234),
			ConstString(dest="ename", value="Err"),
			ConstString(dest="pkey", value="payload"),
			ConstructError(dest="t3", code="c0", event_name="ename", payload="t2", attr_key="pkey"),
		],
		terminator=Goto(target="exit"),
	)
	exit_block = BasicBlock(name="exit", instructions=[], terminator=None)
	func = MirFunc(
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry, "exit": exit_block},
		entry="entry",
	)
	result = MirPreAnalysis(code_to_exc={1234: "MyException"}).analyze(func)
	# Calls are tracked separately; only constructors count as may_fail in v1.
	assert ("entry", 0) in result.call_sites
	assert ("entry", 1) in result.call_sites
	assert ("entry", 2) in result.may_fail
	assert ("entry", 6) in result.may_fail
	assert ("entry", 6) in result.construct_error_sites
	assert result.exception_types == {"MyException"}


def test_pure_ops_not_marked():
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="c0", value=0),
			LoadLocal(dest="t0", local="x"),
		],
		terminator=Goto(target="exit"),
	)
	exit_block = BasicBlock(name="exit", instructions=[], terminator=None)
	func = MirFunc(
		name="f",
		params=[],
		locals=["x"],
		blocks={"entry": entry, "exit": exit_block},
		entry="entry",
	)
	result = MirPreAnalysis().analyze(func)
	assert result.may_fail == set()
	assert result.construct_error_sites == set()
