from lang.driftc.core.types_core import TypeTable, TypeKind
from lang.driftc.core.generic_type_expr import GenericTypeExpr


def test_eval_generic_type_expr_module_qualified_does_not_fallback_to_unique_nominal() -> None:
	table = TypeTable()
	fwd = table.ensure_named("AtomicBool", module_id="std.sync")
	_ = table.declare_struct("lang.atomic", "AtomicBool", ["inner"], [])
	expr = GenericTypeExpr.named("AtomicBool", module_id="std.sync")
	got = table._eval_generic_type_expr(expr, [], module_id="std.log")
	assert got == fwd
	assert table.get(got).kind is TypeKind.FORWARD_NOMINAL
