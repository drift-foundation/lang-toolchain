# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Robustness regression: deeply long identifiers must emit a clean front-end
diagnostic, not an opaque downstream clang error.

Surfaced by a robustness audit: a Drift source
with a single identifier longer than ~1000 characters previously hit an
opaque clang IR-parse error of the form
`error: multiple definition of local value named '__dbg_keepalive_xxxx...'`,
with no source pointer the user could act on. The codegen wraps user
identifiers with prefixes/suffixes (e.g. `__dbg_keepalive_<name>__addr`,
~22 chars of overhead) and clang chokes around ~1023 source-identifier
chars on an unrelated downstream collision/limit.

The fix is a Drift-side cap: `PARSER_MAX_IDENTIFIER_LENGTH = 256`,
enforced once in `parse_program` via `_validate_identifier_lengths`, which
walks the parse tree iteratively (no recursion), checks every `NAME`
token's text length, and raises `ParserIdentifierLengthError` with a span
when the cap is exceeded. Diagnostic dispatch is hooked at all three
parser entry points in `parser/__init__.py`.

This file pins the boundary at 100/255/256/257/1000 — same shape as the
nesting-limit boundary regressions added for rows #1 and #3.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_to_hir


def _gen_long_identifier(n: int) -> str:
	name = "x" * n
	return (
		"module main;\n"
		"pub fn main() nothrow -> Int {\n"
		f"\tvar {name} = 7;\n"
		f"\treturn {name};\n"
		"}\n"
	)


def _parse(src: str, tmp_path: Path, name: str) -> list:
	path = tmp_path / f"{name}.drift"
	path.write_text(src)
	_m, _t, _e, diags = parse_drift_to_hir(path)
	return [d for d in diags if d.severity == "error"]


def test_moderate_identifier_compiles(tmp_path: Path) -> None:
	"""100-char identifier must parse cleanly (well below limit)."""
	errors = _parse(_gen_long_identifier(100), tmp_path, "moderate")
	assert not errors, f"unexpected errors at d=100: {[d.message for d in errors]}"


def test_identifier_length_just_below_compiles(tmp_path: Path) -> None:
	"""255-char identifier must compile cleanly (one below the published limit)."""
	errors = _parse(_gen_long_identifier(255), tmp_path, "d255")
	assert not errors, f"unexpected errors at d=255: {[d.message for d in errors]}"


def test_identifier_length_at_published_limit_compiles(tmp_path: Path) -> None:
	"""Exactly 256-char identifier must compile cleanly.

	Pins the published contract: identifiers up to 256 characters are
	allowed. Same boundary discipline as the nesting-limit regressions.
	"""
	errors = _parse(_gen_long_identifier(256), tmp_path, "d256")
	assert not errors, f"unexpected errors at d=256: {[d.message for d in errors]}"


def test_identifier_length_one_above_emits_clean_diagnostic(tmp_path: Path) -> None:
	"""257-char identifier must produce a structured diagnostic, not an opaque clang error."""
	errors = _parse(_gen_long_identifier(257), tmp_path, "d257")
	assert errors, "expected error diagnostic at d=257"
	assert any(
		"identifier length" in d.message.lower()
		for d in errors
	), f"no identifier-length diagnostic; got: {[d.message for d in errors]}"
	# The diagnostic must mention the actual length so users can correlate
	# with their input.
	assert any("257" in d.message for d in errors), (
		f"diagnostic does not mention the offending length 257: {[d.message for d in errors]}"
	)


def test_identifier_length_far_above_does_not_crash(tmp_path: Path) -> None:
	"""1000-char identifier must produce the same clean diagnostic, not a crash.

	Sanity that the cap works at lengths well past the boundary, not just
	at the boundary.
	"""
	errors = _parse(_gen_long_identifier(1000), tmp_path, "d1000")
	assert errors
	assert any("identifier length" in d.message.lower() for d in errors)
	assert any("1000" in d.message for d in errors)


def test_identifier_length_diagnostic_has_span(tmp_path: Path) -> None:
	"""The identifier-length diagnostic must point at the offending source location.

	Pins the contract that the diagnostic is actionable: it includes a
	span (file/line/column) that lets users find the offending identifier
	in their source.
	"""
	errors = _parse(_gen_long_identifier(500), tmp_path, "span")
	assert errors
	# At least one error must have a span with a populated line.
	assert any(
		getattr(d, "span", None) is not None
		and getattr(getattr(d, "span", None), "line", None) is not None
		for d in errors
	), f"diagnostic has no source span: {errors}"
