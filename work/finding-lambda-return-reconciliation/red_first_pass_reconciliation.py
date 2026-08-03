# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Red first-pass boundary probes for inferred-lambda return reconciliation.

These intentionally live in work/ while K finishes the overlapping #1 patch.
Move/adapt them into lang/tests/type_checker before implementing #2.
"""

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.type_checker import TypeChecker


def _check_direct_call(lam: H.HLambda) -> tuple[TypeTable, object, H.HCall]:
	table = TypeTable()
	call = H.HCall(fn=lam, args=[H.HLiteralBool(value=False)], kwargs=[])
	body = H.HBlock(statements=[H.HExprStmt(expr=call)])
	result = TypeChecker(table).check_function(
		FunctionId(module="main", name="main", ordinal=0),
		body,
	)
	return table, result, call


def _mismatch_messages(result: object) -> list[str]:
	return [
		d.message
		for d in result.diagnostics
		if "does not match inferred lambda return type" in d.message
	]


def test_prefix_return_is_reconciled_with_value_tail_in_primary_typecheck() -> None:
	lam = H.HLambda(
		params=[H.HParam(name="b", type=TypeExpr(name="Bool"))],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HIf(
				cond=H.HVar(name="b"),
				then_block=H.HBlock(statements=[
					H.HReturn(value=H.HLiteralString(value="x")),
				]),
				else_block=None,
			),
			H.HExprStmt(expr=H.HLiteralInt(value=1)),
		]),
	)
	table, result, call = _check_direct_call(lam)
	assert table.get(result.typed_fn.expr_types[call.node_id]).name == "Int"
	assert _mismatch_messages(result) == [
		"return type 'String' does not match inferred lambda return type 'Int'"
	]


def test_all_statement_returns_are_reconciled_in_primary_typecheck() -> None:
	lam = H.HLambda(
		params=[H.HParam(name="b", type=TypeExpr(name="Bool"))],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HIf(
				cond=H.HVar(name="b"),
				then_block=H.HBlock(statements=[
					H.HReturn(value=H.HLiteralInt(value=1)),
				]),
				else_block=H.HBlock(statements=[
					H.HReturn(value=H.HLiteralString(value="x")),
				]),
			),
		]),
	)
	table, result, call = _check_direct_call(lam)
	assert table.get(result.typed_fn.expr_types[call.node_id]).name == "Int"
	assert _mismatch_messages(result) == [
		"return type 'String' does not match inferred lambda return type 'Int'"
	]
