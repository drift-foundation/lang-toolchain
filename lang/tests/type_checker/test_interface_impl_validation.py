from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import InterfaceMethodSchema, InterfaceParamSchema, TypeTable
from lang.driftc.impl_index import ImplMeta, ImplMethodMeta
from lang.driftc.parser import ast as parser_ast
from lang.driftc.type_checker import TypeChecker


def _interface_schema() -> tuple[TypeTable, int]:
	table = TypeTable()
	table.ensure_int()
	iface_id = table.declare_interface("m", "I", type_params=[])
	table.define_interface_schema_methods(
		iface_id,
		[
			InterfaceMethodSchema(
				name="call",
				type_params=[],
				params=[
					InterfaceParamSchema(
						name="self",
						type_expr=GenericTypeExpr(
							name="&",
							args=[GenericTypeExpr(name="Self", args=[])],
						),
					),
					InterfaceParamSchema(name="value", type_expr=GenericTypeExpr(name="Int", args=[])),
				],
				return_type=GenericTypeExpr(name="Int", args=[]),
			)
		],
	)
	return table, iface_id


def test_interface_impl_missing_method_reports_diagnostic():
	table, _iface_id = _interface_schema()
	int_ty = table.ensure_int()
	impl = ImplMeta(
		impl_id=0,
		def_module="m",
		target_type_id=int_ty,
		trait_expr=parser_ast.TypeExpr(name="I", module_id="m"),
		methods=[],
	)
	checker = TypeChecker(type_table=table)
	diags: list[object] = []
	checker.validate_interface_impls([impl], signatures_by_id={}, diagnostics=diags)
	assert any(getattr(d, "code", None) == "E_INTERFACE_METHOD_MISSING" for d in diags)


def test_interface_impl_param_mismatch_reports_diagnostic():
	table, _iface_id = _interface_schema()
	int_ty = table.ensure_int()
	bool_ty = table.ensure_bool()
	fn_id = FunctionId(module="m", name="IImpl::call", ordinal=0)
	sig = FnSignature(
		name=fn_id.name,
		param_type_ids=[int_ty, bool_ty],
		return_type_id=int_ty,
		param_names=["self", "value"],
		declared_can_throw=False,
		is_method=True,
		self_mode="ref",
	)
	impl = ImplMeta(
		impl_id=0,
		def_module="m",
		target_type_id=int_ty,
		trait_expr=parser_ast.TypeExpr(name="I", module_id="m"),
		methods=[
			ImplMethodMeta(
				fn_id=fn_id,
				name="call",
				is_pub=True,
			)
		],
	)
	checker = TypeChecker(type_table=table)
	diags: list[object] = []
	checker.validate_interface_impls([impl], signatures_by_id={fn_id: sig}, diagnostics=diags)
	assert any(getattr(d, "code", None) == "E_INTERFACE_METHOD_PARAM_MISMATCH" for d in diags)
