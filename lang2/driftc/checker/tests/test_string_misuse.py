from lang2.driftc import stage1 as H
from lang2.driftc.checker import Checker, FnSignature
from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.core.types_core import TypeTable


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


def test_if_condition_rejects_param_string() -> None:
	# Param types should seed locals so condition checks fire on params too.
	table = TypeTable()
	block = H.HBlock(
		statements=[
			H.HIf(
				cond=H.HVar("s"),
				then_block=H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(0))]),
				else_block=None,
			)
		]
	)
	sig = FnSignature(
		name="main",
		param_names=["s"],
		param_type_ids=[table.ensure_string()],
		return_type_id=table.ensure_int(),
		declared_can_throw=False,
	)
	checker = Checker(signatures={"main": sig}, hir_blocks={"main": block}, type_table=table)
	diagnostics = checker.check(["main"]).diagnostics
	assert any("if condition must be Bool" in d.message for d in diagnostics)
