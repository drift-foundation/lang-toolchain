from lang.driftc import stage1 as H
from lang.driftc.checker import Checker, FnSignature
from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable


def _run_checker(block: H.HBlock) -> list[Diagnostic]:
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	table = TypeTable()
	# Void return: these fixtures exercise per-statement validators, not
	# return-flow analysis. See test_array_type_checks.py for the same rationale.
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
	result = checker.check_by_id([fn_id])
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
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	checker = Checker(
		signatures_by_id={fn_id: sig},
		hir_blocks_by_id={fn_id: block},
		call_info_by_callsite_id={},
		type_table=table,
	)
	diagnostics = checker.check_by_id([fn_id]).diagnostics
	assert any("if condition must be Bool" in d.message for d in diagnostics)
