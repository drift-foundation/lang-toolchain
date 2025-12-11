from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.checker import FnSignature, Checker
from lang2.driftc.core.types_core import TypeTable
from lang2.driftc.driftc import compile_stubbed_funcs


def _void_sig_table():
	table = TypeTable()
	void_ty = table.ensure_void()
	return table, void_ty


def test_void_function_cannot_return_value():
	table, void_ty = _void_sig_table()
	int_ty = table.ensure_int()
	sigs = {"v": FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False)}
	funcs = {"v": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=1))])}

	checker = Checker(signatures=sigs, hir_blocks=funcs, type_table=table)
	checked = checker.check(funcs.keys())

	assert any("cannot return a value from a Void function" in d.message for d in checked.diagnostics)


def test_non_void_function_must_return_value():
	table = TypeTable()
	int_ty = table.ensure_int()
	sigs = {"f": FnSignature(name="f", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False)}
	funcs = {"f": H.HBlock(statements=[H.HReturn(value=None)])}

	checker = Checker(signatures=sigs, hir_blocks=funcs, type_table=table)
	checked = checker.check(funcs.keys())

	assert any("non-void function must return a value" in d.message for d in checked.diagnostics)


def test_void_call_cannot_be_bound():
	table, void_ty = _void_sig_table()
	sigs = {
		"v": FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False),
		"main": FnSignature(name="main", param_type_ids=[], return_type_id=table.ensure_int(), declared_can_throw=False),
	}
	funcs = {
		"v": H.HBlock(statements=[H.HReturn(value=None)]),
		"main": H.HBlock(statements=[H.HLet(name="x", value=H.HCall(fn=H.HVar(name="v"), args=[])), H.HReturn(value=H.HLiteralInt(value=0))]),
	}

	checker = Checker(signatures=sigs, hir_blocks=funcs, type_table=table)
	checked = checker.check(funcs.keys())

	assert any("cannot bind a Void value" in d.message for d in checked.diagnostics)


def test_void_call_allowed_in_expr_stmt():
	table, void_ty = _void_sig_table()
	int_ty = table.ensure_int()
	sigs = {
		"v": FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False),
		"main": FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	funcs = {
		"v": H.HBlock(statements=[H.HReturn(value=None)]),
		"main": H.HBlock(
			statements=[
				H.HExprStmt(expr=H.HCall(fn=H.HVar(name="v"), args=[])),
				H.HReturn(value=H.HLiteralInt(value=0)),
			]
		),
	}

	_, checked = compile_stubbed_funcs(func_hirs=funcs, signatures=sigs, type_table=table, return_checked=True)

	assert checked.diagnostics == []


def test_void_not_allowed_in_ternary_branches():
	table, void_ty = _void_sig_table()
	int_ty = table.ensure_int()
	sigs = {
		"v": FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False),
		"main": FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	funcs = {
		"v": H.HBlock(statements=[H.HReturn(value=None)]),
		"main": H.HBlock(
			statements=[
				H.HLet(
					name="x",
					value=H.HTernary(
						cond=H.HLiteralBool(value=True),
						then_expr=H.HCall(fn=H.HVar(name="v"), args=[]),
						else_expr=H.HLiteralInt(value=1),
					),
				),
				H.HReturn(value=H.HLiteralInt(value=0)),
			]
		),
	}

	checker = Checker(signatures=sigs, hir_blocks=funcs, type_table=table)
	checked = checker.check(funcs.keys())

	assert any("Void value is not allowed in a ternary expression" in d.message for d in checked.diagnostics)


def test_void_not_allowed_in_array_literal():
	table, void_ty = _void_sig_table()
	int_ty = table.ensure_int()
	sigs = {
		"v": FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False),
		"main": FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	funcs = {
		"v": H.HBlock(statements=[H.HReturn(value=None)]),
		"main": H.HBlock(
			statements=[
				H.HLet(
					name="arr",
					value=H.HArrayLiteral(elements=[H.HCall(fn=H.HVar(name="v"), args=[]), H.HLiteralInt(value=2)]),
				),
				H.HReturn(value=H.HLiteralInt(value=0)),
			]
		),
	}

	checker = Checker(signatures=sigs, hir_blocks=funcs, type_table=table)
	checked = checker.check(funcs.keys())

	assert any("Void value is not allowed in an array literal" in d.message for d in checked.diagnostics)


def test_void_not_allowed_as_try_operand():
	table, void_ty = _void_sig_table()
	sigs = {
		"v": FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False),
		"main": FnSignature(name="main", param_type_ids=[], return_type_id=table.ensure_int(), declared_can_throw=False),
	}
	funcs = {
		"v": H.HBlock(statements=[H.HReturn(value=None)]),
		"main": H.HBlock(
			statements=[
				H.HReturn(value=H.HTryResult(expr=H.HCall(fn=H.HVar(name="v"), args=[]))),
			]
		),
	}

	checker = Checker(signatures=sigs, hir_blocks=funcs, type_table=table)
	checked = checker.check(funcs.keys())

	assert any("try-expression on a non-FnResult operand" in d.message for d in checked.diagnostics)
