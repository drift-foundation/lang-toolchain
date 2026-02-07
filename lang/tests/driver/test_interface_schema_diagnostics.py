from lang.driftc import stage1 as H
from lang.driftc.checker import FnSignature
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import InterfaceMethodSchema, InterfaceParamSchema, TypeTable
from lang.driftc.driftc import compile_stubbed_funcs


def test_driver_reports_interface_schema_fixed_width_diagnostic():
	table = TypeTable()
	int_ty = table.ensure_int()
	table.ensure_error()
	iface_id = table.declare_interface("m", "Callback0", type_params=[])
	table.define_interface_schema_methods(
		iface_id,
		[
			InterfaceMethodSchema(
				name="call",
				type_params=[],
				params=[
					InterfaceParamSchema(name="value", type_expr=GenericTypeExpr(name="Int", args=[]))
				],
				return_type=GenericTypeExpr(name="Uint64", args=[]),
			)
		],
	)
	func_hirs = {
		"main": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=0))]),
	}
	signatures = {
		"main": FnSignature(
			name="main",
			param_type_ids=[],
			return_type_id=int_ty,
			declared_can_throw=False,
		),
	}
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=table,
		return_checked=True,
	)
	assert any(d.code == "E_FIXED_WIDTH_RESERVED" for d in checked.diagnostics)


def test_driver_reports_interface_schema_duplicate_param():
	table = TypeTable()
	int_ty = table.ensure_int()
	table.ensure_error()
	iface_id = table.declare_interface("m", "Callback1", type_params=[])
	table.define_interface_schema_methods(
		iface_id,
		[
			InterfaceMethodSchema(
				name="call",
				type_params=[],
				params=[
					InterfaceParamSchema(name="value", type_expr=GenericTypeExpr(name="Int", args=[])),
					InterfaceParamSchema(name="value", type_expr=GenericTypeExpr(name="Int", args=[])),
				],
				return_type=GenericTypeExpr(name="Int", args=[]),
			)
		],
	)
	func_hirs = {
		"main": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=0))]),
	}
	signatures = {
		"main": FnSignature(
			name="main",
			param_type_ids=[],
			return_type_id=int_ty,
			declared_can_throw=False,
		),
	}
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=table,
		return_checked=True,
	)
	assert any(d.code == "E_DUPLICATE_PARAM" for d in checked.diagnostics)


def test_driver_reports_interface_schema_unknown_type():
	table = TypeTable()
	int_ty = table.ensure_int()
	table.ensure_error()
	iface_id = table.declare_interface("m", "CallbackUnknown", type_params=[])
	table.define_interface_schema_methods(
		iface_id,
		[
			InterfaceMethodSchema(
				name="call",
				type_params=[],
				params=[
					InterfaceParamSchema(name="value", type_expr=GenericTypeExpr(name="Unknown", args=[]))
				],
				return_type=GenericTypeExpr(name="Int", args=[]),
			)
		],
	)
	func_hirs = {
		"main": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=0))]),
	}
	signatures = {
		"main": FnSignature(
			name="main",
			param_type_ids=[],
			return_type_id=int_ty,
			declared_can_throw=False,
		),
	}
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=table,
		return_checked=True,
	)
	assert any(d.code == "E_TYPE_UNKNOWN" for d in checked.diagnostics)
