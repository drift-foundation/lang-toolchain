#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-16
"""
Diagnostics shape tests for the borrow checker.

These tests intentionally do not assert exact line/column values (those depend
on span plumbing in earlier stages). Instead, they lock down the invariant that
borrow-check diagnostics always carry a structured `Span` object, not `None`.
"""

from lang.driftc import stage1 as H
from lang.driftc.borrow_checker_pass import BorrowChecker
from lang.driftc.borrow_checker import PlaceBase, PlaceKind
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeTable


def test_borrowcheck_diagnostics_always_have_spans():
	"""Borrow checker diagnostics must always carry a `Span` (sentinel allowed)."""
	table = TypeTable()
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	bc = BorrowChecker(type_table=table, fn_types={}, base_lookup=base_lookup)

	# Borrowing a literal is a borrow-check error; the diagnostic should carry a
	# structured Span object even though the test HIR has no real source location.
	block = H.HBlock(statements=[H.HExprStmt(expr=H.HBorrow(subject=H.HLiteralInt(1), is_mut=False))])
	diags = bc.check_block(block)
	assert diags, "expected at least one diagnostic"
	assert isinstance(diags[0].span, Span)
