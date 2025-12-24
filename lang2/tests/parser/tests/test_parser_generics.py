# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang2.driftc.parser import parser as p
from lang2.driftc.parser.ast import Binary, Call, LetStmt, ReturnStmt, TypeApp, Name


def test_parse_generic_function_def() -> None:
	prog = p.parse_program(
		"""
fn id<T, U>(value: T, other: U) returns T {
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
fn id<T>(value: T) returns T { return value; }
fn main() returns Int {
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
fn id<T>(value: T) returns T { return value; }
fn main() returns Int {
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
fn id<T>(value: T) returns T { return value; }
fn main() returns Int {
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
fn id<T>(value: T) returns T { return value; }
fn main() returns Int {
	return id<type Int><type String>(1);
}
"""
		)
	msg = str(excinfo.value)
	assert (
		"E-PARSE-TYPEAPP-DUP-TYPEARGS" in msg
		or "E-PARSE-CALL-DUP-TYPEARGS" in msg
	)
