# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.call_contract import (
	ARRAY_METHOD_ARITY_TABLE,
	array_method_arity_issues,
	call_kwargs_issues,
)
from lang.driftc.core.span import Span


def test_array_get_arity_mismatch() -> None:
	"""get with 0 args → issue."""
	issues = array_method_arity_issues("get", 0, span=Span())
	assert len(issues) == 1
	assert issues[0].code == "E_ARRAY_METHOD_ARITY"
	assert "Array.get arity mismatch" in issues[0].message


def test_array_pop_correct_arity() -> None:
	"""pop with 0 args → empty."""
	issues = array_method_arity_issues("pop", 0, span=Span())
	assert issues == []


def test_array_method_table_covers_known_methods() -> None:
	"""All documented array methods must be present."""
	expected = {"get", "ref_at", "pop", "push", "insert", "remove", "swap_remove", "swap", "set", "clear", "reserve", "shrink_to_fit"}
	assert set(ARRAY_METHOD_ARITY_TABLE.keys()) == expected


def test_call_kwargs_issues_rejects_non_empty() -> None:
	"""kwargs present → issue."""
	issues = call_kwargs_issues("method call", [{"name": "x"}], span=Span())
	assert len(issues) == 1
	assert issues[0].code == "E_CALL_KWARGS_REJECTED"
	assert "keyword arguments" in issues[0].message


def test_call_kwargs_issues_empty_passes() -> None:
	"""Empty kwargs → empty."""
	issues = call_kwargs_issues("call", [], span=Span())
	assert issues == []
	issues2 = call_kwargs_issues("call", None, span=Span())
	assert issues2 == []
