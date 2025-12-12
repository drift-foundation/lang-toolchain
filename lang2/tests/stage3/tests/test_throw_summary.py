# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage3 throw summary aggregation tests.
"""

from lang2.driftc.stage2 import (
	MirFunc,
	BasicBlock,
	ConstInt,
	ConstString,
	ConstructDV,
	ConstructError,
	Goto,
)
from lang2.driftc.stage3 import ThrowSummaryBuilder


def test_throw_summary_records_construct_error_and_exc_types():
	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="c0", value=7),
			ConstructDV(dest="p", dv_type_name="Err", args=[]),
			ConstString(dest="pkey", value="payload"),
			ConstructError(dest="e0", code="c0", payload="p", attr_key="pkey"),
		],
		terminator=Goto(target="exit"),
	)
	exit_block = BasicBlock(name="exit", instructions=[], terminator=None)
	funcs = {
		"f": MirFunc(
			name="f",
			params=[],
			locals=[],
			blocks={"entry": entry, "exit": exit_block},
			entry="entry",
		)
	}

	summaries = ThrowSummaryBuilder().build(funcs, code_to_exc={7: "MyExc"})
	s = summaries["f"]
	assert s.constructs_error is True
	assert ("entry", 1) in s.may_fail_sites
	assert s.exception_types == {"MyExc"}
