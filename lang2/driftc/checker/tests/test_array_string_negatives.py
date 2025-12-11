from lang2.driftc import stage1 as H
from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.core.types_core import TypeTable
from lang2.driftc.checker import Checker
from lang2.driftc.checker import FnInfo
from lang2.driftc.checker import FnSignature
from lang2.driftc.stage2 import mir_nodes as M


def _checker_for_hir(block: H.HBlock) -> tuple[Checker, dict[str, FnInfo], list[Diagnostic]]:
	# Minimal checker setup: a single function "main" with inferred signature.
	sig = FnSignature(name="main", return_type_id=None, param_type_ids=[], declared_can_throw=False)
	checker = Checker(signatures={"main": sig}, hir_blocks={"main": block}, type_table=None)
	diagnostics: list[Diagnostic] = []
	checked = checker.check(["main"])
	return checker, checked.fn_infos, diagnostics


def _typing_ctx(
	checker: Checker,
	fn_infos: dict[str, FnInfo],
	diagnostics: list[Diagnostic],
) -> Checker._TypingContext:
	# Build the shared typing context the checker now expects: threaded fn_infos,
	# locals, diagnostics, and a seeded parameter environment.
	ctx = checker._TypingContext(
		checker=checker,
		table=checker._type_table,
		fn_infos=fn_infos,
		current_fn=fn_infos["main"],
		locals={},
		diagnostics=diagnostics,
	)
	checker._seed_locals_from_signature(ctx)
	return ctx


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
	checker, fn_infos, diagnostics = _checker_for_hir(block)
	ctx = _typing_ctx(checker, fn_infos, diagnostics)
	# Run validation passes
	checker._validate_array_exprs(block, ctx)
	assert any("array index must be Int" in d.message for d in diagnostics)


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
	checker, fn_infos, diagnostics = _checker_for_hir(block)
	ctx = _typing_ctx(checker, fn_infos, diagnostics)
	checker._validate_array_exprs(block, ctx)
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
	checker = Checker(signatures={"main": sig}, hir_blocks={"main": block}, type_table=table)
	diagnostics = checker.check(["main"]).diagnostics
	assert any("array index must be Int" in d.message for d in diagnostics)


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
	checker, fn_infos, diagnostics = _checker_for_hir(block)
	ctx = _typing_ctx(checker, fn_infos, diagnostics)
	checker._validate_array_exprs(block, ctx)
	assert any("assignment type mismatch for indexed array element" in d.message for d in diagnostics)
