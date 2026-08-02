# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""W0 totality validator pins (reject-redundant-call-borrows).

The typed validator asserts the declared-reference argument policy's
invariant: every SOURCE-WRITTEN borrow surviving typed mode in a call
argument slot carries a policy classification, and never "redundant"
(those were rejected). Constructor targets are outside the rule.
Checker-path drift (a new call family forgetting to classify) surfaces
as an internal diagnostic instead of silently skipping the rule.
"""
from __future__ import annotations

from lang.driftc.checker.typed_validator import validate_typed_hir
from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget


def _mk_call(policy_class: str | None, *, source_written: bool = True, ctor: bool = False):
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int = table.ensure_ref(int_ty)
	place = H.HPlaceExpr(base=H.HVar(name="x"), projections=[], loc=Span())
	borrow = H.HBorrow(subject=place, is_mut=False, source_written=source_written, policy_class=policy_class)
	call = H.HCall(fn=H.HVar(name="f"), args=[borrow])
	call.callsite_id = 1
	if ctor:
		target = CallTarget.constructor(int_ty, "Ctor")
	else:
		target = CallTarget.direct(None)  # type: ignore[arg-type]
	info = CallInfo(target=target, sig=CallSig(param_types=(ref_int,), user_ret_type=int_ty, can_throw=False))
	block = H.HBlock(statements=[H.HExprStmt(expr=call)])
	return block, {1: info}, table


def _validate(block, infos, table) -> list[Diagnostic]:
	res = validate_typed_hir(
		block,
		call_info_by_callsite_id=infos,
		expr_types=None,
		type_table=table,
		tc_diag=lambda **kw: Diagnostic(**kw),
	)
	return [d for d in res.diagnostics if getattr(d, "severity", None) == "error"]


def test_unclassified_source_borrow_is_internal_error() -> None:
	block, infos, table = _mk_call(None)
	errs = _validate(block, infos, table)
	assert any("unclassified source-written borrow" in d.message for d in errs), [d.message for d in errs]


def test_redundant_classified_but_accepted_is_internal_error() -> None:
	block, infos, table = _mk_call("redundant")
	errs = _validate(block, infos, table)
	assert any("REDUNDANT-classified borrow argument was accepted" in d.message for d in errs), [d.message for d in errs]


def test_exempt_and_coercion_classes_pass() -> None:
	for cls in ("exempt", "coercion"):
		block, infos, table = _mk_call(cls)
		errs = _validate(block, infos, table)
		assert not any("W0 checker bug" in d.message for d in errs), (cls, [d.message for d in errs])


def test_surviving_mut_rvalue_binding_is_internal_error() -> None:
	"""MUT_RVALUE_BINDING is a REJECTION class — a borrow carrying it must
	never survive typed mode."""
	block, infos, table = _mk_call("mut_rvalue_binding")
	errs = _validate(block, infos, table)
	assert any("MUT_RVALUE_BINDING-classified borrow argument was accepted" in d.message for d in errs), [d.message for d in errs]



def test_compiler_synthesized_borrow_needs_no_class() -> None:
	block, infos, table = _mk_call(None, source_written=False)
	errs = _validate(block, infos, table)
	assert not any("W0 checker bug" in d.message for d in errs), [d.message for d in errs]


def test_constructor_targets_are_outside_the_rule() -> None:
	block, infos, table = _mk_call(None, ctor=True)
	errs = _validate(block, infos, table)
	assert not any("W0 checker bug" in d.message for d in errs), [d.message for d in errs]
