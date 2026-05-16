# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Static audit of `DriftString` by-value parameter ownership
discipline in `lang/language_runtime/`.

Scans `.c` definitions for functions whose signature contains one or
more `DriftString <name>` (by-value) parameters and requires each
such parameter to be handled by EITHER:

  1. A `DRIFT_OWNED_STRING DriftString <local> = <param>;` shadow
     within the first ~12 lines of the body (the `cleanup` attribute
     auto-releases the stake at scope exit), OR
  2. An inline allow marker on a comment line within the first
     ~12 lines of the body OR the line immediately above the
     function signature:
         `/* drift-owned-string-audit: allow <reason> -- <params> */`

Allowed reason vocabulary (typos fail):
  - `refcount-primitive`            -- function IS retain/release/free
  - `read-only-borrow`              -- pure read; refcount unchanged
                                       (covers BOTH "string primitive
                                       like eq/cmp/to_cstr/concat"
                                       AND "Drift built-in / intrinsic
                                       call site that doesn't pre-
                                       retain", per the convention
                                       split discovered during the
                                       DRIFT_OWNED_STRING slice; see
                                       drift_assert_loc and
                                       drift_bounds_check)
  - `consumed-by-noreturn-callee`   -- passed to a noreturn function
                                       that takes the stake; cleanup
                                       can't fire on noreturn paths
                                       anyway, so the macro is moot
  - `internal-borrowed-helper`      -- static helper whose public
                                       caller already wraps with
                                       DRIFT_OWNED_STRING

This is the discipline-side guard for the by-value-DriftString
ownership contract documented in `lang/language_runtime/string_runtime.h`.

This is a HARD GATE -- no warning-only escape (no audit-pending).
Every site decides in-slice.

Update the SCAN_DIRS list if a new language_runtime subtree is added.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[3]

# Directories to scan for .c definitions.  Sub-dirs walked recursively.
SCAN_DIRS = (
	"lang/language_runtime",
)

# Files explicitly excluded (none today; reserved for sentinel/abi-version
# files that don't follow the runtime ABI).
EXCLUDE_RELATIVE = frozenset()

ALLOWED_REASONS = frozenset({
	"refcount-primitive",
	"read-only-borrow",
	"consumed-by-noreturn-callee",
	"internal-borrowed-helper",
})

# Function definition: type, name, params...  Sloppy but sufficient for
# the runtime files (which are conventionally one-line signatures or
# multi-line with the open-brace on its own line).
#
# We do this as a two-pass scan: find `{` lines that open function
# bodies, walk backwards to the signature, then forwards through the
# body.  Real C parsing is overkill; the runtime is small.
_FN_OPEN_RE = re.compile(
	# Anchor: a line ending in `) {` (most function defs in this tree
	# put the brace on the same line as the closing paren).
	r"^\s*([a-zA-Z_][\w\s\*]*?)\s+([a-zA-Z_]\w*)\s*\(([^;]*?)\)\s*\{",
	re.MULTILINE,
)

# Parameter shape: `DriftString name` or `struct DriftString name` by
# value (not `DriftString *name`, not `DriftString[]`).
_PARAM_BY_VALUE_RE = re.compile(
	r"\b(?:struct\s+)?DriftString\s+([a-zA-Z_]\w*)\b(?!\s*[*\[])"
)

# Shadow shape: `DRIFT_OWNED_STRING DriftString <local> = <expr>;`.
_SHADOW_RE = re.compile(
	r"\bDRIFT_OWNED_STRING\s+DriftString\s+([a-zA-Z_]\w*)\s*=\s*([a-zA-Z_]\w*)\s*;"
)

# Allow marker shape: `drift-owned-string-audit: allow <reason> -- <params>`
# Matched line-anywhere (single-line or inside a multi-line block comment).
# The C comment delimiters /* */ are not part of the match -- we only
# require the marker text to appear on a comment-prefixed line in the
# search window.  Params list ends at end-of-line OR `*/` OR newline.
_MARKER_RE = re.compile(
	r"drift-owned-string-audit:\s*allow\s+([a-z\-]+)\s*--\s*([^\n*]+?)\s*(?:\*/|$)",
	re.MULTILINE,
)


def _iter_c_files() -> Iterable[Path]:
	for rel in SCAN_DIRS:
		base = ROOT / rel
		if not base.exists():
			continue
		for c_path in sorted(base.rglob("*.c")):
			rel_path = str(c_path.relative_to(ROOT))
			if rel_path in EXCLUDE_RELATIVE:
				continue
			yield c_path


