# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import parser as p


def test_parse_use_trait_records_trait_ref() -> None:
	prog = p.parse_program(
		"""
module m_main;

use trait m_traits.Show;

fn main() -> Int { return 0; }
"""
	)
	assert len(prog.used_traits) == 1
	tr = prog.used_traits[0]
	assert tr.module_path == ["m_traits"]
	assert tr.name == "Show"
