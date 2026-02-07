# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""Try/catch validation for catch-all placement."""

from __future__ import annotations

import pytest

from lang.driftc.stage2 import HIRToMIR, mir_nodes as M, make_builder
from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1.normalize import normalize_hir


def test_multiple_catch_all_rejected():
	"""Lowering should reject more than one catch-all arm."""
	builder = make_builder(FunctionId(module="main", name="try_multi_catchall", ordinal=0))
	lower = HIRToMIR(builder)

	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(statements=[]),
				catches=[
					H.HCatchArm(event_fqn=None, binder=None, block=H.HBlock(statements=[])),
					H.HCatchArm(event_fqn=None, binder=None, block=H.HBlock(statements=[])),
				],
			)
		]
	)
	with pytest.raises(RuntimeError):
		lower.lower_block(normalize_hir(hir))


def test_catch_all_not_last_is_rejected():
	"""Catch-all before event arms is rejected to avoid unreachable handlers."""
	builder = make_builder(FunctionId(module="main", name="try_catchall_first", ordinal=0))
	type_table = TypeTable()
	type_table.exception_schemas = {"m:X": ("m:X", [])}
	lower = HIRToMIR(builder, type_table=type_table, exc_env={"m:EvtA": 1})

	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(statements=[H.HThrow(value=H.HExceptionInit(event_fqn="m:X", pos_args=[], kw_args=[]))]),
				catches=[
					H.HCatchArm(event_fqn=None, binder=None, block=H.HBlock(statements=[])),
					H.HCatchArm(event_fqn="m:EvtA", binder="a", block=H.HBlock(statements=[])),
				],
			)
		]
	)
	with pytest.raises(RuntimeError):
		lower.lower_block(normalize_hir(hir))