def _find_function_bodies(src: str) -> Iterable[tuple[str, str, list[str], str, int]]:
	"""Yield (return_type, fn_name, by_value_param_names, body_window, sig_line_idx)
	for every function definition in `src` that takes at least one
	by-value `DriftString` parameter.

	`body_window` is the text of the first ~12 lines of the function
	body, used for shadow / marker matching.
	"""
	lines = src.split("\n")
	for m in _FN_OPEN_RE.finditer(src):
		params_text = m.group(3)
		params = _PARAM_BY_VALUE_RE.findall(params_text)
		if not params:
			continue
		ret_type = m.group(1).strip()
		fn_name = m.group(2)
		# Locate signature line index (1-based) via offset.
		sig_line_idx = src.count("\n", 0, m.start()) + 1
		# Body window: 12 lines after the open brace.
		body_start = src.find("{", m.start())
		assert body_start != -1
		# Find the line index of the `{`.
		brace_line_idx = src.count("\n", 0, body_start)
		body_lines = lines[brace_line_idx : brace_line_idx + 13]
		body_window = "\n".join(body_lines)
		yield ret_type, fn_name, params, body_window, sig_line_idx


def _marker_above_signature(src: str, sig_line_idx: int) -> tuple[str, set[str]] | None:
	"""If the lines immediately above the signature contain an allow
	marker (within ~15 lines -- wide enough for a multi-line block
	comment with context), parse it.  Returns (reason, param_names)
	or None.  Stops the upward search at a blank line OR the previous
	function's closing brace, so markers do not bleed across functions.
	"""
	lines = src.split("\n")
	# Walk back from the signature line, stopping at the previous
	# function boundary (blank line above a non-comment line, or a
	# `}` at column 0).  Cap at 20 lines.
	start = sig_line_idx
	for i in range(sig_line_idx - 1, max(-1, sig_line_idx - 21), -1):
		line = lines[i]
		stripped = line.strip()
		if stripped == "":
			# Blank line -- a marker is allowed to be separated from
			# the signature by a blank, but two blanks in a row hit
			# the previous function.  Walk one more step.
			if i > 0 and lines[i - 1].strip() == "":
				break
			start = i
			continue
		if line.startswith("}"):
			break
		start = i
	window = "\n".join(lines[start : sig_line_idx])
	m = _MARKER_RE.search(window)
	if m is None:
		return None
	reason = m.group(1)
	params_in_marker = {p.strip() for p in m.group(2).split(",") if p.strip()}
	return reason, params_in_marker


def _marker_inside_body(body_window: str) -> tuple[str, set[str]] | None:
	"""Same as above but searches the body window."""
	m = _MARKER_RE.search(body_window)
	if m is None:
		return None
	reason = m.group(1)
	params_in_marker = {p.strip() for p in m.group(2).split(",") if p.strip()}
	return reason, params_in_marker


def _shadowed_params(body_window: str, params: list[str]) -> set[str]:
	"""Return the set of params whose value is captured by a
	`DRIFT_OWNED_STRING DriftString <local> = <param>;` shadow in
	the body window.

	The local name may differ from the param (`name_in -> name`).
	We pair by RHS identifier."""
	shadows = _SHADOW_RE.findall(body_window)
	rhs_set = {rhs for (_lhs, rhs) in shadows}
	return {p for p in params if p in rhs_set}


def test_drift_owned_string_audit() -> None:
	"""Hard gate: every `.c` definition with a by-value DriftString
	param either uses `DRIFT_OWNED_STRING` or carries an explicit
	allow marker with a recognized reason."""
	violations: list[str] = []
	any_fn_seen = False
	for c_path in _iter_c_files():
		src = c_path.read_text(encoding="utf-8")
		for ret_type, fn_name, params, body_window, sig_line_idx in _find_function_bodies(src):
			any_fn_seen = True
			rel = str(c_path.relative_to(ROOT))
			shadowed = _shadowed_params(body_window, params)
			unshadowed = [p for p in params if p not in shadowed]
			if not unshadowed:
				continue

			marker = _marker_above_signature(src, sig_line_idx)
			if marker is None:
				marker = _marker_inside_body(body_window)

			if marker is None:
				violations.append(
					f"{rel}:{sig_line_idx} `{fn_name}`: by-value DriftString "
					f"param(s) {unshadowed} have no DRIFT_OWNED_STRING shadow "
					f"and no allow marker.\n"
					f"  Fix: either shadow each param with "
					f"`DRIFT_OWNED_STRING DriftString <local> = <param>;` at "
					f"the top of the body, OR add a comment marker:\n"
					f"  /* drift-owned-string-audit: allow <reason> -- "
					f"{', '.join(unshadowed)} */\n"
					f"  Allowed reasons: {sorted(ALLOWED_REASONS)}"
				)
				continue

			reason, marker_params = marker
			if reason not in ALLOWED_REASONS:
				violations.append(
					f"{rel}:{sig_line_idx} `{fn_name}`: marker reason "
					f"`{reason}` is not in the allowed vocabulary "
					f"{sorted(ALLOWED_REASONS)}."
				)
				continue
			missing = [p for p in unshadowed if p not in marker_params]
			if missing:
				violations.append(
					f"{rel}:{sig_line_idx} `{fn_name}`: allow marker reason "
					f"`{reason}` does not cover params {missing}.  Marker "
					f"named: {sorted(marker_params)}; unshadowed: "
					f"{sorted(unshadowed)}.  Either add the missing params "
					f"to the marker `--` list or shadow them with "
					f"DRIFT_OWNED_STRING."
				)

	assert any_fn_seen, (
		"audit scanner found zero function definitions -- regex / scope is "
		"probably broken (check SCAN_DIRS and _FN_OPEN_RE)."
	)
	assert not violations, (
		f"{len(violations)} drift-owned-string audit violation(s):\n\n"
		+ "\n\n".join(violations)
	)


