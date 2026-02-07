# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import parser as p
from lang.driftc.parser.ast import Lambda, LetStmt


def test_arrow_suffix_in_function_body() -> None:
	# Ensure member-through-ref `->` isn't mis-tokenized as a return type marker.
	prog = p.parse_program(
		"""
fn main() -> Int {
	val p: &Point = point;
	val y = p->x;
	return 0;
}
"""
	)
	assert prog.functions and prog.functions[0].name == "main"
	prog = p.parse_program(
		"""
fn main()->Int { val y=p->x; return 0; }
"""
	)
	assert prog.functions and prog.functions[0].name == "main"

	prog = p.parse_program(
		"""
fn main() -> Int {
	val p: &Point = point;
	val z = p->a->b;
	return 0;
}
"""
	)
	assert prog.functions and prog.functions[0].name == "main"


def test_lambda_return_type_within_function_body() -> None:
	# Ensure lambda return annotations parse correctly inside function bodies.
	prog = p.parse_program(
		"""
fn main() -> Int {
	val f = |x: Int| -> Int => x;
	return f(1);
}
"""
	)
	let_stmt = next(s for s in prog.functions[0].body.statements if isinstance(s, LetStmt))
	lambda_expr = let_stmt.value
	assert isinstance(lambda_expr, Lambda)
	assert lambda_expr.ret_type is not None
	assert lambda_expr.ret_type.name == "Int"

	prog = p.parse_program(
		"""
fn main() -> Int {
	val f: Fn(Int)->Fn(Int)->Int = make();
	return 0;
}
"""
	)
	assert prog.functions and prog.functions[0].name == "main"

	prog = p.parse_program(
		"""
fn main() -> Int {
	val g: Fn(Fn(Int)->Int)->Int = make();
	return 0;
}
"""
	)
	assert prog.functions and prog.functions[0].name == "main"
