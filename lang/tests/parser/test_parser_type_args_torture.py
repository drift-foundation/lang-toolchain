# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import fields, is_dataclass

import pytest
from lark.exceptions import UnexpectedInput

from lang.driftc.parser import parser as p
from lang.driftc.parser.ast import (
	AssignStmt,
	AugAssignStmt,
	Call,
	Expr,
	ExprStmt,
	LetStmt,
	QualifiedMember,
	ReturnStmt,
)
from lang.driftc.parser.parser import QualifiedMemberParseError


def _iter_expr(expr: Expr):
	if isinstance(expr, Expr):
		yield expr
	if is_dataclass(expr):
		for f in fields(expr):
			value = getattr(expr, f.name)
			if isinstance(value, Expr):
				yield from _iter_expr(value)
			elif isinstance(value, list):
				for item in value:
					if isinstance(item, Expr):
						yield from _iter_expr(item)
					elif is_dataclass(item):
						for sub in fields(item):
							sub_value = getattr(item, sub.name)
							if isinstance(sub_value, Expr):
								yield from _iter_expr(sub_value)


def _calls_with_type_args(expr: Expr) -> list[Call]:
	return [e for e in _iter_expr(expr) if isinstance(e, Call) and e.type_args]


def _stmt_exprs(stmt):
	if isinstance(stmt, ReturnStmt) and stmt.value is not None:
		yield stmt.value
	elif isinstance(stmt, ExprStmt):
		yield stmt.value
	elif isinstance(stmt, LetStmt):
		yield stmt.value
	elif isinstance(stmt, AssignStmt):
		yield stmt.target
		yield stmt.value
	elif isinstance(stmt, AugAssignStmt):
		yield stmt.target
		yield stmt.value


def _parse_main(body: str):
	src = f"""
fn main() -> Int {{
{body}
}}
"""
	return p.parse_program(src)


def test_comparisons_do_not_create_call_type_args() -> None:
	prog = _parse_main("return a < b > (c);")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	assert not any(_calls_with_type_args(expr) for expr in exprs)

	prog = _parse_main("return a < b && c > d;")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	assert not any(_calls_with_type_args(expr) for expr in exprs)

	prog = _parse_main("return a < b > c;")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	assert not any(_calls_with_type_args(expr) for expr in exprs)


def test_legacy_call_syntax_not_treated_as_type_args() -> None:
	prog = _parse_main("return id<Int>(1);")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	assert not any(_calls_with_type_args(expr) for expr in exprs)


def test_qualified_member_legacy_post_args_not_treated_as_type_args() -> None:
	with pytest.raises(UnexpectedInput):
		p.parse_program(
			"""
variant Optional<T> { Some(value: T), None }
fn main() -> Int { return Optional::None<Int>(); }
"""
		)


def test_call_type_args_with_marker_and_whitespace() -> None:
	prog = _parse_main("return id<type Int>(1);")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	calls = [c for expr in exprs for c in _calls_with_type_args(expr)]
	assert calls and calls[0].type_args[0].name == "Int"

	prog = _parse_main("return id < type Int > ( 1 );")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	calls = [c for expr in exprs for c in _calls_with_type_args(expr)]
	assert calls and calls[0].type_args[0].name == "Int"


def test_call_type_args_with_nested_types() -> None:
	prog = _parse_main("return id<type Array<String>>(x);")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	calls = [c for expr in exprs for c in _calls_with_type_args(expr)]
	assert calls
	assert calls[0].type_args[0].name == "Array"
	assert calls[0].type_args[0].args[0].name == "String"

	prog = _parse_main("return id<type Array<Array<String> > >(x);")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	calls = [c for expr in exprs for c in _calls_with_type_args(expr)]
	assert calls
	assert calls[0].type_args[0].name == "Array"
	assert calls[0].type_args[0].args[0].name == "Array"


def test_call_type_args_with_shift_operator_nearby() -> None:
	prog = _parse_main("val x = a >> b + id<type Array<Array<Int> > >(y); return 0;")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	calls = [c for expr in exprs for c in _calls_with_type_args(expr)]
	assert calls


