# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage3 (MIR pre-analysis) unit tests.

Currently validates address-taken detection over simple MIR snippets.
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import (
	MirFunc,
	BasicBlock,
	AddrOfLocal,
	LoadLocal,
	Goto,
	Call,
	ConstructError,
	ConstInt,
	ConstString,
	ExcSetParamsJson,
)
from lang.driftc.stage3 import MirPreAnalysis


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
		fn_id=FunctionId(module="main", name="test", ordinal=0),
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
	# Slice 7c-2 (ABI 14, 2026-05-06) update: dropped the
	# `ConstructDV(...)` instruction that fed the legacy
	# `ConstructError(payload=DV, attr_key=...)` shape.  At ABI 14
	# the throw lowering emits `ConstructError(payload=None,
	# attr_key=None)` followed by `ExcSetParamsJson(json_text)` —
	# the same shape pre-analysis sees in production lowering.
	entry = BasicBlock(
		name="entry",
		instructions=[
			Call(dest="t0", fn_id=FunctionId(module="main", name="foo", ordinal=0), args=[], can_throw=False),
			ConstInt(dest="c0", value=1234),
			ConstString(dest="ename", value="Err"),
			ConstString(dest="params", value="{}"),
			ConstructError(dest="t3", code="c0", event_fqn="ename", payload=None, attr_key=None),
			ExcSetParamsJson(error="t3", json_text="params"),
		],
		terminator=Goto(target="exit"),
	)
	exit_block = BasicBlock(name="exit", instructions=[], terminator=None)
	func = MirFunc(
		fn_id=FunctionId(module="main", name="f", ordinal=0),
		name="f",
		params=[],
		locals=[],
		blocks={"entry": entry, "exit": exit_block},
		entry="entry",
	)
	result = MirPreAnalysis(code_to_exc={1234: "MyException"}).analyze(func)
	# Calls are tracked separately; only constructors count as may_fail in v1.
	assert ("entry", 0) in result.call_sites
	# ConstructError is the only may-fail site at index 4.
	assert ("entry", 4) in result.may_fail
	assert ("entry", 4) in result.construct_error_sites
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
		fn_id=FunctionId(module="main", name="f", ordinal=0),
		name="f",
		params=[],
		locals=["x"],
		blocks={"entry": entry, "exit": exit_block},
		entry="entry",
	)
	result = MirPreAnalysis().analyze(func)
	assert result.may_fail == set()
	assert result.construct_error_sites == set()
