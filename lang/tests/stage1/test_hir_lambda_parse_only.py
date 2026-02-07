# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.stage1 import AstToHIR, HCall, HLambda
from lang.driftc.stage0 import ast as s0


def test_hir_lower_lambda_immediate_call_structure() -> None:
	expr = s0.Call(
		func=s0.Lambda(
			params=[s0.Param(name="x", type_expr=None, loc=None)],
			body_expr=s0.Name(ident="x"),
			body_block=None,
			loc=None,
		),
		args=[s0.Literal(value=1)],
		kwargs=[],
		loc=None,
	)
	hir = AstToHIR().lower_expr(expr)
	assert isinstance(hir, HCall)
	assert isinstance(hir.fn, HLambda)
	assert hir.fn.params[0].name == "x"
	assert hir.fn.body_expr is not None


def test_hir_lower_lambda_standalone_structural() -> None:
	lam = s0.Lambda(
		params=[],
		body_expr=s0.Literal(value=1),
		body_block=None,
		loc=None,
	)
	hir = AstToHIR().lower_expr(lam)
	assert isinstance(hir, HLambda)
	assert hir.body_expr is not None


def test_hir_lower_lambda_explicit_captures() -> None:
	lam = s0.Lambda(
		params=[],
		captures=[
			s0.CaptureItem(name="a", kind="copy", loc=None),
			s0.CaptureItem(name="b", kind="ref_mut", loc=None),
		],
		body_expr=s0.Name(ident="a"),
		body_block=None,
		loc=None,
	)
	hir = AstToHIR().lower_expr(lam)
	assert isinstance(hir, HLambda)
	assert hir.explicit_captures is not None
	assert [cap.name for cap in hir.explicit_captures] == ["a", "b"]
	assert [cap.kind for cap in hir.explicit_captures] == ["copy", "ref_mut"]
