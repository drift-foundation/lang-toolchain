"""
Exercises the checker stub's signature inference and catch-arm validation.
"""

from __future__ import annotations

from lang.driftc.checker import Checker, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.checker.catch_arms import CatchArmInfo
import pytest


def test_checker_infers_fnresult_and_declared_events_from_signature():
	"""Checker should infer can-throw + declared events from FnSignature."""
	from lang.driftc.core.types_core import TypeTable

	table = TypeTable()
	err_ty = table.ensure_error()
	ok_ty = table.ensure_int()
	fnresult_ty = table.ensure_fnresult(ok_ty, err_ty)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	signatures = {
		fn_id: FnSignature(
			name="f",
			return_type=("FnResult", "Ok", "Err"),
			throws_events=("EvtA", "EvtB"),
			param_type_ids=[],
			return_type_id=fnresult_ty,
			error_type_id=err_ty,
		),
	}
	checked = Checker(
		signatures_by_id=signatures,
		hir_blocks_by_id={},
		call_info_by_callsite_id={},
		type_table=table,
	).check_by_id([fn_id])

	info = checked.fn_infos_by_id[fn_id]
	assert info.declared_can_throw is True
	assert info.declared_events == frozenset({"EvtA", "EvtB"})
	assert info.return_type == ("FnResult", "Ok", "Err")
	assert checked.diagnostics == []


def test_checker_validates_catch_arms_and_accumulates_diagnostics():
	"""Checker should run catch-arm validation and accumulate diagnostics."""
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	signatures = {fn_id: FnSignature(name="f", return_type="Int")}
	# Provide a minimal HIR with invalid catch arms so the checker discovers them.
	from lang.driftc import stage1 as H  # local import to avoid circular test deps

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
	checker = Checker(
		signatures_by_id=signatures,
		hir_blocks_by_id={fn_id: hir},
		call_info_by_callsite_id={},
		exception_catalog={"m:Evt": 1},
	)

	checked = checker.check_by_id([fn_id])

	assert checked.diagnostics, "expected diagnostics for invalid catch arms"
	msgs = [diag.message for diag in checked.diagnostics]
	assert any("multiple catch-all" in msg for msg in msgs)
	assert any("catch-all must be the last" in msg for msg in msgs)


def test_checker_method_call_missing_callinfo_is_bug():
	"""Method calls require CallInfo when provided to the checker."""
	from lang.driftc.core.function_id import FunctionId
	from lang.driftc import stage1 as H

	hir = H.HBlock(
		statements=[
			H.HExprStmt(
				expr=H.HMethodCall(
					receiver=H.HVar("p"),
					method_name="bump",
					args=[],
				)
			),
		]
	)
	hir.statements[0].expr.node_id = 1
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	signatures = {fn_id: FnSignature(name="main", return_type="Int")}
	checker = Checker(
		signatures_by_id=signatures,
		hir_blocks_by_id={fn_id: hir},
		call_info_by_callsite_id={fn_id: {}},
	)
	checked = checker.check_by_id([fn_id])
	assert any(
		diag.code == "E_INTERNAL_MISSING_CALLINFO" for diag in checked.diagnostics
	), "expected missing CallInfo diagnostic"


def test_checker_does_not_mutate_node_ids_on_normalized_hir():
	"""Checker should not reassign node ids on already-normalized HIR."""
	from dataclasses import fields, is_dataclass
	from lang.driftc import stage1 as H
	from lang.driftc.stage1.normalize import normalize_hir

	fn_id = FunctionId(module="main", name="main", ordinal=0)
	signatures = {fn_id: FnSignature(name="main", return_type="Int")}
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HCall(fn=H.HVar("foo"), args=[H.HLiteralInt(1)])),
		]
	)
	normalized = normalize_hir(block)
	checker = Checker(
		signatures_by_id=signatures,
		hir_blocks_by_id={fn_id: normalized},
		call_info_by_callsite_id={fn_id: {}},
	)
	checker.check_by_id([fn_id])
	seen: dict[int, str] = {}
	dups: list[tuple[int, str, str]] = []

	def walk(obj: object) -> None:
		if isinstance(obj, H.HExpr):
			node_id = getattr(obj, "node_id", 0)
			kind = type(obj).__name__
			prev = seen.get(node_id)
			if prev is None:
				seen[node_id] = kind
			elif prev != kind:
				dups.append((node_id, prev, kind))
		if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
			return
		if is_dataclass(obj):
			for f in fields(obj):
				walk(getattr(obj, f.name))
			return
		if isinstance(obj, (list, tuple)):
			for item in obj:
				walk(item)
			return
		if isinstance(obj, dict):
			for key in sorted(obj.keys(), key=repr):
				walk(obj[key])
			return

	walk(normalized)
	assert not dups, f"duplicate node_id values detected: {dups[:5]}"
