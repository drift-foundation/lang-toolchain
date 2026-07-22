# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Static audit of direct MIR mutation discipline in the stage2
ownership/cleanup passes.

Scans the scoped ownership/cleanup passes (`drop_flags.py`,
`cleanup_authoring.py`, `match_cleanup_authoring.py`, `string_arc.py`,
`string_stakes.py`, `overwrite_cleanup.py`) for the mutation
patterns enumerated in `work/ledger-cache-safety/plan.md` and
requires each match to be paired with EITHER:

  1. A `mark_ledger_dirty(` call within ±5 source lines, OR
  2. An inline allow marker on the same line or the line
     immediately above:
         `# ledger-cache-safety-audit: allow <reason>`

This is the discipline-side guard for the ledger-cache-safety
contract: the runtime-assertion side
(`test_ledger_cache_safety_dirty_bit.py`) only catches stale
*reads*; this audit catches the upstream "forgot to mark
dirty after a mutation" miss before it ships.

K-review (2026-05-16) framing: "forgot the rebuild" became
"forgot the dirty mark" without this audit.  The audit closes
that gap.

Limits — explicitly accepted per plan:
  * Alias-mutation shapes (e.g. `xs = blk.instructions;
    xs.append(...)`) are not caught; reviewers should flag them
    manually.
  * The scan is regex-based, not AST-based.
  * Reason-string semantics are not validated.

Update the SCOPED_FILES list if a new offending site appears in
a file not yet covered.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[3]

SCOPED_FILES = (
	"lang/driftc/stage2/drop_flags.py",
	"lang/driftc/stage2/cleanup_authoring.py",
	"lang/driftc/stage2/match_cleanup_authoring.py",
	"lang/driftc/stage2/string_arc.py",
	# B-arch-1a: the call-arg stake materialization pass mutates
	# block.instructions and must follow the same dirty-bit discipline.
	"lang/driftc/stage2/string_stakes.py",
	# Slice B1: the instruction-local overwrite-cleanup pass rewrites
	# block.instructions (R2/R7 old-value releases) and marks the
	# ledger dirty under the same discipline.
	"lang/driftc/stage2/overwrite_cleanup.py",
	# B2+C S3: the isolated site-3 Return-boundary emitter appends drop
	# sequences to Return blocks and writes local_types; it marks the
	# ledger dirty (iff emission occurred) under the same discipline.
	"lang/driftc/stage2/site3_return_emitter.py",
)

# Each pattern matches a direct MIR mutation shape.  Kept narrow
# enough that false-positives are rare; broaden only with care.
_MUTATION_PATTERNS = tuple(
	re.compile(pat)
	for pat in (
		r"\.instructions\s*=(?!=)",          # whole-list replacement
		r"\.instructions\.append\(",         # append
		r"\.instructions\.insert\(",         # insert
		r"\bdel\s+.*\.instructions",         # del slice/element
		r"\.terminator\s*=(?!=)",            # terminator rewrite
		r"\bfunc\.blocks\[.+?\]\s*=(?!=)",   # block replacement
		r"\bfunc\.blocks\.append\(",         # new block appended
		r"\bfunc\.blocks\.insert\(",         # new block inserted
	)
)

_ALLOW_RE = re.compile(r"#\s*ledger-cache-safety-audit:\s*allow\s+(\S.*)")
_MARK_RE = re.compile(r"\bmark_ledger_dirty\s*\(")

_PROXIMITY_WINDOW = 5  # lines either side of the mutation


def _flag_line(line: str) -> bool:
	"""True iff `line` contains any mutation pattern."""
	return any(pat.search(line) for pat in _MUTATION_PATTERNS)


def _has_allow_marker(lines: list[str], i: int) -> bool:
	"""True iff line `i` carries an inline allow marker, or the
	immediately preceding non-blank source line carries one."""
	if _ALLOW_RE.search(lines[i]):
		return True
	if i > 0 and _ALLOW_RE.search(lines[i - 1]):
		return True
	return False


def _has_nearby_mark(lines: list[str], i: int) -> bool:
	"""True iff a `mark_ledger_dirty(` call appears within
	`_PROXIMITY_WINDOW` lines of `i` (inclusive)."""
	lo = max(0, i - _PROXIMITY_WINDOW)
	hi = min(len(lines), i + _PROXIMITY_WINDOW + 1)
	return any(_MARK_RE.search(lines[j]) for j in range(lo, hi))


