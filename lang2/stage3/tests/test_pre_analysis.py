# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage3 (MIR pre-analysis) unit tests.

Currently validates address-taken detection over simple MIR snippets.
"""

from __future__ import annotations

from lang2.stage2 import MirFunc, BasicBlock, AddrOfLocal, LoadLocal, Goto
from lang2.stage3 import MirPreAnalysis


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
	# may_fail is stubbed; should be empty for now.
	assert result.may_fail_instrs == set()
