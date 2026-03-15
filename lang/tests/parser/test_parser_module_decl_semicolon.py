# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: module declarations require a trailing semicolon.

`module main;` is the canonical form.  `module main` (without semicolon)
is a parse error, consistent with import, use, and other declaration-level
statements.
"""
from __future__ import annotations

import pytest
from lark.exceptions import UnexpectedToken

from lang.driftc.parser import parser as p


def test_module_decl_with_semicolon() -> None:
	"""module main; must parse successfully."""
	prog = p.parse_program("module main;\n\nfn main() nothrow -> Int { return 0; }\n")
	assert prog is not None
	assert prog.module == "main"


def test_module_decl_dotted_with_semicolon() -> None:
	"""module net.tls; must parse successfully."""
	prog = p.parse_program("module net.tls;\n\nfn main() nothrow -> Int { return 0; }\n")
	assert prog is not None
	assert prog.module == "net.tls"


def test_module_decl_without_semicolon_is_rejected() -> None:
	"""module main (no semicolon) must be a parse error."""
	with pytest.raises(UnexpectedToken):
		p.parse_program("module main\nfn main() nothrow -> Int { return 0; }\n")