# ---------------------------------------------------------------------------
# Parser self-tests (synthetic .c snippets fed directly to the matchers).
# These pin the regex and matcher behavior independently of whichever real
# runtime .c files happen to live in the tree -- so the audit can't silently
# pass for the wrong reason if someone refactors the runtime.
# ---------------------------------------------------------------------------


def _classify_snippet(src: str) -> tuple[bool, str | None]:
	"""Run the same logic the main audit applies, but on a synthetic
	source string.  Returns (passes, violation_substring) where
	`passes` is True iff every by-value DriftString param is either
	shadowed or covered by a valid marker."""
	for ret_type, fn_name, params, body_window, sig_line_idx in _find_function_bodies(src):
		shadowed = _shadowed_params(body_window, params)
		unshadowed = [p for p in params if p not in shadowed]
		if not unshadowed:
			continue
		marker = _marker_above_signature(src, sig_line_idx) or _marker_inside_body(body_window)
		if marker is None:
			return False, f"unshadowed {unshadowed!r} no marker"
		reason, marker_params = marker
		if reason not in ALLOWED_REASONS:
			return False, f"bad reason {reason!r}"
		missing = [p for p in unshadowed if p not in marker_params]
		if missing:
			return False, f"marker missing params {missing!r}"
	return True, None


def test_audit_self_macro_shadow_passes() -> None:
	"""A function that wraps a by-value DriftString param with
	`DRIFT_OWNED_STRING DriftString local = param_in;` passes
	the audit without needing a marker."""
	src = (
		"int64_t drift_io_open(DriftString path_in, int64_t flags) {\n"
		"\tDRIFT_OWNED_STRING DriftString path = path_in;\n"
		"\treturn (int64_t)path.len;\n"
		"}\n"
	)
	passes, _ = _classify_snippet(src)
	assert passes, "DRIFT_OWNED_STRING shadow should satisfy the audit"


def test_audit_self_bad_marker_reason_fails() -> None:
	"""An allow marker with an unrecognized reason vocabulary fails."""
	src = (
		"/* drift-owned-string-audit: allow audit-pending -- s */\n"
		"void drift_thing(DriftString s) {\n"
		"\t(void)s;\n"
		"}\n"
	)
	passes, why = _classify_snippet(src)
	assert not passes and "bad reason" in (why or ""), (
		"unknown marker reason should fail the audit; got passes="
		f"{passes!r}, why={why!r}"
	)


def test_audit_self_missing_param_in_marker_fails() -> None:
	"""An allow marker that only names some of the unshadowed params
	leaves the rest uncovered and fails."""
	src = (
		"/* drift-owned-string-audit: allow read-only-borrow -- a */\n"
		"int drift_compare(DriftString a, DriftString b) {\n"
		"\t(void)a; (void)b;\n"
		"\treturn 0;\n"
		"}\n"
	)
	passes, why = _classify_snippet(src)
	assert not passes and "missing params" in (why or ""), (
		"marker that omits one of the unshadowed params should fail; "
		f"got passes={passes!r}, why={why!r}"
	)


def test_audit_self_unshadowed_no_marker_fails() -> None:
	"""A by-value DriftString receiver with neither a macro shadow nor
	any allow marker is a hard violation."""
	src = (
		"void drift_unguarded(DriftString s) {\n"
		"\t(void)s;\n"
		"}\n"
	)
	passes, why = _classify_snippet(src)
	assert not passes and "no marker" in (why or ""), (
		"unshadowed by-value DriftString without marker should fail; "
		f"got passes={passes!r}, why={why!r}"
	)


def test_audit_self_marker_covering_all_params_passes() -> None:
	"""A correctly-formed allow marker covering every by-value
	DriftString param (convention-B receivers) passes."""
	src = (
		"/* drift-owned-string-audit: allow read-only-borrow -- a, b */\n"
		"int drift_compare(DriftString a, DriftString b) {\n"
		"\t(void)a; (void)b;\n"
		"\treturn 0;\n"
		"}\n"
	)
	passes, why = _classify_snippet(src)
	assert passes, f"correctly-marked convention-B receiver should pass; why={why!r}"
