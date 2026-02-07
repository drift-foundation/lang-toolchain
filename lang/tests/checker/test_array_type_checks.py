from lang.driftc import stage1 as H
from lang.driftc.checker import Checker, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable


def _run_checker(func_hir):
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	table = TypeTable()
	checker = Checker(
		signatures_by_id={
			fn_id: FnSignature(
				name="main",
				param_type_ids=[],
				return_type_id=table.ensure_int(),
				declared_can_throw=False,
			)
		},
		hir_blocks_by_id={fn_id: func_hir},
		call_info_by_callsite_id={},
		type_table=table,
	)
	checked = checker.check_by_id([fn_id])
	return checked.diagnostics


def test_array_literal_mismatched_types_reports_diagnostic():
	hir = H.HBlock(
		statements=[
			H.HExprStmt(
				expr=H.HArrayLiteral(
					elements=[
						H.HLiteralInt(value=1),
						H.HLiteralString(value="x"),
					]
				)
			)
		]
	)
	diagnostics = _run_checker(hir)
	assert any("array literal elements do not have a consistent type" in d.message for d in diagnostics)


def test_array_string_literal_with_int_element_reports_diagnostic():
	hir = H.HBlock(
		statements=[
			H.HExprStmt(
				expr=H.HArrayLiteral(
					elements=[
						H.HLiteralString(value="a"),
						H.HLiteralInt(value=1),
					]
				)
			)
		]
	)
	diagnostics = _run_checker(hir)
	assert any("array literal elements do not have a consistent type" in d.message for d in diagnostics)


def test_array_index_requires_int_index():
	hir = H.HBlock(
		statements=[
			H.HExprStmt(
				expr=H.HIndex(
					subject=H.HArrayLiteral(elements=[H.HLiteralInt(value=1), H.HLiteralInt(value=2)]),
					index=H.HLiteralBool(value=True),
				)
			)
		]
	)
	diagnostics = _run_checker(hir)
	assert any("array index must be Int" in d.message for d in diagnostics)


def test_array_string_index_requires_int_index():
	hir = H.HBlock(
		statements=[
			H.HLet(
				name="xs",
				value=H.HArrayLiteral(elements=[H.HLiteralString(value="a"), H.HLiteralString(value="b")]),
			),
			H.HExprStmt(
				expr=H.HIndex(
					subject=H.HVar(name="xs"),
					index=H.HLiteralString(value="0"),
				)
			),
		]
	)
	diagnostics = _run_checker(hir)
	assert any("array index must be Int" in d.message for d in diagnostics)


def test_array_index_assignment_type_mismatch():
	hir = H.HBlock(
		statements=[
			H.HAssign(
				target=H.HIndex(
					subject=H.HArrayLiteral(elements=[H.HLiteralInt(value=1), H.HLiteralInt(value=2)]),
					index=H.HLiteralInt(value=0),
				),
				value=H.HLiteralBool(value=False),
			)
		]
	)
	diagnostics = _run_checker(hir)
	assert any("assignment type mismatch" in d.message for d in diagnostics)


def test_array_literal_and_index_ok_produces_no_diagnostic():
	hir = H.HBlock(
		statements=[
			H.HLet(
				name="xs",
				value=H.HArrayLiteral(elements=[H.HLiteralInt(value=1), H.HLiteralInt(value=2)]),
			),
			H.HExprStmt(expr=H.HIndex(subject=H.HVar(name="xs"), index=H.HLiteralInt(value=0))),
		]
	)
	diagnostics = _run_checker(hir)
	assert diagnostics == []


def test_array_index_store_ok_produces_no_diagnostic():
	hir = H.HBlock(
		statements=[
			H.HAssign(
				target=H.HIndex(
					subject=H.HArrayLiteral(elements=[H.HLiteralInt(value=1), H.HLiteralInt(value=2)]),
					index=H.HLiteralInt(value=1),
				),
				value=H.HLiteralInt(value=42),
			)
		]
	)
	diagnostics = _run_checker(hir)
	assert diagnostics == []


def test_indexing_non_array_reports_diagnostic():
	hir = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HIndex(subject=H.HLiteralInt(value=1), index=H.HLiteralInt(value=0))),
		]
	)
	diagnostics = _run_checker(hir)
	assert any("indexing requires an Array value" in d.message for d in diagnostics)
