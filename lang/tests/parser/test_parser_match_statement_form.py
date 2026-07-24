# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Parser→stage0 boundary pin for `MatchExpr.statement_form`.

The grammar has TWO match productions — `match_expr` (value_block arms:
every arm ends with a bare trailing expression) and `match_stmt` (plain
statement-block arms) — and the production name is the SOLE authority
for the classification (`_build_match_expr` derives it from
`_name(tree)` and rejects any other production).  Erasing this flag at
the parser→stage0 boundary caused the reload-coordinator regression:
AST→HIR force-value-contexted a statement-form lambda-tail match and
the checker rejected its return-exiting arms with E-MATCH-NO-VALUE.

Pinned here:
  * `match_expr` (expression position) parses with statement_form == False;
  * `match_stmt` (statement position) parses with statement_form == True;
  * the stage0 conversion preserves BOTH values.

The end-to-end companion is the coordinator-shaped compile/run pin in
lang/tests/driver/test_lambda_trailing_match_value.py (positive pin 7)
plus test_reload_coordinator itself.
"""
from __future__ import annotations

from lang.driftc.parser import _convert_expr
from lang.driftc.parser import ast as parser_ast
from lang.driftc.parser import parser as p
from lang.driftc.stage0 import ast as s0

_SOURCE = """
fn classify(n: Int) -> Int {
	// statement position, block arms exiting via return: match_stmt.
	match n > 0 {
		true => { return 1; },
		false => { return 0; }
	}
}

fn pick(n: Int) -> Int {
	// expression position, value_block arms: match_expr.
	return match n > 0 { true => { 1 }, false => { 0 }, };
}
"""


def _parse_fns() -> tuple[parser_ast.MatchExpr, parser_ast.MatchExpr]:
	prog = p.parse_program(_SOURCE)
	by_name = {fn.name: fn for fn in prog.functions}

	stmt = by_name["classify"].body.statements[-1]
	assert isinstance(stmt, parser_ast.ExprStmt), stmt
	stmt_match = stmt.value
	assert isinstance(stmt_match, parser_ast.MatchExpr), stmt_match

	ret = by_name["pick"].body.statements[-1]
	assert isinstance(ret, parser_ast.ReturnStmt), ret
	expr_match = ret.value
	assert isinstance(expr_match, parser_ast.MatchExpr), expr_match
	return stmt_match, expr_match


def test_parser_records_statement_form() -> None:
	stmt_match, expr_match = _parse_fns()
	assert stmt_match.statement_form is True, (
		"match_stmt production must parse with statement_form=True"
	)
	assert expr_match.statement_form is False, (
		"match_expr production must parse with statement_form=False"
	)


def test_stage0_conversion_preserves_statement_form() -> None:
	stmt_match, expr_match = _parse_fns()
	s0_stmt = _convert_expr(stmt_match)
	s0_expr = _convert_expr(expr_match)
	assert isinstance(s0_stmt, s0.MatchExpr) and isinstance(s0_expr, s0.MatchExpr)
	assert s0_stmt.statement_form is True, (
		"conversion must preserve statement_form=True"
	)
	assert s0_expr.statement_form is False, (
		"conversion must preserve statement_form=False"
	)
