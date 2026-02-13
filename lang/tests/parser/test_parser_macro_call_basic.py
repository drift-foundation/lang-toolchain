from __future__ import annotations

import pytest
from lark.exceptions import UnexpectedInput

from lang.driftc.parser import parser as p
from lang.driftc.parser.ast import LetStmt, MacroCall, ReturnStmt, Attr, Name


def _parse_main(body: str):
	src = f"""
fn main() -> Int {{
{body}
}}
"""
	return p.parse_program(src)


def test_macro_call_parses_basic() -> None:
	prog = _parse_main("return log.info!(a, b, c);")
	stmt = prog.functions[0].body.statements[0]
	assert isinstance(stmt, ReturnStmt)
	assert isinstance(stmt.value, MacroCall)
	assert len(stmt.value.args) == 3
	assert isinstance(stmt.value.func, Attr)
	assert stmt.value.func.attr == "info"
	assert isinstance(stmt.value.func.value, Name)
	assert stmt.value.func.value.ident == "log"


def test_macro_call_parses_qualified_path() -> None:
	prog = _parse_main("val _ = std.log.debug!(x, y, z); return 0;")
	stmt = prog.functions[0].body.statements[0]
	assert isinstance(stmt, LetStmt)
	assert isinstance(stmt.value, MacroCall)
	assert isinstance(stmt.value.func, Attr)
	assert stmt.value.func.attr == "debug"


@pytest.mark.parametrize(
	"expr",
	[
		"return log.info!;",
		"return log.info!<type Int>(a, b, c);",
	],
)
def test_macro_call_rejects_invalid_shapes(expr: str) -> None:
	with pytest.raises(UnexpectedInput):
		_parse_main(expr)
