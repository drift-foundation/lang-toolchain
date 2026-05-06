from lang.driftc.core.function_id import FunctionId
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage3 throw summary aggregation tests.

Slice 7c-2 (ABI 14, 2026-05-06) update: `ConstructDV` is deleted
along with the rest of the DV substrate.  This test now uses the
ABI 14 throw shape — `ConstructError(payload=None, attr_key=None)`
plus `ExcSetParamsJson(json_text)` for the params projection —
which is what production lowering emits post-Slice 7b.
"""

from lang.driftc.stage2 import (
	MirFunc,
	BasicBlock,
	ConstInt,
	ConstString,
	ConstructError,
	ExcSetParamsJson,
	Goto,
)
from lang.driftc.stage3 import ThrowSummaryBuilder


def test_throw_summary_records_construct_error_and_exc_types():
	entry = BasicBlock(
		name="entry",
	instructions=[
		ConstInt(dest="c0", value=7),
		ConstString(dest="ename", value="Err"),
		ConstString(dest="params", value="{}"),
		ConstructError(dest="e0", code="c0", event_fqn="m:Err", payload=None, attr_key=None),
		ExcSetParamsJson(error="e0", json_text="params"),
	],
		terminator=Goto(target="exit"),
	)
	exit_block = BasicBlock(name="exit", instructions=[], terminator=None)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	funcs = {
		fn_id: MirFunc(
			fn_id=fn_id,
			name="f",
			params=[],
			locals=[],
			blocks={"entry": entry, "exit": exit_block},
			entry="entry",
		)
	}

	summaries = ThrowSummaryBuilder().build(funcs, code_to_exc={7: "MyExc"})
	s = summaries[fn_id]
	assert s.constructs_error is True
	# ConstructError is at index 3 (after the two ConstString preambles).
	assert ("entry", 3) in s.may_fail_sites
	assert s.exception_types == {"MyExc"}
