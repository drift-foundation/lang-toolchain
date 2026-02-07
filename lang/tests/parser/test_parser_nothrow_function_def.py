# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import parser as p


def test_parse_function_def_nothrow() -> None:
	prog = p.parse_program(
		"""
fn add1(x: Int) nothrow -> Int { return x + 1; }
"""
	)
	assert len(prog.functions) == 1
	assert prog.functions[0].declared_nothrow is True
