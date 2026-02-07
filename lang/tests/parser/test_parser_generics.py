# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.parser import parser as p
from lang.driftc.parser.ast import Binary, Call, LetStmt, ReturnStmt, TypeApp, Name


def test_parse_generic_function_def() -> None:
	prog = p.parse_program(
		"""
fn id<T, U>(value: T, other: U) -> T {
	return value;
}
"""
	)
	assert len(prog.functions) == 1
	fn = prog.functions[0]
	assert fn.type_params == ["T", "U"]


def test_parse_call_with_type_args() -> None:
	prog = p.parse_program(
		"""
fn id<T>(value: T) -> T { return value; }
fn main() -> Int {
	return id<type Int>(1);
}
"""
	)
	stmt = prog.functions[1].body.statements[0]
	assert isinstance(stmt, ReturnStmt)
	assert isinstance(stmt.value, Call)
	assert stmt.value.type_args is not None
	assert stmt.value.type_args[0].name == "Int"


def test_parse_call_type_args_requires_marker() -> None:
	prog = p.parse_program(
		"""
fn id<T>(value: T) -> T { return value; }
fn main() -> Int {
	return id<Int>(1);
}
"""
	)
	stmt = prog.functions[1].body.statements[0]
	assert isinstance(stmt, ReturnStmt)
	assert isinstance(stmt.value, Binary)


def test_parse_type_app_reference() -> None:
	prog = p.parse_program(
		"""
fn id<T>(value: T) -> T { return value; }
fn main() -> Int {
	val f = id<type Int>;
	return f(1);
}
"""
	)
	stmt = prog.functions[1].body.statements[0]
	assert isinstance(stmt, LetStmt)
	assert isinstance(stmt.value, TypeApp)
	assert isinstance(stmt.value.func, Name)
	assert stmt.value.func.ident == "id"
	assert stmt.value.type_args[0].name == "Int"


def test_parse_type_app_duplicate_rejected() -> None:
	with pytest.raises(p.QualifiedMemberParseError) as excinfo:
		p.parse_program(
			"""
fn id<T>(value: T) -> T { return value; }
fn main() -> Int {
	return id<type Int><type String>(1);
}
"""
		)
	msg = str(excinfo.value)
	assert (
		"E-PARSE-TYPEAPP-DUP-TYPEARGS" in msg
		or "E-PARSE-CALL-DUP-TYPEARGS" in msg
	)


def test_parse_trait_method_type_params() -> None:
	prog = p.parse_program(
		"""
trait Show {
	fn show<T>(self: &Self, value: T) -> Int;
}
"""
	)
	assert len(prog.traits) == 1
	tr = prog.traits[0]
	assert len(tr.methods) == 1
	method = tr.methods[0]
	assert method.type_params == ["T"]


def test_parse_nested_type_args_without_space() -> None:
	prog = p.parse_program(
		"""
struct Box<T> { v: T }
fn main() -> Int {
	val xs: Array<Array<Int>> = [[1]];
	return 0;
}
"""
	)
	stmt = prog.functions[0].body.statements[0]
	assert isinstance(stmt, LetStmt)
	assert stmt.type_expr is not None
	assert stmt.type_expr.name == "Array"
	assert stmt.type_expr.args[0].name == "Array"
