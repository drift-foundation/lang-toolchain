from lang.driftc import stage1 as H
from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.checker import Checker
from lang.driftc.checker import FnSignature
from lang.driftc.stage2 import mir_nodes as M


def _run_checker(block: H.HBlock) -> list[Diagnostic]:
	# Minimal checker setup: a single function "main" with inferred signature.
	# Void return: these fixtures exercise per-statement validators, not
	# return-flow analysis. See test_array_type_checks.py for the same rationale.
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	table = TypeTable()
	sig = FnSignature(
		name="main",
		return_type_id=table.ensure_void(),
		param_type_ids=[],
		declared_can_throw=False,
	)
	checker = Checker(
		signatures_by_id={fn_id: sig},
		hir_blocks_by_id={fn_id: block},
		call_info_by_callsite_id={},
		type_table=table,
	)
	return checker.check_by_id([fn_id]).diagnostics


def test_array_index_with_string_index_reports_diagnostic():
	block = H.HBlock(
		statements=[
			H.HLet(
				name="xs",
				value=H.HArrayLiteral(elements=[H.HLiteralString("a"), H.HLiteralString("b")]),
				declared_type_expr=None,
			),
			H.HReturn(
				value=H.HField(
					subject=H.HIndex(
						subject=H.HVar(name="xs"),
						index=H.HLiteralString("0"),
					),
					name="len",
				)
			),
		]
	)
	diagnostics = _run_checker(block)
	assert any("array index must be an Int" in d.message for d in diagnostics)


def test_array_literal_mixed_element_types_reports_diagnostic():
	block = H.HBlock(
		statements=[
			H.HLet(
				name="xs",
				value=H.HArrayLiteral(elements=[H.HLiteralString("a"), H.HLiteralInt(1)]),
				declared_type_expr=None,
			),
			H.HReturn(value=H.HLiteralInt(0)),
		]
	)
	diagnostics = _run_checker(block)
	assert any("array literal elements do not have a consistent type" in d.message for d in diagnostics)


def test_array_param_index_string_reports_diagnostic():
	# Ensure parameter types seed locals so array index validation sees them.
	table = TypeTable()
	array_of_string = table.new_array(table.ensure_string())
	block = H.HBlock(
		statements=[
			H.HReturn(
				value=H.HIndex(
					subject=H.HVar(name="xs"),
					index=H.HLiteralString("0"),
				)
			)
		]
	)
	sig = FnSignature(
		name="main",
		param_names=["xs"],
		param_type_ids=[array_of_string],
		return_type_id=table.ensure_int(),
		declared_can_throw=False,
	)
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	checker = Checker(
		signatures_by_id={fn_id: sig},
		hir_blocks_by_id={fn_id: block},
		call_info_by_callsite_id={},
		type_table=table,
	)
	diagnostics = checker.check_by_id([fn_id]).diagnostics
	assert any("array index must be an Int" in d.message for d in diagnostics)


def test_array_index_store_type_mismatch_reports_diagnostic():
	block = H.HBlock(
		statements=[
			H.HLet(
				name="xs",
				value=H.HArrayLiteral(elements=[H.HLiteralString("a"), H.HLiteralString("b")]),
				declared_type_expr=None,
			),
			H.HAssign(
				target=H.HIndex(subject=H.HVar(name="xs"), index=H.HLiteralInt(0)),
				value=H.HLiteralBool(True),
			),
		]
	)
	diagnostics = _run_checker(block)
	assert any("assignment type mismatch for indexed array element" in d.message for d in diagnostics)
