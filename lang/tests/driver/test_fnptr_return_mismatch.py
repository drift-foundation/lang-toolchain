# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from lang.driftc import stage1 as H
from lang.driftc.checker import FnSignature
from lang.driftc.core.types_core import TypeTable
from lang.driftc.driftc import compile_stubbed_funcs


def _int_table():
	table = TypeTable()
	int_ty = table.ensure_int()
	table.ensure_error()
	return table, int_ty


def test_fnptr_return_nothrow_rejects_can_throw():
	table, int_ty = _int_table()
	fnptr_nothrow = table.ensure_function([int_ty], int_ty, can_throw=False)
	func_hirs = {
		"boom": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=1))]),
		"make_bad": H.HBlock(statements=[H.HReturn(value=H.HVar(name="boom"))]),
	}
	signatures = {
		"boom": FnSignature(name="boom", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=True),
		"make_bad": FnSignature(
			name="make_bad",
			param_type_ids=[],
			return_type_id=fnptr_nothrow,
			declared_can_throw=False,
		),
	}
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=table,
		return_checked=True,
	)
	assert any("no overload of 'boom' matches function type" in d.message for d in checked.diagnostics)


def test_fnptr_return_can_throw_allows_nothrow():
	table, int_ty = _int_table()
	fnptr_can_throw = table.ensure_function([int_ty], int_ty, can_throw=True)
	func_hirs = {
		"add1": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=1))]),
		"make_ok": H.HBlock(statements=[H.HReturn(value=H.HVar(name="add1"))]),
	}
	signatures = {
		"add1": FnSignature(name="add1", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False),
		"make_ok": FnSignature(
			name="make_ok",
			param_type_ids=[],
			return_type_id=fnptr_can_throw,
			declared_can_throw=False,
		),
	}
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=table,
		return_checked=True,
	)
	assert checked.diagnostics == []
