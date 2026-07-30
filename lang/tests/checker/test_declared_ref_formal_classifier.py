# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Table-driven pins for the SHARED W0 declaration-origin classifier
(`declared_ref_formal`) and mask builder (`build_declared_ref_mask`) —
every call family routes formal classification through these; a wrong
False is accepted as EXEMPT, so these pins are the validator's blind
spot coverage (implementation review round 3, finding 1).
"""
from __future__ import annotations

import pytest

from lang.driftc.checker.call_resolver import build_declared_ref_mask, declared_ref_formal
from lang.driftc.core.types_core import TypeTable
from lang.driftc.parser import ast as parser_ast


class _Te:
	"""Minimal type-expr stand-in (name/args/param_index)."""

	def __init__(self, name=None, args=None, param_index=None):
		self.name = name
		self.args = args or []
		self.param_index = param_index


@pytest.fixture()
def tt() -> TypeTable:
	return TypeTable()


def test_table(tt: TypeTable) -> None:
	string = tt.ensure_string()
	ref_string = tt.ensure_ref(string)
	refmut_string = tt.ensure_ref_mut(string)
	generics = frozenset({"T", "V", "Self"})
	cases = [
		# (label, type_expr, resolved, expect)
		("concrete &String", _Te("&", [_Te("String")]), ref_string, True),
		("concrete &mut String", _Te("&mut", [_Te("String")]), refmut_string, True),
		("alias resolving to a reference (D6)", _Te("Handle"), ref_string, True),
		("ref-rooted generic &T instantiated", _Te("&", [_Te("T")]), refmut_string, True),
		("bare generic T instantiated at &String", _Te("T"), ref_string, False),
		("bare generic V instantiated at &mut", _Te("V"), refmut_string, False),
		("interface param_index node at a ref", _Te(None, param_index=0), ref_string, False),
		# LANGUAGE_BUG #5: param_index is AUTHORITATIVE — producers differ in
		# what they leave in `name` (builtin Callback* schemas carry "";
		# others may carry a residual name). All must stay exempt.
		("builtin-schema param_index node (empty-string name) at a ref", _Te("", param_index=0), ref_string, False),
		("param_index node with residual name NOT in generic set", _Te("A", param_index=0), ref_string, False),
		("no syntax available, resolved REF", None, ref_string, True),
		("no syntax available, resolved value", None, string, False),
		("by-value String", _Te("String"), string, False),
		("unresolved", _Te("&", [_Te("String")]), None, False),
	]
	for label, te, pid, expect in cases:
		got = declared_ref_formal(te, pid, tt, generic_param_names=generics)
		assert got is expect, f"{label}: expected {expect}, got {got}"


def test_builder_receiver_exclusion(tt: TypeTable) -> None:
	string = tt.ensure_string()
	ref_string = tt.ensure_ref(string)
	mask = build_declared_ref_mask(
		[_Te("&", [_Te("Self")]), _Te("&", [_Te("String")])],
		[ref_string, ref_string],
		tt,
		generic_param_names=frozenset({"Self"}),
		param_names=["self", "rec"],
	)
	assert mask == [False, True], mask


def test_builder_without_names_keeps_slot0(tt: TypeTable) -> None:
	"""Receiver exclusion keys on the `self` NAME — a free fn whose first
	param happens to be a reference is still declared."""
	string = tt.ensure_string()
	ref_string = tt.ensure_ref(string)
	mask = build_declared_ref_mask(
		[_Te("&", [_Te("String")])],
		[ref_string],
		tt,
		param_names=["arg"],
	)
	assert mask == [True], mask
