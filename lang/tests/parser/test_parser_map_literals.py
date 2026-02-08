from lang.driftc.parser import parser as p
from lang.driftc.parser import ast as parser_ast


def test_parse_map_literal_in_let() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
	val attrs = { user: 1, reason: 2 };
	return 0;
}
"""
	)
	stmt = prog.functions[0].body.statements[0]
	assert isinstance(stmt, parser_ast.LetStmt)
	assert isinstance(stmt.value, parser_ast.MapLiteral)
	assert [entry.key for entry in stmt.value.entries] == ["user", "reason"]


def test_parse_map_literal_string_keys() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
	val attrs = { "user": 1, "reason": 2, };
	return 0;
}
"""
	)
	stmt = prog.functions[0].body.statements[0]
	assert isinstance(stmt, parser_ast.LetStmt)
	assert isinstance(stmt.value, parser_ast.MapLiteral)
	assert [entry.key for entry in stmt.value.entries] == ["user", "reason"]


def test_parse_empty_map_literal_colon_form() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
	val attrs = {:};
	return 0;
}
"""
	)
	stmt = prog.functions[0].body.statements[0]
	assert isinstance(stmt, parser_ast.LetStmt)
	assert isinstance(stmt.value, parser_ast.MapLiteral)
	assert stmt.value.entries == []
