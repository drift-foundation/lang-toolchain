from lang2.driftc import stage1 as H
from lang2.driftc.checker import FnSignature
from lang2.driftc.core.types_core import TypeTable
from lang2.driftc.driftc import compile_stubbed_funcs


def _int_table():
	table = TypeTable()
	int_ty = table.new_scalar("Int")
	bool_ty = table.new_scalar("Bool")
	err_ty = table.new_error("Error")
	table._int_type = int_ty  # type: ignore[attr-defined]
	table._bool_type = bool_ty  # type: ignore[attr-defined]
	table._error_type = err_ty  # type: ignore[attr-defined]
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
	assert mir_funcs  # ensure pipeline ran
	assert any("argument 0 to f has type" in d.message for d in checked.diagnostics)


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


def test_result_ok_without_signature_type_ids_does_not_blow_up():
	func_hirs = {
		"can": H.HBlock(statements=[H.HReturn(value=H.HResultOk(value=H.HLiteralInt(value=1)))]),
	}
	# Signatures empty -> FnInfo.signature is None, _infer_hir_expr_type will synthesize
	# a FnResult<Unknown, Error> TypeId for HResultOk; ensure no crash/diagnostic.
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures={},
		declared_can_throw={"can": True},
		return_checked=True,
	)
	assert mir_funcs
	assert checked.diagnostics == []
