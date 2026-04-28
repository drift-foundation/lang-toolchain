# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import parser as p
from lang.driftc.parser.ast import Lambda, Name, Call


def test_parse_lambda_expr_body() -> None:
	expr = p._parse_expr_fragment("|x: Int| => x")
	assert isinstance(expr, Lambda)
	assert expr.params and expr.params[0].name == "x"
	assert expr.params[0].type_expr is not None
	assert isinstance(expr.body_expr, Name)
	assert expr.body_block is None


def test_parse_lambda_block_body() -> None:
	expr = p._parse_expr_fragment("| | => { 1 }")
	assert isinstance(expr, Lambda)
	assert expr.params == []
	assert expr.ret_type is None
	assert expr.body_expr is None
	assert expr.body_block is not None


def test_parse_lambda_with_returns_expr_body() -> None:
	expr = p._parse_expr_fragment("|x: Int| -> Int => x")
	assert isinstance(expr, Lambda)
	assert expr.params and expr.params[0].name == "x"
	assert expr.ret_type is not None
	assert expr.ret_type.name == "Int"
	assert isinstance(expr.body_expr, Name)
	assert expr.body_block is None


def test_parse_lambda_with_returns_block_body() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
    return (|x: Int| -> Int => { return x; })(1);
}
"""
	)
	fn = prog.functions[0]
	call = fn.body.statements[0].value
	assert isinstance(call, Call)
	assert isinstance(call.func, Lambda)
	expr = call.func
	assert expr.params and expr.params[0].name == "x"
	assert expr.ret_type is not None
	assert expr.ret_type.name == "Int"
	assert expr.body_expr is None
	assert expr.body_block is not None


def test_parse_lambda_with_captures_list() -> None:
	"""Capture list with all five explicit modes — copy / move / share /
	& / &mut.  The bareword form (NAME with no mode keyword) was
	removed in 0.31.22; see `test_parse_lambda_bareword_capture_rejected`."""
	expr = p._parse_expr_fragment("|x: Int| captures (copy i, &mut y, &z, move w, share s) => x")
	assert isinstance(expr, Lambda)
	assert expr.captures is not None
	assert [cap.name for cap in expr.captures] == ["i", "y", "z", "w", "s"]
	assert [cap.kind for cap in expr.captures] == ["copy", "ref_mut", "ref", "move", "share"]


def test_parse_lambda_bareword_capture_rejected() -> None:
	"""Bareword `captures(x)` (no mode keyword) must fail at parse
	time as of 0.31.22.  Pre-0.31.22 it parsed with `kind="auto"`
	and silently lowered to a borrowed-cell capture, producing
	silent runtime miscompiles for escaping closures.  See
	`project_bareword_captures_removed.md` and
	`lang/tests/driver/test_bareword_capture_rejected.py` for the
	end-to-end carriers.

	The parser raises `lark.exceptions.UnexpectedToken` and the
	message lists the available capture-mode tokens
	(`COPY` / `MOVE` / `SHARE` / `AMP`) so the user sees what to
	write instead — same intent as the driver-level
	`test_bareword_capture_single_var_rejected` carrier, but
	pinned at the parser layer.
	"""
	import pytest
	from lark.exceptions import UnexpectedToken
	with pytest.raises(UnexpectedToken) as excinfo:
		p._parse_expr_fragment("|x: Int| captures (x) => x")
	msg = str(excinfo.value)
	# At least three of the four explicit-mode tokens must appear
	# in the parser's "expected one of" list — that's the menu the
	# user picks from to fix the source.
	mode_tokens = ("COPY", "MOVE", "SHARE", "AMP")
	hits = sum(1 for tok in mode_tokens if tok in msg)
	assert hits >= 3, (
		f"parser error must enumerate at least three of "
		f"{mode_tokens} so the user knows the explicit modes; "
		f"got message:\n{msg}"
	)


def test_parse_lambda_with_nothrow_modifier() -> None:
	expr = p._parse_expr_fragment("| | nothrow => 1")
	assert isinstance(expr, Lambda)
	assert expr.declared_nothrow is True
