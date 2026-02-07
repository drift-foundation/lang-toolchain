from lang.driftc.core.function_id import FunctionId
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage3 throw summary aggregation tests.
"""

from lang.driftc.stage2 import (
	MirFunc,
	BasicBlock,
	ConstInt,
	ConstString,
	ConstructDV,
	ConstructError,
	Goto,
)
from lang.driftc.stage3 import ThrowSummaryBuilder


def test_throw_summary_records_construct_error_and_exc_types():
	entry = BasicBlock(
		name="entry",
	instructions=[
		ConstInt(dest="c0", value=7),
		ConstructDV(dest="p", dv_type_name="Err", args=[]),
		ConstString(dest="ename", value="Err"),
		ConstString(dest="pkey", value="payload"),
		ConstructError(dest="e0", code="c0", event_fqn="m:Err", payload="p", attr_key="pkey"),
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
	assert ("entry", 1) in s.may_fail_sites
	assert s.exception_types == {"MyExc"}