def _audit_file(rel_path: str) -> list[tuple[int, str]]:
	"""Return list of (lineno, source) for every offending line."""
	path = ROOT / rel_path
	lines = path.read_text().splitlines()
	offenders: list[tuple[int, str]] = []
	for i, line in enumerate(lines):
		if not _flag_line(line):
			continue
		if _has_allow_marker(lines, i):
			continue
		if _has_nearby_mark(lines, i):
			continue
		offenders.append((i + 1, line.strip()))
	return offenders


def test_ledger_cache_safety_mutation_audit() -> None:
	"""Every direct MIR mutation in the scoped files must be
	paired with a nearby `mark_ledger_dirty(...)` call OR an inline
	allow marker with a free-text reason.  If you see this test
	fail, see `work/ledger-cache-safety/plan.md` for the contract
	and the allow-reason vocabulary."""
	all_offenders: list[str] = []
	for rel in SCOPED_FILES:
		offenders = _audit_file(rel)
		for lineno, src in offenders:
			all_offenders.append(f"  {rel}:{lineno}  {src}")
	if all_offenders:
		hint = (
			"\n\nAdd `mark_ledger_dirty(func, '<reason>')` within "
			f"{_PROXIMITY_WINDOW} lines of each mutation, OR an inline "
			"allow marker:\n"
			"  # ledger-cache-safety-audit: allow <reason>\n\n"
			"See `work/ledger-cache-safety/plan.md` for the contract."
		)
		raise AssertionError(
			"ledger-cache-safety audit found unmarked MIR mutations:\n"
			+ "\n".join(all_offenders)
			+ hint
		)


# -- self-tests for the audit's own logic -----------------------------------


def _audit_string(src: str) -> list[tuple[int, str]]:
	"""Run the audit against an in-memory snippet (for self-tests)."""
	lines = src.splitlines()
	offenders: list[tuple[int, str]] = []
	for i, line in enumerate(lines):
		if not _flag_line(line):
			continue
		if _has_allow_marker(lines, i):
			continue
		if _has_nearby_mark(lines, i):
			continue
		offenders.append((i + 1, line.strip()))
	return offenders


def test_audit_self_flags_unmarked_mutation() -> None:
	"""Audit must flag a mutation that has no nearby mark and no
	allow marker."""
	snippet = (
		"def f(func):\n"
		"    blk.instructions = []\n"
		"    return func\n"
	)
	offenders = _audit_string(snippet)
	assert len(offenders) == 1, f"expected 1 offender, got {offenders}"
	assert "blk.instructions = []" in offenders[0][1]


def test_audit_self_accepts_marked_mutation() -> None:
	"""A mutation paired with mark_ledger_dirty within ±5 lines
	must NOT be flagged."""
	snippet = (
		"def f(func):\n"
		"    blk.instructions = []\n"
		"    mark_ledger_dirty(func, 'test.foo')\n"
		"    return func\n"
	)
	offenders = _audit_string(snippet)
	assert offenders == [], f"unexpected offenders: {offenders}"


def test_audit_self_accepts_inline_allow_marker() -> None:
	"""A mutation with an inline allow marker must NOT be flagged."""
	snippet = (
		"def f():\n"
		"    new_blk.instructions.append(thing)  # ledger-cache-safety-audit: allow new-block\n"
		"    return\n"
	)
	offenders = _audit_string(snippet)
	assert offenders == [], f"unexpected offenders: {offenders}"


def test_audit_self_accepts_allow_marker_on_preceding_line() -> None:
	"""An allow marker on the line above the mutation is also
	acceptable (for cases where the inline comment doesn't fit)."""
	snippet = (
		"def f():\n"
		"    # ledger-cache-safety-audit: allow new-block\n"
		"    new_blk.instructions.append(thing)\n"
		"    return\n"
	)
	offenders = _audit_string(snippet)
	assert offenders == [], f"unexpected offenders: {offenders}"


def test_audit_self_distance_boundary() -> None:
	"""A mark exactly at the ±5 boundary counts; one line beyond
	does not."""
	# Mark at distance 5 below — accepted.
	snippet_ok = (
		"line1: blk.instructions = []\n"
		"line2\n"
		"line3\n"
		"line4\n"
		"line5\n"
		"line6: mark_ledger_dirty(func, 'x')\n"
	)
	offenders = _audit_string(snippet_ok)
	assert offenders == [], f"distance-5 should be accepted: {offenders}"

	# Mark at distance 6 below — rejected.
	snippet_fail = (
		"line1: blk.instructions = []\n"
		"line2\n"
		"line3\n"
		"line4\n"
		"line5\n"
		"line6\n"
		"line7: mark_ledger_dirty(func, 'x')\n"
	)
	offenders = _audit_string(snippet_fail)
	assert len(offenders) == 1, f"distance-6 should be flagged: {offenders}"
