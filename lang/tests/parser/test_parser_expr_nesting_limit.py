# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Robustness regression: deeply nested parenthesized expressions must not
crash the parser.

Surfaced by `work/robustness/robustness-matrix.md` row #3: deeply nested
`(((((1)))))` expression input crashes driftc with Python `RecursionError`
in the parser builder (`_build_postfix` → `_build_expr` → `_build_postfix`
recursion, ~3 stack frames per source level).

The fix is a parser-level expression-nesting depth guard
(`PARSER_MAX_EXPR_NESTING_DEPTH = 256`, default same as the block guard)
that emits a clean Drift diagnostic (`expression nesting depth exceeds N`)
instead of a Python traceback.

This file pins the boundary at 100/255/256/257/1500 — same shape as the
block-nesting boundary regression added for row #1.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_to_hir


def _gen_nested_paren_expr(n: int) -> str:
	expr = "1"
	for _ in range(n):
		expr = "(" + expr + ")"
	return f"module main;\npub fn main() nothrow -> Int {{\n\treturn {expr};\n}}\n"


def _parse(src: str, tmp_path: Path, name: str) -> list:
	path = tmp_path / f"{name}.drift"
	path.write_text(src)
	_m, _t, _e, diags = parse_drift_to_hir(path)
	return [d for d in diags if d.severity == "error"]


def test_moderate_nested_paren_expr_compiles(tmp_path: Path) -> None:
	"""100 nested parens must parse cleanly (well below limit)."""
	errors = _parse(_gen_nested_paren_expr(100), tmp_path, "moderate")
	assert not errors, f"unexpected errors at d=100: {[d.message for d in errors]}"


def test_expr_nesting_limit_just_below_compiles(tmp_path: Path) -> None:
	"""255 nested parens must compile cleanly (one below the published limit)."""
	errors = _parse(_gen_nested_paren_expr(255), tmp_path, "d255")
	assert not errors, f"unexpected errors at d=255: {[d.message for d in errors]}"


def test_expr_nesting_limit_at_published_limit_compiles(tmp_path: Path) -> None:
	"""Exactly 256 nested parens must compile cleanly.

	This pins the published contract: 256 nested expression levels are
	allowed. Same boundary discipline as the block-nesting guard (row #1).
	"""
	errors = _parse(_gen_nested_paren_expr(256), tmp_path, "d256")
	assert not errors, f"unexpected errors at d=256: {[d.message for d in errors]}"


def test_expr_nesting_limit_one_above_emits_clean_diagnostic(tmp_path: Path) -> None:
	"""257 nested parens must produce a structured diagnostic, not a crash."""
	errors = _parse(_gen_nested_paren_expr(257), tmp_path, "d257")
	assert errors, "expected error diagnostic at d=257"
	assert any(
		"expression nesting" in d.message.lower() or "expression nesting depth" in d.message.lower()
		for d in errors
	), f"no expression-nesting diagnostic; got: {[d.message for d in errors]}"


def test_deep_nested_paren_expr_does_not_crash(tmp_path: Path) -> None:
	"""500+ nested parens must still produce a structured diagnostic.

	Sanity that the guard works at depths well past the limit, not just
	at the boundary.
	"""
	errors = _parse(_gen_nested_paren_expr(1500), tmp_path, "deep")
	assert errors
	assert any(
		"expression nesting" in d.message.lower()
		for d in errors
	)
