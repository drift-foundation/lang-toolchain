from lang2 import stage1 as H
from lang2.checker import Checker, FnSignature
from lang2.core.diagnostics import Diagnostic


def _run_checker(block: H.HBlock) -> list[Diagnostic]:
	sig = FnSignature(name="main", return_type_id=None, param_type_ids=[], declared_can_throw=False)
	checker = Checker(signatures={"main": sig}, hir_blocks={"main": block}, type_table=None)
	result = checker.check(["main"])
	return result.diagnostics


def test_string_plus_int_reports_diagnostic() -> None:
	block = H.HBlock(
		statements=[
			H.HReturn(
				value=H.HBinary(
					op=H.BinaryOp.ADD,
					left=H.HLiteralString("a"),
					right=H.HLiteralInt(1),
				)
			)
		]
	)
	diagnostics = _run_checker(block)
	assert any("string binary ops require String operands" in d.message for d in diagnostics)


def test_if_condition_rejects_string() -> None:
	block = H.HBlock(
		statements=[
			H.HIf(
				cond=H.HLiteralString("true"),
				then_block=H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(0))]),
				else_block=None,
			)
		]
	)
	diagnostics = _run_checker(block)
	assert any("if condition must be Bool" in d.message for d in diagnostics)