def test_qualified_member_generics_pre_and_post() -> None:
	prog = p.parse_program(
		"""
variant Optional<T> { Some(value: T), None }
fn main() -> Int { return Optional<Int>::None(); }
"""
	)
	stmt = prog.functions[0].body.statements[0]
	expr = next(_stmt_exprs(stmt))
	call = next(iter(_calls_with_type_args(expr)), None)
	assert call is None
	assert isinstance(expr, Call)
	assert isinstance(expr.func, QualifiedMember)
	assert expr.func.base_type.args[0].name == "Int"

	prog = p.parse_program(
		"""
variant Optional<T> { Some(value: T), None }
fn main() -> Int { return Optional::None<type Int>(); }
"""
	)
	stmt = prog.functions[0].body.statements[0]
	expr = next(_stmt_exprs(stmt))
	calls = _calls_with_type_args(expr)
	assert calls and calls[0].type_args[0].name == "Int"


def test_qualified_member_generics_nested_and_whitespace() -> None:
	prog = p.parse_program(
		"""
variant Optional<T> { Some(value: T), None }
fn main() -> Int { return Optional < Array<Array<String>> > :: None ( ); }
"""
	)
	stmt = prog.functions[0].body.statements[0]
	expr = next(_stmt_exprs(stmt))
	assert isinstance(expr, Call)
	assert isinstance(expr.func, QualifiedMember)
	assert expr.func.base_type.args[0].name == "Array"

	prog = p.parse_program(
		"""
variant Optional<T> { Some(value: T), None }
	fn main() -> Int { return Optional :: None < type Array<Array<String> > > ( ); }
"""
	)
	stmt = prog.functions[0].body.statements[0]
	expr = next(_stmt_exprs(stmt))
	calls = _calls_with_type_args(expr)
	assert calls and calls[0].type_args[0].name == "Array"


@pytest.mark.parametrize(
	"expr",
	[
		"return Optional<Int>::None<type Int>();",
		"return Optional<Array<String>>::None<type Array<String> >();",
		"return Optional<Int>::None<type String>();",
		"return Optional < Int > :: None < type Int > ( );",
	],
)
def test_duplicate_type_args_parse_and_keep_both_channels(expr: str) -> None:
	src = f"""
variant Optional<T> {{ Some(value: T), None }}
fn main() -> Int {{ {expr} }}
"""
	prog = p.parse_program(src)
	stmt = prog.functions[0].body.statements[0]
	call_expr = next(_stmt_exprs(stmt))
	assert isinstance(call_expr, Call)
	assert call_expr.type_args is not None
	assert isinstance(call_expr.func, QualifiedMember)
	assert call_expr.func.base_type.args


def test_postfix_chain_parses_and_keeps_type_args() -> None:
	prog = _parse_main("return a.b<type Int>(x).c(d).e<type String>();")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	calls = [c for expr in exprs for c in _calls_with_type_args(expr)]
	assert len(calls) == 2


def test_type_marker_only_in_call_suffix() -> None:
	prog = _parse_main("return foo(x < y, z);")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	assert not any(_calls_with_type_args(expr) for expr in exprs)

	with pytest.raises(UnexpectedInput):
		p.parse_program("fn main() -> Int { return foo(<type Int>); }")

	prog = _parse_main("return foo(bar < baz > qux);")
	exprs = list(_stmt_exprs(prog.functions[0].body.statements[0]))
	assert not any(_calls_with_type_args(expr) for expr in exprs)


@pytest.mark.parametrize(
	"expr",
	[
		"return id<type>(1);",
		"return id<type Int,>(1);",
		"return id<type Int String>(1);",
		"return id<type Int>(",
		"return Optional::None<type Int>(",
	],
)
def test_call_type_args_error_shapes(expr: str) -> None:
	src = f"fn main() -> Int {{ {expr} }}"
	with pytest.raises(UnexpectedInput):
		p.parse_program(src)


@pytest.mark.parametrize(
	"expr",
	[
		"return id::<Int>(1);",
		"return Optional::None::<Int>();",
		"return Type::Ctor::<Int>();",
	],
)
def test_no_turbofish_anywhere(expr: str) -> None:
	src = f"fn main() -> Int {{ {expr} }}"
	with pytest.raises(UnexpectedInput):
		p.parse_program(src)
