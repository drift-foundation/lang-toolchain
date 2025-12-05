# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Integration: stage2 → stage3 → stage4 throw checks.
Ensures the throw summary plus declared_can_throw map drive the invariants.
"""

from __future__ import annotations

import pytest

from lang2 import stage1 as H
from lang2.stage2 import HIRToMIR, MirBuilder
from lang2.stage3.throw_summary import ThrowSummaryBuilder
from lang2.stage4 import run_throw_checks


def _lower_fn(name: str, hir_block: H.HBlock) -> tuple[str, object]:
	builder = MirBuilder(name=name)
	HIRToMIR(builder).lower_block(hir_block)
	return name, builder.func


def test_can_throw_function_passes_checks():
	"""A declared can-throw function that throws should pass all invariants."""
	fn_name, mir_fn = _lower_fn(
		"f_can",
		H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="X", args=[]))]),
	)
	mir_funcs = {fn_name: mir_fn}
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc={})
	declared_can_throw = {fn_name: True}

	infos = run_throw_checks(mir_funcs, summaries, declared_can_throw)
	assert fn_name in infos
	assert infos[fn_name].declared_can_throw is True


def test_non_can_throw_function_violates_invariant():
	"""A function that throws but is not declared can-throw should fail checks."""
	fn_name, mir_fn = _lower_fn(
		"f_non_can",
		H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="X", args=[]))]),
	)
	mir_funcs = {fn_name: mir_fn}
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc={})
	declared_can_throw = {fn_name: False}

	with pytest.raises(RuntimeError):
		run_throw_checks(mir_funcs, summaries, declared_can_throw)
