from lang.driftc import stage1 as H
from lang.driftc.checker import FnSignature
from lang.driftc.core.types_core import TypeTable
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.parser import ast as parser_ast


def _int_table():
	table = TypeTable()
	int_ty = table.ensure_int()
	bool_ty = table.ensure_bool()
	table.ensure_error()
	return table, int_ty, bool_ty


def test_call_type_mismatch_reports_diagnostic():
	table, int_ty, bool_ty = _int_table()
	func_hirs = {
		"f": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=0))]),
		"main": H.HBlock(statements=[H.HReturn(value=H.HCall(fn=H.HVar(name="f"), args=[H.HLiteralBool(value=True)]))]),
	}
	signatures = {
		"f": FnSignature(name="f", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False),
		"main": FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=table,
		return_checked=True,
	)
	assert any("no matching overload for function 'f'" in d.message for d in checked.diagnostics)


def test_call_type_match_has_no_diagnostic():
	table, int_ty, _bool_ty = _int_table()
	func_hirs = {
		"f": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=0))]),
		"main": H.HBlock(statements=[H.HReturn(value=H.HCall(fn=H.HVar(name="f"), args=[H.HLiteralInt(value=1)]))]),
	}
	signatures = {
		"f": FnSignature(name="f", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False),
		"main": FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=table,
		return_checked=True,
	)
	assert checked.diagnostics == []


def test_lambda_explicit_return_type_mismatch_reports_diagnostic():
	table, int_ty, _bool_ty = _int_table()
	table.ensure_string()
	lam = H.HLambda(
		params=[H.HParam(name="x", binding_id=1)],
		ret_type=parser_ast.TypeExpr(name="Int"),
		body_expr=H.HLiteralString(value="hi"),
		body_block=None,
	)
	func_hirs = {
		"main": H.HBlock(statements=[H.HReturn(value=H.HCall(fn=lam, args=[H.HLiteralInt(value=1)]))]),
	}
	signatures = {
		"main": FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=table,
		return_checked=True,
	)
	# 0.34.2: the type-checker's shared return-value authority diagnoses the
	# lambda-tail mismatch.  The checker's raw-equality body re-inference is
	# DELETED entirely — a lambda call without CallInfo is a contract failure,
	# never a re-typing fallback.
	assert any("return type 'String' does not match declared type 'Int'" in d.message for d in checked.diagnostics)
