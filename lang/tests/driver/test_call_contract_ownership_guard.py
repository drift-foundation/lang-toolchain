# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Anti-regression guard: call-shape validation must stay centralized in call_contract.py.

This test greps for known duplication patterns in checker/ and stage2/ that
indicate ad-hoc call-shape validation has been re-introduced outside the
approved call_contract.py seam.

If this test fails, the offending code should either:
1. Delegate the decision to call_contract.py (keep checker/stage2 message formatting), or
2. Be classified as intentionally out-of-scope with rationale added to work-progress.md.
"""
from __future__ import annotations

import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "driftc")
_CHECKER_DIR = os.path.join(_ROOT, "checker")
_CALL_RESOLVER = os.path.join(_CHECKER_DIR, "call_resolver.py")
_HIR_TO_MIR = os.path.join(_ROOT, "stage2", "hir_to_mir.py")


def _count_pattern(filepath: str, pattern: str) -> list[tuple[int, str]]:
	"""Return (line_number, line) for each match of regex pattern in file."""
	matches = []
	rgx = re.compile(pattern)
	with open(filepath) as f:
		for lineno, line in enumerate(f, 1):
			if rgx.search(line):
				matches.append((lineno, line.rstrip()))
	return matches


def test_no_inline_field_validation_in_checker() -> None:
	"""Checker must not have inline field-name membership/seen-set checks.

	These patterns indicate ctor field validation not delegated to ctor_call_issues().
	"""
	patterns = [
		r"kw\.name\s+not\s+in\s+field_names",
		r"kw\.name\s+in\s+seen",
	]
	for pat in patterns:
		hits = _count_pattern(_CALL_RESOLVER, pat)
		assert hits == [], f"inline field validation in call_resolver.py: {hits}"


def test_no_ad_hoc_kwargs_rejection_in_checker() -> None:
	"""Checker kwargs rejection must delegate decision to call_kwargs_issues().

	Matches: bare `if getattr(expr, "kwargs", None):` followed by a diagnostic
	append (not reading kwargs for CallInfo building). Only lines that are
	already delegated via call_kwargs_issues() are approved.

	Approved pattern: `if call_kwargs_issues(..., getattr(expr, "kwargs", ...)):`
	"""
	hits = _count_pattern(_CALL_RESOLVER, r'^\s+if getattr\(expr, "kwargs", None\)\s*:')
	assert hits == [], f"ad-hoc kwargs rejection in call_resolver.py (should use call_kwargs_issues): {hits}"


def test_no_ad_hoc_kwargs_assertion_in_hir_to_mir() -> None:
	"""hir_to_mir kwargs assertions must delegate decision to call_kwargs_issues().

	Matches: bare `if getattr(expr, "kwargs", None):` followed by AssertionError
	(not reading kwargs for lowering). Already-migrated guards use
	call_kwargs_issues() and are not matched.
	"""
	hits = _count_pattern(_HIR_TO_MIR, r'^\s+if getattr\(expr, "kwargs", None\)\s*:')
	assert hits == [], f"ad-hoc kwargs assertion in hir_to_mir.py (should use call_kwargs_issues): {hits}"
