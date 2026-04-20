# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Robustness regression: deeply nested blocks must not crash the parser builder.

Surfaced by a robustness audit: deeply nested
`{ { { ... } } }` block input crashes driftc with Python `RecursionError` in
the parser AST builder (`_build_stmt` → `_build_block` → `_build_stmt` ...).

The fix is a parser-level nesting-depth guard that emits a clean Drift
diagnostic instead of letting the Python recursion limit surface as a
traceback.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_to_hir


def _gen_nested_blocks(n: int) -> str:
	body = "return 0;"
	for _ in range(n):
		body = "{\n" + body + "\n}"
	return f"module main;\npub fn main() nothrow -> Int {{\n{body}\n}}\n"


def _parse(src: str, tmp_path: Path, name: str) -> list:
	path = tmp_path / f"{name}.drift"
	path.write_text(src)
	_m, _t, _e, diags = parse_drift_to_hir(path)
	return [d for d in diags if d.severity == "error"]


def test_moderate_nested_blocks_still_compiles(tmp_path: Path) -> None:
	"""Sanity: 100 inner blocks must parse cleanly (well below the limit).

	Pins that the limit isn't set so low it breaks normal deeply-indented code.
	"""
	errors = _parse(_gen_nested_blocks(100), tmp_path, "moderate")
	assert not errors, f"unexpected errors at d=100: {[d.message for d in errors]}"


def test_nesting_limit_boundary_just_below_compiles(tmp_path: Path) -> None:
	"""255 inner blocks must compile cleanly (one below the published limit).

	Pins the lower edge of the boundary so a future tightening of the limit
	below 256 cannot land silently.
	"""
	errors = _parse(_gen_nested_blocks(255), tmp_path, "d255")
	assert not errors, f"unexpected errors at d=255: {[d.message for d in errors]}"


def test_nesting_limit_boundary_at_published_limit_compiles(tmp_path: Path) -> None:
	"""Exactly 256 inner blocks must compile cleanly.

	This pins the published contract: the parser allows up to 256 nested
	inner blocks beneath a function body. A regression here would mean the
	limit moved off the documented value (the original implementation had an
	off-by-one that counted the enclosing function body block against the
	contract; corrected in 0.27.156).
	"""
	errors = _parse(_gen_nested_blocks(256), tmp_path, "d256")
	assert not errors, f"unexpected errors at d=256: {[d.message for d in errors]}"


def test_nesting_limit_boundary_one_above_emits_clean_diagnostic(tmp_path: Path) -> None:
	"""257 inner blocks must produce a structured diagnostic, not a crash.

	Pins the upper edge of the boundary. The diagnostic must mention
	nesting/depth so users can act on it.
	"""
	errors = _parse(_gen_nested_blocks(257), tmp_path, "d257")
	assert errors, "expected error diagnostic at d=257"
	assert any(
		"nesting" in d.message.lower() or "depth" in d.message.lower()
		for d in errors
	), f"no nesting/depth diagnostic found; got: {[d.message for d in errors]}"


def test_deep_nested_blocks_emit_clean_diagnostic_not_crash(tmp_path: Path) -> None:
	"""500 inner blocks must still produce a structured diagnostic, not a crash.

	Sanity that the guard works at depths well past the limit, not just at
	the boundary.
	"""
	errors = _parse(_gen_nested_blocks(500), tmp_path, "d500")
	assert errors
	assert any(
		"nesting" in d.message.lower() or "depth" in d.message.lower()
		for d in errors
	)
