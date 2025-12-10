#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Signature-driven auto-borrow tests."""

from lang2 import stage1 as H
from lang2.borrow_checker_pass import BorrowChecker
from lang2.borrow_checker import PlaceBase, PlaceKind
from lang2.checker import FnSignature
from lang2.core.types_core import TypeTable, TypeId


def _bc_with_sig(table: TypeTable, sig: FnSignature):
	unk: TypeId = table.ensure_unknown()
	fn_types = {PlaceBase(PlaceKind.LOCAL, 1, "x"): unk}
	base_lookup = lambda hv: PlaceBase(
		PlaceKind.LOCAL,
		getattr(hv, "binding_id", -1) if getattr(hv, "binding_id", None) is not None else -1,
		hv.name if hasattr(hv, "name") else str(hv),
	)
	return BorrowChecker(
		type_table=table,
		fn_types=fn_types,
		binding_types=None,
		base_lookup=base_lookup,
		enable_auto_borrow=True,
		signatures={sig.name: sig},
	)


def test_hcall_signature_driven_auto_borrow_prevents_move():
	# Without auto-borrow, the call would move x (Unknown is move-only) and the later use would be use-after-move.
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralString("s"), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=H.HCall(fn=H.HVar("foo"), args=[H.HVar("x", binding_id=1)])),
			H.HExprStmt(expr=H.HVar("x", binding_id=1)),
		]
	)
	table = TypeTable()
	ref_sig = FnSignature(name="foo", param_type_ids=[table.ensure_ref(table.ensure_unknown())])
	diags = _bc_with_sig(table, ref_sig).check_block(block)
	assert diags == []


def test_hmethod_signature_driven_auto_borrow_prevents_move():
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralString("s"), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=H.HMethodCall(receiver=H.HVar("x", binding_id=1), method_name="m", args=[])),
			H.HExprStmt(expr=H.HVar("x", binding_id=1)),
		]
	)
	table = TypeTable()
	ref_sig = FnSignature(name="m", param_type_ids=[table.ensure_ref(table.ensure_unknown())])
	diags = _bc_with_sig(table, ref_sig).check_block(block)
	assert diags == []
