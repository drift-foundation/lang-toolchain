# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Meta-test: ensure no live code under `lang/driftc/` references stale
`TypeKind` enum members.

Background: the `VARIANT_INSTANCE` LANGUAGE_BUG (2026-05-23) was a stale
reference to a `TypeKind` enum member that never landed; the checker
guarded path stayed dormant for months and crashed in production for
the app team.  Similar prior deletions exist in this enum:

  - `TypeKind.DIAGNOSTICVALUE` was removed in slice 7c-3 (ABI 14,
    2026-05-06) along with the entire DV substrate.

This test asserts (via AST inspection — not text grep, so comments and
docstrings are ignored) that every `TypeKind.<NAME>` attribute access in
production code under `lang/driftc/` references an actually-defined
member of the live `TypeKind` enum.  Adding a new member of TypeKind
needs no change here; removing or renaming one forces the audit.

If this test fails, you have either:
  (a) introduced a typo / stale reference (fix the reference), or
  (b) removed a TypeKind member that some site still uses (decide:
      restore the member, or sweep the references first).
"""

from __future__ import annotations

import ast
from pathlib import Path

from lang.driftc.core.types_core import TypeKind


REPO_ROOT = Path(__file__).resolve().parents[3]
DRIFTC_DIR = REPO_ROOT / "lang" / "driftc"


def _collect_typekind_attrs(py_path: Path) -> set[str]:
	"""Return the set of `TypeKind.<NAME>` attribute names referenced in `py_path`."""
	try:
		tree = ast.parse(py_path.read_text(encoding="utf-8"))
	except SyntaxError as e:
		raise AssertionError(f"failed to parse {py_path}: {e}")
	found: set[str] = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "TypeKind":
			found.add(node.attr)
	return found


def test_no_stale_typekind_references_in_driftc() -> None:
	live_members = {m.name for m in TypeKind}
	stale_by_file: dict[Path, set[str]] = {}
	scanned = 0
	for py_path in DRIFTC_DIR.rglob("*.py"):
		# Skip __pycache__ artifacts (not source).
		if "__pycache__" in py_path.parts:
			continue
		scanned += 1
		attrs = _collect_typekind_attrs(py_path)
		stale = attrs - live_members
		if stale:
			stale_by_file[py_path.relative_to(REPO_ROOT)] = stale
	# Sanity: ensure the walker actually saw the codebase.
	assert scanned > 50, (
		f"expected to scan >50 driftc python files, got {scanned} — "
		"walker is probably misconfigured"
	)
	if stale_by_file:
		report = "\n".join(
			f"  {path}: {sorted(stale)}" for path, stale in sorted(stale_by_file.items())
		)
		raise AssertionError(
			"stale TypeKind member references found in driftc — these are not "
			"defined on the live `TypeKind` enum and will AttributeError at "
			"runtime the moment their guard fires:\n" + report + "\n"
			f"Live members: {sorted(live_members)}"
		)


# Belt-and-braces: hard-pin known-historic-stale names so that even if
# someone re-adds them to TypeKind temporarily, a reviewer sees the
# explicit "this used to be stale" intent.
_HISTORICAL_STALE = ("VARIANT_INSTANCE", "STRUCT_INSTANCE", "DIAGNOSTICVALUE")


def test_historical_stale_typekind_members_are_not_live_or_referenced() -> None:
	live_members = {m.name for m in TypeKind}
	hits: dict[str, list[Path]] = {}
	for name in _HISTORICAL_STALE:
		assert name not in live_members, (
			f"{name} is in `TypeKind` again — historical context says this "
			"was deleted intentionally; if reintroducing, remove it from the "
			"_HISTORICAL_STALE list with a comment explaining why."
		)
	for py_path in DRIFTC_DIR.rglob("*.py"):
		if "__pycache__" in py_path.parts:
			continue
		attrs = _collect_typekind_attrs(py_path)
		for name in _HISTORICAL_STALE:
			if name in attrs:
				hits.setdefault(name, []).append(py_path.relative_to(REPO_ROOT))
	if hits:
		report = "\n".join(
			f"  TypeKind.{name}: " + ", ".join(str(p) for p in paths)
			for name, paths in sorted(hits.items())
		)
		raise AssertionError(
			"historical-stale TypeKind member used in live code:\n" + report
		)
