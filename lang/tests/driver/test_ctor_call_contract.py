# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.call_contract import CtorFieldSpec, ctor_call_issues
from lang.driftc.core.span import Span


_POINT = CtorFieldSpec(field_names=("x", "y", "z"))


def test_ctor_call_issues_arity_mismatch_positional() -> None:
	"""Too few positional args → E_CTOR_ARITY_MISMATCH."""
	issues = ctor_call_issues(1, (), _POINT, ctor_label="struct", span=Span())
	assert len(issues) == 1
	assert issues[0].code == "E_CTOR_ARITY_MISMATCH"
	assert "expected 3, got 1" in issues[0].message


def test_ctor_call_issues_unknown_field() -> None:
	"""Named arg not in field list → E_CTOR_UNKNOWN_FIELD."""
	issues = ctor_call_issues(0, ("x", "y", "w"), _POINT, ctor_label="variant", span=Span())
	unknown = [i for i in issues if i.code == "E_CTOR_UNKNOWN_FIELD"]
	assert unknown
	assert "'w'" in unknown[0].message


def test_ctor_call_issues_duplicate_field() -> None:
	"""Same named arg twice → E_CTOR_DUPLICATE_FIELD."""
	issues = ctor_call_issues(0, ("x", "y", "x"), _POINT, ctor_label="struct", span=Span())
	dups = [i for i in issues if i.code == "E_CTOR_DUPLICATE_FIELD"]
	assert dups
	assert "'x'" in dups[0].message


def test_ctor_call_issues_missing_field() -> None:
	"""Gaps in coverage → E_CTOR_MISSING_FIELDS."""
	issues = ctor_call_issues(0, ("x",), _POINT, ctor_label="variant", span=Span())
	missing = [i for i in issues if i.code == "E_CTOR_MISSING_FIELDS"]
	assert missing
	assert "y" in missing[0].message
	assert "z" in missing[0].message


def test_ctor_call_issues_valid_positional_passes() -> None:
	"""Correct positional → empty."""
	issues = ctor_call_issues(3, (), _POINT, ctor_label="struct", span=Span())
	assert issues == []


def test_ctor_call_issues_valid_named_passes() -> None:
	"""Correct named → empty."""
	issues = ctor_call_issues(0, ("x", "y", "z"), _POINT, ctor_label="variant", span=Span())
	assert issues == []
